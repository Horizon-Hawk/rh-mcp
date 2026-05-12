"""Compressed-volatility (Bollinger squeeze) scanner.

Finds tickers where the 20-day Bollinger bandwidth is in the bottom percentile
of its 6-month history — i.e., volatility has compressed. When price is also
near the upper band, the squeeze is about to release directionally.

This is the precursor signal to the 52-week-high scanner: catches setups
~$0.50 earlier (before the breakout candle confirms). APLS at $40.50 with
ATR 0.17% of price is the textbook case — the 52w scanner only saw it at
$41.07 when the move was already underway.

Output ranked by compression depth (lowest bandwidth percentile first).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

BB_PERIOD = 20                  # standard Bollinger period
BB_STDEV = 2.0                  # standard 2-sigma bands
HISTORY_DAYS = 126              # ~6 trading months
COMPRESSION_PERCENTILE = 20.0   # bottom 20% of bandwidth = squeeze
PROXIMITY_UPPER_PCT = 2.0       # price within 2% of upper band = ready to break
MIN_PRICE = 5.00
MIN_AVG_VOLUME = 200_000        # skip illiquid

DEFAULT_TOP_N = 20
_BATCH_SIZE = 75
_BATCH_SLEEP = 0.1


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_universe_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(tickers))


def _fetch_year_bars(tickers: list[str]) -> dict[str, list[dict]]:
    """Pull ~1 year of daily bars per ticker, batched."""
    out: dict[str, list[dict]] = {t: [] for t in tickers}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            raw = rh.stocks.get_stock_historicals(
                chunk, interval="day", span="year", bounds="regular",
            ) or []
        except Exception:
            raw = []
        for b in raw:
            sym = (b.get("symbol") or "").upper()
            if sym not in out:
                continue
            try:
                out[sym].append({
                    "date": b["begins_at"][:10],
                    "close": float(b["close_price"]),
                })
            except (KeyError, ValueError, TypeError):
                pass
        time.sleep(_BATCH_SLEEP)
    for sym in out:
        out[sym].sort(key=lambda x: x["date"])
    return out


def _bandwidth(closes: list[float]) -> float:
    """One Bollinger bandwidth value at the given window's tail.

    Bandwidth = (upper - lower) / sma = 4 * stdev / sma.
    Normalized so it's comparable across price levels.
    """
    if len(closes) < BB_PERIOD:
        return float("nan")
    window = closes[-BB_PERIOD:]
    sma = mean(window)
    if sma <= 0:
        return float("nan")
    sd = stdev(window) if len(window) > 1 else 0.0
    return (BB_STDEV * 2 * sd) / sma


def _bandwidth_series(closes: list[float]) -> list[float]:
    """Compute bandwidth at each day where a full window exists."""
    if len(closes) < BB_PERIOD:
        return []
    out = []
    for i in range(BB_PERIOD, len(closes) + 1):
        window = closes[i - BB_PERIOD:i]
        sma = mean(window)
        if sma <= 0:
            continue
        sd = stdev(window) if len(window) > 1 else 0.0
        out.append((BB_STDEV * 2 * sd) / sma)
    return out


def _upper_band(closes: list[float]) -> float:
    """Upper Bollinger band at the current bar."""
    if len(closes) < BB_PERIOD:
        return float("nan")
    window = closes[-BB_PERIOD:]
    sma = mean(window)
    sd = stdev(window) if len(window) > 1 else 0.0
    return sma + BB_STDEV * sd


def _percentile_rank(value: float, series: list[float]) -> float:
    """What percentile is `value` at within `series` (lower = more compressed)."""
    if not series:
        return 50.0
    below = sum(1 for x in series if x < value)
    return round(below / len(series) * 100, 1)


def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_fundamentals(chunk, info=None) or []
        except Exception:
            results = []
        for sym, f in zip(chunk, results):
            if not f:
                continue
            out[sym.upper()] = {
                "avg_volume": _to_float(f.get("average_volume")),
                "sector": f.get("sector"),
                "industry": f.get("industry"),
                "market_cap": _to_float(f.get("market_cap")),
                "high_52w": _to_float(f.get("high_52_weeks")),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    compression_percentile: float = COMPRESSION_PERCENTILE,
    proximity_upper_pct: float = PROXIMITY_UPPER_PCT,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan for Bollinger squeeze candidates.

    A candidate must have:
    - 20d bandwidth in the bottom `compression_percentile`% of 6-month history
    - Current close within `proximity_upper_pct`% of the upper band (loaded for a break)
    - Price ≥ min_price and avg_volume ≥ min_avg_volume (filter junk)
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe — provide tickers list or universe_file"}

    login()
    scanned_at = datetime.now().isoformat(timespec="seconds")

    funds = _fetch_fundamentals(tickers)
    bars = _fetch_year_bars(tickers)

    passed_basic = 0
    passed_compression = 0
    candidates: list[dict] = []

    for ticker in tickers:
        f = funds.get(ticker)
        sym_bars = bars.get(ticker, [])
        if not f or len(sym_bars) < BB_PERIOD:
            continue
        avg_vol = f.get("avg_volume") or 0
        if avg_vol < min_avg_volume:
            continue
        closes = [b["close"] for b in sym_bars]
        last_close = closes[-1]
        if last_close < min_price:
            continue
        passed_basic += 1

        # Most recent N bars used for percentile comparison
        history_series = _bandwidth_series(closes[-(HISTORY_DAYS + BB_PERIOD - 1):])
        if len(history_series) < 30:
            continue  # not enough history

        current_bw = history_series[-1]
        bw_pct = _percentile_rank(current_bw, history_series)
        if bw_pct > compression_percentile:
            continue
        passed_compression += 1

        upper = _upper_band(closes)
        if upper != upper or upper <= 0:  # NaN guard
            continue
        pct_to_upper = (last_close - upper) / upper * 100  # negative = below upper
        if pct_to_upper < -proximity_upper_pct:
            continue  # too far below upper band — not loaded for break

        candidates.append({
            "ticker": ticker,
            "price": last_close,
            "upper_band": round(upper, 4),
            "pct_to_upper": round(pct_to_upper, 2),
            "bandwidth": round(current_bw, 4),
            "bandwidth_pct": bw_pct,
            "high_52w": f.get("high_52w"),
            "sector": f.get("sector"),
            "industry": f.get("industry"),
            "market_cap": f.get("market_cap"),
        })

    candidates.sort(key=lambda x: x["bandwidth_pct"])  # lowest percentile first
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "scanned_at": scanned_at,
        "total_scanned": len(tickers),
        "passed_basic": passed_basic,
        "passed_compression": passed_compression,
        "passed_proximity": len(candidates),
        "candidates": candidates,
    }
