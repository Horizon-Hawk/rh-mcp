"""MNQ Opening Range Breakout scanner — Phase 3.6 strategy.

Status-on-demand: call any time during the trading day to get current state of
today's ORB setup. Does NOT auto-fire orders. User reviews signal and decides.

Validated on 2y of real MNQ 15-min bars (505 sessions, 345 trades):
  - PF 1.93 with 25pt trailing stop
  - $5,500/yr expected at 1 contract (base)
  - Max DD $558 over 2y
  - Profitable in 2024, 2025 (chop), 2026 YTD

Strategy spec:
  ENTRY:
    OR = 09:30-09:45 ET 15-min bar high/low
    LONG  trigger: 09:45-11:00 ET 15m bar CLOSES above OR_high  → enter next bar open
    SHORT trigger: 09:45-11:00 ET 15m bar CLOSES below OR_low   → enter next bar open
  FILTER:
    Skip if prior-day VIX close is in [18.00, 22.00] dead zone
    Skip if any major macro print within 60min of OR (user-checked)
  EXIT (whichever first):
    Initial stop: 50pt from entry ($100 risk, hard cap)
    Trail stop:   25pt from MFE (engages immediately, ratchets toward profit)
    Time exit:    16:00 ET market-on-close
  SIZE: 1 base contract; +1 if prior-day VIX > 28 (crisis regime stack)

States returned:
  pre_or:           before 09:30 ET on a trading day, or OR bar not yet posted
  or_forming:       09:30-09:45 ET, OR bar still building
  or_set:           09:45-11:00 ET, OR locked, watching for breakout
  long_trigger:     LONG breakout confirmed, signal active
  short_trigger:    SHORT breakout confirmed, signal active
  window_closed_no_signal: past 11:00 ET, no breakout fired today
  in_position:      a trigger fired earlier today (user has open position assumed)
  skipped_vix_deadzone: prior-day VIX 18-22 — strategy skipped today
  skipped_macro:    user marked macro skip
  off_session:      weekend or pre-09:30 ET on trading day
"""
from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))  # rough fallback

# VIX only changes once a day at 16:00 ET — cache per ET date
_VIX_CACHE: dict[str, tuple[Optional[float], Optional[str]]] = {}


VIX_DEAD_LOW = 18.0
VIX_DEAD_HIGH = 22.0
VIX_CRISIS = 28.0
INIT_STOP_POINTS = 50.0    # legacy MNQ default — see ORB_CONFIG for per-root values
TRAIL_POINTS = 25.0
POINT_VALUE_DOLLARS = 2.0  # legacy MNQ default

OR_START_HHMM = (9, 30)
OR_END_HHMM = (9, 45)
BREAKOUT_WINDOW_END_HHMM = (11, 0)
SESSION_END_HHMM = (16, 0)

# Per-instrument ORB config. Stop/trail points are sized for similar dollar
# risk across the cohort (~$100 on micros, ~$500-1000 on minis).
# regime_gate="vix": uses VIX dead-zone filter. Non-equity-index instruments
# would need a different vol index (OVX for oil, GVZ for gold) — not enabled
# until those gates are validated.
#
# 'validated': True only for MNQ (the 2y massive.com backtest). Others are
# BETA — running on the same pattern but stop/trail are educated guesses
# until task #19 backtest fills in real values.
ORB_CONFIG: dict[str, dict] = {
    "MNQ": {"point_value": 2.0,  "init_stop_pts": 50.0, "trail_pts": 25.0,
            "regime_gate": "vix", "validated": True,  "tier": "A"},
    "MES": {"point_value": 5.0,  "init_stop_pts": 20.0, "trail_pts": 10.0,
            "regime_gate": "vix", "validated": False, "tier": "BETA"},
    "NQ":  {"point_value": 20.0, "init_stop_pts": 50.0, "trail_pts": 25.0,
            "regime_gate": "vix", "validated": False, "tier": "BETA"},
    "ES":  {"point_value": 50.0, "init_stop_pts": 20.0, "trail_pts": 10.0,
            "regime_gate": "vix", "validated": False, "tier": "BETA"},
    "RTY": {"point_value": 50.0, "init_stop_pts": 10.0, "trail_pts":  5.0,
            "regime_gate": "vix", "validated": False, "tier": "BETA"},
    "M2K": {"point_value": 5.0,  "init_stop_pts": 10.0, "trail_pts":  5.0,
            "regime_gate": "vix", "validated": False, "tier": "BETA"},
}


