"""MCL RSI(2) backtest — port of MNQ Phase 3.5 framework to gold daily.

Pattern scan result that prompted this:
  MCL RSI(2)<5 daily → LONG, 5d hold:  WR 57%, mean +0.568%, N=249 (3.5y)

Strategy variants tested (escalating filter rigor):
  V1: Bare RSI(2)<5 long  →  no SMA filter, no volume filter
  V2: Phase 3.5 spec      →  RSI<5 AND close > 200-SMA AND vol >= 1.2× 20d
  V3: Long+Short          →  V2 + RSI>95 short branch (testing MCL short edge)
  V4: Bare RSI(2)>95 short →  no filters

For each: trades, win rate, PF, total $, max DD, annualized.

Data: backtest_data/mcl_1d_continuous.csv (1041 RTH-aggregated daily bars,
Oct 2022 → May 2026).
"""
from __future__ import annotations
import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

POINT_VALUE = 10.0  # $/pt MCL = $10
DATA_FILE = Path("backtest_data/mcl_1d_continuous.csv")


@dataclass
class DailyBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


def load_bars() -> list[DailyBar]:
    out = []
    with DATA_FILE.open() as f:
        for r in csv.DictReader(f):
            out.append(DailyBar(
                date=r["date"], open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=int(r["volume"]),
            ))
    out.sort(key=lambda b: b.date)
    return out


