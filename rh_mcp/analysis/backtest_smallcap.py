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
    """Pull current fundamentals + float once per ticker, cache.

    Computes BOTH strict and loose quality flags. Strict uses the default
    quality_filter thresholds (margin>=30%, ROE>=8%); loose uses margin>=15%
    + profit>=0% only — captures more small-caps where strict over-filters.
    """
    t = ticker.upper()
    if t in _cache:
        return _cache[t]
    out = {
        "market_cap": None, "float_shares": None, "is_small_cap": False,
        "is_low_float": False, "is_quality": False, "is_loose_quality": False,
        "gross_margin": None, "roe": None, "profit_margin": None,
    }
    try:
        info = yf.Ticker(t).info
        mc = _to_float(info.get("marketCap"))
        flt = _to_float(info.get("floatShares"))
        gm = _to_float(info.get("grossMargins"))
        pm = _to_float(info.get("profitMargins"))
        roe = _to_float(info.get("returnOnEquity"))
        out["market_cap"] = mc
        out["float_shares"] = flt
        out["is_small_cap"] = (mc is not None and 0 < mc <= SMALL_CAP_MAX)
        out["is_low_float"] = (flt is not None and 0 < flt <= MAX_FLOAT)
        out["gross_margin"] = gm
        out["roe"] = roe
        out["profit_margin"] = pm
        # Loose quality: just margin>=15% AND profit_margin>=0% (no ROE / DE)
        out["is_loose_quality"] = (
            gm is not None and gm >= 0.15
            and pm is not None and pm >= 0.0
        )
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


