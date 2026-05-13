"""Stack backtest: small-cap + mid-cap bullish_8k on one $36K bucket.

Loads both enriched 4yr corpora, filters each to direction=long, tags each
row with its universe, then runs them on a single shared equity bucket
with concurrent-position limits. Surfaces:
  - Standalone returns per universe (sanity check)
  - Combined return (the real number after concurrent-limit competition)
  - Trade counts per universe after the merge
  - Ticker overlap between the two universes' signals
"""
import csv
import sys
from pathlib import Path
from datetime import date as _date, timedelta

from rh_mcp.analysis import backtest_smallcap as bt

SMALLCAP_CSV = Path("C:/Users/algee/.edgar_cache/backtest_smallcap_4yr.csv")
MIDCAP_CSV = Path("C:/Users/algee/.edgar_cache/backtest_midcap_4yr.csv")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path, universe_label):
    rows = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence")) or 0.5
            if r.get("next_day_return") is None:
                continue
            r["universe"] = universe_label
            rows.append(r)
    return rows


def _date_key(s):
    try:
        return tuple(int(p) for p in s.split("-"))
    except Exception:
        return (0, 0, 0)


def simulate_stack(rows, hold_days=5, starting_equity=36_000.0,
                   max_concurrent=5, max_position_pct=0.15,
                   stop_distance_pct=0.03,
                   daily_loss_limit_pct=0.06, weekly_loss_limit_pct=0.10):
    """Walk-forward sim with concurrent limit, shared equity, ticker dedupe.
    Same logic as backtest_combined.simulate_combined but specialized to
    long-only and tracks universe-of-origin for each trade.
    """
    rows = sorted(rows, key=lambda r: _date_key(r.get("filing_date") or ""))

    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    open_positions = []
    trades_by_uni = {}
    overlap_skips = {}
    skipped_concurrent = 0
    skipped_daily = 0
    skipped_weekly = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_date = None
    current_week = None

    def _iso_week(d):
        try:
            y, m, day = (int(p) for p in d.split("-"))
            iso = _date(y, m, day).isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except Exception:
            return d

    for r in rows:
        date = r.get("filing_date") or ""
        if not date:
            continue
        ticker = (r.get("ticker") or "").upper()
        uni = r.get("universe", "?")

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
            skipped_daily += 1
            continue
        if weekly_pnl < -weekly_loss_limit_pct * equity:
            skipped_weekly += 1
            continue
        if len(open_positions) >= max_concurrent:
            skipped_concurrent += 1
            continue
        if any(pp.get("ticker") == ticker for pp in open_positions):
            overlap_skips[uni] = overlap_skips.get(uni, 0) + 1
            continue

        # Sizing
        conf = r.get("direction_confidence") or 0.5
        if conf >= 0.7:
            risk_pct = 0.03
        elif conf >= 0.5:
            risk_pct = 0.02
        else:
            risk_pct = 0.01
        position_value = min(equity * risk_pct / stop_distance_pct,
                             equity * max_position_pct)

        ret = r["next_day_return"] if hold_days == 1 else r["five_day_return"]
        if ret is None:
            continue
        pnl = position_value * ret
        try:
            y, m, day = (int(p) for p in date.split("-"))
            exit_date = (_date(y, m, day) + timedelta(days=hold_days)).isoformat()
        except Exception:
            exit_date = date

        open_positions.append({
            "exit_date": exit_date,
            "ticker": ticker,
            "pnl_pending": pnl,
            "universe": uni,
        })
        trades_by_uni[uni] = trades_by_uni.get(uni, 0) + 1

    for p in open_positions:
        equity += p["pnl_pending"]

    return {
        "starting_equity": starting_equity,
        "ending_equity": round(equity, 2),
        "total_return_pct": round((equity - starting_equity) / starting_equity * 100, 2),
        "peak_equity": round(peak, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades": sum(trades_by_uni.values()),
        "trades_by_universe": trades_by_uni,
        "overlap_skips": overlap_skips,
        "skipped_concurrent": skipped_concurrent,
        "skipped_daily": skipped_daily,
        "skipped_weekly": skipped_weekly,
    }


def main():
    print(f"loading small-cap corpus...", file=sys.stderr)
    sm = load(SMALLCAP_CSV, "small")
    print(f"loading mid-cap corpus...", file=sys.stderr)
    md = load(MIDCAP_CSV, "mid")
    print(f"  small: {len(sm)} rows  mid: {len(md)} rows", file=sys.stderr)

    sm_bull = [r for r in sm if r.get("direction") == "long"]
    md_bull = [r for r in md if r.get("direction") == "long"]
    stack = sm_bull + md_bull

    # Ticker overlap diagnostic — same ticker filing in both universes? (shouldn't,
    # since Finviz cap_small and cap_mid are mutually exclusive ranges)
    sm_tickers = {r["ticker"] for r in sm_bull}
    md_tickers = {r["ticker"] for r in md_bull}
    overlap = sm_tickers & md_tickers
    print(f"\nuniverse overlap: small={len(sm_tickers)} unique tickers, mid={len(md_tickers)}, intersect={len(overlap)}")
    if overlap:
        print(f"  overlapping tickers (likely cap drift across years): {sorted(overlap)[:10]}{'...' if len(overlap) > 10 else ''}")

    print()
    print("=" * 80)
    print("STANDALONE sims (5d hold, $36K each, framework risk rules)")
    print("=" * 80)
    print(f"{'Strategy':<25} {'Trades':>7} {'EndEq':>10} {'Ret%':>8} {'DD%':>6}")
    for label, rs in (("small-cap only", sm_bull), ("mid-cap only", md_bull)):
        sim = bt.simulate_equity_curve(rs, label, hold_days=5, side="long")
        print(f"{label:<25} {sim['trades_taken']:>7} ${sim['ending_equity']:>9.0f} {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>6.2f}")

    print()
    print("=" * 80)
    print("STACK sim (5d hold, ONE $36K bucket, max 5 concurrent across universes)")
    print("=" * 80)
    sim = simulate_stack(stack, hold_days=5)
    print(f"Total return     : {sim['total_return_pct']:+.2f}%  (${sim['starting_equity']:,.0f} → ${sim['ending_equity']:,.0f})")
    print(f"Max drawdown     : {sim['max_drawdown_pct']:.2f}%")
    print(f"Peak equity      : ${sim['peak_equity']:,.0f}")
    print(f"Trades taken     : {sim['total_trades']}")
    print()
    print("Trades by universe (after merge + concurrent limit):")
    for u, n in sorted(sim['trades_by_universe'].items(), key=lambda kv: -kv[1]):
        print(f"  {u:<10} {n:>5}")
    print()
    print("Overlap skips (ticker held by another open trade):")
    for u, n in sorted(sim['overlap_skips'].items(), key=lambda kv: -kv[1]):
        if n > 0:
            print(f"  {u:<10} {n:>5}")
    print()
    print(f"Skipped (concurrent>=5): {sim['skipped_concurrent']}")
    print(f"Skipped (daily limit):   {sim['skipped_daily']}")
    print(f"Skipped (weekly limit):  {sim['skipped_weekly']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
