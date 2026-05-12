"""Alert management — read/add/deactivate price_alerts.json.

This module manages the local alert file consumed by price_alert_monitor.py.
The monitor reloads the file each cycle, so MCP edits take effect within seconds.

Configurable via env var RH_ALERTS_FILE (default: C:\\Users\\algee\\price_alerts.json).
For shared distribution, friends can point this at their own monitor's alert file.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

ALERTS_FILE = Path(os.environ.get("RH_ALERTS_FILE", r"C:\Users\algee\price_alerts.json"))


def _read_alerts() -> list:
    if not ALERTS_FILE.exists():
        return []
    try:
        return json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_alerts(alerts: list) -> None:
    """Atomic write — temp file + rename to avoid the monitor reading mid-write."""
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(alerts, indent=2)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        dir=str(ALERTS_FILE.parent), prefix=".alerts_", suffix=".tmp",
    )
    try:
        tmp.write(payload)
        tmp.close()
        os.replace(tmp.name, ALERTS_FILE)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


def list_alerts(active_only: bool = True, ticker: str | None = None) -> dict:
    """List price alerts.

    Args:
        active_only: True (default) returns only alerts with active=True.
        ticker: Filter to one ticker (case-insensitive).
    """
    alerts = _read_alerts()
    out = alerts
    if active_only:
        out = [a for a in out if a.get("active", True)]
    if ticker:
        sym = ticker.upper()
        out = [a for a in out if (a.get("ticker") or "").upper() == sym]
    return {"success": True, "count": len(out), "alerts": out}


def add_alert(
    ticker: str,
    target: float,
    direction: str,
    grade: str = "watch",
    note: str = "",
    active: bool = True,
) -> dict:
    """Add a new price alert.

    Args:
        ticker: Stock symbol.
        target: Price level to trigger at.
        direction: 'above' or 'below'.
        grade: 'A+'|'A'|'B'|'stop'|'target'|'prep'|'watch'|'earnings-drift'|'A-short' etc.
        note: Free-text description shown when alert fires.
    """
    direction = direction.strip().lower()
    if direction not in ("above", "below"):
        return {"success": False, "error": "direction must be 'above' or 'below'"}

    sym = ticker.upper()
    target = float(target)

    # Sanity check: would this alert fire on the next monitor poll because
    # the target is already on the "trigger side" of the current price?
    # We fetch a live price here — cheap call, well worth the 200ms to avoid
    # premature-fire surprises (e.g. staging "above $98" when stock already at $98.18).
    warning = None
    current_price = None
    try:
        from rh_mcp.lib.rh_client import client
        rh = client()
        latest = rh.get_latest_price(sym)
        if latest and latest[0]:
            current_price = float(latest[0])
            if direction == "above" and current_price >= target:
                warning = (
                    f"target ${target:.2f} is at or below current price ${current_price:.2f} "
                    f"— alert will fire immediately on next monitor poll"
                )
            elif direction == "below" and current_price <= target:
                warning = (
                    f"target ${target:.2f} is at or above current price ${current_price:.2f} "
                    f"— alert will fire immediately on next monitor poll"
                )
    except Exception:
        pass  # price-check is advisory; don't block staging on its failure

    alerts = _read_alerts()
    new_alert = {
        "ticker": sym,
        "target": target,
        "direction": direction,
        "active": bool(active),
        "fired": False,
        "grade": grade,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    alerts.append(new_alert)
    _write_alerts(alerts)

    result = {"success": True, "added": new_alert, "total_alerts": len(alerts)}
    if current_price is not None:
        result["current_price"] = current_price
    if warning:
        result["warning"] = warning
    return result


def deactivate_alert(ticker: str, target: float | None = None, grade: str | None = None) -> dict:
    """Deactivate matching alerts (sets active=False, doesn't delete).

    Args:
        ticker: Stock symbol (required).
        target: Match alerts at this exact price (optional — if omitted, matches all for ticker).
        grade: Further filter by grade (optional).
    """
    sym = ticker.upper()
    alerts = _read_alerts()
    matched = []
    for a in alerts:
        if (a.get("ticker") or "").upper() != sym:
            continue
        if target is not None and abs(float(a.get("target", 0)) - float(target)) > 0.001:
            continue
        if grade is not None and a.get("grade") != grade:
            continue
        if not a.get("active", True):
            continue  # already inactive
        a["active"] = False
        a["note"] = (a.get("note", "") + f" | DEACTIVATED {datetime.now().strftime('%Y-%m-%d %H:%M')}").strip(" |")
        matched.append({"ticker": sym, "target": a["target"], "grade": a.get("grade"), "direction": a.get("direction")})
    if matched:
        _write_alerts(alerts)
    return {"success": True, "deactivated_count": len(matched), "deactivated": matched}


def delete_alert(ticker: str, target: float, direction: str) -> dict:
    """Hard delete a specific alert by ticker+target+direction."""
    sym = ticker.upper()
    direction = direction.lower()
    alerts = _read_alerts()
    before = len(alerts)
    alerts = [
        a for a in alerts
        if not (
            (a.get("ticker") or "").upper() == sym
            and abs(float(a.get("target", 0)) - float(target)) < 0.001
            and (a.get("direction") or "").lower() == direction
        )
    ]
    removed = before - len(alerts)
    if removed:
        _write_alerts(alerts)
    return {"success": True, "removed_count": removed}


def auto_stage_trade_card(
    ticker: str,
    entry: float,
    stop: float,
    target: float | None = None,
    shares: float | None = None,
    direction: str = "long",
    grade: str = "A",
    thesis: str = "",
) -> dict:
    """Atomically write the full trade-card alerts for a new entry.

    For a LONG, creates:
    - STOP at `stop` (direction=below)
    - BE alert at `entry` (direction=below, fires if price drops back to entry — for monitoring breakeven trail)
    - +1R alert (direction=above, R = entry - stop)
    - +2R alert (direction=above)
    - TARGET at `target` (direction=above, only if provided)

    For a SHORT, mirrors the directions.

    This closes the gap where 2R triggers (like PANW $211.76 today) lived only in memory
    and never made it into the monitor's watch list.
    """
    direction = direction.lower()
    if direction not in ("long", "short"):
        return {"success": False, "error": "direction must be 'long' or 'short'"}
    if (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
        return {"success": False, "error": f"invalid stop {stop} for {direction} entry {entry}"}

    sym = ticker.upper()
    r = abs(entry - stop)
    one_r = entry + r if direction == "long" else entry - r
    two_r = entry + 2 * r if direction == "long" else entry - 2 * r

    sh_str = f"{shares:.4f} sh" if shares is not None else "position"
    base_note = f"{direction.upper()} {sh_str} @ ${entry:.2f}, stop ${stop:.2f}, R=${r:.2f}"
    if thesis:
        base_note += f". Thesis: {thesis[:120]}"

    stage = []

    # STOP
    stage.append({
        "ticker": sym,
        "target": float(stop),
        "direction": "below" if direction == "long" else "above",
        "active": True,
        "fired": False,
        "grade": "stop",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": f"STOP - {base_note}. Close immediately on fire.",
    })

    # +1R signal (move stop to BE)
    stage.append({
        "ticker": sym,
        "target": float(one_r),
        "direction": "above" if direction == "long" else "below",
        "active": True,
        "fired": False,
        "grade": "1R",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": f"+1R reached - move stop to BE ${entry:.2f}. {base_note}",
    })

    # +2R signal (partial exit + trail)
    stage.append({
        "ticker": sym,
        "target": float(two_r),
        "direction": "above" if direction == "long" else "below",
        "active": True,
        "fired": False,
        "grade": "2R",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": f"+2R reached - order_book check, 50% partial if no wall, trail 50% of profit. {base_note}",
    })

    # TARGET (optional)
    if target is not None:
        stage.append({
            "ticker": sym,
            "target": float(target),
            "direction": "above" if direction == "long" else "below",
            "active": True,
            "fired": False,
            "grade": "target",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "note": f"TARGET - close remaining position. {base_note}",
        })

    alerts = _read_alerts()
    alerts.extend(stage)
    _write_alerts(alerts)
    return {
        "success": True,
        "ticker": sym,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "R": round(r, 4),
        "one_R": round(one_r, 4),
        "two_R": round(two_r, 4),
        "target": target,
        "alerts_added": len(stage),
        "stage": stage,
    }
