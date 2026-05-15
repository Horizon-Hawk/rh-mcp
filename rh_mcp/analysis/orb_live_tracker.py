"""Live MNQ ORB tracker — RH-live polling (no yfinance, no massive.com lag).

Polls get_quote_by_ticker every POLL_SECS during the OR formation window and
the breakout window, builds running 15-min OHLC bars from the tick stream,
locks OR_high/OR_low at exact 9:45:00 ET, and detects breakouts on each bar
close. Writes state to ~/orb_live_state.json after every tick so callers
(e.g., scan_orb_mnq) can read it.

Run as a daemon during market hours:
    python -m rh_mcp.analysis.orb_live_tracker

Or call run_session() / poll_once() from another script if you want to control
timing yourself.

Design choices:
  - Single state JSON file at $HOME/orb_live_state.json (override with
    ORB_LIVE_STATE_PATH env var). Atomic write (tmp + rename) so readers
    never see torn state.
  - 5s poll interval — RH futures quote endpoint handles this comfortably.
  - Tick-based bar construction: open = first tick of the 15-min bucket,
    high/low = running max/min, close = last tick. Close finalized when
    bucket boundary crosses (e.g., 10:00:00 ET locks the 9:45-10:00 bar).
  - Breakout detection on bar finalization, not on tick — matches the
    backtest spec which uses bar-CLOSE-beyond-OR.
  - Entry price = open of the NEXT bar after breakout (matches Phase 3.6).

Schema of ~/orb_live_state.json:
{
  "date": "2026-05-14",
  "root": "MNQ",
  "or_window": {"start_hhmm": [9, 30], "end_hhmm": [9, 45]},
  "or_bar":   {"open": 29475.0, "high": 29658.5, "low": 29462.5, "close": 29658.5,
               "complete": true, "tick_count": 180},
  "or_high":  29658.5,
  "or_low":   29462.5,
  "or_locked": true,
  "bars": [
    {"start_hhmm": [9, 45], "end_hhmm": [10, 0], "open": ..., "high": ..., ...},
    ...
  ],
  "current_bar": {"start_hhmm": [10, 30], "end_hhmm": [10, 45], "open": ...},
  "breakout":  null | {
    "direction": "long" | "short",
    "bar_close_price": 29672.0,
    "bar_close_time_et": "2026-05-14T10:45:00-04:00",
    "entry_price_expected": 29685.38,  # populated after next bar opens
    "init_stop": 29635.38,
    "init_stop_pts": 50.0,
    "trail_pts": 25.0,
  },
  "last_tick_ts_et": "2026-05-14T10:50:13-04:00",
  "last_tick_price": 29700.50,
  "session_state": "pre_or" | "or_forming" | "or_set" | "long_trigger" |
                   "short_trigger" | "window_closed_no_signal" | "complete",
}
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))

from rh_mcp import auth
from rh_mcp.analysis import futures_client


# -----------------------------------------------------------------------------
# Config — keep aligned with orb_mnq.py
# -----------------------------------------------------------------------------

POLL_SECS = 5
OR_START = (9, 30)
OR_END = (9, 45)
BREAKOUT_WINDOW_END = (11, 0)
SESSION_END = (16, 0)
INIT_STOP_PTS = 50.0
TRAIL_PTS = 25.0

STATE_PATH = Path(os.environ.get(
    "ORB_LIVE_STATE_PATH",
    str(Path.home() / "orb_live_state.json"),
))


# -----------------------------------------------------------------------------
# Bar math
# -----------------------------------------------------------------------------

def _now_et() -> datetime:
    return datetime.now(tz=ET)


# -----------------------------------------------------------------------------
# Overnight S/R levels — one-shot historical pull at startup
# -----------------------------------------------------------------------------

def _front_month_symbol(root: str, ref: datetime) -> str:
    """MNQ front-month symbol for massive.com API. Codes: H=Mar, M=Jun,
    U=Sep, Z=Dec. Returns single-digit-year format (e.g., MNQM6 for Jun 2026)."""
    yr = ref.year % 10
    mo = ref.month
    if mo <= 3:
        return f"{root}H{yr}"
    if mo <= 6:
        return f"{root}M{yr}"
    if mo <= 9:
        return f"{root}U{yr}"
    return f"{root}Z{yr}"


def fetch_overnight_levels(root: str = "MNQ") -> dict:
    """One-shot pull of recent MNQ bars from massive.com → overnight high/low.

    Window = prior RTH close (yesterday 16:00 ET; Friday 16:00 ET if Monday)
    through current time. Filters bars within window and returns
    {"on_high": float, "on_low": float, "bars_used": int} on success.
    Returns {"on_high": None, "on_low": None, "error": str} on any failure
    so the tracker can still run with PM-only S/R.

    Requires ~/.marketdata_api_key. Uses massive.com (same source as backtest
    data — known stable, 5/min rate limit, ~1s response).
    """
    import urllib.request
    import urllib.error

    key_path = Path.home() / ".marketdata_api_key"
    if not key_path.exists():
        return {"on_high": None, "on_low": None,
                "error": f"no API key at {key_path}"}
    try:
        key = key_path.read_text().strip()
    except Exception as e:
        return {"on_high": None, "on_low": None, "error": f"key read: {e}"}

    now = _now_et()
    symbol = _front_month_symbol(root.upper(), now)

    # Pull last 72h to comfortably cover Friday→Monday gap on weekends
    gte = (now - timedelta(hours=72)).strftime("%Y-%m-%d")
    lte = now.strftime("%Y-%m-%d")
    url = (f"https://api.massive.com/futures/v1/aggs/{symbol}"
           f"?resolution=15min"
           f"&window_start.gte={gte}&window_start.lte={lte}&limit=400")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}",
                      "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"on_high": None, "on_low": None, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"on_high": None, "on_low": None, "error": str(e)}

    bars = data.get("results") or []
    if not bars:
        return {"on_high": None, "on_low": None, "error": "no bars returned"}

    # Overnight window: prior RTH close → today's OR start (09:30 ET)
    today_or_start = now.replace(hour=OR_START[0], minute=OR_START[1],
                                  second=0, microsecond=0)
    # Friday close if today is Monday, else yesterday's close
    if now.weekday() == 0:
        prior_close = (now - timedelta(days=3)).replace(
            hour=16, minute=0, second=0, microsecond=0)
    else:
        prior_close = (now - timedelta(days=1)).replace(
            hour=16, minute=0, second=0, microsecond=0)

    on_high = None
    on_low = None
    bars_used = 0
    for b in bars:
        ts_ns = b.get("window_start") or 0
        ts = datetime.fromtimestamp(ts_ns / 1e9,
                                     tz=timezone.utc).astimezone(ET)
        if not (prior_close <= ts < today_or_start):
            continue
        bars_used += 1
        h = b.get("high")
        l = b.get("low")
        if h is not None:
            on_high = h if on_high is None else max(on_high, h)
        if l is not None:
            on_low = l if on_low is None else min(on_low, l)

    if bars_used == 0:
        return {"on_high": None, "on_low": None,
                "error": f"no bars in window {prior_close} → {today_or_start}"}
    return {"on_high": on_high, "on_low": on_low, "bars_used": bars_used,
            "symbol": symbol,
            "window_start_et": prior_close.isoformat(timespec="seconds"),
            "window_end_et": today_or_start.isoformat(timespec="seconds")}


def _bar_bucket(now: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    """Which 15-min bar bucket does `now` fall in? Returns (start_hhmm, end_hhmm)."""
    minute_of_day = now.hour * 60 + now.minute
    bucket_idx = minute_of_day // 15
    start_min = bucket_idx * 15
    end_min = start_min + 15
    return ((start_min // 60, start_min % 60), (end_min // 60, end_min % 60))


def _is_or_bucket(start_hhmm: tuple[int, int]) -> bool:
    return start_hhmm == OR_START


def _is_breakout_window(start_hhmm: tuple[int, int]) -> bool:
    """09:45 through 10:45 inclusive — the 5 bars where a breakout-close is valid."""
    h, m = start_hhmm
    minute_of_day = h * 60 + m
    or_end_min = OR_END[0] * 60 + OR_END[1]                  # 585
    breakout_end_min = BREAKOUT_WINDOW_END[0] * 60 + BREAKOUT_WINDOW_END[1]  # 660
    # Valid breakout bars are bars whose START is in [09:45, 10:45]
    # (a bar STARTING at 10:45 will CLOSE at 11:00 = BREAKOUT_WINDOW_END)
    return or_end_min <= minute_of_day <= breakout_end_min - 15


# -----------------------------------------------------------------------------
# State file I/O
# -----------------------------------------------------------------------------

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomic write via tmp + rename so readers never see torn JSON."""
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_PATH)


