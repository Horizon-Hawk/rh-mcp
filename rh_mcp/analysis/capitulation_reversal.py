"""Capitulation reversal scanner.

Two-bar pattern:
- Yesterday: ≥3× avg volume, closed ≥5% down, close in bottom 25% of day's range
- Today: close in top 50% of today's range (reversal candle), today's high > yesterday's close

Edge: capitulation lows often mark short-term bottoms. 60-65% win rate on the
bounce, 2:1 R:R achievable with stop below yesterday's low.

Best run mid-to-late session after today's bar has settled (10:30+ AM ET).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

MIN_VOL_RATIO_YESTERDAY = 3.0   # yesterday's vol vs 20d avg
MIN_DECLINE_PCT_YESTERDAY = 5.0  # how far yesterday dropped
MAX_CLOSE_IN_RANGE_PCT_YESTERDAY = 25.0  # close must be in bottom N% of range
MIN_CLOSE_IN_RANGE_PCT_TODAY = 50.0      # today's close in top N% of range
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000

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


def _fetch_recent_bars(tickers: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {t: [] for t in tickers}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            raw = rh.stocks.get_stock_historicals(
                chunk, interval="day", span="month", bounds="regular",
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
                    "open": float(b["open_price"]),
                    "high": float(b["high_price"]),
                    "low": float(b["low_price"]),
                    "close": float(b["close_price"]),
                    "volume": int(b["volume"]),
                })
            except (KeyError, ValueError, TypeError):
                pass
        time.sleep(_BATCH_SLEEP)
    for sym in out:
        out[sym].sort(key=lambda x: x["date"])
    return out


def _close_pct_of_range(bar: dict) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 50.0
    return (bar["close"] - bar["low"]) / rng * 100


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_vol_ratio_yesterday: float = MIN_VOL_RATIO_YESTERDAY,
    min_decline_pct_yesterday: float = MIN_DECLINE_PCT_YESTERDAY,
    max_close_in_range_pct_yesterday: float = MAX_CLOSE_IN_RANGE_PCT_YESTERDAY,
    min_close_in_range_pct_today: float = MIN_CLOSE_IN_RANGE_PCT_TODAY,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find capitulation+reversal two-bar patterns. Best run mid-session onwards."""
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")
    bars = _fetch_recent_bars(tickers)

    candidates: list[dict] = []
    for ticker in tickers:
        b = bars.get(ticker, [])
        if len(b) < 22:  # need ~1 month for 20d avg + 2 days of pattern
            continue

        yesterday = b[-2]
        today = b[-1]
        if today["close"] < min_price:
            continue

        # 20d avg volume excluding the last 2 bars (so the capitulation day doesn't pollute the avg)
        avg_vol_20 = mean(x["volume"] for x in b[-22:-2]) if len(b) >= 22 else 0
        if avg_vol_20 < min_avg_volume:
            continue

        # Yesterday capitulation check
        if yesterday["volume"] / avg_vol_20 < min_vol_ratio_yesterday:
            continue
        prior_close = b[-3]["close"] if len(b) >= 3 else yesterday["open"]
        if prior_close <= 0:
            continue
        decline_pct = (yesterday["close"] - prior_close) / prior_close * 100
        if decline_pct > -min_decline_pct_yesterday:
            continue  # not a real capitulation
        if _close_pct_of_range(yesterday) > max_close_in_range_pct_yesterday:
            continue  # not enough weakness at close

        # Today reversal check
        if _close_pct_of_range(today) < min_close_in_range_pct_today:
            continue
        if today["high"] < yesterday["close"]:
            continue  # no upside reaction
        if today["close"] < yesterday["close"]:
            continue  # still declining

        bounce_pct = (today["close"] - yesterday["close"]) / yesterday["close"] * 100

        candidates.append({
            "ticker": ticker,
            "yesterday_close": yesterday["close"],
            "yesterday_decline_pct": round(decline_pct, 2),
            "yesterday_vol_ratio": round(yesterday["volume"] / avg_vol_20, 2),
            "yesterday_close_in_range_pct": round(_close_pct_of_range(yesterday), 1),
            "today_close": today["close"],
            "today_close_in_range_pct": round(_close_pct_of_range(today), 1),
            "bounce_pct_so_far": round(bounce_pct, 2),
            "suggested_stop_below": yesterday["low"],
        })

    # Rank by yesterday's capitulation intensity (more dramatic = more potential bounce)
    candidates.sort(key=lambda x: x["yesterday_vol_ratio"] * abs(x["yesterday_decline_pct"]), reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "candidates": candidates,
    }
