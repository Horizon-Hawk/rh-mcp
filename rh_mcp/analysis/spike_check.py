"""Volume-spike entry validator: staleness, drift, fade gates.

Ported from spike_check.py CLI. Returns GO / STALE / CHASING / FADING.
"""

from __future__ import annotations

from datetime import datetime

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

STALE_SECONDS = 180
CHASE_PCT = 5.0
FADE_PCT = 4.0


def analyze(ticker: str, spike_price: float, ts: str) -> dict:
    """Validate a spike entry. ts = naive 'YYYY-MM-DDTHH:MM:SS' (same clock as datetime.now)."""
    ticker = ticker.upper()
    try:
        spike_time = datetime.fromisoformat(ts)
    except ValueError:
        return {"ticker": ticker, "verdict": "ERROR", "reason": f"invalid timestamp: {ts}"}

    elapsed = (datetime.now() - spike_time).total_seconds()

    login()
    prices = rh.get_latest_price(ticker)
    if not prices or not prices[0]:
        return {
            "ticker": ticker, "verdict": "ERROR",
            "reason": "could not fetch current price",
            "elapsed_seconds": int(elapsed),
        }

    current_price = float(prices[0])
    drift_pct = (current_price - spike_price) / spike_price * 100

    if elapsed > STALE_SECONDS:
        verdict = "STALE"
        reason = f"{int(elapsed)}s since spike — move is over ({STALE_SECONDS}s limit)"
    elif drift_pct > CHASE_PCT:
        verdict = "CHASING"
        reason = f"Price ran +{drift_pct:.1f}% from spike level — too extended to enter"
    elif drift_pct < -CHASE_PCT:
        verdict = "CHASING"
        reason = f"Price dropped {drift_pct:.1f}% from spike level (short) — too extended"
    elif drift_pct < -FADE_PCT:
        verdict = "FADING"
        reason = f"Price faded {drift_pct:.1f}% from spike — likely a fake, skip"
    else:
        verdict = "GO"
        reason = (
            f"{int(elapsed)}s elapsed, price at ${current_price:.2f} "
            f"({drift_pct:+.1f}% from spike ${spike_price:.2f}) — within limits"
        )

    return {
        "ticker": ticker,
        "verdict": verdict,
        "reason": reason,
        "spike_price": spike_price,
        "current_price": current_price,
        "drift_pct": round(drift_pct, 2),
        "elapsed_seconds": int(elapsed),
        "use_price": current_price,
    }
