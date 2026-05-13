"""Full validated stack with REAL margin modeling.

Same four strategies as _run_full_stack_backtest.py (sm_bullish_8k +
mid_bullish_8k + buyback + pead_bounce), but the simulator now:

  - Starts from user's actual equity (default $36,541.78)
  - Has a margin_limit (default $35,000 — the user's RH setting)
  - When a position requires capital > available cash, uses margin
  - Tracks margin_used across positions, blocks new trades when
    margin_used + new_margin would exceed margin_limit
  - Charges 8% APR interest on the borrowed portion, paid at close
  - Increases per-position cap to 25% (small-cap reduction off — with
    margin available, the user can size up to the framework cap)

Effective expansion: $36.5K equity + $35K margin = $71.5K of deployable
buying power, but with real interest cost per day the position is open.
"""
import csv
import sys
from pathlib import Path
from datetime import date as _date, timedelta

EIGHTK_SMALL = Path("C:/Users/algee/.edgar_cache/backtest_smallcap_4yr.csv")
EIGHTK_MID = Path("C:/Users/algee/.edgar_cache/backtest_midcap_4yr.csv")
RAW_8K = Path("C:/Users/algee/.edgar_cache/8k_history_4yr.csv")

REAL_EQUITY = 36_541.78
REAL_MARGIN_LIMIT = 35_000.00
MARGIN_APR = 0.08
DAILY_RATE = MARGIN_APR / 365


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_enriched(path):
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


def _parse_date(s):
    try:
        y, m, d = (int(p) for p in s.split("-"))
        return _date(y, m, d)
    except Exception:
        return None


def build_strategy_rows():
    sm = _load_enriched(EIGHTK_SMALL)
    md = _load_enriched(EIGHTK_MID)
    raw = _load_raw(RAW_8K)

    out = []
    for r in sm:
        if r.get("direction") != "long":
            continue
        rr = dict(r)
        rr.update({"strategy": "sm_bullish_8k", "hold_days": 5, "t1_stop_pct": -0.05, "side": "long"})
        out.append(rr)
    for r in md:
        if r.get("direction") != "long":
            continue
        rr = dict(r)
        rr.update({"strategy": "mid_bullish_8k", "hold_days": 5, "t1_stop_pct": None, "side": "long"})
        out.append(rr)
    for r in raw:
        kws = (r.get("body_keywords") or "").split("|")
        if "buyback_authorized" not in kws and "dividend_increase" not in kws:
            continue
        rr = dict(r)
        rr.update({"strategy": "buyback", "hold_days": 5, "t1_stop_pct": None, "side": "long"})
        out.append(rr)
    for r in raw:
        if "2.02" not in (r.get("item_codes") or "").split("|"):
            continue
        t1 = r.get("next_day_return")
        t5 = r.get("five_day_return")
        if t1 is None or t5 is None or t1 >= 0:
            continue
        drift = (1 + t5) / (1 + t1) - 1
        rr = dict(r)
        rr["next_day_return"] = drift
        rr["five_day_return"] = drift
        rr.update({"strategy": "pead_bounce", "hold_days": 1, "t1_stop_pct": None, "side": "long"})
        out.append(rr)
    return out


