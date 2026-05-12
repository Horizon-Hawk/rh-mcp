"""Cross-sectional momentum (12-1) scanner.

Jegadeesh-Titman 1993: top-decile of 12-month return (excluding the most
recent month, to filter short-term reversal noise) outperforms bottom-decile
by 7-10% annualized over the next 3-12 months. Carhart momentum factor.

This is a SLEEVE scanner — output is a ranked list intended for portfolio
construction, not single-trade entry. Holding period is typically 1-3 months
with monthly rebalance.

Output: tickers ranked by (12mo return - 1mo return), descending.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

MIN_PRICE = 5.0
MIN_AVG_VOLUME = 200_000
LOOKBACK_DAYS_LONG = 252         # ~12 months
LOOKBACK_DAYS_RECENT = 21        # ~1 month (the "1" in 12-1, excluded from the ranking signal)

DEFAULT_TOP_N = 20
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


def _fetch_year_bars(tickers: list[str]) -> dict[str, list[dict]]:
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
            try:
                avg_vol = float(f.get("average_volume") or 0)
            except (ValueError, TypeError):
                avg_vol = 0
            out[sym.upper()] = {"avg_volume": avg_vol, "sector": f.get("sector"), "market_cap": f.get("market_cap")}
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Rank universe by 12-month return excluding the most recent month."""
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")
    funds = _fetch_fundamentals(tickers)
    bars = _fetch_year_bars(tickers)

    candidates: list[dict] = []
    for ticker in tickers:
        f = funds.get(ticker)
        if not f or f.get("avg_volume", 0) < min_avg_volume:
            continue
        b = bars.get(ticker, [])
        if len(b) < LOOKBACK_DAYS_LONG - 20:  # tolerate a few missing days
            continue
        closes = [x["close"] for x in b]
        last = closes[-1]
        if last < min_price:
            continue
        # 12-month return: from oldest available to ~1 month ago
        # 1-month return: from ~1 month ago to today
        if len(closes) < LOOKBACK_DAYS_RECENT + 1:
            continue
        recent_idx = -LOOKBACK_DAYS_RECENT
        long_start = max(0, len(closes) - LOOKBACK_DAYS_LONG)
        ref_long = closes[long_start]
        ref_recent = closes[recent_idx]
        if ref_long <= 0 or ref_recent <= 0:
            continue
        ret_12m = (last - ref_long) / ref_long * 100
        ret_1m = (last - ref_recent) / ref_recent * 100
        signal = ret_12m - ret_1m  # the "12-1" signal
        candidates.append({
            "ticker": ticker,
            "price": last,
            "ret_12m_pct": round(ret_12m, 2),
            "ret_1m_pct": round(ret_1m, 2),
            "momentum_signal": round(signal, 2),
            "sector": f.get("sector"),
            "market_cap": f.get("market_cap"),
        })

    candidates.sort(key=lambda x: x["momentum_signal"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_filters": len(candidates),
        "candidates": candidates,
        "note": "This is a portfolio-construction signal. Hold 1-3 months, monthly rebalance.",
    }
