"""Full validated stack: small_bullish_8k (+stop) + mid_bullish_8k + buyback + PEAD bounce.

Runs all FOUR stress-test-validated strategies on a single shared $36K
equity bucket. Walks forward by filing_date, max 5 concurrent positions
across all strategies. Tracks per-strategy trade counts + ticker collisions
between strategies (a stock held long via 8K-bullish blocks a buyback
entry on the same ticker the same day).

The earlier backtest_combined.py included strategies that the 4yr stress
test invalidated (FRD_LONG, SHORT bearish_8K, low_float variants). Its
+490% headline is inflated. This script uses ONLY the validated four:

  1. small_bullish_8k: 5d hold, -5% t+1 stop (validated rule)
  2. mid_bullish_8k:   5d hold, no t+1 stop (rule doesn't transfer)
  3. buyback:          5d hold (body_keyword filter on 8k_history_4yr)
  4. pead_bounce:      1d effective (synthetic drift t+1 -> t+5 after
                       negative earnings reaction)
"""
import csv
import sys
from pathlib import Path
from datetime import date as _date, timedelta

EIGHTK_SMALL = Path("C:/Users/algee/.edgar_cache/backtest_smallcap_4yr.csv")
EIGHTK_MID = Path("C:/Users/algee/.edgar_cache/backtest_midcap_4yr.csv")
RAW_8K = Path("C:/Users/algee/.edgar_cache/8k_history_4yr.csv")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_enriched(path):
    """Load enriched corpus (has direction labels + cap/float flags)."""
    rows = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence")) or 0.5
            if r["next_day_return"] is None:
                continue
            rows.append(r)
    return rows


def _load_raw(path):
    """Load raw 4yr 8K corpus (for buyback / PEAD body keyword filtering)."""
    rows = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = 0.5
            if r["next_day_return"] is None:
                continue
            rows.append(r)
    return rows


def _date_key(s):
    try:
        return tuple(int(p) for p in s.split("-"))
    except Exception:
        return (0, 0, 0)


def build_strategy_rows():
    """Produce a single list of trade-ready rows, each tagged with strategy
    name + hold_days + per-row t1_stop_pct."""
    sm = _load_enriched(EIGHTK_SMALL)
    md = _load_enriched(EIGHTK_MID)
    raw = _load_raw(RAW_8K)

    out = []

    # Strategy 1: small-cap bullish_8k (with -5% t+1 stop)
    for r in sm:
        if r.get("direction") != "long":
            continue
        rr = dict(r)
        rr["strategy"] = "sm_bullish_8k"
        rr["hold_days"] = 5
        rr["t1_stop_pct"] = -0.05
        rr["side"] = "long"
        out.append(rr)

    # Strategy 2: mid-cap bullish_8k (no stop)
    for r in md:
        if r.get("direction") != "long":
            continue
        rr = dict(r)
        rr["strategy"] = "mid_bullish_8k"
        rr["hold_days"] = 5
        rr["t1_stop_pct"] = None
        rr["side"] = "long"
        out.append(rr)

    # Strategy 3: buyback / dividend_increase (long 5d)
    for r in raw:
        kws = (r.get("body_keywords") or "").split("|")
        if "buyback_authorized" not in kws and "dividend_increase" not in kws:
            continue
        rr = dict(r)
        rr["strategy"] = "buyback"
        rr["hold_days"] = 5
        rr["t1_stop_pct"] = None
        rr["side"] = "long"
        out.append(rr)

    # Strategy 4: PEAD negative-earnings bounce (synthetic drift, 1-day "hold")
    for r in raw:
        if "2.02" not in (r.get("item_codes") or "").split("|"):
            continue
        t1 = r.get("next_day_return")
        t5 = r.get("five_day_return")
        if t1 is None or t5 is None or t1 >= 0:
            continue
        drift = (1 + t5) / (1 + t1) - 1
        rr = dict(r)
        # Replace the return fields with the drift; sim treats it as 1-day
        rr["next_day_return"] = drift
        rr["five_day_return"] = drift
        rr["strategy"] = "pead_bounce"
        rr["hold_days"] = 1
        rr["t1_stop_pct"] = None
        rr["side"] = "long"
        out.append(rr)

    return out


