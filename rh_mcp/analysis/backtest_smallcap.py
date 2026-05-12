"""Backtest harness for the small-cap thesis stack.

Operates over the existing 8-K reaction corpus (~/.edgar_cache/8k_history.csv)
and augments each row with current fundamentals + float metrics from yfinance.
Then runs the same row set through each filter (none / quality / float ≤ 100M
/ both) and reports win rate, average return, and the equity-curve impact of
each filter combination.

Caveats baked into the design:
- yfinance fundamentals are CURRENT, not point-in-time. Margins shift slowly
  for >$300M caps so the look-ahead is small but real. Conclusions should be
  treated as directional, not precise.
- Float is also current. For names that did secondary offerings during the
  lookback period, today's float is larger than it was at the filing date —
  meaning our "ratio" is conservative (under-counts rotation).
- Liquidity guard is a runtime check on the live order book; it cannot be
  backtested. Excluded from the ablation.

Usage:
    python -m rh_mcp.analysis.backtest_smallcap

Outputs a printed summary table + writes ~/.edgar_cache/backtest_smallcap.csv
with each row's filter eligibility flags + returns for downstream analysis.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from statistics import mean, median

try:
    import yfinance as yf
except ImportError as e:
    raise SystemExit("backtest_smallcap requires yfinance — pip install yfinance") from e

from rh_mcp.analysis import edgar_history_builder as _builder
from rh_mcp.analysis import quality_filter as _qf

DEFAULT_CORPUS = _builder.DEFAULT_CSV_PATH
DEFAULT_OUTPUT = _builder.DEFAULT_CSV_PATH.parent / "backtest_smallcap.csv"

SMALL_CAP_MAX = 3_000_000_000
MAX_FLOAT = 100_000_000


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_corpus(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"corpus not found at {path}. Run build_8k_history first.")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            if r["next_day_return"] is None:
                continue
            rows.append(r)
    return rows


def _enrich_ticker(ticker: str, _cache: dict) -> dict:
    """Pull current fundamentals + float once per ticker, cache."""
    t = ticker.upper()
    if t in _cache:
        return _cache[t]
    out = {
        "market_cap": None, "float_shares": None, "is_small_cap": False,
        "is_low_float": False, "is_quality": False,
        "gross_margin": None, "roe": None,
    }
    try:
        info = yf.Ticker(t).info
        mc = _to_float(info.get("marketCap"))
        flt = _to_float(info.get("floatShares"))
        out["market_cap"] = mc
        out["float_shares"] = flt
        out["is_small_cap"] = (mc is not None and 0 < mc <= SMALL_CAP_MAX)
        out["is_low_float"] = (flt is not None and 0 < flt <= MAX_FLOAT)
        out["gross_margin"] = _to_float(info.get("grossMargins"))
        out["roe"] = _to_float(info.get("returnOnEquity"))
    except Exception:
        pass
    try:
        q = _qf.check(t)
        out["is_quality"] = bool(q.get("is_quality"))
    except Exception:
        pass
    _cache[t] = out
    return out


def _stats(returns: list[float]) -> dict:
    if not returns:
        return {"n": 0, "win_rate": None, "avg": None, "median": None, "total": None}
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": len(returns),
        "win_rate": round(wins / len(returns) * 100, 1),
        "avg": round(mean(returns) * 100, 3),
        "median": round(median(returns) * 100, 3),
        "total": round(sum(returns) * 100, 2),
    }


def run(
    corpus_path: Path = DEFAULT_CORPUS,
    output_path: Path = DEFAULT_OUTPUT,
    direction_only: str | None = None,   # 'long' / 'short' / None=all
) -> dict:
    rows = _load_corpus(corpus_path)
    print(f"[backtest] loaded {len(rows)} rows from {corpus_path}", file=sys.stderr)

    # Optional direction filter — only score signals matching a single direction
    # (e.g. 'long' to score the bullish 8-K subset against the small-cap thesis).
    if direction_only in ("long", "short"):
        if direction_only == "long":
            rows = [r for r in rows if r["next_day_return"] is not None and r["next_day_return"] > -10]
        # (no row-level direction label in corpus today; this hook is here for
        # future when scan_8k stamps a direction column into 8k_history.csv)

    # Cache enrichment by ticker
    ticker_cache: dict = {}

    enriched: list[dict] = []
    for i, r in enumerate(rows):
        if (i + 1) % 200 == 0:
            print(f"[backtest] enriched {i + 1}/{len(rows)}", file=sys.stderr)
        meta = _enrich_ticker(r["ticker"], ticker_cache)
        r.update(meta)
        enriched.append(r)

    # Ablations: combinations of {small_cap, low_float, quality}
    # 'all' is the baseline (no filter at all).
    buckets = {
        "all": enriched,
        "small_cap": [r for r in enriched if r["is_small_cap"]],
        "low_float": [r for r in enriched if r["is_low_float"]],
        "small_cap+low_float": [r for r in enriched if r["is_small_cap"] and r["is_low_float"]],
        "small_cap+quality": [r for r in enriched if r["is_small_cap"] and r["is_quality"]],
        "small_cap+low_float+quality": [
            r for r in enriched
            if r["is_small_cap"] and r["is_low_float"] and r["is_quality"]
        ],
        "quality_only": [r for r in enriched if r["is_quality"]],
    }

    print()
    print(f"{'Filter':<32} {'N':>5} {'Win%':>6} {'AvgR%':>7} {'MedR%':>7} {'TotR%':>8}")
    print("-" * 70)
    rollup: dict[str, dict] = {}
    for label, rs in buckets.items():
        t1 = [r["next_day_return"] for r in rs if r["next_day_return"] is not None]
        s = _stats(t1)
        rollup[label] = s
        if s["n"] == 0:
            print(f"{label:<32} {0:>5} {'—':>6} {'—':>7} {'—':>7} {'—':>8}")
        else:
            print(f"{label:<32} {s['n']:>5} {s['win_rate']:>6} {s['avg']:>+7.3f} {s['median']:>+7.3f} {s['total']:>+8.2f}")

    # Write per-row CSV for further analysis
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    extra = ["is_small_cap", "is_low_float", "is_quality", "market_cap", "float_shares", "gross_margin", "roe"]
    fields = [f for f in fields if f not in extra] + extra
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in enriched:
            writer.writerow({k: r.get(k) for k in fields})
    print(f"\n[backtest] wrote per-row CSV → {output_path}", file=sys.stderr)
    return {"success": True, "rows": len(enriched), "rollup": rollup, "output": str(output_path)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Small-cap thesis backtest")
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Input 8-K history CSV")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Per-row output CSV")
    p.add_argument("--direction", choices=("long", "short"), default=None,
                   help="Filter to a single 8-K direction (placeholder — corpus lacks direction column)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    run(Path(args.corpus), Path(args.output), direction_only=args.direction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