def _cfg(root: str) -> dict:
    """Return ORB_CONFIG entry for root. Raises ValueError if root not supported."""
    cfg = ORB_CONFIG.get(root.upper())
    if cfg is None:
        raise ValueError(
            f"ORB not configured for root {root!r}. "
            f"Supported: {list(ORB_CONFIG)}"
        )
    return cfg


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _today_at(hh: int, mm: int, ref: datetime | None = None) -> datetime:
    base = (ref or _now_et()).astimezone(ET)
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _get_vix_prior_close(now: datetime) -> tuple[Optional[float], Optional[str]]:
    """Fetch prior-day VIX close from FRED (St. Louis Fed). No auth, no rate
    limit, sub-second. CBOE publishes VIX daily to FRED with ~1 day delay,
    which gives us exactly what we need (yesterday's close).

    Cached per ET date — VIX only changes once at 16:00 ET.
    """
    import urllib.request, csv, io
    from datetime import timedelta as td

    today_date = now.astimezone(ET).date()
    cache_key = today_date.isoformat()
    if cache_key in _VIX_CACHE:
        return _VIX_CACHE[cache_key]

    # Pull last 10 days of VIX (covers long weekends + holidays)
    cosd = (today_date - td(days=10)).isoformat()
    coed = today_date.isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd={cosd}&coed={coed}"

    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            body = r.read().decode()
    except Exception:
        return None, None

    last_close: float | None = None
    last_date_str: str | None = None
    reader = csv.DictReader(io.StringIO(body))
    for row in reader:
        try:
            d_str = row.get("observation_date") or row.get("DATE") or ""
            v_str = row.get("VIXCLS") or ""
            if not d_str or v_str in ("", "."):
                continue
            v = float(v_str)
            # Skip today's row if FRED happens to have it (we want PRIOR close)
            if d_str >= cache_key:
                continue
            last_close = v
            last_date_str = d_str
        except (ValueError, KeyError):
            continue

    if last_close is None:
        return None, None

    result = (last_close, last_date_str)
    _VIX_CACHE[cache_key] = result
    return result


def _front_month_ticker(now: datetime, root: str = "MNQ") -> str:
    """Front-month code for any quarterly-cycle root on given date.

    Cycle: H(Mar)/M(Jun)/U(Sep)/Z(Dec). Single-digit year (massive.com convention).
    Rolls ~14th of expiry month.
    """
    d = now.astimezone(ET)
    y = d.year % 10
    m, day = d.month, d.day
    root = root.upper()
    if (m, day) < (3, 14):
        return f"{root}H{y}"
    if (m, day) < (6, 14):
        return f"{root}M{y}"
    if (m, day) < (9, 14):
        return f"{root}U{y}"
    if (m, day) < (12, 14):
        return f"{root}Z{y}"
    return f"{root}H{(y + 1) % 10}"


def _get_mnq_intraday_massive(now: datetime, root: str = "MNQ") -> list[dict] | None:
    """Pull recent 15-min bars from massive.com for any CME equity-index root.
    <1s typically. Returns None on any failure.
    """
    import os, urllib.request, json
    try:
        key = open(os.path.expanduser("~/.marketdata_api_key")).read().strip()
    except Exception:
        return None
    ticker = _front_month_ticker(now, root=root)
    # Pull last ~3 trading days. lte=today+1 because massive.com interprets a
    # bare YYYY-MM-DD as start-of-day UTC — using today excludes today's bars.
    today = now.astimezone(ET).date()
    from datetime import timedelta as td
    gte = (today - td(days=4)).isoformat()
    lte = (today + td(days=1)).isoformat()
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
        bars.append({
            "dt_et": dt_et,
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": int(b.get("volume") or 0),
        })
    bars.sort(key=lambda b: b["dt_et"])
    return bars or None


def _get_mnq_intraday(now: datetime, root: str = "MNQ") -> list[dict] | None:
    """Pull recent 15-min bars from massive.com for the given root (typically <1s)."""
    return _get_mnq_intraday_massive(now, root=root)


def _bars_for_today(bars: list[dict], ref: datetime) -> list[dict]:
    today = ref.astimezone(ET).date()
    return [b for b in bars if b["dt_et"].date() == today]


