"""Post-earnings IV-crush drift scanner.

Finds stocks where:
- Earnings reported within the last N days (default 5)
- IV rank has dropped to < threshold (premium crushed back to baseline)
- Stock is still in an uptrend (above 20d SMA) — drift thesis intact
- Price up since the earnings print (confirming positive reaction)

Edge: post-earnings IV crush makes options cheap again, but stocks that
beat & raised often drift higher for 1-2 weeks afterwards. Buying cheap
calls during the IV-crushed window captures the residual drift at low
premium cost.
"""

from __future__ import annotations

import concurrent.futures
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.analysis import iv_rank as _iv_rank
from rh_mcp.auth import login

MAX_DAYS_SINCE_EARNINGS = 5
MAX_IV_RANK = 30.0
MIN_PRICE = 10.0
MIN_PCT_SINCE_EARNINGS = 1.0   # positive drift floor
MAX_IV_PARALLEL = 4

DEFAULT_TOP_N = 15


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


def _recent_earnings_date(ticker: str, max_days: int) -> dict | None:
    """Return the most recent reported earnings date within max_days, or None."""
    try:
        raw = rh.stocks.get_earnings(ticker, info=None) or []
    except Exception:
        return None
    today = date.today()
    cutoff = today - timedelta(days=max_days)
    candidates = []
    for r in raw:
        report = r.get("report") or {}
        ds = report.get("date")
        actual = (r.get("eps") or {}).get("actual")
        if not ds or actual is None:
            continue
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff <= d <= today:
            candidates.append({"date": ds, "days_ago": (today - d).days})
    if not candidates:
        return None
    return min(candidates, key=lambda x: x["days_ago"])


def _stock_status(ticker: str) -> dict | None:
    """One-shot pull of recent daily bars to compute price-vs-SMA and pct-since-N-days."""
    try:
        raw = rh.stocks.get_stock_historicals(
            ticker, interval="day", span="month", bounds="regular",
        ) or []
    except Exception:
        return None
    closes = []
    for b in raw:
        try:
            closes.append(float(b["close_price"]))
        except (KeyError, ValueError, TypeError):
            pass
    if len(closes) < 20:
        return None
    sma20 = mean(closes[-20:])
    return {
        "last_close": closes[-1],
        "sma20": sma20,
        "above_sma": closes[-1] > sma20,
        "closes": closes,  # for pct-since-earnings calc by caller
    }


def _iv_rank_one(ticker: str) -> dict:
    try:
        result = _iv_rank.analyze(ticker)
        if isinstance(result, list) and result:
            return result[0]
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    return {"ticker": ticker, "error": "no data"}


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    max_days_since_earnings: int = MAX_DAYS_SINCE_EARNINGS,
    max_iv_rank: float = MAX_IV_RANK,
    min_price: float = MIN_PRICE,
    min_pct_since_earnings: float = MIN_PCT_SINCE_EARNINGS,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find post-earnings IV-crushed drift candidates."""
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe — provide tickers list or universe_file"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    # Stage 1: find names with recent earnings
    recent_earners: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_recent_earnings_date, t, max_days_since_earnings): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            er = fut.result()
            if er:
                recent_earners.append({"ticker": t, **er})

    # Stage 2: filter by uptrend + drift since earnings
    passed_drift: list[dict] = []
    for r in recent_earners:
        status = _stock_status(r["ticker"])
        if not status or not status["above_sma"]:
            continue
        closes = status["closes"]
        # pct since earnings: compare today's close to close `days_ago` days back
        idx_back = max(0, len(closes) - 1 - r["days_ago"])
        ref_close = closes[idx_back]
        if ref_close <= 0:
            continue
        pct_since = (status["last_close"] - ref_close) / ref_close * 100
        if pct_since < min_pct_since_earnings:
            continue
        r["last_close"] = status["last_close"]
        r["sma20"] = status["sma20"]
        r["pct_since_earnings"] = round(pct_since, 2)
        passed_drift.append(r)

    # Stage 3: IV rank filter (expensive — parallel-bounded)
    candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IV_PARALLEL) as ex:
        futures = {ex.submit(_iv_rank_one, e["ticker"]): e for e in passed_drift}
        for fut in concurrent.futures.as_completed(futures):
            e = futures[fut]
            ivr = fut.result()
            if ivr.get("error"):
                continue
            current_price = ivr.get("current_price")
            iv_rank = ivr.get("iv_rank")
            if not current_price or current_price < min_price:
                continue
            if iv_rank is None or iv_rank > max_iv_rank:
                continue
            candidates.append({
                "ticker": e["ticker"],
                "price": current_price,
                "iv_rank": iv_rank,
                "iv_percentile": ivr.get("iv_percentile"),
                "earnings_date": e["date"],
                "days_since_earnings": e["days_ago"],
                "pct_since_earnings": e["pct_since_earnings"],
                "above_sma20": True,
            })

    # Sort by iv_rank ascending (cheapest premium first)
    candidates.sort(key=lambda x: x["iv_rank"])
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_recent_earnings": len(recent_earners),
        "passed_drift_filter": len(passed_drift),
        "passed_iv_threshold": len(candidates),
        "candidates": candidates,
    }