def _new_state(now: datetime, root: str) -> dict:
    return {
        "date": now.astimezone(ET).date().isoformat(),
        "root": root,
        "or_window": {"start_hhmm": list(OR_START), "end_hhmm": list(OR_END)},
        "or_bar": None,
        "or_high": None,
        "or_low": None,
        "or_locked": False,
        "bars": [],
        "current_bar": None,
        "breakout": None,
        "last_tick_ts_et": now.astimezone(ET).isoformat(timespec="seconds"),
        "last_tick_price": None,
        "session_state": "pre_or",
        # PM/overnight high/low — running min/max of ticks before OR_START.
        # Used by scan_orb_mnq to gate triggers when PM S/R sits near OR boundary.
        # pm_high/pm_low: same-day premarket (04:00 → 09:30 ET) running min/max,
        #                 populated tick-by-tick by process_tick().
        # on_high/on_low: overnight session (prior 16:00 ET → today 09:30 ET),
        #                 populated by a single fetch_overnight_levels() call at
        #                 session startup. Required for full overnight S/R
        #                 coverage since tick polling can't reach back in time.
        "pm_high": None,
        "pm_low": None,
        "on_high": None,
        "on_low": None,
        "on_fetch_meta": None,
    }


# -----------------------------------------------------------------------------
# Tick handling
# -----------------------------------------------------------------------------