def _find_or_bar(today_bars: list[dict]) -> dict | None:
    """The 15m bar starting at 09:30 ET."""
    for b in today_bars:
        if b["dt_et"].hour == OR_START_HHMM[0] and b["dt_et"].minute == OR_START_HHMM[1]:
            return b
    return None


def _find_breakout(today_bars: list[dict], or_high: float, or_low: float) -> dict | None:
    """Return the first 15m bar in 09:45-11:00 ET window that closed beyond OR.
    Returns dict with direction, breakout_bar_close, et_time, or None if none."""
    for b in today_bars:
        t = b["dt_et"]
        # Within breakout window (09:45 <= t < 11:00)?
        in_window = (
            (t.hour == 9 and t.minute >= 45)
            or (t.hour == 10)
            or (t.hour == 11 and t.minute == 0)
        )
        if not in_window:
            continue
        if b["close"] > or_high:
            return {"direction": "long", "breakout_bar": b}
        if b["close"] < or_low:
            return {"direction": "short", "breakout_bar": b}
    return None


def _next_bar_after(bars: list[dict], ref_bar: dict) -> dict | None:
    """The bar immediately after ref_bar in time order. None if ref_bar is the latest."""
    for i, b in enumerate(bars):
        if b["dt_et"] == ref_bar["dt_et"] and i + 1 < len(bars):
            return bars[i + 1]
    return None


