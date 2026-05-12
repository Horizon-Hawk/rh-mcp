"""Post-Earnings Announcement Drift (PEAD) scanner.

The most academically robust market anomaly (Bernard & Thomas 1989, replicated
30+ times). Stocks with positive earnings surprises drift higher for ~60 days
after the print. Literature edge: 0.5-1.5% per month excess return.

Filters:
- Earnings reported in last 5-30 days (skip too-fresh and too-stale)
- EPS beat estimate by >= min_eps_beat_pct (default 5%)
- Stock opened up on print day by >= min_gap_pct (confirms positive reaction)
- Today's price still above pre-earnings close (drift intact)

Output ranked by surprise magnitude × distance from pre-earnings price.
"""

from __future__ import annotations

import concurrent.futures
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

MIN_DAYS_SINCE_EARNINGS = 5
MAX_DAYS_SINCE_EARNINGS = 30
MIN_EPS_BEAT_PCT = 5.0          # actual must beat estimate by >= this %
MIN_GAP_PCT = 3.0               # print-day open must be >= this % above prior close
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 200_000
MIN_MARKET_CAP = 50_000_000_000  # $50B — backtested step-function in edge by size:
                                 #   Full universe (1592, ≥$2B):    52.7% win / +1.02% avg / PF 1.23
                                 #   Mega-cap subset (314, ≥$50B):  56.3% win / +2.47% avg / PF 1.74  <-- live default
                                 #   Hand-picked 30 mega-caps:      64.7% win / +3.42% avg / PF 2.32 (but heavy survivorship bias)
                                 # The $2B floor did nothing — the user's stock_universe.txt already pre-filters to mid+ caps.
                                 # $50B is the sweet spot: real edge, enough names (314), survivorship pressure manageable.

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


def _recent_earnings_beat(ticker: str, min_days: int, max_days: int, min_beat_pct: float) -> dict | None:
    try:
        raw = rh.stocks.get_earnings(ticker, info=None) or []
    except Exception:
        return None
    today = date.today()
    candidates = []
    for r in raw:
        report = r.get("report") or {}
        ds = report.get("date")
        eps = r.get("eps") or {}
        estimate = eps.get("estimate")
        actual = eps.get("actual")
        if not ds or estimate is None or actual is None:
            continue
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            est = float(estimate)
            act = float(actual)
        except (ValueError, TypeError):
            continue
        days_ago = (today - d).days
        if not (min_days <= days_ago <= max_days):
            continue
        if est <= 0:
            continue
        beat_pct = (act - est) / abs(est) * 100
        if beat_pct < min_beat_pct:
            continue
        candidates.append({
            "earnings_date": ds,
            "days_ago": days_ago,
            "eps_estimate": est,
            "eps_actual": act,
            "beat_pct": round(beat_pct, 1),
        })
    if not candidates:
        return None
    return min(candidates, key=lambda x: x["days_ago"])


def _gap_and_drift(ticker: str, earnings_date: str) -> dict | None:
    """Pull 60d daily bars; compute gap on earnings open + today vs pre-earnings close."""
    try:
        raw = rh.stocks.get_stock_historicals(
            ticker, interval="day", span="3month", bounds="regular",
        ) or []
    except Exception:
        return None
    bars = []
    for b in raw:
        try:
            bars.append({
                "date": b["begins_at"][:10],
                "open": float(b["open_price"]),
                "close": float(b["close_price"]),
                "volume": int(b["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            pass
    if len(bars) < 30:
        return None
    bars.sort(key=lambda x: x["date"])
    # Find the print-day bar (first bar on or after the announcement)
    print_idx = None
    for i, b in enumerate(bars):
        if b["date"] >= earnings_date:
            print_idx = i
            break
    if print_idx is None or print_idx == 0:
        return None
    pre_close = bars[print_idx - 1]["close"]
    print_open = bars[print_idx]["open"]
    today_close = bars[-1]["close"]
    if pre_close <= 0:
        return None
    return {
        "pre_earnings_close": pre_close,
        "print_open": print_open,
        "today_close": today_close,
        "gap_pct": round((print_open - pre_close) / pre_close * 100, 2),
        "drift_pct": round((today_close - pre_close) / pre_close * 100, 2),
    }


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
            try:
                market_cap = float(f.get("market_cap") or 0)
            except (ValueError, TypeError):
                market_cap = 0
            out[sym.upper()] = {
                "avg_volume": avg_vol,
                "market_cap": market_cap,
                "sector": f.get("sector"),
                "industry": f.get("industry"),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_days_since_earnings: int = MIN_DAYS_SINCE_EARNINGS,
    max_days_since_earnings: int = MAX_DAYS_SINCE_EARNINGS,
    min_eps_beat_pct: float = MIN_EPS_BEAT_PCT,
    min_gap_pct: float = MIN_GAP_PCT,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    min_market_cap: float = MIN_MARKET_CAP,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find PEAD candidates: recent earnings beat, gap-up confirmation, drift intact.

    Default min_market_cap = $2B based on tonight's universe-wide backtest
    showing the PEAD edge concentrates in mid+ caps (large-cap 64.7% win rate
    vs full-universe 52.7%). Set to 0 to scan all qualifying names.
    """
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

    # Stage 1: find recent earnings beats (cheap)
    beats: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_recent_earnings_beat, t, min_days_since_earnings, max_days_since_earnings, min_eps_beat_pct): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            r = fut.result()
            if r:
                beats.append({"ticker": t, **r})

    # Stage 2: gap + drift verification
    candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_gap_and_drift, b["ticker"], b["earnings_date"]): b for b in beats}
        for fut in concurrent.futures.as_completed(futures):
            b = futures[fut]
            gd = fut.result()
            if not gd:
                continue
            if gd["gap_pct"] < min_gap_pct:
                continue
            if gd["today_close"] < gd["pre_earnings_close"]:
                continue  # drift broken — price now below pre-earnings close
            if gd["today_close"] < min_price:
                continue
            fund = funds.get(b["ticker"], {})
            if fund.get("avg_volume", 0) < min_avg_volume:
                continue
            if fund.get("market_cap", 0) < min_market_cap:
                continue
            candidates.append({
                "ticker": b["ticker"],
                "price": gd["today_close"],
                "market_cap": fund.get("market_cap"),
                "earnings_date": b["earnings_date"],
                "days_since_earnings": b["days_ago"],
                "eps_estimate": b["eps_estimate"],
                "eps_actual": b["eps_actual"],
                "beat_pct": b["beat_pct"],
                "gap_pct": gd["gap_pct"],
                "drift_pct": gd["drift_pct"],
                "pre_earnings_close": gd["pre_earnings_close"],
                "sector": fund.get("sector"),
                "industry": fund.get("industry"),
                "score": round(b["beat_pct"] * gd["drift_pct"] / 100, 2),
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_beat_filter": len(beats),
        "passed_drift_filter": len(candidates),
        "candidates": candidates,
    }
