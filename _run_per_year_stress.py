"""Per-year breakdown of all validated strategies — 8K bullish/bearish from
the 4yr enriched corpus, FRD/gap-and-go from the 4yr momentum signals.

Same equity-curve sim, $36K reset each year, framework risk rules.
"""
import csv
import sys
from pathlib import Path
from statistics import mean

from rh_mcp.analysis import backtest_smallcap as _bt

EIGHTK_CSV = Path("C:/Users/algee/.edgar_cache/backtest_smallcap_4yr.csv")
MOMENTUM_CSV = Path("C:/Users/algee/.edgar_cache/momentum_signals_4yr.csv")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    """CSV booleans come back as 'True'/'False' strings."""
    if v in (None, "", "False", "false", "0"):
        return False
    return True


def load_8k(path):
    rows = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence"))
            r["is_small_cap"] = _to_bool(r.get("is_small_cap"))
            r["is_low_float"] = _to_bool(r.get("is_low_float"))
            if r["next_day_return"] is None:
                continue
            rows.append(r)
    return rows


def load_momentum(path):
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


def bucket_by_year(rows):
    by_year = {}
    for r in rows:
        y = (r.get("filing_date") or "")[:4]
        if y and y.isdigit():
            by_year.setdefault(y, []).append(r)
    return by_year


def main():
    eightk = load_8k(EIGHTK_CSV)
    mom = load_momentum(MOMENTUM_CSV)
    print(f"loaded {len(eightk)} 8K rows, {len(mom)} momentum rows", file=sys.stderr)

    eightk_by_year = bucket_by_year(eightk)
    mom_by_year = bucket_by_year(mom)

    all_years = sorted(set(eightk_by_year.keys()) | set(mom_by_year.keys()))
    print(f"years present: {all_years}", file=sys.stderr)

    print()
    print("=" * 110)
    print("Per-year equity sim ($36K reset each year):")
    print("=" * 110)
    print(
        f"{'Year':<6} {'Strategy':<40} {'Trades':>7} {'EndEq':>10} {'Ret%':>8} {'MaxDD%':>7}"
    )
    print("-" * 110)
    for year in all_years:
        eightk_rs = eightk_by_year.get(year, [])
        mom_rs = mom_by_year.get(year, [])

        # 8K strategy variants
        bull_8k = [r for r in eightk_rs if r.get("direction") == "long"]
        bull_8k_lf = [r for r in bull_8k if r["is_small_cap"] and r["is_low_float"]]
        bear_8k_lf = [r for r in eightk_rs if r.get("direction") == "short" and r["is_small_cap"] and r["is_low_float"]]

        # Momentum
        frd = [r for r in mom_rs if r.get("signal_type") == "frd"]
        gag = [r for r in mom_rs if r.get("signal_type") == "gap_and_go"]

        for label, rs, side, hold in (
            ("bullish_8k (all small-cap)", bull_8k, "long", 5),
            ("bullish_8k+lf", bull_8k_lf, "long", 5),
            ("SHORT bearish_8k+lf", bear_8k_lf, "short", 1),
            ("FRD_LONG t5", frd, "long", 5),
            ("FRD_SHORT t1", frd, "short", 1),
            ("gap_and_go t1", gag, "long", 1),
        ):
            if not rs:
                continue
            sim = _bt.simulate_equity_curve(rs, label, hold_days=hold, side=side)
            print(
                f"{year:<6} {label:<40} {sim['trades_taken']:>7} "
                f"${sim['ending_equity']:>9.0f} {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>7.2f}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
