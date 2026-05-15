"""Stitch front-month continuous series for any micro futures root.

Source: C:/Users/algee/MNQ historicals/{ROOT}/{ROOT} MM-YY.Last.txt
Format: YYYYMMDD HHMMSS;open;high;low;close;volume  (1-min bars, ET)

Usage:
    python _stitch_micros.py MES
    python _stitch_micros.py MGC
    python _stitch_micros.py MCL

For each session_end_date, the front-month contract is whichever had the
highest total volume on that date. Emit only that contract's bars for the
session — clean continuous series, no overlap.

Outputs (per root):
  backtest_data/{root_lower}_1min_continuous.csv
  backtest_data/{root_lower}_15min_continuous.csv
  backtest_data/{root_lower}_1d_continuous.csv  (daily RTH-aggregated)
"""
from __future__ import annotations
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC_BASE = Path("C:/Users/algee/MNQ historicals")


def parse_line(line: str) -> dict | None:
    parts = line.strip().split(";")
    if len(parts) != 6:
        return None
    try:
        ts = datetime.strptime(parts[0], "%Y%m%d %H%M%S")
        return {
            "ts": ts,
            "open": float(parts[1]),
            "high": float(parts[2]),
            "low": float(parts[3]),
            "close": float(parts[4]),
            "volume": int(parts[5]),
        }
    except (ValueError, IndexError):
        return None


def load_contract(path: Path) -> list[dict]:
    bars = []
    with path.open() as f:
        for line in f:
            b = parse_line(line)
            if b is None:
                continue
            b["contract"] = path.stem
            b["session"] = b["ts"].date().isoformat()
            bars.append(b)
    return bars


def stitch(root: str) -> None:
    root = root.upper()
    src = SRC_BASE / root
    if not src.exists():
        print(f"Source directory not found: {src}")
        return

    out_1m  = Path(f"backtest_data/{root.lower()}_1min_continuous.csv")
    out_15m = Path(f"backtest_data/{root.lower()}_15min_continuous.csv")
    out_1d  = Path(f"backtest_data/{root.lower()}_1d_continuous.csv")
    out_1m.parent.mkdir(parents=True, exist_ok=True)

    # Load every contract
    print(f"=== {root} ===")
    all_bars: dict[str, list[dict]] = {}
    files = sorted(src.glob(f"{root} *.txt"))
    for path in files:
        bars = load_contract(path)
        if bars:
            ticker = path.stem
            all_bars[ticker] = bars
            print(f"  {ticker}: {len(bars):>7,} bars  "
                  f"{bars[0]['ts'].date()} → {bars[-1]['ts'].date()}")

    if not all_bars:
        print("No bars loaded")
        return

    # Per-session volume per contract → pick winner
    session_volume: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ticker, bars in all_bars.items():
        for b in bars:
            session_volume[b["session"]][ticker] += b["volume"]
    winner: dict[str, str] = {
        s: max(vols, key=vols.get) for s, vols in session_volume.items()
    }

    sessions_sorted = sorted(winner.keys())
    print(f"\n{len(sessions_sorted)} sessions covered")
    print("Front-month rolls:")
    prev = None
    for s in sessions_sorted:
        if winner[s] != prev:
            print(f"  {s}: {prev} → {winner[s]}")
            prev = winner[s]

    # Emit 1-min bars from winning contract per session
    out_bars: list[dict] = []
    for ticker, bars in all_bars.items():
        for b in bars:
            if winner.get(b["session"]) == ticker:
                out_bars.append(b)
    out_bars.sort(key=lambda b: b["ts"])

    # 1-min continuous
    with out_1m.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "contract"])
        for b in out_bars:
            w.writerow([b["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                       b["open"], b["high"], b["low"], b["close"],
                       b["volume"], b["contract"]])
    print(f"\n1-min:  {len(out_bars):>9,} bars → {out_1m}")

    # Resample to 15-min
    def floor15(ts: datetime) -> datetime:
        m15 = (ts.minute // 15) * 15
        return ts.replace(minute=m15, second=0, microsecond=0)

    buckets: dict[datetime, dict] = {}
    order: list[datetime] = []
    for b in out_bars:
        key = floor15(b["ts"])
        if key not in buckets:
            buckets[key] = {
                "ts": key, "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b["volume"],
            }
            order.append(key)
        else:
            bk = buckets[key]
            bk["high"] = max(bk["high"], b["high"])
            bk["low"] = min(bk["low"], b["low"])
            bk["close"] = b["close"]
            bk["volume"] += b["volume"]
    with out_15m.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for key in order:
            bk = buckets[key]
            w.writerow([bk["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                       bk["open"], bk["high"], bk["low"], bk["close"], bk["volume"]])
    print(f"15-min: {len(order):>9,} bars → {out_15m}")

    # Daily aggregation (RTH 9:30-16:00 ET)
    daily: dict[str, dict] = defaultdict(lambda: {
        "open": None, "high": -1e18, "low": 1e18, "close": None, "volume": 0
    })
    for b in out_bars:
        hhmm = b["ts"].hour * 100 + b["ts"].minute
        if 930 <= hhmm <= 1600:
            d = b["ts"].date().isoformat()
            row = daily[d]
            if row["open"] is None:
                row["open"] = b["open"]
            row["high"] = max(row["high"], b["high"])
            row["low"] = min(row["low"], b["low"])
            row["close"] = b["close"]
            row["volume"] += b["volume"]
    with out_1d.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for d in sorted(daily.keys()):
            row = daily[d]
            if row["open"] is None:
                continue
            w.writerow([d, row["open"], row["high"], row["low"],
                       row["close"], row["volume"]])
    print(f"daily:  {len(daily):>9,} bars → {out_1d}")

    print(f"\nFirst: {out_bars[0]['ts']}  ({out_bars[0]['contract']})")
    print(f"Last:  {out_bars[-1]['ts']}  ({out_bars[-1]['contract']})\n")


if __name__ == "__main__":
    roots = sys.argv[1:] if len(sys.argv) > 1 else ["MES", "MGC", "MCL"]
    for root in roots:
        stitch(root)
