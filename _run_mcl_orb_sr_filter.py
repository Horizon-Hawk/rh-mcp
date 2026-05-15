"""MNQ ORB — S/R-near-OR confirmation-candle filter test.

Hypothesis: when a known S/R level (prior-day high/low/close) sits within X
points of OR_high or OR_low, the breakout bar isn't enough — require the NEXT
15m bar after breakout to ALSO close beyond OR level. Two-bar confirmation
filters out chop-fades where the breakout pokes through a level and
immediately reverses.

Motivating example (paper trade 2026-05-15):
  PM support 29,153.75   OR_low 29,154.25 (0.5 pt gap)
  Breakout bar closed 29,147.25 (7 pts below OR_low) → triggered short
  Next bar opened 29,153.25, ran to 29,208 → STOPPED OUT in 10 minutes
  Confirmation rule: next bar would have closed ABOVE 29,154 (reclaim) → skip.

Tests:
  - Baseline FULL spec (current production)
  - + sr_filter with proximity thresholds 5 / 10 / 15 / 20 pts
  - Each: report trade count, win rate, PF, $/trade, total, max DD

If PF improves AND trade count drops modestly, rule is a keeper.
"""
from __future__ import annotations
import csv, importlib.util, sys
from collections import defaultdict
from statistics import mean

