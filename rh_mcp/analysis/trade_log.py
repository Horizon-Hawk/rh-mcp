"""Append-only structured trade log — CSV I/O for the trade journal.

Ported from trade_logger.py CLI. All functions operate on a single CSV file
(configurable via RH_TRADE_CSV env var). The MCP tool wrappers call these
directly instead of spawning trade_logger.py as a subprocess.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

CSV_PATH = Path(os.environ.get("RH_TRADE_CSV", r"C:\Users\algee\robinhood_trades.csv"))

FIELDS = [
    "trade_id", "account", "ticker", "direction", "strategy",
    "entry_date", "entry_time", "entry_price", "shares", "position_value",
    "stop_price", "target_price",
    "exit_date", "exit_time", "exit_price",
    "holding_period_minutes", "pnl_dollars", "pnl_pct",
    "grade", "thesis", "exit_reason", "notes", "status",
    "remaining_shares", "partials",
]


def _ensure_csv() -> None:
    """Create CSV if missing; migrate header if new columns added."""
    if not CSV_PATH.exists():
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
        return

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        current_fields = reader.fieldnames or []
        rows = list(reader)

    if set(current_fields) >= set(FIELDS):
        return

    for row in rows:
        if "remaining_shares" not in row or not row.get("remaining_shares"):
            row["remaining_shares"] = row.get("shares", "") if row.get("status") == "OPEN" else "0"
        if "partials" not in row or not row.get("partials"):
            row["partials"] = "[]"
        for field in FIELDS:
            row.setdefault(field, "")

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _pnl(direction: str, entry_price: float, exit_price: float, shares: float) -> float:
    if direction == "long":
        return (exit_price - entry_price) * shares
    return (entry_price - exit_price) * shares


def open_trade(
    account: str,
    ticker: str,
    direction: str,
    strategy: str,
    entry_price: float,
    shares: float,
    stop_price: float | None = None,
    target_price: float | None = None,
    thesis: str = "",
    entry_dt: datetime | None = None,
) -> str:
    """Create an OPEN row. Returns the trade_id."""
    _ensure_csv()
    entry_dt = entry_dt or datetime.now()
    trade_id = uuid.uuid4().hex[:8]
    row = {
        "trade_id": trade_id,
        "account": account,
        "ticker": ticker.upper(),
        "direction": direction.lower(),
        "strategy": strategy,
        "entry_date": entry_dt.strftime("%Y-%m-%d"),
        "entry_time": entry_dt.strftime("%H:%M:%S"),
        "entry_price": f"{entry_price:.4f}",
        "shares": f"{shares:.4f}",
        "position_value": f"{abs(shares * entry_price):.2f}",
        "stop_price": f"{stop_price:.4f}" if stop_price is not None else "",
        "target_price": f"{target_price:.4f}" if target_price is not None else "",
        "exit_date": "",
        "exit_time": "",
        "exit_price": "",
        "holding_period_minutes": "",
        "pnl_dollars": "",
        "pnl_pct": "",
        "grade": "",
        "thesis": thesis,
        "exit_reason": "",
        "notes": "",
        "status": "OPEN",
        "remaining_shares": f"{shares:.4f}",
        "partials": "[]",
    }
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
    return trade_id


def _finalize_close_row(row: dict, exit_dt: datetime, grade: str = "", exit_reason: str = "", notes: str = "") -> None:
    entry_str = f"{row['entry_date']} {row['entry_time']}"
    entry_dt = datetime.strptime(entry_str, "%Y-%m-%d %H:%M:%S")
    holding_minutes = int((exit_dt - entry_dt).total_seconds() / 60)

    entry_price = float(row["entry_price"])
    orig_shares = float(row["shares"])
    partials = json.loads(row.get("partials") or "[]")

    total_pnl = sum(p["pnl"] for p in partials)
    total_value = sum(p["price"] * p["shares"] for p in partials)
    total_shares_closed = sum(p["shares"] for p in partials)

    weighted_avg_exit = (total_value / total_shares_closed) if total_shares_closed else entry_price
    pnl_pct = (total_pnl / (entry_price * orig_shares)) * 100 if entry_price and orig_shares else 0.0

    row["exit_date"] = exit_dt.strftime("%Y-%m-%d")
    row["exit_time"] = exit_dt.strftime("%H:%M:%S")
    row["exit_price"] = f"{weighted_avg_exit:.4f}"
    row["holding_period_minutes"] = str(holding_minutes)
    row["pnl_dollars"] = f"{total_pnl:.2f}"
    row["pnl_pct"] = f"{pnl_pct:.2f}"
    if grade:
        row["grade"] = grade
    row["exit_reason"] = exit_reason or row.get("exit_reason", "")
    if notes:
        row["notes"] = notes
    row["status"] = "CLOSED"
    row["remaining_shares"] = "0"


def partial_exit(
    trade_id: str,
    exit_price: float,
    shares_to_exit: float,
    reason: str = "",
    notes: str = "",
    exit_dt: datetime | None = None,
) -> tuple[bool, float | None]:
    """Record a partial exit. Auto-closes if remaining hits 0.

    Returns (True, remaining_after) on success, (False, current_or_None) on error.
    """
    _ensure_csv()
    exit_dt = exit_dt or datetime.now()

    with CSV_PATH.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if row["trade_id"] == trade_id and row["status"] == "OPEN":
            entry_price = float(row["entry_price"])
            direction = row["direction"]
            remaining = float(row.get("remaining_shares") or row["shares"])

            if shares_to_exit > remaining + 0.0001:
                return False, remaining

            partial_pnl = _pnl(direction, entry_price, exit_price, shares_to_exit)
            partials = json.loads(row.get("partials") or "[]")
            partials.append({
                "date": exit_dt.strftime("%Y-%m-%d"),
                "time": exit_dt.strftime("%H:%M:%S"),
                "price": round(exit_price, 4),
                "shares": round(shares_to_exit, 4),
                "pnl": round(partial_pnl, 2),
                "reason": reason,
                "notes": notes,
            })

            new_remaining = remaining - shares_to_exit
            row["partials"] = json.dumps(partials)
            row["remaining_shares"] = f"{new_remaining:.4f}"

            if new_remaining < 0.0001:
                _finalize_close_row(row, exit_dt, exit_reason=reason or "final partial", notes=notes)

            with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            return True, new_remaining

    return False, None


def close_trade(
    trade_id: str,
    exit_price: float,
    grade: str = "",
    exit_reason: str = "",
    notes: str = "",
    exit_dt: datetime | None = None,
) -> bool:
    """Close out remaining shares. Returns True if the trade was found and closed."""
    _ensure_csv()
    exit_dt = exit_dt or datetime.now()

    with CSV_PATH.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if row["trade_id"] == trade_id and row["status"] == "OPEN":
            entry_price = float(row["entry_price"])
            direction = row["direction"]
            remaining = float(row.get("remaining_shares") or row["shares"])

            final_pnl = _pnl(direction, entry_price, exit_price, remaining)
            partials = json.loads(row.get("partials") or "[]")
            partials.append({
                "date": exit_dt.strftime("%Y-%m-%d"),
                "time": exit_dt.strftime("%H:%M:%S"),
                "price": round(exit_price, 4),
                "shares": round(remaining, 4),
                "pnl": round(final_pnl, 2),
                "reason": exit_reason or "final close",
                "notes": notes,
            })
            row["partials"] = json.dumps(partials)
            row["remaining_shares"] = "0"

            _finalize_close_row(row, exit_dt, grade=grade, exit_reason=exit_reason, notes=notes)

            with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            return True

    return False


def get_open_trades() -> list[dict]:
    _ensure_csv()
    with CSV_PATH.open("r", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["status"] == "OPEN"]


def get_all_trades() -> list[dict]:
    _ensure_csv()
    with CSV_PATH.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def delete_trade(trade_id: str) -> bool:
    _ensure_csv()
    with CSV_PATH.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    new_rows = [r for r in rows if r["trade_id"] != trade_id]
    if len(new_rows) == len(rows):
        return False
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(new_rows)
    return True
