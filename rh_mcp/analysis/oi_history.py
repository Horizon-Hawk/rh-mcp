"""Daily OI snapshot + delta detection — the foundation `scan_unusual_oi` v2 needs.

RH doesn't expose historical OI directly. This module snapshots the current
chain (top-OI strikes per ticker) into JSON files keyed by date, then provides
a delta function that compares today's snapshot to N days ago and flags strikes
where OI grew by `min_delta_pct` or `min_delta_abs`.

Workflow:
1. **Daily snapshot job** — call `snapshot_universe(tickers)` once per day after
   close. Writes `RH_OI_HISTORY_DIR/YYYY-MM-DD/<ticker>.json` with the OI snapshot.
2. **Delta query** — call `find_oi_spikes(tickers, days_back=1)` to compare
   today's chain vs the snapshot from N days ago.

Files are append-only — older snapshots accumulate so you can backtest delta
thresholds. Default retention: 90 days (run cleanup separately).

Storage path: $RH_OI_HISTORY_DIR (default: C:/Users/algee/.oi_history).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

OI_HISTORY_DIR = Path(os.environ.get("RH_OI_HISTORY_DIR", r"C:\Users\algee\.oi_history"))
TOP_STRIKES_PER_SIDE = 10        # keep top 10 call + top 10 put per ticker
MIN_OI_TO_SNAPSHOT = 50          # ignore strikes below this OI
MAX_SCAN_PARALLEL = 4

DEFAULT_MIN_DELTA_PCT = 50.0
DEFAULT_MIN_DELTA_ABS = 500


def _snapshot_one(ticker: str) -> dict | None:
    """Pull current top-OI strikes (calls + puts) for the nearest reasonable expiry."""
    try:
        chain = rh.options.get_chains(ticker)
        dates = sorted(chain.get("expiration_dates", []))
    except Exception:
        return None
    # Pick expiry 14-45 days out — focus on the active near-term chain
    today = date.today()
    target_expiry = None
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta_days = (dt - today).days
        if 14 <= delta_days <= 45:
            target_expiry = d
            break
    if not target_expiry:
        return None

    try:
        calls = rh.options.find_options_by_expiration(ticker, target_expiry, optionType="call") or []
        puts = rh.options.find_options_by_expiration(ticker, target_expiry, optionType="put") or []
    except Exception:
        return None

    def _top(rows):
        parsed = []
        for r in rows:
            try:
                oi = int(float(r.get("open_interest") or 0))
                strike = float(r.get("strike_price") or 0)
                if oi >= MIN_OI_TO_SNAPSHOT and strike > 0:
                    parsed.append({"strike": strike, "oi": oi})
            except (ValueError, TypeError):
                pass
        parsed.sort(key=lambda x: x["oi"], reverse=True)
        return parsed[:TOP_STRIKES_PER_SIDE]

    return {
        "ticker": ticker,
        "snapshot_date": today.isoformat(),
        "expiry": target_expiry,
        "calls": _top(calls),
        "puts": _top(puts),
    }


def snapshot_universe(tickers: list[str]) -> dict:
    """Snapshot OI for a list of tickers and write to OI_HISTORY_DIR/YYYY-MM-DD/."""
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "no tickers"}

    login()
    today_str = date.today().isoformat()
    out_dir = OI_HISTORY_DIR / today_str
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SCAN_PARALLEL) as ex:
        futures = {ex.submit(_snapshot_one, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            snap = fut.result()
            if not snap:
                failed += 1
                continue
            (out_dir / f"{t}.json").write_text(json.dumps(snap, indent=2))
            written += 1

    return {
        "success": True,
        "snapshot_date": today_str,
        "out_dir": str(out_dir),
        "written": written,
        "failed": failed,
    }


def _load_snapshot(ticker: str, snap_date: date) -> dict | None:
    p = OI_HISTORY_DIR / snap_date.isoformat() / f"{ticker}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def find_oi_spikes(
    tickers: list[str],
    days_back: int = 1,
    min_delta_pct: float = DEFAULT_MIN_DELTA_PCT,
    min_delta_abs: int = DEFAULT_MIN_DELTA_ABS,
) -> dict:
    """Compare today's OI vs a snapshot from `days_back` days ago.

    Flag any strike where:
    - delta_oi >= min_delta_abs AND
    - delta_oi / prior_oi * 100 >= min_delta_pct

    Returns per-ticker list of spike strikes. If today hasn't been snapshotted yet,
    snapshots inline before comparing. If the historical snapshot is missing, that
    ticker is skipped (with a note).
    """
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "no tickers"}

    today = date.today()
    prior = today - timedelta(days=days_back)

    # Snapshot today inline if missing
    today_dir = OI_HISTORY_DIR / today.isoformat()
    needs_snapshot = [t for t in tickers if not (today_dir / f"{t}.json").exists()]
    if needs_snapshot:
        snapshot_universe(needs_snapshot)

    results: list[dict] = []
    missing: list[str] = []
    for ticker in tickers:
        today_snap = _load_snapshot(ticker, today)
        prior_snap = _load_snapshot(ticker, prior)
        if not today_snap or not prior_snap:
            missing.append(ticker)
            continue
        if today_snap.get("expiry") != prior_snap.get("expiry"):
            # Expiry rolled — can't compare apples to apples
            missing.append(ticker)
            continue

        prior_calls = {c["strike"]: c["oi"] for c in prior_snap.get("calls", [])}
        prior_puts = {p["strike"]: p["oi"] for p in prior_snap.get("puts", [])}

        spikes: list[dict] = []
        for side, today_rows, prior_map in [
            ("CALL", today_snap.get("calls", []), prior_calls),
            ("PUT", today_snap.get("puts", []), prior_puts),
        ]:
            for r in today_rows:
                today_oi = r["oi"]
                prior_oi = prior_map.get(r["strike"], 0)
                delta_oi = today_oi - prior_oi
                if delta_oi < min_delta_abs:
                    continue
                if prior_oi > 0 and (delta_oi / prior_oi * 100) < min_delta_pct:
                    continue
                spikes.append({
                    "side": side,
                    "strike": r["strike"],
                    "today_oi": today_oi,
                    "prior_oi": prior_oi,
                    "delta_oi": delta_oi,
                    "delta_pct": round(delta_oi / prior_oi * 100, 1) if prior_oi else float("inf"),
                })

        if spikes:
            spikes.sort(key=lambda x: x["delta_oi"], reverse=True)
            results.append({
                "ticker": ticker,
                "expiry": today_snap.get("expiry"),
                "spike_count": len(spikes),
                "spikes": spikes,
            })

    results.sort(key=lambda r: sum(s["delta_oi"] for s in r["spikes"]), reverse=True)

    return {
        "success": True,
        "compared_dates": {"today": today.isoformat(), "prior": prior.isoformat()},
        "tickers_scanned": len(tickers),
        "missing_snapshots": missing,
        "tickers_with_spikes": len(results),
        "results": results,
    }