spec = importlib.util.spec_from_file_location("orb_raw", "_run_mnq_orb_backtest.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["orb_raw"] = mod
spec.loader.exec_module(mod)
Bar = mod.Bar
load_bars = mod.load_bars
group_by_session = mod.group_by_session
POINT_VALUE = mod.POINT_VALUE

spec2 = importlib.util.spec_from_file_location("orb_filt", "_run_mnq_orb_filtered.py")
modf = importlib.util.module_from_spec(spec2)
sys.modules["orb_filt"] = modf
spec2.loader.exec_module(modf)
load_daily = modf.load_daily

spec3 = importlib.util.spec_from_file_location("orb_vix", "_run_mnq_orb_vix.py")
modv = importlib.util.module_from_spec(spec3)
sys.modules["orb_vix"] = modv
spec3.loader.exec_module(modv)
load_vix = modv.load_vix


def daily_features_extended(daily: dict[str, dict]) -> dict[str, dict]:
    """Like daily_features but also surfaces prev_high, prev_low for S/R use."""
    dates = sorted(daily.keys())
    feats: dict[str, dict] = {}
    closes = []
    trs = []
    for i, d in enumerate(dates):
        row = daily[d]
        closes.append(row["close"])
        if i > 0:
            prev_close = daily[dates[i - 1]]["close"]
            tr = max(row["high"] - row["low"],
                     abs(row["high"] - prev_close),
                     abs(row["low"] - prev_close))
            trs.append(tr)
        else:
            trs.append(row["high"] - row["low"])

        if i >= 50:
            sma50 = sum(closes[i - 50:i]) / 50
        else:
            sma50 = None
        if i >= 14:
            atr14 = sum(trs[i - 14:i]) / 14
        else:
            atr14 = None
        prev_close = daily[dates[i - 1]]["close"] if i > 0 else None
        prev_high  = daily[dates[i - 1]]["high"]  if i > 0 else None
        prev_low   = daily[dates[i - 1]]["low"]   if i > 0 else None
        feats[d] = {
            "sma50": sma50,
            "atr14": atr14,
            "prev_close": prev_close,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "today_close": row["close"],
        }
    return feats


def simulate_managed_sr(
    or_bar: Bar,
    breakout_bars: list[Bar],
    manage_bars: list[Bar],
    eod_bar: Bar,
    sr_levels: list[float],
    sr_proximity_pts: float,
    stop_pts: float = 50.0,
    trail_pts: float | None = None,
) -> dict | None:
    """ORB simulator with S/R confirmation gating + optional trailing stop.

    If any S/R level sits within `sr_proximity_pts` of OR_high or OR_low,
    a single bar breakout is NOT enough — require the next 15m bar after
    breakout to also close beyond the OR level in the breakout direction.

    trail_pts: distance for trailing stop. None = EOD baseline (no trail).
               25.0 = production spec (validated 25pt trail).

    Returns None if no qualifying signal (or filter rejects it). Otherwise the
    same dict shape as simulate_managed.
    """
    or_high = or_bar.high
    or_low = or_bar.low
    if or_high - or_low <= 0:
        return None

    # Is S/R near either OR boundary?
    sr_near_high = any(abs(or_high - lvl) <= sr_proximity_pts for lvl in sr_levels)
    sr_near_low  = any(abs(or_low  - lvl) <= sr_proximity_pts for lvl in sr_levels)

    # Find first breakout
    direction = None
    breakout_idx = None
    for i, b in enumerate(breakout_bars[:-1]):
        if b.close > or_high:
            direction = "long"
            breakout_idx = i
            break
        elif b.close < or_low:
            direction = "short"
            breakout_idx = i
            break
    if direction is None:
        return None

    # S/R confirmation rule: if level is near OR boundary in the breakout
    # direction, require next bar after breakout_bar to also close beyond.
    needs_confirm = (
        (direction == "long"  and sr_near_high) or
        (direction == "short" and sr_near_low)
    )
    if needs_confirm:
        # Need at least 2 more bars after breakout_idx: one for confirmation,
        # one for entry. Entry is on the bar AFTER confirmation close.
        if breakout_idx + 2 >= len(breakout_bars):
            return None  # No room for confirm + entry
        confirm_bar = breakout_bars[breakout_idx + 1]
        if direction == "long":
            if confirm_bar.close <= or_high:
                return None  # Confirmation failed — reclaim
        else:
            if confirm_bar.close >= or_low:
                return None  # Confirmation failed — reclaim
        entry_idx = breakout_idx + 2
    else:
        entry_idx = breakout_idx + 1

    if entry_idx >= len(breakout_bars):
        return None
    entry_bar = breakout_bars[entry_idx]
    entry = entry_bar.open

    if direction == "long":
        stop = entry - stop_pts
    else:
        stop = entry + stop_pts
    risk = stop_pts

    remainder = breakout_bars[entry_idx + 1:] + manage_bars
    if eod_bar not in remainder and eod_bar is not entry_bar:
        remainder = remainder + [eod_bar]

    # Bar-by-bar management. Trail engages immediately from entry if trail_pts set.
    exit_price = None
    exit_reason = None
    mfe = entry  # max favorable excursion
    for b in remainder:
        # Stop check first (within-bar conservatism — stop wins ties)
        if direction == "long":
            hit_stop = b.low <= stop
        else:
            hit_stop = b.high >= stop
        if hit_stop:
            exit_price = stop
            exit_reason = "trail" if (trail_pts is not None and stop != (entry - stop_pts if direction == "long" else entry + stop_pts)) else "stop"
            break
        # Trail maintenance: ratchet stop closer as MFE extends
        if trail_pts is not None:
            if direction == "long":
                if b.high > mfe:
                    mfe = b.high
                    new_trail = mfe - trail_pts
                    if new_trail > stop:
                        stop = new_trail
            else:
                if b.low < mfe or mfe == entry:
                    mfe = b.low if b.low < mfe or mfe == entry else mfe
                    new_trail = mfe + trail_pts
                    if new_trail < stop:
                        stop = new_trail
    if exit_price is None:
        exit_price = eod_bar.close
        exit_reason = "eod"

    if direction == "long":
        pnl_pts = exit_price - entry
    else:
        pnl_pts = entry - exit_price
    pnl_dollars = pnl_pts * POINT_VALUE
    r_mult = pnl_pts / risk

    return {
        "date": or_bar.date,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "total_pnl_pts": pnl_pts,
        "total_pnl_dollars": pnl_dollars,
        "r_multiple": r_mult,
        "sr_filter_applied": needs_confirm,
    }


def _build_rejection_zones(
    bars: list[Bar],
    wick_threshold: float = 5.0,
    zone_width: float = 10.0,
    min_hits: int = 2,
    lookback_sessions: int = 5,
) -> dict[str, list[float]]:
    """For each session date, compute S/R zone midpoints using rolling rejection
    events from prior `lookback_sessions` of bars.

    A "rejection event" on a 15m bar = wick (high-body or body-low) extends
    `wick_threshold` pts beyond the bar's body. That wick's tip is registered
    as a tested-and-rejected price.

    Events are clustered: rejections within `zone_width` of each other form one
    zone. Zones with >= `min_hits` rejections qualify as valid S/R for that
    forward session.

    Returns: dict {date: [zone_midpoint, ...]}
    """
    by_date: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        by_date[b.date].append(b)
    dates = sorted(by_date.keys())

    zones_by_date: dict[str, list[float]] = {}
    for i, d in enumerate(dates):
        if i < lookback_sessions:
            zones_by_date[d] = []
            continue

        # Collect rejection events from prior N sessions (NOT today — avoid look-ahead)
        events: list[float] = []
        for j in range(i - lookback_sessions, i):
            prev_d = dates[j]
            for b in by_date[prev_d]:
                upper_wick = b.high - max(b.open, b.close)
                lower_wick = min(b.open, b.close) - b.low
                if upper_wick >= wick_threshold:
                    events.append(b.high)
                if lower_wick >= wick_threshold:
                    events.append(b.low)

        if not events:
            zones_by_date[d] = []
            continue

        events.sort()
        clusters: list[list[float]] = []
        current = [events[0]]
        for p in events[1:]:
            if p - current[-1] <= zone_width:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)

        midpoints = [
            (min(c) + max(c)) / 2
            for c in clusters if len(c) >= min_hits
        ]
        zones_by_date[d] = midpoints
    return zones_by_date


