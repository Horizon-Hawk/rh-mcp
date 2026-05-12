"""Failed breakout short scanner.

Finds stocks that broke above their prior-day high in the morning but then
failed back inside the prior range — a classic short setup that works well
in choppy / range-bound tape.

Logic:
- Yesterday's daily bar gives prior-day high (PDH) and low (PDL)
- Today's intraday 5-min bars give the morning extension high + current close
- A "failed breakout" requires:
  1. Today's high > PDH by at least `min_breakout_pct` (real breakout, not just touch)
  2. Current close <= PDH (price has fallen back inside the prior range)
  3. The fade has volume — today's volume so far should be at or above avg pace
  4. Time of day: only meaningful after the first 30-60 min (let the move set up)

Output ranked by "fade depth" — how far back below the morning high price has fallen.
The deeper the fade from the breakout high, the higher conviction the failed-breakout
short signal.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

MIN_BREAKOUT_PCT = 0.3       # today's high must clear PDH by at least this % (filter noise)
MIN_FADE_DEPTH_PCT = 0.5     # current close must be at least this % below today's high
MIN_PRICE = 5.00
MIN_AVG_VOLUME = 500_000

DEFAULT_TOP_N = 15
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


def _fetch_daily_bars(tickers: list[str]) -> dict[str, list[dict]]:
    """Pull last week of daily bars per ticker for PDH/PDL."""
    out: dict[str, list[dict]] = {t: [] for t in tickers}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            raw = rh.stocks.get_stock_historicals(
                chunk, interval="day", span="week", bounds="regular",
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


def _fetch_intraday(tickers: list[str]) -> dict[str, dict]:
    """Today's intraday summary: high/low/last + total volume so far."""
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            raw = rh.stocks.get_stock_historicals(
                chunk, interval="5minute", span="day", bounds="regular",
            ) or []
        except Exception:
            raw = []
        today = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d")  # ~ET date
        by_sym: dict[str, list[dict]] = {}
        for b in raw:
            sym = (b.get("symbol") or "").upper()
            if not sym:
                continue
            ts = b.get("begins_at") or ""
            if not ts.startswith(today):
                continue
            try:
                by_sym.setdefault(sym, []).append({
                    "high": float(b["high_price"]),
                    "low": float(b["low_price"]),
                    "close": float(b["close_price"]),
                    "volume": int(b["volume"]),
                })
            except (KeyError, ValueError, TypeError):
                pass
        for sym, bars in by_sym.items():
            if not bars:
                continue
            out[sym] = {
                "today_high": max(b["high"] for b in bars),
                "today_low": min(b["low"] for b in bars),
                "today_last": bars[-1]["close"],
                "today_volume": sum(b["volume"] for b in bars),
                "bar_count": len(bars),
            }
        time.sleep(_BATCH_SLEEP)
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
            out[sym.upper()] = {
                "avg_volume": _to_float(f.get("average_volume")),
                "sector": f.get("sector"),
                "industry": f.get("industry"),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_breakout_pct: float = MIN_BREAKOUT_PCT,
    min_fade_depth_pct: float = MIN_FADE_DEPTH_PCT,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan for failed breakout shorts: broke above PDH today, then faded back inside.

    Best run mid-session (~10:30 AM ET onwards) — too early in the day and
    the morning extension hasn't fully developed yet. After 2 PM, the signal
    weakens because more time means more chance of recovery.
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

    daily = _fetch_daily_bars(tickers)
    intraday = _fetch_intraday(tickers)
    funds = _fetch_fundamentals(tickers)

    candidates: list[dict] = []
    for ticker in tickers:
        f = funds.get(ticker)
        d = daily.get(ticker, [])
        i = intraday.get(ticker)
        if not f or not i or len(d) < 2:
            continue
        avg_vol = f.get("avg_volume") or 0
        if avg_vol < min_avg_volume:
            continue

        # Prior-day high = previous daily bar (skip today's partial daily if present)
        today_str = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d")
        prior_days = [bar for bar in d if bar["date"] < today_str]
        if not prior_days:
            continue
        pdh = prior_days[-1]["high"]
        pdl = prior_days[-1]["low"]
        if pdh <= 0:
            continue

        today_high = i["today_high"]
        today_last = i["today_last"]
        if today_last < min_price:
            continue

        # Did it actually break out today?
        breakout_pct = (today_high - pdh) / pdh * 100
        if breakout_pct < min_breakout_pct:
            continue

        # Did it fail back below PDH?
        if today_last > pdh:
            continue  # still above PDH, not a fade

        # How deep is the fade from today's high?
        fade_depth_pct = (today_high - today_last) / today_high * 100
        if fade_depth_pct < min_fade_depth_pct:
            continue

        candidates.append({
            "ticker": ticker,
            "prior_day_high": pdh,
            "prior_day_low": pdl,
            "today_high": today_high,
            "today_last": today_last,
            "today_volume": i["today_volume"],
            "breakout_pct": round(breakout_pct, 2),
            "fade_depth_pct": round(fade_depth_pct, 2),
            "below_pdh_pct": round((today_last - pdh) / pdh * 100, 2),
            "sector": f.get("sector"),
            "industry": f.get("industry"),
        })

    # Rank by fade depth — deeper fade = stronger signal
    candidates.sort(key=lambda x: x["fade_depth_pct"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "scanned_at": scanned_at,
        "total_scanned": len(tickers),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
