"""ORB mega-cap breadth signal — pull 8 NQ mega-caps from massive.com and
compute how many confirmed direction during the OR or breakout bar.

Exploratory finding from 60d sample (NOT statistically validated):
  - HIGH breadth consensus (>=5/8 confirming) at the 09:45 breakout bar
    correlated with failure on 11/13 stop-outs.
  - Interpretation: when everyone piles in at the breakout, it's exhausted.
  - LOW/divided breadth at breakout correlated with follow-through.

Treat as SOFT WARNING, not hard skip. Sample is small and regime-specific
(needs 5x more trades to validate). Output is informational alongside the
trigger.

Caching: today's breadth is computed once and cached in memory for the rest
of the day to avoid burning quota on repeated scans.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, time as dtime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))


MEGA_CAPS = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO"]
KEY_FILE = "~/.marketdata_api_key"

# In-memory cache: {(date_iso, window_label): {ticker: {open, close, pct}}}
_CACHE: dict[tuple[str, str], dict] = {}


def _load_key() -> Optional[str]:
    try:
        return open(os.path.expanduser(KEY_FILE)).read().strip()
    except Exception:
        return None


def _fetch_bar_for_window(ticker: str, date_iso: str, start_hh: int, start_mm: int, key: str) -> dict | None:
    """Pull the 15-min bar that starts at start_hh:start_mm ET on date_iso for ticker.

    Massive returns timestamps in ms epoch (UTC). The 09:30 ET bar = 13:30 UTC = 14:30 UTC depending on DST.
    Returns {open, high, low, close, volume, et_time} or None.
    """
    url = (f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/15/minute/"
           f"{date_iso}/{date_iso}?adjusted=true&limit=200")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    if d.get("status") != "OK":
        return None

    # Find the bar starting at start_hh:start_mm ET
    for b in d.get("results", []) or []:
        t_ms = b["t"]
        dt_et = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).astimezone(ET)
        if dt_et.hour == start_hh and dt_et.minute == start_mm:
            return {
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b.get("v"),
                "et_time": dt_et.strftime("%Y-%m-%d %H:%M ET"),
            }
    return None


def _compute_breadth(bars_by_ticker: dict[str, dict | None]) -> dict:
    """Given a dict of {ticker: bar or None}, compute the breadth metrics.

    Returns:
      n_up:      count of tickers where close > open (bullish bar)
      n_down:    count where close < open
      n_flat:    count where close == open
      n_missing: tickers that failed to fetch
      moves:     per-ticker pct change (open -> close)
      consensus_score: max(n_up, n_down) / 8 — how unanimous the breadth is
    """
    n_up = n_down = n_flat = n_missing = 0
    moves: dict[str, float | None] = {}
    for t, bar in bars_by_ticker.items():
        if bar is None or bar.get("open") in (None, 0):
            n_missing += 1
            moves[t] = None
            continue
        pct = (bar["close"] - bar["open"]) / bar["open"] * 100
        moves[t] = round(pct, 3)
        if pct > 0:
            n_up += 1
        elif pct < 0:
            n_down += 1
        else:
            n_flat += 1
    n_resolved = n_up + n_down + n_flat
    return {
        "n_up": n_up,
        "n_down": n_down,
        "n_flat": n_flat,
        "n_missing": n_missing,
        "moves": moves,
        "consensus_score": max(n_up, n_down) / 8.0,
    }


def get_breadth(date_et: str | None = None, window: str = "breakout") -> dict:
    """Compute mega-cap breadth for the given window on date_et.

    Args:
      date_et: 'YYYY-MM-DD'. Defaults to today (ET).
      window: 'or' (09:30 ET bar) or 'breakout' (09:45 ET bar).

    Returns dict with breadth metrics. Cached per (date, window) in memory.
    Soft-warning interpretation: consensus_score >= 0.625 (5/8) at breakout
    bar correlated with failure in the 60d exploratory sample.
    """
    if date_et is None:
        date_et = datetime.now(tz=ET).strftime("%Y-%m-%d")

    if window == "or":
        start_hh, start_mm = 9, 30
    elif window == "breakout":
        start_hh, start_mm = 9, 45
    else:
        return {"success": False, "error": f"unknown window: {window}"}

    cache_key = (date_et, window)
    if cache_key in _CACHE:
        return {**_CACHE[cache_key], "cached": True}

    key = _load_key()
    if not key:
        return {"success": False, "error": "marketdata API key not found at ~/.marketdata_api_key"}

    bars: dict[str, dict | None] = {}
    for ticker in MEGA_CAPS:
        bars[ticker] = _fetch_bar_for_window(ticker, date_et, start_hh, start_mm, key)
        # Rate limit: 5 calls/min = 12s between calls. We have 8 tickers = ~96s total.
        time.sleep(13)

    metrics = _compute_breadth(bars)
    consensus = metrics["consensus_score"]

    result = {
        "success": True,
        "date_et": date_et,
        "window": window,
        "window_start_et": f"{start_hh:02d}:{start_mm:02d}",
        "tickers_evaluated": len(MEGA_CAPS),
        **metrics,
        "warning_threshold": 0.625,  # 5/8 = exhaustion signal
        "warning_active": consensus >= 0.625,
        "interpretation": _interpret(metrics["n_up"], metrics["n_down"], metrics["n_missing"]),
    }
    _CACHE[cache_key] = result
    return result


def _interpret(n_up: int, n_down: int, n_missing: int) -> str:
    if n_missing >= 4:
        return "insufficient data (too many tickers missing) — soft signal unavailable"
    if n_up >= 6:
        return (f"{n_up}/8 mega-caps UP at this bar — strong bullish consensus. "
                "Exploratory data suggests LONG breakouts at this consensus level FAIL more often (likely exhaustion). "
                "Soft warning: if your trigger is LONG here, consider waiting for divergence or skipping.")
    if n_down >= 6:
        return (f"{n_down}/8 mega-caps DOWN at this bar — strong bearish consensus. "
                "Same caveat for SHORT triggers: high consensus correlated with failure in 60d exploratory sample.")
    if 4 <= n_up <= 5 and 4 <= n_down <= 5:
        return f"breadth divided ({n_up} up / {n_down} down) — no exhaustion warning, breakout has room to run"
    direction = "up" if n_up > n_down else "down"
    return f"mild {direction}-bias ({max(n_up, n_down)}/8 confirming). Soft signal only."


def clear_cache() -> None:
    """For tests/admin — invalidate today's cached breadth."""
    _CACHE.clear()
