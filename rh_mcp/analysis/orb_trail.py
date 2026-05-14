"""ORB trailing-stop manager.

Given an open MNQ position (any source — not just ORB triggers), compute the
current 25-point trailing-stop level based on max favorable excursion (MFE)
since entry. Caller updates their broker-side stop to the recommended level.

The trail is the SAME math as in the ORB backtest (validated PF 1.93):
  Initial stop floor: entry +/- 50pt (depending on direction)
  Trail level:        MFE -/+ 25pt
  Use whichever stop is TIGHTER (closer to current price)
  Never widen — only move toward profit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))


INIT_STOP_POINTS = 50.0
TRAIL_POINTS = 25.0
POINT_VALUE_DOLLARS = 2.0


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _today_at(hh: int, mm: int) -> datetime:
    return _now_et().replace(hour=hh, minute=mm, second=0, microsecond=0)


def _find_mnq_position() -> Optional[dict]:
    """Return the open MNQ position (any month) from rh-mcp, normalized.

    Returns: {direction, entry_price, quantity, contract_code, raw}, or None.
    """
    try:
        from rh_mcp.analysis import futures_client as fc
        positions = fc.get_positions() or []
    except Exception:
        return None

    for p in positions:
        # Locate the MNQ ticker — schema may use different field names; check common ones.
        sym = (p.get("contract") or p.get("contract_code") or p.get("symbol")
               or p.get("ticker") or "").upper()
        if "MNQ" not in sym:
            continue
        qty_raw = p.get("quantity") or p.get("quantity_long") or p.get("quantity_short") or "0"
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        # Direction: explicit field or sign of quantity
        direction = (p.get("direction") or p.get("side") or "").upper()
        if direction not in ("LONG", "SHORT"):
            direction = "LONG" if qty > 0 else "SHORT"
        avg_price_raw = (p.get("average_price") or p.get("average_open_price")
                         or p.get("avg_price") or p.get("entry_price") or 0)
        try:
            avg_price = float(avg_price_raw)
        except (TypeError, ValueError):
            avg_price = 0.0
        return {
            "direction": direction,
            "entry_price": avg_price,
            "quantity": abs(qty),
            "contract_code": sym,
            "raw": p,
        }
    return None


def _front_month_ticker(now: datetime) -> str:
    """MNQ front-month code for given date. Quarterly roll ~14th of expiry month."""
    d = now.astimezone(ET)
    y = d.year % 10
    m, day = d.month, d.day
    if (m, day) < (3, 14):  return f"MNQH{y}"
    if (m, day) < (6, 14):  return f"MNQM{y}"
    if (m, day) < (9, 14):  return f"MNQU{y}"
    if (m, day) < (12, 14): return f"MNQZ{y}"
    return f"MNQH{(y + 1) % 10}"


def _pull_mnq_bars_since(entry_dt_et: datetime) -> list[dict] | None:
    """Pull 15m MNQ bars from massive.com covering entry to now."""
    try:
        key = open(os.path.expanduser("~/.marketdata_api_key")).read().strip()
    except Exception:
        return None
    ticker = _front_month_ticker(datetime.now(tz=ET))
    today = datetime.now(tz=ET).date()
    gte = entry_dt_et.date().isoformat()
    lte = today.isoformat()
    url = (f"https://api.massive.com/futures/v1/aggs/{ticker}"
           f"?resolution=15min&window_start.gte={gte}&window_start.lte={lte}&limit=5000")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    if d.get("status") != "OK":
        return None
    bars = []
    for b in d.get("results", []) or []:
        ns = b["window_start"]
        dt_et = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(ET)
        if dt_et < entry_dt_et:
            continue
        bars.append({
            "dt_et": dt_et,
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        })
    bars.sort(key=lambda b: b["dt_et"])
    return bars or None


def analyze(
    entry_time_et: str | None = None,
    current_stop: float | None = None,
) -> dict:
    """Compute current trailing-stop level for the open MNQ position.

    Args:
      entry_time_et: ISO timestamp of entry. If None, defaults to today's 09:45 ET
        (standard ORB entry time — works for typical use case).
      current_stop: If provided, compares to recommended level and reports the
        delta (in points + dollars).

    Returns dict with:
      state: 'no_position' | 'trail_below_init_floor' | 'trail_active' | 'data_error'
      recommended_stop: float
      stop_action: 'tighten' | 'no_change' (vs current_stop)
      action: human-readable instruction
    """
    pos = _find_mnq_position()
    if pos is None:
        return {
            "success": True,
            "state": "no_position",
            "as_of_et": _now_et().isoformat(timespec="seconds"),
            "reason": "no open MNQ position found",
        }

    direction = pos["direction"]
    entry = pos["entry_price"]
    qty = pos["quantity"]

    if entry_time_et:
        try:
            entry_dt = datetime.fromisoformat(entry_time_et)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=ET)
            entry_dt = entry_dt.astimezone(ET)
        except ValueError:
            return {
                "success": False,
                "state": "data_error",
                "reason": f"could not parse entry_time_et={entry_time_et!r}",
            }
    else:
        entry_dt = _today_at(9, 45)

    bars = _pull_mnq_bars_since(entry_dt)
    if not bars:
        return {
            "success": False,
            "state": "data_error",
            "reason": "could not fetch MNQ 15m bars",
        }

    # Initial stop (never widen below this — it's the hard cap)
    if direction == "LONG":
        init_stop = entry - INIT_STOP_POINTS
        # MFE = highest high since entry
        mfe = max(b["high"] for b in bars)
        trail_level = mfe - TRAIL_POINTS
        recommended_stop = max(init_stop, trail_level)
        current_price = bars[-1]["close"]
        unreal_pnl_pts = current_price - entry
    else:  # SHORT
        init_stop = entry + INIT_STOP_POINTS
        mfe = min(b["low"] for b in bars)
        trail_level = mfe + TRAIL_POINTS
        recommended_stop = min(init_stop, trail_level)
        current_price = bars[-1]["close"]
        unreal_pnl_pts = entry - current_price

    unreal_pnl_dollars = unreal_pnl_pts * POINT_VALUE_DOLLARS * qty
    favorable_excursion_pts = abs(mfe - entry)

    # Decide if trail is currently above the init-stop floor
    if direction == "LONG":
        trail_active = trail_level > init_stop
    else:
        trail_active = trail_level < init_stop

    # Compare to caller's current stop if provided
    stop_action = None
    delta_pts = None
    if current_stop is not None:
        if direction == "LONG":
            # For long, "tighter" means stop is HIGHER (closer to current price)
            delta_pts = recommended_stop - current_stop
            stop_action = "tighten" if delta_pts > 0.25 else "no_change"
        else:
            # For short, "tighter" means stop is LOWER
            delta_pts = current_stop - recommended_stop
            stop_action = "tighten" if delta_pts > 0.25 else "no_change"

    result = {
        "success": True,
        "state": "trail_active" if trail_active else "trail_below_init_floor",
        "as_of_et": _now_et().isoformat(timespec="seconds"),
        "position": {
            "contract": pos["contract_code"],
            "direction": direction,
            "entry_price": round(entry, 2),
            "quantity": qty,
        },
        "current_price": round(current_price, 2),
        "mfe": round(mfe, 2),
        "favorable_excursion_pts": round(favorable_excursion_pts, 2),
        "unrealized_pnl_pts": round(unreal_pnl_pts, 2),
        "unrealized_pnl_dollars": round(unreal_pnl_dollars, 2),
        "init_stop_floor": round(init_stop, 2),
        "trail_level": round(trail_level, 2),
        "recommended_stop": round(recommended_stop, 2),
        "bars_since_entry": len(bars),
    }

    if current_stop is not None:
        result["current_stop"] = current_stop
        result["stop_delta_pts"] = round(delta_pts, 2)
        result["stop_action"] = stop_action
        if stop_action == "tighten":
            if direction == "LONG":
                result["action"] = (
                    f"UPDATE stop from {current_stop:.2f} to {recommended_stop:.2f} "
                    f"(+{delta_pts:.1f}pts tighter, lock in ${delta_pts * POINT_VALUE_DOLLARS * qty:.0f})"
                )
            else:
                result["action"] = (
                    f"UPDATE stop from {current_stop:.2f} to {recommended_stop:.2f} "
                    f"(−{delta_pts:.1f}pts tighter, lock in ${delta_pts * POINT_VALUE_DOLLARS * qty:.0f})"
                )
        else:
            result["action"] = f"HOLD stop at {current_stop:.2f} — trail has not moved"
    else:
        result["action"] = (
            f"Set/maintain stop at {recommended_stop:.2f} "
            f"({'trail engaged from MFE' if trail_active else 'initial 50pt floor still binding'})"
        )

    return result