def rsi2(closes: list[float]) -> float | None:
    if len(closes) < 3:
        return None
    gains, losses = [], []
    for k in (-2, -1):
        d = closes[k] - closes[k - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def backtest(
    bars: list[DailyBar],
    long_branch: bool = True,
    short_branch: bool = False,
    use_sma_filter: bool = True,
    use_volume_filter: bool = True,
    sma_period: int = 200,
    oversold: float = 5.0,
    overbought: float = 95.0,
    long_vol_mult: float = 1.2,
    short_vol_mult: float = 1.0,
    max_hold: int = 5,
    long_exit_rsi: float = 70.0,
    short_exit_rsi: float = 30.0,
) -> dict:
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    trades = []
    i = 0
    while i < len(bars) - 1:
        # Need enough history for SMA + RSI
        if use_sma_filter and i < sma_period:
            i += 1
            continue
        if i < 20:
            i += 1
            continue
        r = rsi2(closes[:i + 1])
        if r is None:
            i += 1
            continue
        sma = mean(closes[i - sma_period:i]) if use_sma_filter else None
        avg_vol = mean(vols[i - 20:i]) if use_volume_filter else None
        c = closes[i]
        v = vols[i]

        direction = None
        if long_branch and r < oversold:
            if use_sma_filter and c <= sma:
                pass
            elif use_volume_filter and v < long_vol_mult * avg_vol:
                pass
            else:
                direction = "long"
        if direction is None and short_branch and r > overbought:
            if use_sma_filter and c >= sma:
                pass
            elif use_volume_filter and v < short_vol_mult * avg_vol:
                pass
            else:
                direction = "short"
        if direction is None:
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= len(bars):
            break
        entry = bars[entry_idx].open
        exit_idx = None
        exit_reason = None
        for k in range(entry_idx, min(entry_idx + max_hold, len(bars))):
            kr = rsi2(closes[:k + 1])
            if kr is None:
                continue
            if direction == "long" and kr > long_exit_rsi:
                exit_idx = k; exit_reason = "rsi_revert"; break
            if direction == "short" and kr < short_exit_rsi:
                exit_idx = k; exit_reason = "rsi_revert"; break
        if exit_idx is None:
            exit_idx = min(entry_idx + max_hold - 1, len(bars) - 1)
            exit_reason = "time"
        exit_price = bars[exit_idx].close
        pnl_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
        trades.append({
            "signal": bars[i].date, "entry_date": bars[entry_idx].date,
            "exit_date": bars[exit_idx].date, "direction": direction,
            "rsi": r, "entry": entry, "exit": exit_price,
            "pnl_pts": pnl_pts, "pnl_dollars": pnl_pts * POINT_VALUE,
            "exit_reason": exit_reason, "hold": exit_idx - entry_idx + 1,
        })
        i = exit_idx + 1
    return _summary(trades, len(bars))


def _summary(trades, n_sessions):
    if not trades:
        return {"n": 0, "trades": []}
    pnls = [t["pnl_dollars"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    gw = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    pf = gw / gl if gl > 0 else float("inf")
    equity = peak = mdd = 0
    for p in pnls:
        equity += p; peak = max(peak, equity); mdd = max(mdd, peak - equity)
    yrs = n_sessions / 252.0
    return {
        "n": len(trades), "win_rate": wins / len(trades),
        "avg_dollars": mean(pnls), "avg_pts": mean(t["pnl_pts"] for t in trades),
        "pf": pf, "total": sum(pnls), "max_dd": mdd,
        "annualized": sum(pnls) / yrs if yrs > 0 else 0,
        "trades": trades,
    }


def report(label, res):
    if res.get("n", 0) == 0:
        print(f"  {label:<50} no trades")
        return
    yrs = "?"
    print(f"  {label:<50} N={res['n']:>3}  WR {res['win_rate']:>4.0%}  "
          f"avg ${res['avg_dollars']:>+6.0f}  PF {res['pf']:>5.2f}  "
          f"total ${res['total']:>+8,.0f}  ann ${res['annualized']:>+7,.0f}  "
          f"DD ${res['max_dd']:>6,.0f}")


def main():
    bars = load_bars()
    print(f"MCL daily bars: {len(bars)}  ({bars[0].date} → {bars[-1].date})")
    print(f"Years: {len(bars) / 252.0:.2f}\n")

    print("=" * 138)
    print("MCL RSI(2) STRATEGY VARIANTS — daily, 5d max hold, RSI-revert exit")
    print("=" * 138)

    # V1: bare LONG only
    res = backtest(bars, long_branch=True, short_branch=False,
                   use_sma_filter=False, use_volume_filter=False)
    report("V1: bare LONG (RSI<5 only, no filters)", res)
    v1 = res

    # V2: Phase 3.5 LONG spec
    res = backtest(bars, long_branch=True, short_branch=False,
                   use_sma_filter=True, use_volume_filter=True)
    report("V2: LONG Phase 3.5 spec (SMA200 + vol 1.2x)", res)
    v2 = res

    # V3: LONG + SHORT Phase 3.5 spec
    res = backtest(bars, long_branch=True, short_branch=True,
                   use_sma_filter=True, use_volume_filter=True)
    report("V3: LONG + SHORT Phase 3.5 spec", res)
    v3 = res

    # V4: bare SHORT
    res = backtest(bars, long_branch=False, short_branch=True,
                   use_sma_filter=False, use_volume_filter=False)
    report("V4: bare SHORT (RSI>95 only)", res)
    v4 = res

    # V5: LONG SMA filter only (skip volume)
    res = backtest(bars, long_branch=True, short_branch=False,
                   use_sma_filter=True, use_volume_filter=False)
    report("V5: LONG SMA200 filter only", res)

    # V6: LONG volume filter only (skip SMA)
    res = backtest(bars, long_branch=True, short_branch=False,
                   use_sma_filter=False, use_volume_filter=True)
    report("V6: LONG vol 1.2x filter only", res)

    print()
    print("=" * 138)
    print("YEAR-BY-YEAR breakdown for V2 (Phase 3.5 LONG spec)")
    print("=" * 138)
    from collections import defaultdict
    by_year = defaultdict(list)
    for t in v2["trades"]:
        by_year[t["entry_date"][:4]].append(t)
    for yr in sorted(by_year.keys()):
        ts = by_year[yr]
        pnls = [t["pnl_dollars"] for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        gw = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf = gw / gl if gl > 0 else float("inf")
        print(f"  {yr}: N={len(ts):>3}  WR {wins/len(ts):>4.0%}  "
              f"avg ${mean(pnls):>+6.0f}  PF {pf:>5.2f}  total ${sum(pnls):>+8,.0f}")

    # Trade log for V2 (recommended config)
    print()
    print("=" * 138)
    print(f"V2 TRADE LOG (Phase 3.5 LONG spec, {v2['n']} trades)")
    print("=" * 138)
    print(f"{'Signal':<12} {'Entry':<12} {'Exit':<12} {'Dir':<5} {'RSI':>5} "
          f"{'Entry':>8} {'Exit':>8} {'PnL $':>8} {'Reason':<12} {'Hold':>4}")
    for t in v2["trades"]:
        print(f"{t['signal']:<12} {t['entry_date']:<12} {t['exit_date']:<12} "
              f"{t['direction']:<5} {t['rsi']:>5.1f} "
              f"{t['entry']:>8.2f} {t['exit']:>8.2f} "
              f"{t['pnl_dollars']:>+8,.0f} {t['exit_reason']:<12} {t['hold']:>4}")


if __name__ == "__main__":
    main()
