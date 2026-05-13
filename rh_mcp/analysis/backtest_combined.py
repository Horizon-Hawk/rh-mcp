"""Combined backtest — merges all validated small-cap signals onto a single
equity bucket, respects concurrent-position limits, and tracks trade overlap
between strategies to expose double-counting.

Three signal sources combined:
1. 8-K bullish + small_cap + low_float (long, 5-day hold) — from
   ~/.edgar_cache/8k_history.csv
2. 8-K bearish + small_cap + low_float (short, 1-day hold) — same CSV
3. FRD LONG (bounce) — from ~/.edgar_cache/momentum_signals_smallcap_30pct.csv
   (or whatever momentum_backtest last wrote)
4. Gap-and-go 20% (long, 5-day hold on small-cap, 1-day on penny) — same momentum CSV

Why this matters: the individual backtests showed +118% (FRD-LONG t5) and
+62% (8K-bullish t5) on the small-cap universe. Both detect catalyst-driven
runs on small caps with low float — they very likely fire on the SAME
underlying stocks within days of each other. The combined backtest answers:
when capital is shared and concurrent-position limits enforce real-world
exclusion, what's the realistic blended return?

Run: python -m rh_mcp.analysis.backtest_combined
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date as _date, timedelta
from pathlib import Path
from statistics import mean

try:
    import yfinance as yf
except ImportError as e:
    raise SystemExit("backtest_combined requires yfinance") from e

from rh_mcp.analysis import backtest_smallcap as _bt
from rh_mcp.analysis import edgar_client as _edgar


DEFAULT_8K_CSV = _edgar.CACHE_DIR / "8k_history.csv"
DEFAULT_MOMENTUM_CSV = _edgar.CACHE_DIR / "momentum_signals.csv"

# Signal definitions — each is (label, source, filter_fn, side, hold_days)
# filter_fn takes an enriched row and returns True if it passes the bucket criteria.


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_8k_corpus(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence"))
            if r.get("next_day_return") is None:
                continue
            rows.append(r)
    return rows


def _load_momentum_corpus(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r["next_day_return"] = _to_float(r.get("next_day_return"))
            r["five_day_return"] = _to_float(r.get("five_day_return"))
            r["direction_confidence"] = _to_float(r.get("direction_confidence"))
            if r.get("next_day_return") is None:
                continue
            # Momentum signals use 'date' as the column; standardize to filing_date
            r["filing_date"] = r.get("date")
            rows.append(r)
    return rows


def _enrich_ticker_cache(rows: list[dict]) -> dict[str, dict]:
    """One yfinance call per unique ticker for market cap and float."""
    cache: dict[str, dict] = {}
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    for i, t in enumerate(tickers):
        if (i + 1) % 100 == 0:
            print(f"[combined] enrich {i + 1}/{len(tickers)}", file=sys.stderr)
        try:
            info = yf.Ticker(t).info
            cache[t] = {
                "market_cap": _to_float(info.get("marketCap")),
                "float_shares": _to_float(info.get("floatShares")),
            }
        except Exception:
            cache[t] = {"market_cap": None, "float_shares": None}
    return cache


def _filter_8k_bullish_smallcap_lf(rows: list[dict], cache: dict) -> list[dict]:
    out = []
    for r in rows:
        if r.get("direction") != "long":
            continue
        meta = cache.get(r["ticker"], {})
        mc = meta.get("market_cap")
        flt = meta.get("float_shares")
        if not (mc and 0 < mc <= 3_000_000_000):
            continue
        if not (flt and 0 < flt <= 100_000_000):
            continue
        out.append(r)
    return out


def _filter_8k_bearish_smallcap_lf(rows: list[dict], cache: dict) -> list[dict]:
    out = []
    for r in rows:
        if r.get("direction") != "short":
            continue
        meta = cache.get(r["ticker"], {})
        mc = meta.get("market_cap")
        flt = meta.get("float_shares")
        if not (mc and 0 < mc <= 3_000_000_000):
            continue
        if not (flt and 0 < flt <= 100_000_000):
            continue
        out.append(r)
    return out


def _filter_frd_long(rows: list[dict], cache: dict) -> list[dict]:
    return [r for r in rows if r.get("signal_type") == "frd"]


def _filter_gag_long(rows: list[dict], cache: dict) -> list[dict]:
    return [r for r in rows if r.get("signal_type") == "gap_and_go"]


def _date_key(s: str) -> tuple:
    try:
        return tuple(int(p) for p in s.split("-"))
    except Exception:
        return (0, 0, 0)


def _next_trading_day(d: str, days: int) -> str:
    try:
        y, m, day = (int(p) for p in d.split("-"))
        return (_date(y, m, day) + timedelta(days=days)).isoformat()
    except Exception:
        return d


def simulate_combined(
    strategies: list[dict],
    starting_equity: float = 36_000.0,
    daily_loss_limit_pct: float = 0.06,
    weekly_loss_limit_pct: float = 0.10,
    account_floor: float = 3_000.0,
    max_concurrent: int = 5,
    max_position_pct: float = 0.15,
    stop_distance_pct: float = 0.03,
) -> dict:
    """Simulate ALL signals on one shared equity bucket.

    Each strategy entry is:
        {label, rows, side, hold_days}
    Rows are merged, sorted by filing_date, and processed sequentially.
    Concurrent-position limit applies across ALL strategies — a stock held
    long via 8K-bullish blocks an FRD-LONG entry on the same name. This
    surfaces real-world conflicts the per-strategy backtests hid.
    """
    all_signals: list[dict] = []
    for strat in strategies:
        for r in strat["rows"]:
            all_signals.append({
                **r,
                "strategy": strat["label"],
                "side": strat["side"],
                "hold_days": strat["hold_days"],
            })
    all_signals.sort(key=lambda r: _date_key(r.get("filing_date") or r.get("date") or ""))

    equity = starting_equity
    peak_equity = starting_equity
    max_dd_pct = 0.0
    open_positions: list[dict] = []
    trades_by_strategy: dict[str, int] = {}
    skipped_by_strategy: dict[str, int] = {}
    overlap_skips: dict[str, int] = {}   # ticker collision skips
    skipped_daily = 0
    skipped_weekly = 0
    skipped_floor = 0
    skipped_concurrent = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_date = None
    current_week = None

    def _iso_week(d: str) -> str:
        try:
            y, m, day = (int(p) for p in d.split("-"))
            iso = _date(y, m, day).isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except Exception:
            return d

    for sig in all_signals:
        date = sig.get("filing_date") or sig.get("date")
        if not date:
            continue
        strategy = sig["strategy"]
        ticker = sig.get("ticker", "")

        # Close positions whose hold has elapsed (compare date keys, not exact dates)
        still_open = []
        for pos in open_positions:
            if _date_key(date) >= _date_key(pos["exit_date"]):
                equity += pos["pnl_pending"]
                daily_pnl += pos["pnl_pending"]
                weekly_pnl += pos["pnl_pending"]
            else:
                still_open.append(pos)
        open_positions = still_open

        # Date / week boundary
        if date != current_date:
            current_date = date
            daily_pnl = 0.0
            wk = _iso_week(date)
            if wk != current_week:
                current_week = wk
                weekly_pnl = 0.0

        # DD tracking
        if equity > peak_equity:
            peak_equity = equity
        dd = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0
        if dd > max_dd_pct:
            max_dd_pct = dd

        # Skips
        if equity < account_floor:
            skipped_floor += 1
            skipped_by_strategy[strategy] = skipped_by_strategy.get(strategy, 0) + 1
            continue
        if daily_pnl < -daily_loss_limit_pct * equity:
            skipped_daily += 1
            skipped_by_strategy[strategy] = skipped_by_strategy.get(strategy, 0) + 1
            continue
        if weekly_pnl < -weekly_loss_limit_pct * equity:
            skipped_weekly += 1
            skipped_by_strategy[strategy] = skipped_by_strategy.get(strategy, 0) + 1
            continue
        if len(open_positions) >= max_concurrent:
            skipped_concurrent += 1
            skipped_by_strategy[strategy] = skipped_by_strategy.get(strategy, 0) + 1
            continue

        # Ticker collision: skip if same ticker already has an open position
        if any(p.get("ticker") == ticker for p in open_positions):
            overlap_skips[strategy] = overlap_skips.get(strategy, 0) + 1
            skipped_by_strategy[strategy] = skipped_by_strategy.get(strategy, 0) + 1
            continue

        # Sizing
        conf = sig.get("direction_confidence") or 0.0
        if conf >= 0.7:
            risk_pct = 0.03  # A+
        elif conf >= 0.5:
            risk_pct = 0.02  # A
        else:
            risk_pct = 0.01  # B
        risk_dollars = equity * risk_pct
        position_value = min(risk_dollars / stop_distance_pct, equity * max_position_pct)

        # Pick return based on hold
        ret = sig.get("next_day_return") if sig["hold_days"] == 1 else sig.get("five_day_return")
        if ret is None:
            continue
        if sig["side"] == "short":
            ret = -ret

        pnl = position_value * ret
        exit_date = _next_trading_day(date, sig["hold_days"])
        open_positions.append({
            "exit_date": exit_date,
            "ticker": ticker,
            "pnl_pending": pnl,
            "strategy": strategy,
        })
        trades_by_strategy[strategy] = trades_by_strategy.get(strategy, 0) + 1

    # Close remaining at face value
    for pos in open_positions:
        equity += pos["pnl_pending"]

    return {
        "starting_equity": starting_equity,
        "ending_equity": round(equity, 2),
        "total_return_pct": round((equity - starting_equity) / starting_equity * 100, 2),
        "peak_equity": round(peak_equity, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "trades_by_strategy": trades_by_strategy,
        "skipped_by_strategy": skipped_by_strategy,
        "overlap_skips": overlap_skips,
        "skipped_daily_limit": skipped_daily,
        "skipped_weekly_limit": skipped_weekly,
        "skipped_concurrent": skipped_concurrent,
        "skipped_floor": skipped_floor,
        "total_trades": sum(trades_by_strategy.values()),
    }


def run(
    eightk_csv: Path = DEFAULT_8K_CSV,
    momentum_csv: Path = DEFAULT_MOMENTUM_CSV,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    print(f"[combined] loading 8K corpus  ← {eightk_csv}", file=sys.stderr)
    eightk_rows = _load_8k_corpus(Path(eightk_csv))
    print(f"[combined]   {len(eightk_rows)} rows", file=sys.stderr)
    print(f"[combined] loading momentum corpus  ← {momentum_csv}", file=sys.stderr)
    mom_rows = _load_momentum_corpus(Path(momentum_csv))
    print(f"[combined]   {len(mom_rows)} rows", file=sys.stderr)

    if start_date or end_date:
        def _in_range(r):
            d = r.get("filing_date") or r.get("date") or ""
            if start_date and d < start_date:
                return False
            if end_date and d > end_date:
                return False
            return True
        before_8k, before_mom = len(eightk_rows), len(mom_rows)
        eightk_rows = [r for r in eightk_rows if _in_range(r)]
        mom_rows = [r for r in mom_rows if _in_range(r)]
        print(
            f"[combined] date filter [{start_date or '...'} .. {end_date or '...'}]:\n"
            f"[combined]   8K rows: {before_8k} → {len(eightk_rows)}\n"
            f"[combined]   momentum rows: {before_mom} → {len(mom_rows)}",
            file=sys.stderr,
        )

    # Enrich tickers for cap/float lookups (only 8K rows need this; momentum already
    # comes from small_cap_universe so we know they're small)
    all_tickers = {r["ticker"] for r in eightk_rows + mom_rows if r.get("ticker")}
    print(f"[combined] enriching {len(all_tickers)} unique tickers", file=sys.stderr)
    cache = _enrich_ticker_cache(eightk_rows + mom_rows)

    # Build the four strategy buckets
    bull_8k = _filter_8k_bullish_smallcap_lf(eightk_rows, cache)
    bear_8k = _filter_8k_bearish_smallcap_lf(eightk_rows, cache)
    frd = _filter_frd_long(mom_rows, cache)
    gag = _filter_gag_long(mom_rows, cache)
    print(
        f"[combined] strategy row counts:  "
        f"8K_bull={len(bull_8k)}  8K_bear={len(bear_8k)}  FRD={len(frd)}  GAG={len(gag)}",
        file=sys.stderr,
    )

    # Standalone sims (each strategy isolated) for direct comparison vs combined
    print()
    print("=" * 105)
    print("Standalone sims (each strategy isolated, $36K each)")
    print("=" * 105)
    print(
        f"{'Strategy':<40} {'Side':>5} {'Hold':>5} {'Trades':>7} "
        f"{'EndEq':>11} {'Ret%':>8} {'PeakEq':>11} {'MaxDD%':>7}"
    )
    print("-" * 105)
    standalone = [
        ("8K_bullish+sc+lf", bull_8k, "long", 5),
        ("8K_bearish+sc+lf", bear_8k, "short", 1),
        ("FRD_LONG", frd, "long", 5),
        ("gap_and_go", gag, "long", 5),
    ]
    for label, rs, side, hold in standalone:
        sim = _bt.simulate_equity_curve(rs, label, hold_days=hold, side=side)
        print(
            f"{label:<40} {side:>5} {hold:>5} {sim['trades_taken']:>7} "
            f"${sim['ending_equity']:>10.0f} {sim['total_return_pct']:>+8.2f} "
            f"${sim['peak_equity']:>10.0f} {sim['max_drawdown_pct']:>7.2f}"
        )

    # COMBINED simulation: all signals competing for one $36K equity bucket
    print()
    print("=" * 105)
    print("COMBINED sim: all four strategies on ONE $36K bucket, max 5 concurrent")
    print("=" * 105)
    strategies = [
        {"label": "8K_bullish",   "rows": bull_8k, "side": "long",  "hold_days": 5},
        {"label": "8K_bearish",   "rows": bear_8k, "side": "short", "hold_days": 1},
        {"label": "FRD_LONG",     "rows": frd,     "side": "long",  "hold_days": 5},
        {"label": "gap_and_go",   "rows": gag,     "side": "long",  "hold_days": 5},
    ]
    combined = simulate_combined(strategies)

    print(f"Starting equity: ${combined['starting_equity']:,.0f}")
    print(f"Ending equity:   ${combined['ending_equity']:,.0f}")
    print(f"Total return:    {combined['total_return_pct']:+.2f}%")
    print(f"Peak equity:     ${combined['peak_equity']:,.0f}")
    print(f"Max drawdown:    {combined['max_drawdown_pct']:.2f}%")
    print(f"Total trades:    {combined['total_trades']}")
    print()
    print("Trades taken per strategy (after overlap + concurrent limits):")
    for s, n in sorted(combined['trades_by_strategy'].items(), key=lambda kv: -kv[1]):
        print(f"  {s:<20} {n:>5}")
    print()
    print("Overlap skips (ticker already held by another strategy):")
    for s, n in sorted(combined['overlap_skips'].items(), key=lambda kv: -kv[1]):
        if n > 0:
            print(f"  {s:<20} {n:>5}")
    print()
    print("Other skip reasons (combined):")
    print(f"  daily_loss_limit  : {combined['skipped_daily_limit']}")
    print(f"  weekly_loss_limit : {combined['skipped_weekly_limit']}")
    print(f"  concurrent_max    : {combined['skipped_concurrent']}")
    print(f"  account_floor     : {combined['skipped_floor']}")
    return combined


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Combined small-cap thesis backtest")
    p.add_argument("--eightk-csv", default=str(DEFAULT_8K_CSV))
    p.add_argument("--momentum-csv", default=str(DEFAULT_MOMENTUM_CSV))
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD inclusive")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv or sys.argv[1:])
    run(
        eightk_csv=Path(args.eightk_csv),
        momentum_csv=Path(args.momentum_csv),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
