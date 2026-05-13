"""Buyback + PEAD per-year breakdown from the 4yr 8-K corpus.

Two strategies tested:

1. Buyback / dividend_increase: filter 8-K rows where body_keywords contains
   buyback_authorized or dividend_increase. Hold 1d or 5d.

2. PEAD negative-earnings bounce: filter 8-Ks with item code 2.02 where
   t+1 return was negative. Compute drift t+1 -> t+5. Enter at t+1 close,
   ride the partial bounce.

Both run with $36K starting equity, framework risk rules. Per-year breakdown
exposes regime sensitivity. Run after each corpus rebuild to refresh
backtest_data/RESULTS.md.
"""
import csv
import sys
from pathlib import Path
from statistics import mean

from rh_mcp.analysis import backtest_smallcap as bt

CORPUS = Path("C:/Users/algee/.edgar_cache/8k_history_4yr.csv")


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
            r["direction_confidence"] = 0.5  # B-grade default for these signals
            rows.append(r)
    return rows


def by_year(rows):
    out = {}
    for r in rows:
        y = (r.get("filing_date") or "")[:4]
        if y and y.isdigit():
            out.setdefault(y, []).append(r)
    return out


def drift_t1_to_t5(r):
    n, f = r.get("next_day_return"), r.get("five_day_return")
    if n is None or f is None:
        return None
    return (1 + f) / (1 + n) - 1


def stats_line(rs, ret_field):
    rets = [r[ret_field] for r in rs if r.get(ret_field) is not None]
    if not rets:
        return "—"
    wins = sum(1 for r in rets if r > 0)
    return f"N={len(rets)} W%={wins/len(rets)*100:.1f} avg={mean(rets)*100:+.3f}%"


def main():
    print(f"loading {CORPUS}", file=sys.stderr)
    rows = load(CORPUS)
    print(f"  {len(rows)} rows loaded", file=sys.stderr)

    # --- Strategy 1: Buyback / dividend_increase ---
    buyback = [
        r for r in rows
        if "buyback_authorized" in (r.get("body_keywords") or "").split("|")
        or "dividend_increase" in (r.get("body_keywords") or "").split("|")
    ]
    print(f"\nbuyback/dividend_increase rows: {len(buyback)}")

    print()
    print("=" * 75)
    print("Buyback equity sim ($36K reset; small-cap risk rules)")
    print("=" * 75)
    print(f"{'Strategy':<25} {'Hold':>5} {'Trades':>7} {'EndEq':>10} {'Ret%':>8} {'DD%':>6}")
    for hold in (1, 5):
        sim = bt.simulate_equity_curve(buyback, "buyback", hold_days=hold, side="long")
        print(
            f"{'buyback':<25} {hold:>5} {sim['trades_taken']:>7} "
            f"${sim['ending_equity']:>9.0f} {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>6.2f}"
        )

    print()
    print("Buyback per-year (5d hold):")
    print(f"{'Year':<6} {'N':>4} {'W%':>5} {'Ret%':>8} {'DD%':>6}")
    for year, ys in sorted(by_year(buyback).items()):
        sim = bt.simulate_equity_curve(ys, f"buyback_{year}", hold_days=5, side="long")
        rets = [r["five_day_return"] for r in ys if r.get("five_day_return") is not None]
        wins = sum(1 for r in rets if r > 0) if rets else 0
        wp = wins / len(rets) * 100 if rets else 0
        print(f"{year:<6} {len(ys):>4} {wp:>5.1f} {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>6.2f}")

    # --- Strategy 2: PEAD negative-earnings bounce ---
    earnings = [
        r for r in rows
        if "2.02" in (r.get("item_codes") or "").split("|")
        and r.get("next_day_return") is not None
    ]
    negative_reaction = [r for r in earnings if r["next_day_return"] < 0]

    # For PEAD bounce, the "return" is the synthetic t+1 -> t+5 drift
    pead_rows = []
    for r in negative_reaction:
        d = drift_t1_to_t5(r)
        if d is None:
            continue
        rr = dict(r)
        rr["next_day_return"] = d
        rr["five_day_return"] = d
        pead_rows.append(rr)

    print()
    print("=" * 75)
    print(f"PEAD negative-earnings bounce: {len(pead_rows)} signals")
    print("=" * 75)

    # Overall
    sim = bt.simulate_equity_curve(pead_rows, "PEAD_neg_bounce", hold_days=1, side="long")
    drifts = [r["next_day_return"] for r in pead_rows]
    wins = sum(1 for d in drifts if d > 0)
    print(
        f"Overall (4d drift): N={len(pead_rows)} W%={wins/len(pead_rows)*100:.1f} "
        f"avg={mean(drifts)*100:+.3f}% sim={sim['total_return_pct']:+.2f}% DD={sim['max_drawdown_pct']:.2f}%"
    )

    print()
    print("PEAD per-year:")
    print(f"{'Year':<6} {'N':>4} {'W%':>5} {'Avg/trade':>10} {'Ret%':>8} {'DD%':>6}")
    for year, ys in sorted(by_year(pead_rows).items()):
        sim = bt.simulate_equity_curve(ys, f"pead_{year}", hold_days=1, side="long")
        ds = [r["next_day_return"] for r in ys]
        wins = sum(1 for d in ds if d > 0)
        print(
            f"{year:<6} {len(ys):>4} {wins/len(ys)*100:>5.1f} "
            f"{mean(ds)*100:>+9.3f}% {sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>6.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
