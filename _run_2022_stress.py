"""One-shot: load momentum_signals_4yr.csv, slice into year buckets,
run the equity-curve sim on each. Reveals regime-by-regime performance.
"""
import csv
import sys
from pathlib import Path
from statistics import mean, median

from rh_mcp.analysis import backtest_smallcap as _bt

CSV_PATH = Path("C:/Users/algee/.edgar_cache/momentum_signals_4yr.csv")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path):
    rows = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence"))
            if r["next_day_return"] is None:
                continue
            r["filing_date"] = r.get("date")
            rows.append(r)
    return rows


def stats(returns):
    if not returns:
        return (0, None, None, None, None)
    wins = sum(1 for r in returns if r > 0)
    return (
        len(returns),
        round(wins / len(returns) * 100, 1),
        round(mean(returns) * 100, 3),
        round(median(returns) * 100, 3),
        round(sum(returns) * 100, 2),
    )


def main():
    rows = load(CSV_PATH)
    print(f"loaded {len(rows)} momentum signals")
    if not rows:
        return 1

    # Bucket by year-of-signal
    by_year: dict[str, list[dict]] = {}
    for r in rows:
        y = (r["filing_date"] or "")[:4]
        if y:
            by_year.setdefault(y, []).append(r)
    print(f"years present: {sorted(by_year.keys())}")

    # Per-year per-signal stats
    for year in sorted(by_year.keys()):
        rs = by_year[year]
        frd = [r for r in rs if r.get("signal_type") == "frd"]
        gag = [r for r in rs if r.get("signal_type") == "gap_and_go"]
        print(f"\n=== {year} ===  N_frd={len(frd)}  N_gag={len(gag)}")
        for label, group in (("frd", frd), ("gap_and_go", gag)):
            t1 = [r["next_day_return"] for r in group if r["next_day_return"] is not None]
            t5 = [r["five_day_return"] for r in group if r.get("five_day_return") is not None]
            s1 = stats(t1)
            s5 = stats(t5)
            print(
                f"  {label:<12}  t1: N={s1[0]} W={s1[1]} avg={s1[2]}%  t5: N={s5[0]} W={s5[1]} avg={s5[2]}%"
            )

    # Per-year equity sim — FRD_LONG t5 + gap_and_go t1 as the two best validated
    print()
    print("=" * 90)
    print("Per-year equity sim ($36K reset each year):")
    print("=" * 90)
    print(f"{'Year':<6} {'Strategy':<32} {'Trades':>7} {'EndEq':>10} {'Ret%':>8} {'MaxDD%':>7}")
    print("-" * 90)
    for year in sorted(by_year.keys()):
        rs = by_year[year]
        frd = [r for r in rs if r.get("signal_type") == "frd"]
        gag = [r for r in rs if r.get("signal_type") == "gap_and_go"]
        for label, group, side, hold in (
            ("FRD_LONG t5",    frd, "long",  5),
            ("FRD_SHORT t1",   frd, "short", 1),
            ("gap_and_go t1",  gag, "long",  1),
        ):
            sim = _bt.simulate_equity_curve(group, label, hold_days=hold, side=side)
            print(
                f"{year:<6} {label:<32} {sim['trades_taken']:>7} "
                f"${sim['ending_equity']:>9.0f} {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>7.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
