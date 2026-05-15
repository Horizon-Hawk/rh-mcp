"""Statistical pattern scanner for MGC and MCL.

Computes a battery of simple repeatable-pattern tests on the stitched 15-min
data. Goal: surface which patterns have signal (positive EV, high consistency)
before committing to building a full backtest.

Patterns tested:
  1. Day-of-week return bias (Mon-Fri average sessions return)
  2. Time-of-day return profile (which 15-min buckets have directional bias)
  3. Overnight gap behavior (do gaps fill?)
  4. OR fade vs follow-through (does breaking OR sustain or revert?)
  5. RSI(2) extremes (oversold reversion, overbought fade)
  6. Inside-day breakout (low-range day → wide-range day)
  7. Pit close fade (last 30 min often reverses?)
  8. Vol contraction breakout (NR4/NR7 pattern)

Output: per-pattern stat tables. Anything with >55% WR, >0.5 expectancy, and
30+ occurrences becomes a candidate.
"""
from __future__ import annotations
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load_15m(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            ts = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S")
            rows.append({
                "ts": ts,
                "date": ts.date().isoformat(),
                "hhmm": ts.hour * 100 + ts.minute,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low":  float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            })
    return rows


def group_by_session(bars: list[dict], rth_start: int = 930, rth_end: int = 1600) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        if rth_start <= b["hhmm"] <= rth_end:
            out[b["date"]].append(b)
    return out


def daily_summary(sessions: dict[str, list[dict]]) -> dict[str, dict]:
    """Per-session open/high/low/close + range + return."""
    out: dict[str, dict] = {}
    for d in sorted(sessions.keys()):
        bars = sessions[d]
        if not bars:
            continue
        o = bars[0]["open"]
        c = bars[-1]["close"]
        h = max(b["high"] for b in bars)
        l = min(b["low"] for b in bars)
        out[d] = {
            "open": o, "high": h, "low": l, "close": c,
            "range": h - l,
            "return_pct": (c - o) / o * 100 if o else 0,
            "return_pts": c - o,
        }
    return out


def fmt_stat(name: str, n: int, wr: float, mean_r: float, total_r: float) -> str:
    return f"  {name:<45} N={n:>4}  WR {wr:>4.0%}  mean {mean_r:+6.3f}%  total {total_r:+7.2f}%"


def pattern_dow(daily: dict[str, dict]) -> None:
    """Day-of-week return bias."""
    print("\n[1] Day-of-week return bias (% per session)")
    print(f"  {'Day':<12}{'N':>5}{'WR':>8}{'mean':>10}{'total':>10}")
    buckets = defaultdict(list)
    for d, row in daily.items():
        wd = datetime.fromisoformat(d).weekday()
        buckets[wd].append(row["return_pct"])
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for wd in sorted(buckets.keys()):
        rs = buckets[wd]
        wr = sum(1 for r in rs if r > 0) / len(rs)
        mn = statistics.mean(rs)
        tot = sum(rs)
        print(f"  {names[wd]:<12}{len(rs):>5}{wr:>7.0%}{mn:>+9.3f}%{tot:>+9.2f}%")


def pattern_tod(bars_rth: list[dict]) -> None:
    """Time-of-day return profile per 15-min bucket — directional bias."""
    print("\n[2] Time-of-day return per 15-min bar (intra-bar return)")
    print(f"  {'HHMM':<8}{'N':>5}{'WR':>8}{'mean_pts':>12}{'total_pts':>14}")
    buckets = defaultdict(list)
    for b in bars_rth:
        ret = b["close"] - b["open"]
        buckets[b["hhmm"]].append(ret)
    for hhmm in sorted(buckets.keys()):
        rs = buckets[hhmm]
        wr = sum(1 for r in rs if r > 0) / len(rs)
        mn = statistics.mean(rs)
        tot = sum(rs)
        if abs(mn) > 0.05:  # filter out the noise
            star = " ⭐" if abs(mn) > 0.3 else ""
            print(f"  {hhmm:<8}{len(rs):>5}{wr:>7.0%}{mn:>+12.3f}{tot:>+14.2f}{star}")


def pattern_gap(daily: dict[str, dict], bars_rth: list[dict]) -> None:
    """Overnight gap behavior — do gaps fill same day?"""
    print("\n[3] Gap fill behavior (overnight gap vs same-day fill)")
    sessions = sorted(daily.keys())
    fill_stats = {"up_filled": 0, "up_total": 0, "down_filled": 0, "down_total": 0}
    by_day_bars = defaultdict(list)
    for b in bars_rth:
        by_day_bars[b["date"]].append(b)
    for i in range(1, len(sessions)):
        prev_close = daily[sessions[i-1]]["close"]
        today_open = daily[sessions[i]]["open"]
        today_bars = by_day_bars[sessions[i]]
        if not today_bars:
            continue
        gap_pts = today_open - prev_close
        gap_pct = gap_pts / prev_close * 100
        if abs(gap_pct) < 0.2:
            continue  # not a meaningful gap
        # Did price fill the gap (i.e., touch prev_close) during the session?
        if gap_pts > 0:  # gap up
            fill_stats["up_total"] += 1
            session_low = min(b["low"] for b in today_bars)
            if session_low <= prev_close:
                fill_stats["up_filled"] += 1
        else:  # gap down
            fill_stats["down_total"] += 1
            session_high = max(b["high"] for b in today_bars)
            if session_high >= prev_close:
                fill_stats["down_filled"] += 1
    if fill_stats["up_total"]:
        print(f"  Gap UP   filled same-day: {fill_stats['up_filled']:>3}/{fill_stats['up_total']:<3} "
              f"= {fill_stats['up_filled']/fill_stats['up_total']:.0%}")
    if fill_stats["down_total"]:
        print(f"  Gap DOWN filled same-day: {fill_stats['down_filled']:>3}/{fill_stats['down_total']:<3} "
              f"= {fill_stats['down_filled']/fill_stats['down_total']:.0%}")


def pattern_or_followthrough(sessions: dict[str, list[dict]], daily: dict[str, dict]) -> None:
    """Once OR breaks, does price follow through or revert?"""
    print("\n[4] OR breakout follow-through vs reversion")
    or_long_followed = or_long_reverted = 0
    or_short_followed = or_short_reverted = 0
    for d in sorted(sessions.keys()):
        bars = sessions[d]
        if len(bars) < 4:
            continue
        # OR = first 15min bar (9:30-9:45)
        or_bar = next((b for b in bars if b["hhmm"] == 930), None)
        if not or_bar:
            continue
        or_high, or_low = or_bar["high"], or_bar["low"]
        post = [b for b in bars if 945 <= b["hhmm"] <= 1100]
        if len(post) < 3:
            continue
        # First breakout direction
        direction = None
        breakout_idx = None
        for i, b in enumerate(post):
            if b["close"] > or_high:
                direction = "long"
                breakout_idx = i
                break
            if b["close"] < or_low:
                direction = "short"
                breakout_idx = i
                break
        if direction is None:
            continue
        # Did price keep going (close of session beyond OR) or revert (close back inside)?
        eod_close = bars[-1]["close"]
        if direction == "long":
            if eod_close > or_high:
                or_long_followed += 1
            else:
                or_long_reverted += 1
        else:
            if eod_close < or_low:
                or_short_followed += 1
            else:
                or_short_reverted += 1
    total_long = or_long_followed + or_long_reverted
    total_short = or_short_followed + or_short_reverted
    if total_long:
        print(f"  LONG  break  → followed {or_long_followed}/{total_long} = {or_long_followed/total_long:.0%}")
    if total_short:
        print(f"  SHORT break  → followed {or_short_followed}/{total_short} = {or_short_followed/total_short:.0%}")
    print(f"  If <50% followed → MEAN REVERSION trade (fade the break)")
    print(f"  If >60% followed → TREND trade (ORB strategy works)")


def pattern_rsi2(daily: dict[str, dict]) -> None:
    """RSI(2) extremes on daily — does oversold/overbought revert?"""
    dates = sorted(daily.keys())
    closes = [daily[d]["close"] for d in dates]
    if len(closes) < 10:
        return
    # Wilder RSI(2)
    def rsi2(prices):
        gains = []
        losses = []
        rsi_vals = [None]
        for i in range(1, len(prices)):
            ch = prices[i] - prices[i-1]
            gains.append(max(ch, 0))
            losses.append(max(-ch, 0))
            if len(gains) < 2:
                rsi_vals.append(None)
                continue
            avg_gain = sum(gains[-2:]) / 2
            avg_loss = sum(losses[-2:]) / 2
            if avg_loss == 0:
                rsi_vals.append(100)
            else:
                rs = avg_gain / avg_loss
                rsi_vals.append(100 - 100 / (1 + rs))
        return rsi_vals
    rsi = rsi2(closes)

    print("\n[5] RSI(2) reversion on daily closes (5-day hold after signal)")
    long_signal_returns = []
    short_signal_returns = []
    for i in range(len(rsi)):
        if rsi[i] is None:
            continue
        # Need 5 forward bars
        if i + 5 >= len(closes):
            continue
        future_ret = (closes[i + 5] - closes[i]) / closes[i] * 100
        if rsi[i] < 5:  # extremely oversold
            long_signal_returns.append(future_ret)
        elif rsi[i] > 95:  # extremely overbought
            short_signal_returns.append(-future_ret)  # flip sign for short
    if long_signal_returns:
        wr = sum(1 for r in long_signal_returns if r > 0) / len(long_signal_returns)
        mn = statistics.mean(long_signal_returns)
        print(f"  LONG  signal (RSI<5):  N={len(long_signal_returns):>3}  WR {wr:.0%}  mean +{mn:.3f}% (5d hold)")
    if short_signal_returns:
        wr = sum(1 for r in short_signal_returns if r > 0) / len(short_signal_returns)
        mn = statistics.mean(short_signal_returns)
        print(f"  SHORT signal (RSI>95): N={len(short_signal_returns):>3}  WR {wr:.0%}  mean +{mn:.3f}% (5d hold)")


def pattern_nr_breakout(daily: dict[str, dict]) -> None:
    """Narrow-range day → wide-range day (vol expansion)?"""
    print("\n[6] Narrow-range (NR4) day followed by directional move")
    dates = sorted(daily.keys())
    nr_after_returns_long = []
    nr_after_returns_short = []
    for i in range(4, len(dates)):
        # Is today's range the smallest of last 4?
        today_range = daily[dates[i]]["range"]
        ranges = [daily[dates[i-j]]["range"] for j in range(4)]
        if today_range == min(ranges) and i + 1 < len(dates):
            # Next day return
            next_open = daily[dates[i+1]]["open"]
            next_close = daily[dates[i+1]]["close"]
            ret = (next_close - next_open) / next_open * 100
            # Direction: bias by today's close vs open
            if daily[dates[i]]["close"] > daily[dates[i]]["open"]:
                nr_after_returns_long.append(ret)
            else:
                nr_after_returns_short.append(-ret)
    if nr_after_returns_long:
        wr = sum(1 for r in nr_after_returns_long if r > 0) / len(nr_after_returns_long)
        mn = statistics.mean(nr_after_returns_long)
        print(f"  NR4 bullish bias  → next-day long  N={len(nr_after_returns_long):>3}  WR {wr:.0%}  mean +{mn:.3f}%")
    if nr_after_returns_short:
        wr = sum(1 for r in nr_after_returns_short if r > 0) / len(nr_after_returns_short)
        mn = statistics.mean(nr_after_returns_short)
        print(f"  NR4 bearish bias  → next-day short N={len(nr_after_returns_short):>3}  WR {wr:.0%}  mean +{mn:.3f}%")


def pattern_pit_close(bars_rth: list[dict]) -> None:
    """Last 30 min — does direction reverse?"""
    print("\n[7] Pit-close reversion (last 30 min direction vs rest of session)")
    sessions = defaultdict(list)
    for b in bars_rth:
        sessions[b["date"]].append(b)
    reversals = 0
    continuations = 0
    for d, bars in sessions.items():
        if len(bars) < 8:
            continue
        bars = sorted(bars, key=lambda b: b["hhmm"])
        last_30 = [b for b in bars if 1530 <= b["hhmm"] <= 1600]
        if len(last_30) < 2:
            continue
        rest = [b for b in bars if b["hhmm"] < 1530]
        if not rest:
            continue
        rest_dir = rest[-1]["close"] - rest[0]["open"]
        last_dir = last_30[-1]["close"] - last_30[0]["open"]
        if rest_dir * last_dir < 0:  # opposite signs
            reversals += 1
        else:
            continuations += 1
    total = reversals + continuations
    if total:
        print(f"  Reversal in last 30 min: {reversals}/{total} = {reversals/total:.0%}")
        if reversals / total > 0.55:
            print(f"  ⭐ Pit-close fade is a viable setup")


def main() -> None:
    for root in ("MGC", "MCL"):
        print(f"\n{'=' * 72}")
        print(f"PATTERN SCAN — {root}")
        print('=' * 72)
        path = f"backtest_data/{root.lower()}_15min_continuous.csv"
        if not Path(path).exists():
            print(f"  missing data file: {path}")
            continue
        bars = load_15m(path)
        sessions = group_by_session(bars)
        daily = daily_summary(sessions)
        bars_rth = [b for b in bars if 930 <= b["hhmm"] <= 1600]
        print(f"  Sessions: {len(daily)}  |  RTH 15m bars: {len(bars_rth)}")

        pattern_dow(daily)
        pattern_tod(bars_rth)
        pattern_gap(daily, bars_rth)
        pattern_or_followthrough(sessions, daily)
        pattern_rsi2(daily)
        pattern_nr_breakout(daily)
        pattern_pit_close(bars_rth)


if __name__ == "__main__":
    main()