def spy_benchmark(start_date: str, end_date: str, starting_equity: float = 36_000.0) -> dict:
    """Compute SPY buy-and-hold return over the given date window via yfinance.

    Returns same shape as simulate_equity_curve for direct comparison: ending
    equity, total return %, peak equity, max drawdown %.
    """
    try:
        hist = yf.Ticker("SPY").history(start=start_date, end=end_date, auto_adjust=True)
    except Exception as e:
        return {"label": "SPY_buy_and_hold", "error": str(e)}
    if hist is None or hist.empty:
        return {"label": "SPY_buy_and_hold", "error": "no SPY data"}
    closes = hist["Close"].dropna().tolist()
    if len(closes) < 2:
        return {"label": "SPY_buy_and_hold", "error": "insufficient data"}
    shares = starting_equity / closes[0]
    equity_series = [shares * c for c in closes]
    ending = equity_series[-1]
    peak = max(equity_series)
    # Running max drawdown
    max_dd = 0.0
    running_max = equity_series[0]
    for v in equity_series:
        if v > running_max:
            running_max = v
        dd = (running_max - v) / running_max * 100 if running_max > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return {
        "label": "SPY_buy_and_hold",
        "starting_equity": starting_equity,
        "ending_equity": round(ending, 2),
        "total_return_pct": round((ending - starting_equity) / starting_equity * 100, 2),
        "peak_equity": round(peak, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "hold_days": "—",
        "trades_taken": "—",
    }


def _date_key(date_str: str) -> tuple:
    """Sort key turning 'YYYY-MM-DD' into a comparable tuple."""
    try:
        return tuple(int(p) for p in date_str.split("-"))
    except Exception:
        return (0, 0, 0)


def simulate_equity_curve(
    rows: list[dict],
    label: str,
    starting_equity: float = 36_000.0,
    hold_days: int = 1,
    stop_distance_pct: float = 0.03,
    daily_loss_limit_pct: float = 0.06,
    weekly_loss_limit_pct: float = 0.10,
    account_floor: float = 3_000.0,
    max_concurrent: int = 5,
    max_position_pct: float = 0.15,
    side: str = "long",
) -> dict:
    """Walk-forward equity simulator using the small-cap risk framework.

    Rules embedded (from ROBINHOOD.md):
    - Risk per trade tiered by direction_confidence proxy for grade:
        conf >= 0.7 → A+ at 3% risk (small-cap A+)
        conf >= 0.5 → A  at 2% risk
        else        → B  at 1% risk
    - Stop distance: 3% of entry (small-cap default, approximation)
    - Max position: 15% of equity
    - Daily loss limit: -6% → halt new entries that day
    - Weekly loss limit: -10% → halt new entries until next week
    - Account floor: pause if equity < $3K
    - Concurrent open positions: max 5

    `hold_days=1` exits at t1_close; `hold_days=5` exits at t5_close.

    Returns dict with equity-curve, total return, max DD, trades taken/skipped.
    """
    rows = [r for r in rows if r.get("filing_date")]
    rows.sort(key=lambda r: _date_key(r["filing_date"]))

    equity = starting_equity
    peak_equity = starting_equity
    max_drawdown_pct = 0.0
    trades_taken = 0
    skipped_daily = 0
    skipped_weekly = 0
    skipped_concurrent = 0
    skipped_floor = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_date = None
    current_week = None
    open_positions: list[dict] = []   # each: {close_after_date, pnl_pending}
    equity_history: list[tuple] = []

    def _iso_week(d: str) -> str:
        try:
            from datetime import date as _d
            y, m, day = (int(p) for p in d.split("-"))
            iso = _d(y, m, day).isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except Exception:
            return d

    for r in rows:
        date = r["filing_date"]

        # Close any positions whose holding period has elapsed
        # (approximate: hold_days calendar days after filing date)
        still_open = []
        for pos in open_positions:
            if _date_key(date) >= _date_key(pos["exit_date"]):
                equity += pos["pnl_pending"]
                daily_pnl += pos["pnl_pending"]
                weekly_pnl += pos["pnl_pending"]
            else:
                still_open.append(pos)
        open_positions = still_open

        # Day / week boundary resets
        if date != current_date:
            current_date = date
            daily_pnl = 0.0
            week = _iso_week(date)
            if week != current_week:
                current_week = week
                weekly_pnl = 0.0

        # Track drawdown each day
        if equity > peak_equity:
            peak_equity = equity
        dd = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd
        equity_history.append((date, round(equity, 2)))

        # Skip checks
        if equity < account_floor:
            skipped_floor += 1
            continue
        if daily_pnl < -daily_loss_limit_pct * equity:
            skipped_daily += 1
            continue
        if weekly_pnl < -weekly_loss_limit_pct * equity:
            skipped_weekly += 1
            continue
        if len(open_positions) >= max_concurrent:
            skipped_concurrent += 1
            continue

        # Grade from direction_confidence
        conf = 0.0
        try:
            conf = float(r.get("direction_confidence") or 0)
        except Exception:
            conf = 0.0
        if conf >= 0.7:
            risk_pct = 0.03  # A+
        elif conf >= 0.5:
            risk_pct = 0.02  # A
        else:
            risk_pct = 0.01  # B

        # Pick return based on hold period
        ret = r.get("next_day_return") if hold_days == 1 else r.get("five_day_return")
        if ret is None:
            continue
        # Short side: invert the return so a -2% drop becomes a +2% gain
        if side == "short":
            ret = -ret

        # Position sizing: risk_dollars / stop_distance = position_value
        risk_dollars = equity * risk_pct
        position_value = risk_dollars / stop_distance_pct
        position_value = min(position_value, equity * max_position_pct)
        pnl = position_value * ret
        # Add to open positions; closed at exit_date
        from datetime import date as _d, timedelta
        try:
            y, m, day = (int(p) for p in date.split("-"))
            exit_date = (_d(y, m, day) + timedelta(days=hold_days)).isoformat()
        except Exception:
            exit_date = date
        open_positions.append({
            "exit_date": exit_date,
            "pnl_pending": pnl,
        })
        trades_taken += 1

    # Close any remaining open positions at face value (assume they'd resolve)
    for pos in open_positions:
        equity += pos["pnl_pending"]

    total_return_pct = (equity - starting_equity) / starting_equity * 100
    return {
        "label": label,
        "starting_equity": starting_equity,
        "ending_equity": round(equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "peak_equity": round(peak_equity, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "hold_days": hold_days,
        "trades_taken": trades_taken,
        "skipped_daily_limit": skipped_daily,
        "skipped_weekly_limit": skipped_weekly,
        "skipped_concurrent": skipped_concurrent,
        "skipped_floor": skipped_floor,
        "equity_history_len": len(equity_history),
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

    # Helpers for direction-aware bucketing
    has_direction = bool(enriched) and "direction" in enriched[0]
    bullish = [r for r in enriched if r.get("direction") == "long"]
    bearish = [r for r in enriched if r.get("direction") == "short"]

    # Ablations: combinations of {direction, small_cap, low_float, quality}
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
    if has_direction:
        buckets.update({
            "bullish_8k": bullish,
            "bearish_8k": bearish,
            "bullish_8k+small_cap": [r for r in bullish if r["is_small_cap"]],
            "bullish_8k+low_float": [r for r in bullish if r["is_low_float"]],
            "bullish_8k+small_cap+low_float": [
                r for r in bullish if r["is_small_cap"] and r["is_low_float"]
            ],
            "bullish_8k+small_cap+low_float+quality": [
                r for r in bullish
                if r["is_small_cap"] and r["is_low_float"] and r["is_quality"]
            ],
            "bullish_8k+small_cap+low_float+loose_q": [
                r for r in bullish
                if r["is_small_cap"] and r["is_low_float"] and r.get("is_loose_quality")
            ],
            "bearish_8k+small_cap": [r for r in bearish if r["is_small_cap"]],
            "bearish_8k+low_float": [r for r in bearish if r["is_low_float"]],
            "bearish_8k+small_cap+low_float": [
                r for r in bearish if r["is_small_cap"] and r["is_low_float"]
            ],
        })

    print()
    print(
        f"{'Filter':<40} {'N':>5} "
        f"{'t1_W%':>6} {'t1_Avg':>7} {'t1_Tot':>8}  "
        f"{'t5_W%':>6} {'t5_Avg':>7} {'t5_Tot':>8}"
    )
    print("-" * 100)
    rollup: dict[str, dict] = {}
    for label, rs in buckets.items():
        t1 = [r["next_day_return"] for r in rs if r["next_day_return"] is not None]
        t5 = [r["five_day_return"] for r in rs if r.get("five_day_return") is not None]
        s1 = _stats(t1)
        s5 = _stats(t5)
        rollup[label] = {"t1": s1, "t5": s5}
        if s1["n"] == 0:
            print(f"{label:<40} {0:>5} {'—':>6} {'—':>7} {'—':>8}  {'—':>6} {'—':>7} {'—':>8}")
        else:
            t5_w = f"{s5['win_rate']:>6}" if s5["n"] > 0 else f"{'—':>6}"
            t5_a = f"{s5['avg']:>+7.3f}" if s5["n"] > 0 else f"{'—':>7}"
            t5_t = f"{s5['total']:>+8.2f}" if s5["n"] > 0 else f"{'—':>8}"
            print(
                f"{label:<40} {s1['n']:>5} "
                f"{s1['win_rate']:>6} {s1['avg']:>+7.3f} {s1['total']:>+8.2f}  "
                f"{t5_w} {t5_a} {t5_t}"
            )

    # ----------------- Equity curve simulation -----------------
    print()
    print("=" * 105)
    print("Equity-curve simulation: $36K start, small-cap framework risk rules")
    print("=" * 105)
    sim_buckets = [
        ("bullish_8k+small_cap+low_float", buckets.get("bullish_8k+small_cap+low_float", []), "long"),
        ("bullish_8k+small_cap+low_float+quality", buckets.get("bullish_8k+small_cap+low_float+quality", []), "long"),
        ("bullish_8k+small_cap+low_float+loose_q", buckets.get("bullish_8k+small_cap+low_float+loose_q", []), "long"),
        ("bullish_8k", buckets.get("bullish_8k", []), "long"),
        ("SHORT_bearish_8k+small_cap+low_float", buckets.get("bearish_8k+small_cap+low_float", []), "short"),
        ("SHORT_bearish_8k+low_float", buckets.get("bearish_8k+low_float", []), "short"),
    ]
    print(
        f"{'Strategy':<45} {'Side':>5} {'Hold':>5} {'Trades':>7} "
        f"{'EndEq':>11} {'Ret%':>8} {'PeakEq':>11} {'MaxDD%':>7}"
    )
    print("-" * 105)
    sim_results: list[dict] = []
    for label, rs, side in sim_buckets:
        # Short side: only t+1 holds (per finding that bearish t+5 reverts positive)
        holds = (1,) if side == "short" else (1, 5)
        for hold in holds:
            sim = simulate_equity_curve(rs, label, hold_days=hold, side=side)
            sim_results.append(sim)
            print(
                f"{label:<45} {side:>5} {hold:>5} {sim['trades_taken']:>7} "
                f"${sim['ending_equity']:>10.0f} {sim['total_return_pct']:>+8.2f} "
                f"${sim['peak_equity']:>10.0f} {sim['max_drawdown_pct']:>7.2f}"
            )

    # ----------------- SPY benchmark -----------------
    dated_rows = [r for r in enriched if r.get("filing_date")]
    if dated_rows:
        dated_rows.sort(key=lambda r: _date_key(r["filing_date"]))
        start_d = dated_rows[0]["filing_date"]
        end_d = dated_rows[-1]["filing_date"]
        spy = spy_benchmark(start_d, end_d)
        if "error" not in spy:
            print(
                f"{'SPY_buy_and_hold (' + start_d + '→' + end_d + ')':<45} "
                f"{'long':>5} {'B&H':>5} {'—':>7} "
                f"${spy['ending_equity']:>10.0f} {spy['total_return_pct']:>+8.2f} "
                f"${spy['peak_equity']:>10.0f} {spy['max_drawdown_pct']:>7.2f}"
            )
        else:
            print(f"SPY benchmark unavailable: {spy['error']}")

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