def simulate_combined(
    rows, starting_equity=36_000.0,
    max_concurrent=5, max_position_pct=0.15,
    stop_distance_pct=0.03,
    daily_loss_limit_pct=0.06, weekly_loss_limit_pct=0.10,
):
    rows = sorted(rows, key=lambda r: _date_key(r.get("filing_date") or ""))

    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    open_positions = []
    trades_by_strat = {}
    overlap_skips = {}
    skipped_concurrent = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_date = None
    current_week = None

    def _iso_week(d):
        try:
            y, m, day = (int(p) for p in d.split("-"))
            return f"{_date(y, m, day).isocalendar()[0]}-W{_date(y, m, day).isocalendar()[1]:02d}"
        except Exception:
            return d

    for r in rows:
        date = r.get("filing_date") or ""
        if not date:
            continue
        strat = r["strategy"]
        ticker = (r.get("ticker") or "").upper()
        hold_days = r["hold_days"]
        t1_stop = r.get("t1_stop_pct")
        side = r["side"]

        # Close expired positions
        still = []
        for p in open_positions:
            if _date_key(date) >= _date_key(p["exit_date"]):
                equity += p["pnl_pending"]
                daily_pnl += p["pnl_pending"]
                weekly_pnl += p["pnl_pending"]
            else:
                still.append(p)
        open_positions = still

        if date != current_date:
            current_date = date
            daily_pnl = 0.0
            wk = _iso_week(date)
            if wk != current_week:
                current_week = wk
                weekly_pnl = 0.0

        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # Skips
        if daily_pnl < -daily_loss_limit_pct * equity:
            continue
        if weekly_pnl < -weekly_loss_limit_pct * equity:
            continue
        if len(open_positions) >= max_concurrent:
            skipped_concurrent += 1
            continue
        if any(p.get("ticker") == ticker for p in open_positions):
            overlap_skips[strat] = overlap_skips.get(strat, 0) + 1
            continue

        # Effective hold (apply t1 stop)
        t1 = r["next_day_return"]
        t5 = r.get("five_day_return")
        eff_hold = hold_days
        if hold_days == 1:
            ret = t1
        elif t1_stop is not None and t1 is not None and t1 < t1_stop:
            ret = t1
            eff_hold = 1
        else:
            ret = t5

        if ret is None:
            continue
        if side == "short":
            ret = -ret

        conf = r.get("direction_confidence") or 0.5
        risk_pct = 0.03 if conf >= 0.7 else (0.02 if conf >= 0.5 else 0.01)
        position_value = min(equity * risk_pct / stop_distance_pct,
                             equity * max_position_pct)
        pnl = position_value * ret

        try:
            y, m, day = (int(p) for p in date.split("-"))
            exit_date = (_date(y, m, day) + timedelta(days=eff_hold)).isoformat()
        except Exception:
            exit_date = date
        open_positions.append({
            "exit_date": exit_date, "ticker": ticker,
            "pnl_pending": pnl, "strategy": strat,
        })
        trades_by_strat[strat] = trades_by_strat.get(strat, 0) + 1

    for p in open_positions:
        equity += p["pnl_pending"]

    return {
        "starting_equity": starting_equity,
        "ending_equity": round(equity, 2),
        "total_return_pct": round((equity - starting_equity) / starting_equity * 100, 2),
        "peak_equity": round(peak, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades": sum(trades_by_strat.values()),
        "trades_by_strategy": trades_by_strat,
        "overlap_skips": overlap_skips,
        "skipped_concurrent": skipped_concurrent,
    }


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--max-concurrent", type=int, default=5)
    args = p.parse_args()

    rows = build_strategy_rows()
    print(f"Total candidate rows across 4 validated strategies: {len(rows)}", file=sys.stderr)
    by_strat = {}
    for r in rows:
        by_strat.setdefault(r["strategy"], 0)
        by_strat[r["strategy"]] += 1
    for s, n in sorted(by_strat.items()):
        print(f"  {s:<20} {n:>5}", file=sys.stderr)

    print()
    print("=" * 80)
    print(f"FULL VALIDATED STACK ($36K, max {args.max_concurrent} concurrent)")
    print("=" * 80)
    sim = simulate_combined(rows, max_concurrent=args.max_concurrent)
    print(f"Starting equity     : ${sim['starting_equity']:,.0f}")
    print(f"Ending equity       : ${sim['ending_equity']:,.0f}")
    print(f"Total return        : {sim['total_return_pct']:+.2f}%")
    print(f"Peak equity         : ${sim['peak_equity']:,.0f}")
    print(f"Max drawdown        : {sim['max_drawdown_pct']:.2f}%")
    print(f"Total trades taken  : {sim['total_trades']}")
    print()
    print("Trades by strategy (after concurrent + ticker overlap):")
    for s, n in sorted(sim['trades_by_strategy'].items(), key=lambda kv: -kv[1]):
        print(f"  {s:<20} {n:>5}")
    print()
    print("Ticker overlap skips (same ticker already held):")
    for s, n in sorted(sim['overlap_skips'].items(), key=lambda kv: -kv[1]):
        if n > 0:
            print(f"  {s:<20} {n:>5}")
    print(f"\nSkipped by concurrent limit: {sim['skipped_concurrent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
