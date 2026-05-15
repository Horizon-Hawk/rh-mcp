"""Stitch MES front-month series from per-contract .txt files.

Source: C:/Users/algee/MNQ historicals/MES/MES MM-YY.Last.txt
Format: YYYYMMDD HHMMSS;open;high;low;close;volume  (1-min bars, ET)

For each session_end_date (calendar date), the front-month contract is
whichever contract had the highest total volume on that date. We emit
only that contract's bars for that session, producing a clean continuous
series.

Outputs:
  backtest_data/mes_1min_continuous.csv  (1-min)
  backtest_data/mes_15min_continuous.csv (15-min, ORB-ready)
"""
from __future__ import annotations
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC = Path("C:/Users/algee/MNQ historicals/MES")
OUT_1M = Path("backtest_data/mes_1min_continuous.csv")
OUT_15M = Path("backtest_data/mes_15min_continuous.csv")


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
            b["contract"] = path.stem  # e.g. "MES 03-23.Last"
            b["session"] = b["ts"].date().isoformat()
            bars.append(b)
    return bars


def main() -> None:
    OUT_1M.parent.mkdir(parents=True, exist_ok=True)

    # Load every contract
    all_bars: dict[str, list[dict]] = {}
    files = sorted(SRC.glob("MES *.txt"))
    for path in files:
        bars = load_contract(path)
        if bars:
            ticker = path.stem
            all_bars[ticker] = bars
            print(f"  {ticker}: {len(bars):>7,} bars  "
                  f"{bars[0]['ts'].date()} → {bars[-1]['ts'].date()}")

    if not all_bars:
        print("No bars loaded — check SRC path")
        return

    # Per-session volume per contract → pick winner
    session_volume: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ticker, bars in all_bars.items():
        for b in bars:
            session_volume[b["session"]][ticker] += b["volume"]

    winner: dict[str, str] = {}
    for session, vols in session_volume.items():
        winner[session] = max(vols, key=vols.get)

    # Print roll points
    sessions_sorted = sorted(winner.keys())
    print(f"\n{len(sessions_sorted)} sessions covered")
    print("\nFront-month rolls:")
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

    # Write 1-min continuous
    with OUT_1M.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "contract"])
        for b in out_bars:
            w.writerow([b["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                       b["open"], b["high"], b["low"], b["close"],
                       b["volume"], b["contract"]])
    print(f"\n1-min:  {len(out_bars):>9,} bars → {OUT_1M}")

    # Resample to 15-min — bucket by floor of timestamp
    def floor15(ts: datetime) -> datetime:
        m15 = (ts.minute // 15) * 15
        return ts.replace(minute=m15, second=0, microsecond=0)

    buckets: dict[datetime, dict] = {}
    bucket_order: list[datetime] = []
    for b in out_bars:
        key = floor15(b["ts"])
        if key not in buckets:
            buckets[key] = {
                "ts": key, "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b["volume"],
            }
            bucket_order.append(key)
        else:
            bk = buckets[key]
            bk["high"] = max(bk["high"], b["high"])
            bk["low"] = min(bk["low"], b["low"])
            bk["close"] = b["close"]
            bk["volume"] += b["volume"]

    with OUT_15M.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for key in bucket_order:
            bk = buckets[key]
            w.writerow([bk["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                       bk["open"], bk["high"], bk["low"], bk["close"], bk["volume"]])
    print(f"15-min: {len(bucket_order):>9,} bars → {OUT_15M}")

    print(f"\nFirst: {out_bars[0]['ts']}  ({out_bars[0]['contract']})")
    print(f"Last:  {out_bars[-1]['ts']}  ({out_bars[-1]['contract']})")


if __name__ == "__main__":
    main()