def _build_intraday_sr(bars: list[Bar]) -> dict[str, dict]:
    """Compute per-date intraday S/R levels from the raw 15m feed.

    Two intraday levels per session:
      - pm_high / pm_low: same-day bars 04:00-09:29 ET (RTH premarket window)
      - on_high / on_low: prior-day 18:00 → current-day 09:29 ET (overnight Globex)

    These are the levels that actually trip OR breakouts in practice. Daily
    prev_high/prev_low/prev_close are mostly far from OR after Globex action.
    """
    # Bucket bars by their session date
    by_date: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        by_date[b.date].append(b)

    levels: dict[str, dict] = {}
    dates = sorted(by_date.keys())
    for i, d in enumerate(dates):
        # Premarket: same day, 04:00-09:29
        pm_bars = [b for b in by_date[d] if 400 <= b.hhmm < 930]
        pm_high = max((b.high for b in pm_bars), default=None)
        pm_low  = min((b.low  for b in pm_bars), default=None)

        # Overnight: prior day 18:00 → today 09:29
        on_bars = [b for b in by_date[d] if b.hhmm < 930]
        if i > 0:
            prev_d = dates[i - 1]
            on_bars += [b for b in by_date[prev_d] if b.hhmm >= 1800]
        on_high = max((b.high for b in on_bars), default=None)
        on_low  = min((b.low  for b in on_bars), default=None)

        levels[d] = {
            "pm_high": pm_high,
            "pm_low":  pm_low,
            "on_high": on_high,
            "on_low":  on_low,
        }
    return levels