def _breadth_warning(date_iso: str, direction: str) -> dict:
    """Attach mega-cap breadth from cache if available. Does NOT block to fetch
    (would burn ~96s on first call due to 5/min rate limit on massive.com).

    Caller can populate the cache by calling orb_breadth_check() separately —
    then subsequent scan_orb_mnq calls will include the warning automatically.
    """
    try:
        from rh_mcp.analysis import orb_breadth
        # Probe cache only — don't trigger a fetch
        cache_key = (date_iso, "breakout")
        cached = orb_breadth._CACHE.get(cache_key)
        if cached is None:
            return {
                "available": False,
                "reason": "not cached — call orb_breadth_check() to fetch (~96s, burns 8 API calls)",
            }
        consensus = cached.get("consensus_score", 0)
        if direction == "long":
            confirming = cached.get("n_up", 0)
        else:
            confirming = cached.get("n_down", 0)
        return {
            "available": True,
            "confirming_count": f"{confirming}/8",
            "consensus_score": round(consensus, 3),
            "warning_active": confirming >= 5,
            "interpretation": cached.get("interpretation"),
            "moves": cached.get("moves"),
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def _compute_size(prior_vix: float | None, root: str = "MNQ") -> dict:
    """Position size guidance based on VIX regime — per-instrument $ scaling."""
    cfg = _cfg(root)
    init_stop_pts = cfg["init_stop_pts"]
    point_value = cfg["point_value"]
    if prior_vix is not None and prior_vix > VIX_CRISIS:
        return {
            "contracts": 2,
            "rationale": f"VIX prior close {prior_vix:.1f} > {VIX_CRISIS} (crisis stack +1)",
            "max_risk_dollars": 2 * init_stop_pts * point_value,
        }
    return {
        "contracts": 1,
        "rationale": "base size (no VIX-crisis stack)",
        "max_risk_dollars": 1 * init_stop_pts * point_value,
    }


def _try_live_state(root: str, ref: datetime, stale_secs: int = 30) -> dict | None:
    """Read ~/orb_live_state.json and translate to scan_orb_mnq response format.

    Returns None if state file doesn't exist, is for a different date, is for
    a different root, or hasn't been ticked recently. Caller falls back to
    massive.com when this returns None.

    The live tracker (rh_mcp.analysis.orb_live_tracker) writes this file via
    RH-live polling — same data RH's app sees, no yfinance/massive.com lag.
    """
    import json as _json
    from rh_mcp.analysis.orb_live_tracker import STATE_PATH
    if not STATE_PATH.exists():
        return None
    try:
        live = _json.loads(STATE_PATH.read_text())
    except Exception:
        return None
    today_iso = ref.astimezone(ET).date().isoformat()
    if live.get("date") != today_iso:
        return None
    if (live.get("root") or "").upper() != root.upper():
        return None
    # Stale check: tracker should be writing every POLL_SECS (~5s)
    last_tick = live.get("last_tick_ts_et")
    if not last_tick:
        return None
    try:
        last_tick_dt = datetime.fromisoformat(last_tick).astimezone(ET)
    except Exception:
        return None
    if (ref - last_tick_dt).total_seconds() > stale_secs:
        return None
    return live


# Production-validated S/R confirmation rule (intraday-10pt + 25pt trail).
# Backtest result 2y MNQ 2026-05-15: PF 2.01 → 2.37 (+18%), $5,254 → $6,271
# (+19%), max DD unchanged $462. All 3 years improved; 2026 YTD +31%.
# When PM_high or PM_low is within SR_PROXIMITY_PTS of OR boundary in the
# breakout direction, require the NEXT 15m bar to ALSO close beyond OR
# before signaling entry (2-bar confirmation).
SR_PROXIMITY_PTS = 10.0


def _sr_levels_from_live(live: dict) -> list[float]:
    """PM/overnight S/R levels written by orb_live_tracker. Only populated when
    the tracker ran before 09:30 ET — otherwise returns empty list and the gate
    is a no-op (default to baseline behavior)."""
    out = []
    for k in ("pm_high", "pm_low", "on_high", "on_low"):
        v = live.get(k)
        if v is not None:
            out.append(v)
    return out


def _sr_near_breakout_side(or_high: float, or_low: float, direction: str,
                            sr_levels: list[float],
                            proximity_pts: float = SR_PROXIMITY_PTS) -> bool:
    """True if any S/R level sits within proximity of the OR boundary on the
    breakout side. Long breakout cares about OR_high, short about OR_low."""
    if not sr_levels:
        return False
    target = or_high if direction == "long" else or_low
    return any(abs(target - lvl) <= proximity_pts for lvl in sr_levels)


def _confirmation_bar_state(live: dict) -> dict:
    """Find the bar that should serve as the S/R confirmation candle.

    The confirmation bar = the bar immediately AFTER the breakout bar.
    Returns dict with: 'found' (bool), 'complete' (bool), 'close' (float|None),
    'rejected_or' (bool: True if confirm closed back INSIDE OR on the breakout
    side, signaling reclaim and skip)."""
    bk = live.get("breakout") or {}
    bo_time = bk.get("bar_close_time_et")
    if not bo_time:
        return {"found": False, "complete": False, "close": None, "rejected_or": False}
    # bars list contains breakout-window bars sorted by time of close
    bars = live.get("bars") or []
    # Breakout bar in bars list will have end_hhmm matching bo_time's HH:MM.
    # Find its index then look at next.
    try:
        bo_dt = datetime.fromisoformat(bo_time)
        bo_hhmm = (bo_dt.hour, bo_dt.minute)
    except Exception:
        return {"found": False, "complete": False, "close": None, "rejected_or": False}
    idx = None
    for i, b in enumerate(bars):
        if tuple(b.get("end_hhmm", [])) == bo_hhmm:
            idx = i
            break
    if idx is None or idx + 1 >= len(bars):
        # Confirmation bar not yet recorded — could be still building
        cur = live.get("current_bar") or {}
        return {
            "found": cur is not None and cur.get("complete") is False,
            "complete": False,
            "close": cur.get("close"),
            "rejected_or": False,
        }
    confirm_bar = bars[idx + 1]
    if not confirm_bar.get("complete"):
        return {"found": True, "complete": False, "close": confirm_bar.get("close"),
                "rejected_or": False}
    or_high = live.get("or_high")
    or_low = live.get("or_low")
    direction = (live.get("breakout") or {}).get("direction")
    close = confirm_bar.get("close")
    rejected = False
    if direction == "long" and or_high is not None and close is not None:
        rejected = close <= or_high
    elif direction == "short" and or_low is not None and close is not None:
        rejected = close >= or_low
    return {"found": True, "complete": True, "close": close, "rejected_or": rejected}


def _render_live_response(live: dict, ref: datetime, root: str, prior_vix: float,
                          vix_date: str | None) -> dict:
    """Translate live tracker state to the existing analyze() response shape.
    Adds 'source': 'rh_live' so callers can distinguish from massive.com path.
    """
    cfg = _cfg(root)
    init_stop_pts = cfg["init_stop_pts"]
    trail_pts = cfg["trail_pts"]
    point_value = cfg["point_value"]

    common = {
        "success": True,
        "root": root,
        "tier": cfg["tier"],
        "validated": cfg["validated"],
        "source": "rh_live",
        "as_of_et": ref.isoformat(timespec="seconds"),
        "prior_vix": round(prior_vix, 2),
        "vix_as_of": vix_date,
        "or_high": live.get("or_high"),
        "or_low": live.get("or_low"),
        "or_width_pts": round((live.get("or_high") or 0) - (live.get("or_low") or 0), 2)
                        if (live.get("or_high") and live.get("or_low")) else None,
        "current_price": live.get("last_tick_price"),
        "size_guidance": _compute_size(prior_vix, root=root),
    }
    state = live.get("session_state", "pre_or")

    if state in ("long_trigger", "short_trigger"):
        bk = live.get("breakout") or {}
        direction = bk.get("direction", state.split("_")[0])
        entry = bk.get("entry_price_expected")
        init_stop = bk.get("init_stop")
        breakout_close = bk.get("bar_close_price")
        breakout_time = bk.get("bar_close_time_et")

        # S/R confirmation gate (production-validated PF 2.01→2.37 on 2y MNQ).
        sr_levels = _sr_levels_from_live(live)
        sr_near = _sr_near_breakout_side(
            live.get("or_high"), live.get("or_low"), direction, sr_levels,
            proximity_pts=SR_PROXIMITY_PTS,
        )
        if sr_near and sr_levels:
            confirm = _confirmation_bar_state(live)
            sr_meta = {
                "sr_filter_applied": True,
                "sr_levels_checked": [round(x, 2) for x in sr_levels],
                "sr_proximity_pts": SR_PROXIMITY_PTS,
            }
            if not confirm["complete"]:
                # Awaiting next bar close — don't fire yet
                return {
                    **common,
                    "state": f"{direction}_trigger_awaiting_sr_confirmation",
                    "breakout_bar_et": breakout_time,
                    "breakout_bar_close": breakout_close,
                    "next_action": "S/R near OR boundary — waiting for next 15m bar "
                                  "to confirm direction. Will fire only if close also "
                                  "beyond OR; skip if reclaim.",
                    **sr_meta,
                }
            if confirm["rejected_or"]:
                # Confirmation failed — level reclaimed, skip entry per validated rule
                return {
                    **common,
                    "state": "skipped_sr_reclaim",
                    "breakout_bar_et": breakout_time,
                    "breakout_bar_close": breakout_close,
                    "reason": (
                        f"Next bar closed back inside OR ({confirm['close']}) — "
                        f"S/R level reclaimed, signal invalidated. "
                        f"Validated rule: PF 2.01→2.37 on 2y backtest."
                    ),
                    **sr_meta,
                }
            # Confirmation passed — fall through to normal trigger response
        if entry is not None:
            instructions = [
                f"Place MARKET {direction.upper()} for {root} at {entry:.2f} "
                f"({common['size_guidance']['contracts']} contracts)",
                f"Stop-market at {init_stop:.2f} ({init_stop_pts} pts = "
                f"${init_stop_pts * point_value:.0f} risk per contract)",
                f"Begin {trail_pts}pt trailing-stop logic: as price moves favorably, "
                f"raise stop to (MFE - {trail_pts}pts)",
                "Time exit: market-on-close at 16:00 ET if not stopped first",
            ]
            return {
                **common,
                "state": state,
                "breakout_bar_et": breakout_time,
                "breakout_bar_close": breakout_close,
                "entry_price_expected": round(entry, 2),
                "init_stop": round(init_stop, 2),
                "trail_stop_distance_pts": trail_pts,
                "max_loss_per_contract_dollars": init_stop_pts * point_value,
                "instructions": instructions,
                "sr_filter_applied": bool(sr_near and sr_levels),
                "sr_filter_passed": True if (sr_near and sr_levels) else None,
            }
        # Trigger fired but entry-bar OPEN not yet captured (tracker between ticks)
        return {
            **common,
            "state": f"{direction}_trigger_pending_entry",
            "breakout_bar_et": breakout_time,
            "breakout_bar_close": breakout_close,
            "next_action": "Breakout confirmed. Entry price = open of next bar — "
                          "tracker will populate on next tick (<5s).",
        }

    if state == "or_forming":
        cb = live.get("current_bar") or {}
        return {
            **common,
            "state": "or_forming",
            "or_bar_partial": {
                "open": cb.get("open"),
                "running_high": cb.get("high"),
                "running_low": cb.get("low"),
            },
            "next_action": "OR completes at 09:45 ET",
        }

    if state == "or_set":
        return {
            **common,
            "state": "or_set",
            "next_action": f"Watching 15m closes for break above "
                          f"{common['or_high']:.2f} or below {common['or_low']:.2f} until 11:00 ET",
        }

    if state == "window_closed_no_signal":
        return {
            **common,
            "state": "window_closed_no_signal",
            "reason": "No 15m bar in 09:45-11:00 ET window closed beyond OR — no trade today",
        }

    # pre_or, complete, or other states
    return {
        **common,
        "state": state,
    }


def analyze(now: datetime | None = None, root: str = "MNQ") -> dict:
    """Return current ORB state and any active signal for the given root.

    Default root='MNQ' keeps backward compat. Pass MES/NQ/ES/RTY/M2K to scan
    those instruments — uses ORB_CONFIG[root] for stop/trail/point_value.

    Data source priority:
      1. ~/orb_live_state.json (RH-live tracker, preferred) — no lag, exact bars
      2. massive.com 15-min aggregates (fallback) — historical-style, ~1-3min lag
    """
    cfg = _cfg(root)
    init_stop_pts = cfg["init_stop_pts"]
    trail_pts = cfg["trail_pts"]
    point_value = cfg["point_value"]

    ref = (now or _now_et()).astimezone(ET)
    today = ref.date()

    # Off-session checks
    if ref.weekday() >= 5:
        return {
            "success": True,
            "state": "off_session",
            "root": root,
            "as_of_et": ref.isoformat(timespec="seconds"),
            "reason": "weekend",
        }

    # VIX dead-zone filter (early skip — saves data pulls)
    prior_vix, vix_date = _get_vix_prior_close(ref)
    if prior_vix is None:
        return {
            "success": False,
            "state": "data_error",
            "as_of_et": ref.isoformat(timespec="seconds"),
            "reason": "could not fetch prior VIX close",
        }

    if VIX_DEAD_LOW <= prior_vix <= VIX_DEAD_HIGH:
        return {
            "success": True,
            "state": "skipped_vix_deadzone",
            "as_of_et": ref.isoformat(timespec="seconds"),
            "prior_vix": round(prior_vix, 2),
            "vix_as_of": vix_date,
            "reason": f"prior VIX {prior_vix:.2f} is in dead zone [{VIX_DEAD_LOW}, {VIX_DEAD_HIGH}] — skip today",
        }

    # Preferred path: read state from the RH-live tracker (orb_live_tracker.py).
    # Only use it when fresh (last tick within 30s — means tracker is actively
    # polling) and matches today's date + requested root. Falls through to the
    # massive.com path if the tracker isn't running or is stale.
    live = _try_live_state(root, ref)
    if live is not None:
        return _render_live_response(live, ref, root, prior_vix, vix_date)

    # Pre-OR (before 09:30 ET on trading day)
    or_start_dt = _today_at(*OR_START_HHMM, ref=ref)
    or_end_dt = _today_at(*OR_END_HHMM, ref=ref)
    breakout_end_dt = _today_at(*BREAKOUT_WINDOW_END_HHMM, ref=ref)
    session_end_dt = _today_at(*SESSION_END_HHMM, ref=ref)

    if ref < or_start_dt:
        return {
            "success": True,
            "state": "pre_or",
            "root": root,
            "as_of_et": ref.isoformat(timespec="seconds"),
            "prior_vix": round(prior_vix, 2),
            "vix_as_of": vix_date,
            "minutes_until_or": int((or_start_dt - ref).total_seconds() // 60),
            "size_guidance": _compute_size(prior_vix, root=root),
            "next_action": f"OR forms at 09:30 ET ({or_start_dt.strftime('%H:%M')})",
        }

    # Pull intraday bars for current state
    all_bars = _get_mnq_intraday(ref, root=root)
    if not all_bars:
        return {
            "success": False,
            "state": "data_error",
            "root": root,
            "as_of_et": ref.isoformat(timespec="seconds"),
            "reason": f"could not fetch {root} 15m bars from massive.com",
        }

    today_bars = _bars_for_today(all_bars, ref)
    last_bar = today_bars[-1] if today_bars else None

    # OR forming (09:30-09:45 ET)
    if ref < or_end_dt:
        or_bar = _find_or_bar(today_bars)
        return {
            "success": True,
            "state": "or_forming",
            "root": root,
            "as_of_et": ref.isoformat(timespec="seconds"),
            "prior_vix": round(prior_vix, 2),
            "vix_as_of": vix_date,
            "or_bar_partial": {
                "open": or_bar["open"] if or_bar else None,
                "running_high": or_bar["high"] if or_bar else None,
                "running_low": or_bar["low"] if or_bar else None,
            },
            "minutes_until_or_close": int((or_end_dt - ref).total_seconds() // 60),
            "size_guidance": _compute_size(prior_vix, root=root),
            "next_action": f"OR completes at 09:45 ET",
        }

    # OR is set — confirm we have the bar
    or_bar = _find_or_bar(today_bars)
    if or_bar is None:
        return {
            "success": False,
            "state": "data_error",
            "root": root,
            "as_of_et": ref.isoformat(timespec="seconds"),
            "reason": "09:30 ET bar not found in fetched data (may be data delay — try again in a few minutes)",
        }

    or_high = or_bar["high"]
    or_low = or_bar["low"]
    or_width = or_high - or_low

    # Look for a breakout in 09:45-11:00 window
    breakout = _find_breakout(today_bars, or_high, or_low)

    common = {
        "success": True,
        "root": root,
        "tier": cfg["tier"],
        "validated": cfg["validated"],
        "as_of_et": ref.isoformat(timespec="seconds"),
        "prior_vix": round(prior_vix, 2),
        "vix_as_of": vix_date,
        "or_high": or_high,
        "or_low": or_low,
        "or_width_pts": round(or_width, 2),
        "current_price": last_bar["close"] if last_bar else None,
        "size_guidance": _compute_size(prior_vix, root=root),
    }

    if breakout:
        bar = breakout["breakout_bar"]
        direction = breakout["direction"]
        # Entry would be at next bar's open. If we have it, fill in; else show triggered-pending-entry
        next_bar = _next_bar_after(today_bars, bar)
        if next_bar:
            entry_price = next_bar["open"]
            if direction == "long":
                init_stop = entry_price - init_stop_pts
            else:
                init_stop = entry_price + init_stop_pts

            # Soft breadth warning (exploratory — see orb_breadth.py)
            breadth_info = _breadth_warning(today.isoformat(), direction)

            return {
                **common,
                "state": f"{direction}_trigger",
                "breakout_bar_et": bar["dt_et"].isoformat(timespec="seconds"),
                "breakout_bar_close": bar["close"],
                "entry_price_expected": round(entry_price, 2),
                "init_stop": round(init_stop, 2),
                "trail_stop_distance_pts": trail_pts,
                "max_loss_per_contract_dollars": init_stop_pts * point_value,
                "breadth_warning": breadth_info,
                "instructions": [
                    f"Place MARKET {direction.upper()} for {root} at {entry_price:.2f} ({common['size_guidance']['contracts']} contracts)",
                    f"Stop-market at {init_stop:.2f} ({init_stop_pts} pts = ${init_stop_pts * point_value:.0f} risk per contract)",
                    f"Begin {trail_pts}pt trailing-stop logic: as price moves favorably, raise stop to (MFE - {trail_pts}pts)",
                    f"Time exit: market-on-close at 16:00 ET if not stopped first",
                ],
            }
        else:
            # Breakout bar is the latest bar; entry bar not yet open. Tell user to wait.
            return {
                **common,
                "state": f"{direction}_trigger_pending_entry",
                "breakout_bar_et": bar["dt_et"].isoformat(timespec="seconds"),
                "breakout_bar_close": bar["close"],
                "next_action": "Breakout confirmed. Wait for next 15m bar open as entry price. Re-check in <15 min.",
            }

    # No breakout yet — are we still in the window or past it?
    if ref < breakout_end_dt:
        return {
            **common,
            "state": "or_set",
            "minutes_until_window_close": int((breakout_end_dt - ref).total_seconds() // 60),
            "next_action": f"Watching 15m closes for break above {or_high:.2f} or below {or_low:.2f} until 11:00 ET",
        }

    # Window closed without a signal
    if ref < session_end_dt:
        return {
            **common,
            "state": "window_closed_no_signal",
            "reason": "No 15m bar in 09:45-11:00 ET window closed beyond OR — no trade today",
        }

    return {
        **common,
        "state": "off_session",
        "reason": "session ended (past 16:00 ET)",
    }
