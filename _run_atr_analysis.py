"""ATR analysis for bullish_8k small-cap signals.

For each filing in backtest_smallcap_4yr.csv:
- Pull bars via yfinance (cached per ticker)
- Compute 20-day ATR ending at the bar BEFORE filing_date
- Compute ATR-as-pct-of-close (volatility-normalized)
- Bucket rows by ATR% and analyze win rate / avg return
"""
import csv
import sys
from pathlib import Path
from statistics import mean, median

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    raise SystemExit("requires yfinance + pandas")

from rh_mcp.analysis import backtest_smallcap as bt

CORPUS = Path("C:/Users/algee/.edgar_cache/backtest_smallcap_4yr.csv")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load():
    rows = []
    with CORPUS.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                r["next_day_return"] = float(r["next_day_return"]) if r.get("next_day_return") else None
                r["five_day_return"] = float(r["five_day_return"]) if r.get("five_day_return") else None
                r["direction_confidence"] = float(r.get("direction_confidence") or 0.5)
                r["t0_close"] = float(r["t0_close"]) if r.get("t0_close") else None
            except Exception:
                continue
            if r.get("direction") == "long" and r["next_day_return"] is not None and r["five_day_return"] is not None:
                rows.append(r)
    return rows


def compute_atrs(rows):
    """For each unique ticker, pull bars and compute 20d ATR at each filing date."""
    tickers = sorted({r["ticker"] for r in rows})
    print(f"computing ATR for {len(tickers)} unique tickers...", file=sys.stderr)
    bars_by_ticker = {}
    for i, t in enumerate(tickers):
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(tickers)}", file=sys.stderr)
        try:
            df = yf.Ticker(t).history(period="5y", interval="1d", auto_adjust=False)
            if df is not None and not df.empty and len(df) >= 25:
                bars_by_ticker[t] = df
        except Exception:
            continue

    for r in rows:
        r["atr_20d"] = None
        r["atr_pct"] = None
        df = bars_by_ticker.get(r["ticker"])
        if df is None:
            continue
        try:
            target_date = pd.Timestamp(r["filing_date"]).tz_localize(None)
        except Exception:
            continue
        # Find bar at or just before filing_date
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        pos_arr = idx.searchsorted(target_date, side="right") - 1
        pos = int(pos_arr)
        if pos < 20 or pos >= len(df):
            continue
        window = df.iloc[pos - 19:pos + 1]
        if len(window) < 20:
            continue
        high = window["High"].astype(float)
        low = window["Low"].astype(float)
        close = window["Close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.iloc[1:].mean())  # skip first row (no prev close)
        current_close = float(close.iloc[-1])
        if current_close > 0:
            r["atr_20d"] = round(atr, 4)
            r["atr_pct"] = round(atr / current_close * 100, 3)
    return rows


def main():
    rows = load()
    print(f"loaded {len(rows)} bullish_8k rows")
    rows = compute_atrs(rows)
    with_atr = [r for r in rows if r.get("atr_pct") is not None]
    print(f"with ATR computed: {len(with_atr)} ({len(with_atr)/len(rows)*100:.1f}%)")
    print()
    print(f"ATR% distribution: min={min(r['atr_pct'] for r in with_atr):.2f}%, "
          f"median={median(r['atr_pct'] for r in with_atr):.2f}%, "
          f"max={max(r['atr_pct'] for r in with_atr):.2f}%")

    # Bucket by ATR%
    buckets = {
        "0-2% (low vol)": [r for r in with_atr if r["atr_pct"] < 2],
        "2-4%": [r for r in with_atr if 2 <= r["atr_pct"] < 4],
        "4-6%": [r for r in with_atr if 4 <= r["atr_pct"] < 6],
        "6-10%": [r for r in with_atr if 6 <= r["atr_pct"] < 10],
        "10%+ (high vol)": [r for r in with_atr if r["atr_pct"] >= 10],
    }
    print()
    print(f"{'Bucket':<22} {'N':>5} {'Win%':>6} {'AvgT5%':>8} {'MedT5%':>8} {'SimRet':>9} {'DD':>6}")
    print("-" * 80)
    for k in ["0-2% (low vol)", "2-4%", "4-6%", "6-10%", "10%+ (high vol)"]:
        rs = buckets[k]
        if not rs: continue
        t5 = [r["five_day_return"] for r in rs]
        wins = sum(1 for r in t5 if r > 0)
        sim = bt.simulate_equity_curve(rs, k, hold_days=5, side="long", t1_stop_pct=-0.05)
        print(f"{k:<22} {len(rs):>5} {wins/len(t5)*100:>6.1f} "
              f"{mean(t5)*100:>+7.3f}% {median(t5)*100:>+7.3f}% "
              f"{sim['total_return_pct']:>+8.2f}% {sim['max_drawdown_pct']:>5.2f}")

    # Test ATR-based stop: stop at N × ATR instead of fixed -5%
    print()
    print("=== ATR-based stop test (replace fixed -5% with N × ATR) ===")
    print(f"{'Stop rule':<25} {'Trades':>7} {'Ret%':>9} {'DD%':>6}")
    print('-' * 55)
    # Baseline: fixed -5%
    sim_baseline = bt.simulate_equity_curve(with_atr, "fixed -5%", hold_days=5, side="long", t1_stop_pct=-0.05)
    print(f"{'Fixed -5%':<25} {sim_baseline['trades_taken']:>7} "
          f"{sim_baseline['total_return_pct']:>+8.2f} {sim_baseline['max_drawdown_pct']:>6.2f}")

    # ATR-based variants
    for atr_mult in [1.0, 1.5, 2.0, 3.0]:
        # Apply per-row stop based on ATR
        sim_rows = []
        for r in with_atr:
            rr = dict(r)
            atr_pct = r["atr_pct"] / 100  # convert to decimal
            stop_threshold = -atr_pct * atr_mult
            # Apply: if t+1 < stop_threshold, exit at t+1 (replace five_day_return)
            if r["next_day_return"] < stop_threshold:
                rr["five_day_return"] = r["next_day_return"]
            sim_rows.append(rr)
        sim = bt.simulate_equity_curve(sim_rows, f"{atr_mult}x ATR", hold_days=5, side="long")
        print(f"{f'{atr_mult}x ATR stop':<25} {sim['trades_taken']:>7} "
              f"{sim['total_return_pct']:>+8.2f} {sim['max_drawdown_pct']:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