def run_sr_test(
    bars: list[Bar],
    feats: dict,
    vix: dict[str, float],
    sr_proximity_pts: float,
    use_sr_filter: bool = True,
    stop_pts: float = 50.0,
    sr_source: str = "intraday",  # 'intraday' | 'daily' | 'both' | 'rejection_zones'
    trail_pts: float | None = None,
    rz_wick_threshold: float = 5.0,
    rz_zone_width: float = 10.0,
    rz_min_hits: int = 2,
    rz_lookback: int = 5,
) -> list[dict]:
    """Run 15m ORB + full filters (trend50, VIX-deadzone) +/- S/R confirmation.

    sr_source controls which S/R set is used:
      - 'intraday':        PM high/low + overnight high/low
      - 'daily':           prior-day high/low/close
      - 'both':            union of intraday + daily
      - 'rejection_zones': rolling rejection-clustered zones (the new spec)
    """
    intraday_levels = _build_intraday_sr(bars)
    rejection_zones = None
    if sr_source in ("rejection_zones", "rz+intraday"):
        rejection_zones = _build_rejection_zones(
            bars, wick_threshold=rz_wick_threshold,
            zone_width=rz_zone_width, min_hits=rz_min_hits,
            lookback_sessions=rz_lookback,
        )
    by_day = group_by_session(bars)
    or_time = 930
    breakout_lo, breakout_hi = 945, 1100

    vix_dates = sorted(vix.keys())
    prior_vix: dict[str, float] = {}
    for d in sorted(by_day.keys()):
        prev = None
        for vd in vix_dates:
            if vd < d:
                prev = vd
            else:
                break
        if prev is not None:
            prior_vix[d] = vix[prev]

    trades: list[dict] = []
    for date in sorted(by_day.keys()):
        session = sorted(by_day[date], key=lambda b: b.dt)
        or_bar = next((b for b in session if b.hhmm == or_time), None)
        if or_bar is None:
            continue
        feat = feats.get(date)
        if feat is None:
            continue

        v = prior_vix.get(date)
        if v is None or 18.0 <= v <= 22.0:
            continue
        if feat["sma50"] is None or feat["prev_close"] is None:
            continue
        bullish_trend = feat["prev_close"] > feat["sma50"]

        breakout = [b for b in session if breakout_lo <= b.hhmm <= breakout_hi]
        manage = [b for b in session if breakout_hi < b.hhmm <= 1600]
        eod_cands = [b for b in session if b.hhmm <= 1600]
        if not eod_cands:
            continue
        eod_bar = max(eod_cands, key=lambda b: b.dt)

        # S/R levels: assemble per sr_source.
        sr_levels = []
        intraday = intraday_levels.get(date, {})
        if sr_source in ("intraday", "both", "rz+intraday"):
            for k in ("pm_high", "pm_low", "on_high", "on_low"):
                v = intraday.get(k)
                if v is not None:
                    sr_levels.append(v)
        if sr_source in ("daily", "both"):
            for k in ("prev_high", "prev_low", "prev_close"):
                v = feat.get(k)
                if v is not None:
                    sr_levels.append(v)
        if sr_source in ("rejection_zones", "rz+intraday") and rejection_zones is not None:
            sr_levels.extend(rejection_zones.get(date, []))

        # Effective proximity for "no filter" mode = 0 (never gate)
        prox = sr_proximity_pts if use_sr_filter else 0.0
        t = simulate_managed_sr(or_bar, breakout, manage, eod_bar,
                                 sr_levels=sr_levels,
                                 sr_proximity_pts=prox,
                                 stop_pts=stop_pts,
                                 trail_pts=trail_pts)
        if t is None:
            continue

        # Apply trend50 direction filter
        if t["direction"] == "long" and not bullish_trend:
            continue
        if t["direction"] == "short" and bullish_trend:
            continue
        trades.append(t)
    return trades


