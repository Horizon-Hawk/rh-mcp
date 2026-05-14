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
INIT_STOP_POINTS = 50.0
TRAIL_POINTS = 25.0
POINT_VALUE_DOLLARS = 2.0  # MNQ: $2/point per contract

OR_START_HHMM = (9, 30)
OR_END_HHMM = (9, 45)
BREAKOUT_WINDOW_END_HHMM = (11, 0)
SESSION_END_HHMM = (16, 0)


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


def _front_month_ticker(now: datetime) -> str:
    """MNQ front-month code for given date. Rolls ~14th of expiry month.

    Quarterly cycle: H(Mar)/M(Jun)/U(Sep)/Z(Dec). Single-digit year per massive.com.
    """
    d = now.astimezone(ET)
    y = d.year % 10
    m, day = d.month, d.day
    # Pick next quarterly that we haven't rolled out of yet
    if (m, day) < (3, 14):
        return f"MNQH{y}"
    if (m, day) < (6, 14):
        return f"MNQM{y}"
    if (m, day) < (9, 14):
        return f"MNQU{y}"
    if (m, day) < (12, 14):
        return f"MNQZ{y}"
    return f"MNQH{(y + 1) % 10}"  # roll to next year's H


def _get_mnq_intraday_massive(now: datetime) -> list[dict] | None:
    """Pull recent 15-min MNQ bars from massive.com. <1s typically. Returns None on
    any failure (caller falls back to yfinance).
    """
    import os, urllib.request, json
    try:
        key = open(os.path.expanduser("~/.marketdata_api_key")).read().strip()
    except Exception:
        return None
    ticker = _front_month_ticker(now)
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


def _get_mnq_intraday(now: datetime) -> list[dict] | None:
    """Pull recent 15-min MNQ bars from massive.com (typically <1s)."""
    return _get_mnq_intraday_massive(now)


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


def _compute_size(prior_vix: float | None) -> dict:
    """Position size guidance based on VIX regime."""
    if prior_vix is not None and prior_vix > VIX_CRISIS:
        return {
            "contracts": 2,
            "rationale": f"VIX prior close {prior_vix:.1f} > {VIX_CRISIS} (crisis stack +1)",
            "max_risk_dollars": 2 * INIT_STOP_POINTS * POINT_VALUE_DOLLARS,
        }
    return {
        "contracts": 1,
        "rationale": "base size (no VIX-crisis stack)",
        "max_risk_dollars": 1 * INIT_STOP_POINTS * POINT_VALUE_DOLLARS,
    }


def analyze(now: datetime | None = None) -> dict:
    """Return current ORB state and any active signal. Safe to call any time of day.

    Optional `now` for backtesting/inspection — defaults to real current ET time.
    """
    ref = (now or _now_et()).astimezone(ET)
    today = ref.date()

    # Off-session checks
    if ref.weekday() >= 5:
        return {
            "success": True,
            "state": "off_session",
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

    # Pre-OR (before 09:30 ET on trading day)
    or_start_dt = _today_at(*OR_START_HHMM, ref=ref)
    or_end_dt = _today_at(*OR_END_HHMM, ref=ref)
    breakout_end_dt = _today_at(*BREAKOUT_WINDOW_END_HHMM, ref=ref)
    session_end_dt = _today_at(*SESSION_END_HHMM, ref=ref)

    if ref < or_start_dt:
        return {
            "success": True,
            "state": "pre_or",
            "as_of_et": ref.isoformat(timespec="seconds"),
            "prior_vix": round(prior_vix, 2),
            "vix_as_of": vix_date,
            "minutes_until_or": int((or_start_dt - ref).total_seconds() // 60),
            "size_guidance": _compute_size(prior_vix),
            "next_action": f"OR forms at 09:30 ET ({or_start_dt.strftime('%H:%M')})",
        }

    # Pull intraday bars for current state
    all_bars = _get_mnq_intraday(ref)
    if not all_bars:
        return {
            "success": False,
            "state": "data_error",
            "as_of_et": ref.isoformat(timespec="seconds"),
            "reason": "could not fetch MNQ 15m bars from yfinance",
        }

    today_bars = _bars_for_today(all_bars, ref)
    last_bar = today_bars[-1] if today_bars else None

    # OR forming (09:30-09:45 ET)
    if ref < or_end_dt:
        or_bar = _find_or_bar(today_bars)
        return {
            "success": True,
            "state": "or_forming",
            "as_of_et": ref.isoformat(timespec="seconds"),
            "prior_vix": round(prior_vix, 2),
            "vix_as_of": vix_date,
            "or_bar_partial": {
                "open": or_bar["open"] if or_bar else None,
                "running_high": or_bar["high"] if or_bar else None,
                "running_low": or_bar["low"] if or_bar else None,
            },
            "minutes_until_or_close": int((or_end_dt - ref).total_seconds() // 60),
            "size_guidance": _compute_size(prior_vix),
            "next_action": f"OR completes at 09:45 ET",
        }

    # OR is set — confirm we have the bar
    or_bar = _find_or_bar(today_bars)
    if or_bar is None:
        return {
            "success": False,
            "state": "data_error",
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
        "as_of_et": ref.isoformat(timespec="seconds"),
        "prior_vix": round(prior_vix, 2),
        "vix_as_of": vix_date,
        "or_high": or_high,
        "or_low": or_low,
        "or_width_pts": round(or_width, 2),
        "current_price": last_bar["close"] if last_bar else None,
        "size_guidance": _compute_size(prior_vix),
    }

    if breakout:
        bar = breakout["breakout_bar"]
        direction = breakout["direction"]
        # Entry would be at next bar's open. If we have it, fill in; else show triggered-pending-entry
        next_bar = _next_bar_after(today_bars, bar)
        if next_bar:
            entry_price = next_bar["open"]
            if direction == "long":
                init_stop = entry_price - INIT_STOP_POINTS
            else:
                init_stop = entry_price + INIT_STOP_POINTS

            # Soft breadth warning (exploratory — see orb_breadth.py)
            breadth_info = _breadth_warning(today.isoformat(), direction)

            return {
                **common,
                "state": f"{direction}_trigger",
                "breakout_bar_et": bar["dt_et"].isoformat(timespec="seconds"),
                "breakout_bar_close": bar["close"],
                "entry_price_expected": round(entry_price, 2),
                "init_stop": round(init_stop, 2),
                "trail_stop_distance_pts": TRAIL_POINTS,
                "max_loss_per_contract_dollars": INIT_STOP_POINTS * POINT_VALUE_DOLLARS,
                "breadth_warning": breadth_info,
                "instructions": [
                    f"Place MARKET {direction.upper()} for MNQ at {entry_price:.2f} ({common['size_guidance']['contracts']} contracts)",
                    f"Stop-market at {init_stop:.2f} ({INIT_STOP_POINTS} pts = ${INIT_STOP_POINTS * POINT_VALUE_DOLLARS:.0f} risk per contract)",
                    f"Begin 25pt trailing-stop logic: as price moves favorably, raise stop to (MFE - 25pts)",
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