def _new_bar(start_hhmm, end_hhmm, price: float) -> dict:
    return {
        "start_hhmm": list(start_hhmm),
        "end_hhmm": list(end_hhmm),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "tick_count": 1,
        "complete": False,
    }


def _update_bar(bar: dict, price: float) -> None:
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["close"] = price
    bar["tick_count"] += 1


def _finalize_bar(bar: dict, state: dict) -> None:
    """Lock a bar, check OR or breakout, update state."""
    bar["complete"] = True
    start = tuple(bar["start_hhmm"])

    if _is_or_bucket(start):
        # OR bar just closed at 09:45 ET — lock OR_high / OR_low
        state["or_bar"] = bar
        state["or_high"] = bar["high"]
        state["or_low"] = bar["low"]
        state["or_locked"] = True
        if state["session_state"] in ("or_forming", "pre_or"):
            state["session_state"] = "or_set"
        return

    # Otherwise it's a breakout-window bar — check for trigger
    state["bars"].append(bar)
    if state["breakout"] is not None:
        return  # already triggered earlier, just record the bar
    if not state.get("or_locked"):
        return
    or_high = state["or_high"]
    or_low = state["or_low"]
    if bar["close"] > or_high:
        state["breakout"] = {
            "direction": "long",
            "bar_close_price": bar["close"],
            "bar_close_time_et": _bar_close_iso(bar),
            "entry_price_expected": None,  # filled on next tick (open of next bar)
            "init_stop": None,             # filled when entry known
            "init_stop_pts": INIT_STOP_PTS,
            "trail_pts": TRAIL_PTS,
        }
        state["session_state"] = "long_trigger"
    elif bar["close"] < or_low:
        state["breakout"] = {
            "direction": "short",
            "bar_close_price": bar["close"],
            "bar_close_time_et": _bar_close_iso(bar),
            "entry_price_expected": None,
            "init_stop": None,
            "init_stop_pts": INIT_STOP_PTS,
            "trail_pts": TRAIL_PTS,
        }
        state["session_state"] = "short_trigger"