def simulate_with_margin(
    rows,
    starting_equity=REAL_EQUITY,
    margin_limit=REAL_MARGIN_LIMIT,
    max_concurrent=5,
    max_position_pct=0.25,           # framework cap (not the small-cap 0.15)
    stop_distance_pct=0.03,
    daily_loss_limit_pct=0.06,
    weekly_loss_limit_pct=0.10,
    margin_apr=MARGIN_APR,
    use_margin=True,
    slippage_per_trade=0.0,          # decimal (0.004 = 0.4% round-trip)
):
    """Walk forward with cash + margin accounting. Each position is funded
    from cash first; shortfall uses margin (subject to margin_limit).
    Margin interest accrues per day the borrowed capital is outstanding,
    deducted from the trade's proceeds at close.
    """
    rows = sorted(rows, key=lambda r: _date_key(r.get("filing_date") or ""))
    daily_rate = margin_apr / 365

    cash = starting_equity
    margin_used = 0.0
    peak_equity = starting_equity
    max_dd_pct = 0.0
    open_positions = []
    trades_by_strat = {}
    skipped_concurrent = 0
    skipped_margin = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_date = None
    current_week = None
    total_interest_paid = 0.0

    def equity_now():
        # cash + open positions at entry value (we don't mark-to-market here
        # because each row's return is fixed at hold horizon; close-out PnL
        # is realized when the position exits)
        return cash + sum(p["position_value"] for p in open_positions) - margin_used

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
                ret = p["return_pct"]
                # Close P&L at hold horizon
                proceeds = p["position_value"] * (1 + ret)
                # Interest on borrowed portion for the days held
                days_held = max(1, p["days_held"])
                interest = p["margin_at_entry"] * days_held * daily_rate
                total_interest_paid += interest
                proceeds -= interest
                # Pay back margin first
                pay_margin = min(p["margin_at_entry"], proceeds)
                margin_used -= pay_margin
                margin_used = max(0, margin_used)
                cash += proceeds - pay_margin
                # Track P&L for daily/weekly limits
                trade_pnl = proceeds - p["position_value"]  # net of interest
                daily_pnl += trade_pnl
                weekly_pnl += trade_pnl
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

        eq = equity_now()
        if eq > peak_equity:
            peak_equity = eq
        dd = (peak_equity - eq) / peak_equity * 100 if peak_equity > 0 else 0
        if dd > max_dd_pct:
            max_dd_pct = dd

        # Skips
        if daily_pnl < -daily_loss_limit_pct * eq:
            continue
        if weekly_pnl < -weekly_loss_limit_pct * eq:
            continue
        if len(open_positions) >= max_concurrent:
            skipped_concurrent += 1
            continue
        if any(p["ticker"] == ticker for p in open_positions):
            continue

        # Sizing — based on EQUITY (not buying power), per framework
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
        # Slippage applied as a one-time round-trip drag on the realized return
        ret -= slippage_per_trade

        conf = r.get("direction_confidence") or 0.5
        risk_pct = 0.05 if conf >= 0.7 else (0.03 if conf >= 0.5 else 0.015)
        position_value = min(eq * risk_pct / stop_distance_pct,
                             eq * max_position_pct)

        # Margin check
        margin_needed = max(0.0, position_value - cash)
        if not use_margin:
            if margin_needed > 0:
                continue
        else:
            if margin_used + margin_needed > margin_limit:
                skipped_margin += 1
                continue

        # Deduct
        cash_used = min(cash, position_value)
        cash -= cash_used
        margin_used += margin_needed
        try:
            y, m, day = (int(p) for p in date.split("-"))
            exit_date = (_date(y, m, day) + timedelta(days=eff_hold)).isoformat()
        except Exception:
            exit_date = date
        open_positions.append({
            "exit_date": exit_date,
            "ticker": ticker,
            "strategy": strat,
            "position_value": position_value,
            "return_pct": ret,
            "margin_at_entry": margin_needed,
            "days_held": eff_hold,
        })
        trades_by_strat[strat] = trades_by_strat.get(strat, 0) + 1

    # Close any remaining
    for p in open_positions:
        ret = p["return_pct"]
        proceeds = p["position_value"] * (1 + ret)
        interest = p["margin_at_entry"] * max(1, p["days_held"]) * daily_rate
        total_interest_paid += interest
        proceeds -= interest
        pay_margin = min(p["margin_at_entry"], proceeds)
        margin_used -= pay_margin
        margin_used = max(0, margin_used)
        cash += proceeds - pay_margin

    ending_equity = cash + sum(p["position_value"] for p in open_positions) - margin_used
    return {
        "starting_equity": starting_equity,
        "margin_limit": margin_limit,
        "ending_equity": round(ending_equity, 2),
        "total_return_pct": round((ending_equity - starting_equity) / starting_equity * 100, 2),
        "peak_equity": round(peak_equity, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "total_trades": sum(trades_by_strat.values()),
        "trades_by_strategy": trades_by_strat,
        "total_interest_paid": round(total_interest_paid, 2),
        "skipped_concurrent": skipped_concurrent,
        "skipped_margin_limit": skipped_margin,
        "use_margin": use_margin,
    }


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--equity", type=float, default=REAL_EQUITY)
    p.add_argument("--margin-limit", type=float, default=REAL_MARGIN_LIMIT)
    p.add_argument("--max-concurrent", type=int, default=5)
    p.add_argument("--no-margin", action="store_true", help="Cash-only baseline")
    p.add_argument("--slippage", type=float, default=0.0,
                   help="Round-trip slippage per trade as decimal (0.004 = 0.4 percent)")
    args = p.parse_args()

    rows = build_strategy_rows()
    print(f"Total candidate rows: {len(rows)}", file=sys.stderr)

    print()
    print("=" * 80)
    print(f"REAL ACCOUNT BACKTEST")
    print(f"  Starting equity: ${args.equity:,.2f}")
    print(f"  Margin limit:    ${args.margin_limit:,.2f}")
    print(f"  Max concurrent:  {args.max_concurrent}")
    print(f"  Use margin:      {not args.no_margin}")
    print("=" * 80)

    sim = simulate_with_margin(
        rows,
        starting_equity=args.equity,
        margin_limit=args.margin_limit,
        max_concurrent=args.max_concurrent,
        use_margin=not args.no_margin,
        slippage_per_trade=args.slippage,
    )
    print(f"Ending equity        : ${sim['ending_equity']:,.2f}")
    print(f"Total return         : {sim['total_return_pct']:+.2f}%")
    print(f"Peak equity          : ${sim['peak_equity']:,.2f}")
    print(f"Max drawdown         : {sim['max_drawdown_pct']:.2f}%")
    print(f"Total trades         : {sim['total_trades']}")
    print(f"Total interest paid  : ${sim['total_interest_paid']:,.2f}")
    print()
    print("Trades by strategy:")
    for s, n in sorted(sim['trades_by_strategy'].items(), key=lambda kv: -kv[1]):
        print(f"  {s:<20} {n:>5}")
    print()
    print(f"Skipped (concurrent limit): {sim['skipped_concurrent']}")
    print(f"Skipped (margin limit):     {sim['skipped_margin_limit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
