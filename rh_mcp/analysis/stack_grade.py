"""Triple-screen stacked grade: float + RV + news catalyst.

Ported from stack_grade.py CLI. Combines float_check, historicals, and news
in-process (no subprocess fan-out) and returns A+/A/B/SKIP STACK.
"""

from __future__ import annotations

import concurrent.futures

from rh_mcp.analysis import earnings as _earnings  # noqa: F401  reserved for future use
from rh_mcp.analysis import float_check as _flt
from rh_mcp.analysis import historicals as _hist
from rh_mcp.analysis import news as _news


def _grade_one(ticker: str) -> dict:
    """Pull all three signals in parallel for one ticker, combine, return graded dict."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        fut_float = ex.submit(_flt.analyze, ticker)
        fut_hist = ex.submit(_hist.analyze, ticker)
        fut_news = ex.submit(_news.analyze, ticker)
        flt_data = fut_float.result()
        hist_data = fut_hist.result()
        news_data = fut_news.result()

    if isinstance(flt_data, list) and flt_data:
        f0 = flt_data[0]
        squeeze = f0.get("squeeze_grade", "UNKNOWN")
        float_bucket = f0.get("float_bucket", "UNKNOWN")
        short_pct = f0.get("short_pct_of_float")
    else:
        squeeze, float_bucket, short_pct = "UNKNOWN", "UNKNOWN", None

    setup = hist_data.get("setup_grade", "UNKNOWN")
    rv = hist_data.get("volume_ratio", 0) or 0
    body_pct = hist_data.get("body_pct", 0) or 0

    today_count = 0
    today_strong = False
    for n in news_data or []:
        if n.get("age") == "TODAY":
            today_count += 1
            cat = (n.get("catalyst") or "").upper()
            if cat in ("UPGRADE", "BEAT", "DOWNGRADE", "WARNING", "MISS"):
                today_strong = True

    has_today_catalyst = today_count > 0
    has_squeeze = squeeze in ("SQUEEZE", "ELEVATED")

    if setup in ("A", "A+") and has_squeeze and has_today_catalyst:
        stack = "A+ STACK"
    elif setup in ("A", "A+") and (has_squeeze or today_strong):
        stack = "A STACK"
    elif setup in ("A", "A+", "B") and rv >= 1.5:
        stack = "B STACK"
    else:
        stack = "SKIP STACK"

    return {
        "ticker": ticker,
        "setup": setup,
        "rv": rv,
        "body_pct": body_pct,
        "squeeze": squeeze,
        "float_bucket": float_bucket,
        "short_pct": short_pct,
        "today_news": today_count,
        "today_strong": today_strong,
        "stack": stack,
    }


def analyze(tickers: list[str] | str) -> list[dict]:
    """Stacked float + RV + news grade for one or more tickers (run in parallel)."""
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    if not cleaned:
        return []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(cleaned))) as ex:
        futures = {ex.submit(_grade_one, t): t for t in cleaned}
        for f in concurrent.futures.as_completed(futures):
            ticker = futures[f]
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"ticker": ticker, "error": str(e)})
    # Preserve input order for stable output
    by_ticker = {r["ticker"]: r for r in results}
    return [by_ticker[t] for t in cleaned if t in by_ticker]
