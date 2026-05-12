"""Next earnings date checker — SKIP / CAUTION / CLEAR signal.

Ported from earnings.py CLI. Uses robin_stocks (shared login via rh_mcp.auth).
"""

from __future__ import annotations

from datetime import date, datetime

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

SKIP_DAYS = 5
CAUTION_DAYS = 14


def _next_earnings(ticker: str) -> dict:
    today = date.today()
    records = rh.stocks.get_earnings(ticker, info=None) or []

    upcoming = []
    for r in records:
        report = r.get("report") or {}
        date_str = report.get("date")
        actual = (r.get("eps") or {}).get("actual")
        if not date_str:
            continue
        try:
            report_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if report_date >= today and actual is None:
            upcoming.append({
                "date": date_str,
                "days": (report_date - today).days,
                "timing": report.get("timing", "?"),
                "verified": report.get("verified", False),
                "year": r.get("year"),
                "quarter": r.get("quarter"),
                "eps_est": (r.get("eps") or {}).get("estimate"),
            })

    if not upcoming:
        return {
            "ticker": ticker,
            "found": False,
            "signal": "UNKNOWN",
            "message": "No future earnings date found in Robinhood data",
        }

    nearest = min(upcoming, key=lambda x: x["days"])

    if nearest["days"] < SKIP_DAYS:
        signal = "SKIP"
    elif nearest["days"] < CAUTION_DAYS:
        signal = "CAUTION"
    else:
        signal = "CLEAR"

    timing_label = {"am": "before open", "pm": "after close"}.get(nearest["timing"], nearest["timing"])
    verified_label = "confirmed" if nearest["verified"] else "estimated"

    return {
        "ticker": ticker,
        "found": True,
        "next_date": nearest["date"],
        "days_away": nearest["days"],
        "timing": timing_label,
        "verified": nearest["verified"],
        "quarter": f"Q{nearest['quarter']} {nearest['year']}",
        "eps_estimate": nearest["eps_est"],
        "signal": signal,
        "verified_label": verified_label,
    }


def analyze(tickers: list[str] | str) -> list[dict]:
    """Next earnings date + SKIP/CAUTION/CLEAR signal for one or more tickers."""
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    login()
    return [_next_earnings(t) for t in cleaned]
