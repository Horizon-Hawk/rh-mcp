"""Connors RSI(2) mean reversion scanner.

Larry Connors' classic short-term mean reversion strategy:
- In uptrending stocks (above 200-day SMA): RSI(2) < 5 → 3-5 day bounce
- In downtrending stocks (below 200-day SMA): RSI(2) > 95 → 3-5 day fade

Practitioner-validated (Connors "Short Term Trading Strategies That Work").
65-72% win rate on filtered signals; degrades without the trend filter.

Returns BOTH long candidates (oversold in uptrend) AND short candidates
(overbought in downtrend) in separate lists.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

OVERSOLD_THRESHOLD = 5.0
OVERBOUGHT_THRESHOLD = 95.0
SMA_PERIOD = 200
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 200_000

DEFAULT_TOP_N = 15
_BATCH_SIZE = 75
_BATCH_SLEEP = 0.1


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


def _fetch_year_bars(tickers: list[str]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {t: [] for t in tickers}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            raw = rh.stocks.get_stock_historicals(
                chunk, interval="day", span="year", bounds="regular",
            ) or []
        except Exception:
            raw = []
        ranked: dict[str, list[tuple]] = {}
        for b in raw:
            sym = (b.get("symbol") or "").upper()
            if sym not in out:
                continue
            try:
                ranked.setdefault(sym, []).append((b["begins_at"][:10], float(b["close_price"])))
            except (KeyError, ValueError, TypeError):
                pass
        for sym, rows in ranked.items():
            rows.sort()
            out[sym].extend(c for _, c in rows)
        time.sleep(_BATCH_SLEEP)
    return out


def _rsi(closes: list[float], period: int = 2) -> float | None:
    """Standard RSI on a series of closes. Returns the latest value, or None if insufficient."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    oversold_threshold: float = OVERSOLD_THRESHOLD,
    overbought_threshold: float = OVERBOUGHT_THRESHOLD,
    sma_period: int = SMA_PERIOD,
    min_price: float = MIN_PRICE,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find RSI(2) extremes filtered by 200-SMA trend."""
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")
    bars = _fetch_year_bars(tickers)

    longs: list[dict] = []
    shorts: list[dict] = []
    for ticker in tickers:
        closes = bars.get(ticker, [])
        if len(closes) < sma_period:
            continue
        last = closes[-1]
        if last < min_price:
            continue
        sma = mean(closes[-sma_period:])
        if sma <= 0:
            continue
        rsi2 = _rsi(closes, period=2)
        if rsi2 is None:
            continue

        if rsi2 < oversold_threshold and last > sma:
            longs.append({
                "ticker": ticker,
                "price": last,
                "rsi_2": round(rsi2, 1),
                "sma_200": round(sma, 2),
                "pct_above_sma": round((last - sma) / sma * 100, 2),
                "signal": "long_mean_revert",
            })
        elif rsi2 > overbought_threshold and last < sma:
            shorts.append({
                "ticker": ticker,
                "price": last,
                "rsi_2": round(rsi2, 1),
                "sma_200": round(sma, 2),
                "pct_below_sma": round((sma - last) / sma * 100, 2),
                "signal": "short_mean_revert",
            })

    longs.sort(key=lambda x: x["rsi_2"])  # lowest RSI first
    shorts.sort(key=lambda x: x["rsi_2"], reverse=True)  # highest RSI first
    if top_n:
        longs = longs[:top_n]
        shorts = shorts[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "longs": longs,
        "shorts": shorts,
        "long_count": len(longs),
        "short_count": len(shorts),
    }