def report(trades: list[dict], n_sessions: int, label: str) -> None:
    if not trades:
        print(f"  {label:<55} no trades")
        return
    rs = [t["r_multiple"] for t in trades]
    pnls = [t["total_pnl_dollars"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    gw = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    pf = gw / gl if gl > 0 else float("inf")
    equity = 0; peak = 0; mdd = 0
    for p in pnls:
        equity += p; peak = max(peak, equity); mdd = max(mdd, peak - equity)
    yrs = n_sessions / 252.0
    n_filtered = sum(1 for t in trades if t.get("sr_filter_applied"))
    print(f"  {label:<55} N={len(trades):>3}  WR {wins/len(trades):>4.0%}  "
          f"avgR {mean(rs):+5.2f}  PF {pf:>4.2f}  ${mean(pnls):>+5.0f}/t  "
          f"total ${sum(pnls):>+7,.0f}  ann ${sum(pnls)/yrs:>+6,.0f}  "
          f"DD ${mdd:>6,.0f}  | confirmed: {n_filtered}")


def main():
    DATA_FILE = "backtest_data/mcl_15min_continuous.csv"
    bars = load_bars(DATA_FILE)
    print(f"Loaded {len(bars)} bars from {DATA_FILE}")
    # NOTE: POINT_VALUE inherited from MNQ ($2/pt). MCL = $10/pt.
    # Dollar P&L is MNQ-equivalent; multiply by 5x for true MCL dollars.
    # PF / WR / R-multiples are point-value-agnostic.
    daily = load_daily("backtest_data/mcl_1d_continuous.csv")
    feats = daily_features_extended(daily)
    vix = load_vix("backtest_data/vix_5y.csv")
    sessions = len({b.date for b in bars if 930 <= b.hhmm <= 1600})
    print(f"RTH sessions: {sessions}\n")

    print("=" * 130)
    print("SR-NEAR-OR CONFIRMATION-CANDLE FILTER — Sweep on 2y MNQ 15m data")
    print("=" * 130)
    print("Setup: 15m ORB + trend50 + VIX-deadzone-skip + 50pt fixed stop + EOD hold")
    print("S/R levels: prior-day high, low, close")
    print()

    # Baseline (no S/R filter)
    base_trades = run_sr_test(bars, feats, vix, sr_proximity_pts=0, use_sr_filter=False)
    report(base_trades, sessions, "BASELINE (no S/R filter)")

    print()
    print("--- intraday S/R (PM high/low + overnight high/low) ---")
    for prox in [5.0, 10.0, 15.0, 20.0, 30.0]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=True, sr_source="intraday")
        report(ts, sessions, f"intraday  +{prox:>4.0f}pt")

    print()
    print("--- daily S/R (prev day high/low/close) ---")
    for prox in [5.0, 10.0, 15.0, 20.0, 30.0]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=True, sr_source="daily")
        report(ts, sessions, f"daily     +{prox:>4.0f}pt")

    print()
    print("--- BOTH (intraday + daily) ---")
    for prox in [5.0, 10.0, 15.0, 20.0, 30.0]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=True, sr_source="both")
        report(ts, sessions, f"both      +{prox:>4.0f}pt")

    # ========================================================================
    # REJECTION ZONES — rolling 5-session, multi-hit S/R bands
    # ========================================================================
    print()
    print("=" * 130)
    print("REJECTION ZONES — rolling 5-session, multi-hit clustering")
    print("=" * 130)
    print("Setup: 25pt trail (production spec) + zone-based S/R confirmation")
    print()
    # Grid sweep on zone params, single proximity (10pt) since proximity is now zone-relative
    PROX = 10.0
    grid = [
        # (wick_threshold, zone_width, min_hits, lookback)
        (5,  10, 2, 5),
        (5,  10, 3, 5),
        (5,  15, 2, 5),
        (5,  15, 3, 5),
        (5,  20, 2, 5),
        (5,  20, 3, 5),
        (10, 10, 2, 5),
        (10, 10, 3, 5),
        (10, 15, 2, 5),
        (10, 20, 2, 5),
        (15, 15, 2, 5),
        (15, 20, 2, 5),
    ]
    for (wt, zw, mh, lb) in grid:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=PROX,
                         use_sr_filter=True, sr_source="rejection_zones",
                         trail_pts=25.0,
                         rz_wick_threshold=wt, rz_zone_width=zw,
                         rz_min_hits=mh, rz_lookback=lb)
        report(ts, sessions,
               f"rz wick={wt:>2}pt zone={zw:>2}pt hits>={mh} lb={lb}d + 25t trail")

    # Combo: rejection zones + intraday PM/overnight
    print()
    print("--- rejection_zones + intraday (union) ---")
    for (wt, zw, mh, lb) in [(5, 10, 2, 5), (10, 15, 2, 5), (10, 20, 2, 5)]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=PROX,
                         use_sr_filter=True, sr_source="rz+intraday",
                         trail_pts=25.0,
                         rz_wick_threshold=wt, rz_zone_width=zw,
                         rz_min_hits=mh, rz_lookback=lb)
        report(ts, sessions,
               f"rz+intraday wick={wt} zone={zw} hits>={mh} lb={lb}d + 25t trail")

    # ========================================================================
    # TRAIL STRESS TEST — combine intraday S/R filter with 25pt trail (production)
    # ========================================================================
    print()
    print("=" * 130)
    print("TRAIL STRESS TEST: intraday S/R filter + 25pt trail (validated production spec)")
    print("=" * 130)

    trail_base = run_sr_test(bars, feats, vix, sr_proximity_pts=0,
                             use_sr_filter=False, trail_pts=25.0)
    report(trail_base, sessions, "BASELINE + 25pt trail (validated spec)")
    print()
    for prox in [5.0, 10.0, 15.0, 20.0, 30.0]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=True, sr_source="intraday",
                         trail_pts=25.0)
        report(ts, sessions, f"intraday {prox:>4.0f}pt + 25pt trail")

    # 50pt trail variant for completeness
    print()
    print("--- 50pt trail variant ---")
    trail50_base = run_sr_test(bars, feats, vix, sr_proximity_pts=0,
                                use_sr_filter=False, trail_pts=50.0)
    report(trail50_base, sessions, "BASELINE + 50pt trail")
    for prox in [10.0, 20.0]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=True, sr_source="intraday",
                         trail_pts=50.0)
        report(ts, sessions, f"intraday {prox:>4.0f}pt + 50pt trail")

    # By-year breakdown for the trail+intraday combo
    print()
    print("=" * 130)
    print("YEAR-BY-YEAR — intraday-10pt + 25pt trail  vs  baseline + 25pt trail")
    print("=" * 130)
    for label, prox, use in [("BASELINE + 25pt trail", 0, False),
                              ("INTRADAY 10pt + 25pt trail", 10.0, True)]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=use, sr_source="intraday",
                         trail_pts=25.0)
        print(f"\n  --- {label} ---")
        by_year = defaultdict(list)
        for t in ts:
            by_year[t["date"][:4]].append(t)
        for yr in sorted(by_year.keys()):
            yts = by_year[yr]
            pnls = [t["total_pnl_dollars"] for t in yts]
            wins = sum(1 for t in yts if t["r_multiple"] > 0)
            gw = sum(p for p in pnls if p > 0)
            gl = abs(sum(p for p in pnls if p <= 0))
            pf = gw / gl if gl > 0 else float("inf")
            print(f"    {yr}: N={len(yts):>3}  WR {wins/len(yts):>4.0%}  "
                  f"avgR {mean(t['r_multiple'] for t in yts):+5.2f}  "
                  f"PF {pf:>4.2f}  total ${sum(pnls):>+7,.0f}")

    # ========================================================================
    # ORIGINAL EOD baseline year-by-year (kept for reference)
    # ========================================================================
    print()
    print("=" * 130)
    print("YEAR-BY-YEAR — intraday-10pt (EOD, no trail) vs baseline")
    print("=" * 130)
    for label, prox, use, src in [("BASELINE", 0, False, "intraday"),
                                   ("INTRADAY 10pt", 10.0, True, "intraday")]:
        ts = run_sr_test(bars, feats, vix, sr_proximity_pts=prox,
                         use_sr_filter=use, sr_source=src)
        print(f"\n  --- {label} ---")
        by_year = defaultdict(list)
        for t in ts:
            by_year[t["date"][:4]].append(t)
        for yr in sorted(by_year.keys()):
            yts = by_year[yr]
            pnls = [t["total_pnl_dollars"] for t in yts]
            wins = sum(1 for t in yts if t["r_multiple"] > 0)
            gw = sum(p for p in pnls if p > 0)
            gl = abs(sum(p for p in pnls if p <= 0))
            pf = gw / gl if gl > 0 else float("inf")
            print(f"    {yr}: N={len(yts):>3}  WR {wins/len(yts):>4.0%}  "
                  f"avgR {mean(t['r_multiple'] for t in yts):+5.2f}  "
                  f"PF {pf:>4.2f}  total ${sum(pnls):>+7,.0f}")


if __name__ == "__main__":
    main()