def _bar_close_iso(bar: dict) -> str:
    """ISO timestamp for when this bar closed (today + bar's end_hhmm)."""
    now = _now_et()
    h, m = bar["end_hhmm"]
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return dt.isoformat(timespec="seconds")


def process_tick(state: dict, now: datetime, price: float) -> dict:
    """Apply one tick to the state. Returns the updated state."""
    bucket_start, bucket_end = _bar_bucket(now)
    cur = state.get("current_bar")

    # Bar bucket changed → finalize the prior bar and start a fresh one
    if cur is not None:
        prior_start = tuple(cur["start_hhmm"])
        if prior_start != bucket_start:
            _finalize_bar(cur, state)
            # If a breakout just fired, the open of THIS new bar IS the entry price
            bk = state.get("breakout")
            if bk and bk.get("entry_price_expected") is None:
                bk["entry_price_expected"] = price
                if bk["direction"] == "long":
                    bk["init_stop"] = price - INIT_STOP_PTS
                else:
                    bk["init_stop"] = price + INIT_STOP_PTS
            cur = None

    # No current bar (just rolled or first tick) → open a new one
    if cur is None:
        cur = _new_bar(bucket_start, bucket_end, price)
        state["current_bar"] = cur
        # Set session_state for the new bar
        if _is_or_bucket(bucket_start):
            state["session_state"] = "or_forming"
        elif _is_breakout_window(bucket_start):
            if state.get("or_locked") and state.get("breakout") is None:
                if state.get("session_state") not in ("long_trigger", "short_trigger"):
                    state["session_state"] = "or_set"
    else:
        _update_bar(cur, price)

    # Window-closed detection: after BREAKOUT_WINDOW_END with no trigger
    breakout_end_dt = now.replace(
        hour=BREAKOUT_WINDOW_END[0], minute=BREAKOUT_WINDOW_END[1],
        second=0, microsecond=0,
    )
    if now >= breakout_end_dt and state.get("breakout") is None and state.get("or_locked"):
        state["session_state"] = "window_closed_no_signal"

    # Premarket high/low — track every tick before OR_START (09:30 ET) as PM S/R.
    # Once OR is locked, pm_high/pm_low are frozen (no longer updated).
    if not state.get("or_locked"):
        if state.get("pm_high") is None or price > state["pm_high"]:
            state["pm_high"] = price
        if state.get("pm_low") is None or price < state["pm_low"]:
            state["pm_low"] = price

    # Update last-tick markers
    state["last_tick_ts_et"] = now.isoformat(timespec="seconds")
    state["last_tick_price"] = price

    return state


# -----------------------------------------------------------------------------
# Polling loop
# -----------------------------------------------------------------------------

def poll_once(state: dict | None, root: str = "MNQ") -> dict:
    """Pull one RH-live tick, apply to state, return updated state.
    Initializes state on first call (when state is None or for a new date).
    """
    now = _now_et()
    if state is None or state.get("date") != now.astimezone(ET).date().isoformat():
        state = _new_state(now, root)

    # Ensure auth is set up — installs bearer override on shared SESSION.
    # Idempotent: subsequent calls are no-ops after first login().
    try:
        auth.login()
    except Exception as e:
        state["last_error"] = f"auth.login failed: {e}"
        _save_state(state)
        return state

    try:
        quote = futures_client.get_quote_by_ticker(root)
    except Exception as e:
        state["last_error"] = f"quote fetch failed: {e}"
        _save_state(state)
        return state

    if not quote:
        state["last_error"] = "quote returned None"
        _save_state(state)
        return state

    price = quote.get("last") or quote.get("ask") or quote.get("bid")
    if price is None:
        state["last_error"] = "quote missing last/ask/bid"
        _save_state(state)
        return state

    state.pop("last_error", None)
    state = process_tick(state, now, float(price))
    _save_state(state)
    return state


