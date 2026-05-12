"""Backtest harness for two technical small-cap setups:

1. Gap-and-Go (LONG): stock gaps up >=5% with >=2x avg volume, day closes
   above the open (no fill-the-gap reversal). Entry at close, hold 1 or 5 days.

2. First Red Day (SHORT): after 3+ consecutive green closes that produced
   a >=30% cumulative run, the first red close marks distribution. Short
   the next-day attempt, 1-day hold.

Universe defaults to small_cap_universe.txt (Finviz cap_small filter).
For each ticker, pulls 365 days of OHLCV via yfinance once, scans every
day for both signals, records forward returns, writes a single signals
CSV. The same simulate_equity_curve / spy_benchmark helpers from
backtest_smallcap are reused for the equity-curve simulation.

Run: python -m rh_mcp.analysis.momentum_backtest
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import sys
from pathlib import Path
from statistics import mean, median

try:
    import yfinance as yf
except ImportError as e:
    raise SystemExit("momentum_backtest requires yfinance") from e

from rh_mcp.analysis import backtest_smallcap as _bt
from rh_mcp.analysis import edgar_client as _edgar

DEFAULT_UNIVERSE = Path("C:/Users/algee/TraderMCP-RH/small_cap_universe.txt")
DEFAULT_OUTPUT = _edgar.CACHE_DIR / "momentum_signals.csv"

# Gap-and-go thresholds — 20% is the explosive-news regime (vs 5% which is
# just an above-average open). At 20%+ you're looking at true catalyst gaps:
# earnings beats with raised guidance, FDA approvals, acquisitions, etc.
GAG_MIN_GAP_PCT = 0.20            # 20% minimum gap up
GAG_MIN_VOL_RATIO = 2.0           # 2× 20-day avg volume
GAG_MIN_AVG_VOL = 500_000

# FRD thresholds
FRD_MIN_GREEN_DAYS = 3
FRD_MIN_RUN_PCT = 0.30            # 30% cumulative move during the run
FRD_MAX_LOOKBACK = 8              # how many bars back to allow the run start
FRD_MIN_AVG_VOL = 500_000

MIN_PRICE = 5.0

CSV_FIELDS = [
    "ticker", "date", "signal_type",
    "gap_pct", "vol_ratio", "run_pct", "green_days",
    "t0_close", "t1_close", "t5_close",
    "next_day_return", "five_day_return",
    "direction", "direction_confidence",
]


def _load_universe(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        out.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(out))


def _detect_gap_and_go(bars, idx: int, min_gap_pct: float = GAG_MIN_GAP_PCT) -> dict | None:
    if idx < 21 or idx >= len(bars) - 5:
        return None
    today = bars.iloc[idx]
    prior = bars.iloc[idx - 1]
    if float(today["Close"]) < MIN_PRICE:
        return None
    avg_vol = bars["Volume"].iloc[idx - 20:idx].mean()
    if avg_vol < GAG_MIN_AVG_VOL:
        return None
    today_open = float(today["Open"])
    prior_close = float(prior["Close"])
    if prior_close <= 0:
        return None
    gap_pct = (today_open - prior_close) / prior_close
    if gap_pct < min_gap_pct:
        return None
    vol_ratio = float(today["Volume"]) / avg_vol
    if vol_ratio < GAG_MIN_VOL_RATIO:
        return None
    # No-reversal filter: close must finish above the open
    if float(today["Close"]) <= today_open:
        return None
    return {
        "signal_type": "gap_and_go",
        "gap_pct": round(gap_pct, 5),
        "vol_ratio": round(vol_ratio, 3),
        "run_pct": None,
        "green_days": None,
        "direction": "long",
        "direction_confidence": min(1.0, vol_ratio / 4.0),  # 4x vol = full conf
    }


def _detect_frd(bars, idx: int) -> dict | None:
    if idx < FRD_MAX_LOOKBACK or idx >= len(bars) - 5:
        return None
    today = bars.iloc[idx]
    yesterday = bars.iloc[idx - 1]
    if float(today["Close"]) < MIN_PRICE:
        return None
    avg_vol = bars["Volume"].iloc[idx - 20:idx].mean() if idx >= 20 else 0
    if avg_vol < FRD_MIN_AVG_VOL:
        return None
    # Red day requirement
    if float(today["Close"]) >= float(yesterday["Close"]):
        return None
    # Count consecutive green days ending at idx-1
    green_days = 0
    for back in range(1, FRD_MAX_LOOKBACK + 1):
        if idx - back <= 0:
            break
        bar = bars.iloc[idx - back]
        prev = bars.iloc[idx - back - 1]
        if float(bar["Close"]) > float(prev["Close"]):
            green_days += 1
        else:
            break
    if green_days < FRD_MIN_GREEN_DAYS:
        return None
    # Cumulative run: from prior to first green day's prior close → end of green streak
    run_start = float(bars.iloc[idx - green_days - 1]["Close"])
    run_end = float(yesterday["Close"])
    if run_start <= 0:
        return None
    run_pct = (run_end - run_start) / run_start
    if run_pct < FRD_MIN_RUN_PCT:
        return None
    return {
        "signal_type": "frd",
        "gap_pct": None,
        "vol_ratio": None,
        "run_pct": round(run_pct, 4),
        "green_days": green_days,
        # Direction is ambiguous at detection time — the bounce vs continuation
        # bet gets tested by running BOTH sides in the equity-curve simulation.
        # Stored as 'frd' (neutral). Confidence scales with run size.
        "direction": "frd",
        "direction_confidence": min(1.0, run_pct / 0.6),
    }


def _scan_ticker(ticker: str, lookback_days: int, min_gap_pct: float = GAG_MIN_GAP_PCT) -> list[dict]:
    try:
        df = yf.Ticker(ticker).history(
            period=f"{max(lookback_days + 30, 90)}d",
            interval="1d",
            auto_adjust=False,
        )
    except Exception:
        return []
    if df is None or df.empty or len(df) < 25:
        return []
    # Drop today's bar if it's intraday (incomplete)
    out: list[dict] = []
    n = len(df)
    for i in range(21, n - 5):
        gag_sig = _detect_gap_and_go(df, i, min_gap_pct=min_gap_pct)
        frd_sig = _detect_frd(df, i)
        for sig in (gag_sig, frd_sig):
            if not sig:
                continue
            t0 = float(df.iloc[i]["Close"])
            t1 = float(df.iloc[i + 1]["Close"]) if i + 1 < n else None
            t5 = float(df.iloc[i + 5]["Close"]) if i + 5 < n else None
            if t1 is None or t0 <= 0:
                continue
            next_day = (t1 / t0 - 1)
            five_day = (t5 / t0 - 1) if t5 else None
            sig.update({
                "ticker": ticker,
                "date": df.index[i].strftime("%Y-%m-%d"),
                "t0_close": round(t0, 4),
                "t1_close": round(t1, 4),
                "t5_close": round(t5, 4) if t5 else None,
                "next_day_return": round(next_day, 5),
                "five_day_return": round(five_day, 5) if five_day is not None else None,
            })
            out.append(sig)
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
    universe_file: Path = DEFAULT_UNIVERSE,
    output_path: Path = DEFAULT_OUTPUT,
    lookback_days: int = 365,
    workers: int = 8,
    min_gap_pct: float = GAG_MIN_GAP_PCT,
) -> dict:
    universe = _load_universe(Path(universe_file))
    if not universe:
        raise SystemExit(f"no tickers in {universe_file}")
    print(
        f"[momentum] scanning {len(universe)} tickers × {lookback_days}d "
        f"(gap_thresh={min_gap_pct*100:.0f}%)",
        file=sys.stderr,
    )

    all_signals: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_scan_ticker, t, lookback_days, min_gap_pct): t
            for t in universe
        }
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            sigs = fut.result()
            all_signals.extend(sigs)
            done += 1
            if done % 50 == 0:
                print(f"[momentum] {done}/{len(universe)} tickers, {len(all_signals)} signals so far",
                      file=sys.stderr)

    # Write CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for s in all_signals:
            writer.writerow({k: s.get(k) for k in CSV_FIELDS})
    print(f"\n[momentum] wrote {len(all_signals)} signals → {output_path}", file=sys.stderr)

    # Per-signal-type stats
    by_type: dict[str, list[dict]] = {}
    for s in all_signals:
        by_type.setdefault(s["signal_type"], []).append(s)

    print()
    print(
        f"{'Signal type':<20} {'N':>5} "
        f"{'t1_W%':>6} {'t1_Avg':>7} {'t1_Tot':>9}  "
        f"{'t5_W%':>6} {'t5_Avg':>7} {'t5_Tot':>9}"
    )
    print("-" * 90)
    for st, rs in by_type.items():
        t1 = [r["next_day_return"] for r in rs if r.get("next_day_return") is not None]
        t5 = [r["five_day_return"] for r in rs if r.get("five_day_return") is not None]
        s1 = _stats(t1)
        s5 = _stats(t5)
        t5_w = f"{s5['win_rate']:>6}" if s5["n"] > 0 else f"{'—':>6}"
        t5_a = f"{s5['avg']:>+7.3f}" if s5["n"] > 0 else f"{'—':>7}"
        t5_t = f"{s5['total']:>+9.2f}" if s5["n"] > 0 else f"{'—':>9}"
        print(
            f"{st:<20} {s1['n']:>5} "
            f"{s1['win_rate']:>6} {s1['avg']:>+7.3f} {s1['total']:>+9.2f}  "
            f"{t5_w} {t5_a} {t5_t}"
        )

    # Equity curve simulation reusing backtest_smallcap.simulate_equity_curve
    # Add `filing_date` alias so the simulator sees a date key it can sort on
    for s in all_signals:
        s["filing_date"] = s["date"]
    print()
    print("=" * 105)
    print("Equity-curve simulation: $36K start, small-cap framework risk rules")
    print("=" * 105)
    print(
        f"{'Strategy':<35} {'Side':>5} {'Hold':>5} {'Trades':>7} "
        f"{'EndEq':>11} {'Ret%':>8} {'PeakEq':>11} {'MaxDD%':>7}"
    )
    print("-" * 105)
    gag = by_type.get("gap_and_go", [])
    frd = by_type.get("frd", [])
    sim_plan = [
        ("gap_and_go", gag, "long", 1),
        ("gap_and_go", gag, "long", 5),
        ("frd_LONG (bounce)", frd, "long", 1),
        ("frd_LONG (bounce)", frd, "long", 5),
        ("frd_SHORT (continuation)", frd, "short", 1),
        ("frd_SHORT (continuation)", frd, "short", 5),
    ]
    for label, rs, side, hold in sim_plan:
        sim = _bt.simulate_equity_curve(rs, label, hold_days=hold, side=side)
        print(
            f"{label:<35} {side:>5} {hold:>5} {sim['trades_taken']:>7} "
            f"${sim['ending_equity']:>10.0f} {sim['total_return_pct']:>+8.2f} "
            f"${sim['peak_equity']:>10.0f} {sim['max_drawdown_pct']:>7.2f}"
        )

    # SPY benchmark over the signal date range
    if all_signals:
        all_signals.sort(key=lambda s: s["date"])
        start_d = all_signals[0]["date"]
        end_d = all_signals[-1]["date"]
        spy = _bt.spy_benchmark(start_d, end_d)
        if "error" not in spy:
            print(
                f"{'SPY_buy_and_hold':<35} {'long':>5} {'B&H':>5} {'—':>7} "
                f"${spy['ending_equity']:>10.0f} {spy['total_return_pct']:>+8.2f} "
                f"${spy['peak_equity']:>10.0f} {spy['max_drawdown_pct']:>7.2f}"
            )

    return {"success": True, "signals_written": len(all_signals), "output": str(output_path)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gap-and-go + FRD small-cap backtest")
    p.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--lookback-days", type=int, default=365)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--min-gap-pct", type=float, default=GAG_MIN_GAP_PCT,
                   help="Gap-and-go threshold as decimal (e.g. 0.30 for 30 percent)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    run(
        universe_file=Path(args.universe),
        output_path=Path(args.output),
        lookback_days=args.lookback_days,
        workers=args.workers,
        min_gap_pct=args.min_gap_pct,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
