"""Unusual options activity scanner.

True OI-spike detection requires daily OI history snapshots (RH doesn't expose
this). This v1 uses two proxies for "unusual activity" that can be computed
from a single options_scan snapshot:

1. **Volume-to-OI turnover** — for a given strike, today's volume / open
   interest. Values > 1.0 mean today's trading exceeded total existing
   positioning at that strike → fresh institutional positioning likely.

2. **OI concentration vs. typical strikes** — strikes where OI is > 5x the
   median strike OI for the ticker, ranked. These are "wall" strikes that
   options scanners already identify as max-pain pins.

Output: per-ticker anomalies list, plus a `turnover_score` summary that
ranks tickers by aggregate unusual-activity intensity.

Caveats:
- Needs an input ticker list (running options_scan on 1500-name universe
  would take ~3 hours; this is a focused-list tool, not a universe scan)
- Concentration signal weakens for very low-OI names (anything * 5 is still
  small absolute OI) — filtered by min_strike_oi
- No directional bias inferred — caller must interpret call/put context

Future v2: cache OI snapshots daily, then true OI-delta detection becomes
straightforward (compare today's per-strike OI vs yesterday's).
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
from statistics import median

from rh_mcp.analysis import options_scan as _options_scan
from rh_mcp.auth import login

MIN_TURNOVER_RATIO = 0.5    # volume/OI > this = fresh activity
CONCENTRATION_MULTIPLE = 5.0  # strike_oi > N * median_strike_oi = wall
MIN_STRIKE_OI = 100         # ignore very small strikes
MAX_SCAN_PARALLEL = 4

DEFAULT_TOP_N = 15


def _options_one(ticker: str) -> dict:
    """Single options_scan call wrapped with safe error handling."""
    try:
        return _options_scan.analyze(ticker)
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def _find_anomalies(scan: dict, min_oi: int, conc_multiple: float, min_turnover: float) -> list[dict]:
    """Extract per-strike anomalies from a single options_scan output."""
    anomalies: list[dict] = []
    if scan.get("error"):
        return anomalies

    # Gather all strike rows from the call_walls + put_walls lists (these
    # are already the top-OI strikes from options_scan)
    rows = []
    for w in scan.get("call_walls", []):
        rows.append({"side": "CALL", **w})
    for w in scan.get("put_walls", []):
        rows.append({"side": "PUT", **w})

    if not rows:
        return anomalies

    # Concentration: strike_oi > N * median
    med_oi = median([r.get("oi", 0) for r in rows]) or 1
    for r in rows:
        oi = r.get("oi", 0)
        vol = r.get("volume", 0)
        if oi < min_oi:
            continue
        # Turnover anomaly
        if oi > 0 and (vol / oi) >= min_turnover:
            anomalies.append({
                "ticker": scan["ticker"],
                "type": "turnover",
                "side": r["side"],
                "strike": r["strike"],
                "oi": oi,
                "volume": vol,
                "turnover_ratio": round(vol / oi, 2),
                "note": "today's volume is a meaningful fraction of total OI — fresh positioning",
            })
        # Concentration anomaly
        if oi >= med_oi * conc_multiple:
            anomalies.append({
                "ticker": scan["ticker"],
                "type": "concentration",
                "side": r["side"],
                "strike": r["strike"],
                "oi": oi,
                "median_strike_oi": med_oi,
                "concentration_x": round(oi / med_oi, 1),
                "note": f"OI is {oi / med_oi:.1f}x median strike — pin level",
            })

    return anomalies


def analyze(
    tickers: list[str],
    min_strike_oi: int = MIN_STRIKE_OI,
    concentration_multiple: float = CONCENTRATION_MULTIPLE,
    min_turnover_ratio: float = MIN_TURNOVER_RATIO,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan a focused ticker list for unusual options activity.

    NOTE: this is a focused-list tool, NOT a full-universe scanner.
    Each ticker requires a full options_scan (~5-10s). 20 tickers ≈ 30-60s
    with parallel-4 fan-out. Pass scan_all output or your watchlist.
    """
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "no tickers provided"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    # Pull options scans in parallel (rate-limit-bounded)
    scans: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SCAN_PARALLEL) as ex:
        futures = {ex.submit(_options_one, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            scans[t] = fut.result()

    # Extract anomalies per ticker
    per_ticker: dict[str, dict] = {}
    for t, scan in scans.items():
        if scan.get("error"):
            continue
        anomalies = _find_anomalies(scan, min_strike_oi, concentration_multiple, min_turnover_ratio)
        if not anomalies:
            continue
        # Score = sum of turnover_ratios + sum of concentration_x
        score = 0.0
        for a in anomalies:
            if a["type"] == "turnover":
                score += a.get("turnover_ratio", 0)
            elif a["type"] == "concentration":
                score += a.get("concentration_x", 0) / 2.0
        per_ticker[t] = {
            "ticker": t,
            "current_price": scan.get("current_price"),
            "expiry_scanned": scan.get("expiry"),
            "anomaly_count": len(anomalies),
            "turnover_score": round(score, 2),
            "anomalies": anomalies,
        }

    candidates = sorted(per_ticker.values(), key=lambda x: x["turnover_score"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "flagged_count": len(candidates),
        "candidates": candidates,
    }