def run_session(root: str = "MNQ",
                start_hhmm: tuple[int, int] = (9, 25),
                end_hhmm: tuple[int, int] = (11, 5),
                poll_secs: int = POLL_SECS) -> None:
    """Run the live tracker as a foreground daemon during market hours.

    Sleeps until `start_hhmm`, polls every `poll_secs` until `end_hhmm`,
    then exits. State is persisted to STATE_PATH after every tick.
    """
    now = _now_et()
    today_start = now.replace(hour=start_hhmm[0], minute=start_hhmm[1],
                              second=0, microsecond=0)
    today_end = now.replace(hour=end_hhmm[0], minute=end_hhmm[1],
                            second=0, microsecond=0)

    if now < today_start:
        wait_s = (today_start - now).total_seconds()
        print(f"[orb_live_tracker] sleeping {wait_s:.0f}s until {start_hhmm[0]:02d}:{start_hhmm[1]:02d} ET",
              file=sys.stderr)
        time.sleep(wait_s)

    state = _load_state()
    if not state or state.get("date") != _now_et().astimezone(ET).date().isoformat():
        state = _new_state(_now_et(), root)
        _save_state(state)

    # One-shot overnight levels pull — populates on_high/on_low for scan_orb_mnq's
    # S/R confirmation gate. Tick polling alone can't capture pre-04:00 data
    # (overnight Globex + AH from prior session).
    if state.get("on_high") is None:
        try:
            on_levels = fetch_overnight_levels(root=root)
            state["on_high"] = on_levels.get("on_high")
            state["on_low"] = on_levels.get("on_low")
            state["on_fetch_meta"] = {
                k: v for k, v in on_levels.items()
                if k not in ("on_high", "on_low")
            }
            _save_state(state)
            if on_levels.get("on_high") is not None:
                print(f"[orb_live_tracker] overnight S/R: high={on_levels['on_high']} "
                      f"low={on_levels['on_low']} (bars_used={on_levels.get('bars_used')})",
                      file=sys.stderr)
            else:
                print(f"[orb_live_tracker] overnight fetch skipped: "
                      f"{on_levels.get('error')}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[orb_live_tracker] overnight fetch error: {e} (continuing)",
                  file=sys.stderr)

    print(f"[orb_live_tracker] starting poll loop ({root}, {poll_secs}s interval)",
          file=sys.stderr)

    while _now_et() < today_end:
        state = poll_once(state, root=root)
        time.sleep(poll_secs)

    # Mark session complete
    state["session_state"] = state.get("session_state") or "complete"
    if state.get("session_state") not in ("long_trigger", "short_trigger",
                                          "window_closed_no_signal"):
        state["session_state"] = "complete"
    _save_state(state)
    print(f"[orb_live_tracker] session ended — state={state['session_state']}",
          file=sys.stderr)


def main():
    """CLI entry: `python -m rh_mcp.analysis.orb_live_tracker [--root MNQ] [--once]`"""
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default="MNQ", help="Futures root (default MNQ)")
    p.add_argument("--once", action="store_true",
                   help="Single poll then exit (for testing)")
    p.add_argument("--start", default="4:00",
                   help="Start time HH:MM ET (default 4:00 — captures full premarket "
                        "for pm_high/pm_low used by scan_orb_mnq's S/R gate)")
    p.add_argument("--end", default="11:05",
                   help="End time HH:MM ET (default 11:05)")
    args = p.parse_args()

    if args.once:
        state = _load_state()
        new_state = poll_once(state, root=args.root)
        print(json.dumps(new_state, indent=2, default=str))
        return

    sh, sm = (int(x) for x in args.start.split(":"))
    eh, em = (int(x) for x in args.end.split(":"))
    run_session(root=args.root, start_hhmm=(sh, sm), end_hhmm=(eh, em))


if __name__ == "__main__":
    main()
