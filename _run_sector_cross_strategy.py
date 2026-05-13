"""Sector breakdown across all four validated strategies.

For each strategy:
- Build the row set (same filter logic as _run_full_stack_backtest.py)
- Pull sector per ticker via yfinance (cached)
- Bucket by sector and report N, Win%, Avg return, Sim return, DD

Goal: determine if Industrials (or any other sector) is a universal drag
or strategy-specific. If universal, the exclude_sectors default applies
across the stack. If different per-strategy, each scanner needs its own
filter.
"""
import csv
import sys
from pathlib import Path
from statistics import mean
from collections import defaultdict

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("requires yfinance")

from rh_mcp.analysis import backtest_smallcap as bt

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


def _load(path):
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


def build_strategy_sets():
    sm = _load(EIGHTK_SMALL)
    md = _load(EIGHTK_MID)
    raw = _load(RAW_8K)

    sm_bull = [r for r in sm if r.get("direction") == "long"
               and r.get("five_day_return") is not None]
    md_bull = [r for r in md if r.get("direction") == "long"
               and r.get("five_day_return") is not None]
    buyback = [r for r in raw if (
        "buyback_authorized" in (r.get("body_keywords") or "").split("|")
        or "dividend_increase" in (r.get("body_keywords") or "").split("|")
    ) and r.get("five_day_return") is not None]
    pead = []
    for r in raw:
        if "2.02" not in (r.get("item_codes") or "").split("|"):
            continue
        t1 = r.get("next_day_return")
        t5 = r.get("five_day_return")
        if t1 is None or t5 is None or t1 >= 0:
            continue
        rr = dict(r)
        drift = (1 + t5) / (1 + t1) - 1
        rr["next_day_return"] = drift
        rr["five_day_return"] = drift
        pead.append(rr)

    return {
        "sm_bullish_8k": sm_bull,
        "mid_bullish_8k": md_bull,
        "buyback": buyback,
        "pead_bounce": pead,
    }


def fetch_sectors(rows_by_strat):
    all_tickers = set()
    for rs in rows_by_strat.values():
        for r in rs:
            all_tickers.add(r["ticker"])
    print(f"fetching sector for {len(all_tickers)} unique tickers...", file=sys.stderr)
    sectors = {}
    for i, t in enumerate(sorted(all_tickers)):
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(all_tickers)}", file=sys.stderr)
        try:
            sectors[t] = (yf.Ticker(t).info or {}).get("sector") or "Unknown"
        except Exception:
            sectors[t] = "Unknown"
    return sectors


def analyze(rows, sectors, label, hold_days=5):
    by_sector = defaultdict(list)
    for r in rows:
        s = sectors.get(r["ticker"], "Unknown")
        by_sector[s].append(r)

    print(f"\n=== {label} (N={len(rows)}) — hold={hold_days}d ===")
    print(f"{'Sector':<28} {'N':>5} {'Win%':>6} {'AvgT5%':>8} {'SimRet%':>8} {'DD%':>6}")
    print("-" * 75)
    # Sort by N descending
    rows_sorted = sorted(by_sector.items(), key=lambda kv: -len(kv[1]))
    drags = []
    for sec, rs in rows_sorted:
        if len(rs) < 5:
            continue
        t5 = [r["five_day_return"] for r in rs]
        wins = sum(1 for r in t5 if r > 0)
        win_pct = wins / len(t5) * 100
        avg = mean(t5) * 100
        # For PEAD use hold=1 since drift is one-period
        h = 1 if "pead" in label.lower() else hold_days
        sim = bt.simulate_equity_curve(rs, sec, hold_days=h, side="long")
        print(f"{sec[:28]:<28} {len(rs):>5} {win_pct:>6.1f} {avg:>+7.3f}% {sim['total_return_pct']:>+7.2f}% {sim['max_drawdown_pct']:>5.2f}")
        if win_pct < 50 and avg < 0.5:
            drags.append(sec)
    if drags:
        print(f"  weak sectors (win<50% AND avg<+0.5%): {', '.join(drags)}")
    else:
        print(f"  no clear drag sectors")


def main():
    sets = build_strategy_sets()
    for k, v in sets.items():
        print(f"{k}: {len(v)} rows", file=sys.stderr)
    sectors = fetch_sectors(sets)
    for label, rows in sets.items():
        analyze(rows, sectors, label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
