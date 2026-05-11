"""Trade logger wrappers — wraps trade_logger.py as MCP tools."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(os.environ.get("RH_SCRIPTS_DIR", r"C:\Users\algee"))
TRADE_CSV = Path(os.environ.get("RH_TRADE_CSV", r"C:\Users\algee\robinhood_trades.csv"))


def _run_logger(args: list[str], timeout: int = 30) -> dict:
    script = SCRIPTS_DIR / "trade_logger.py"
    if not script.exists():
        return {"success": False, "error": f"trade_logger.py not found at {script}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as e:
        return {"success": False, "error": f"trade_logger failed to launch: {e}"}
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip() if result.returncode else "",
        "returncode": result.returncode,
    }


def log_open(
    account: str,
    ticker: str,
    direction: str,
    strategy: str,
    entry: float,
    shares: float,
    stop: float | None = None,
    target: float | None = None,
    thesis: str = "",
) -> dict:
    """Record a new trade opening.

    Args:
        account: Robinhood account number.
        ticker: Stock symbol.
        direction: 'long' or 'short'.
        strategy: 'momentum_breakout', 'debit_spread', 'earnings_drift', 'ah_earnings',
                  'dead_cat_short', 'intraday', 'spike_entry'.
        entry: Fill price.
        shares: Quantity.
        stop: Stop-loss price (optional).
        target: Profit target price (optional).
        thesis: Free-text setup description.
    """
    args = [
        "open",
        "--account", str(account),
        "--ticker", ticker.upper(),
        "--direction", direction.lower(),
        "--strategy", strategy,
        "--entry", str(entry),
        "--shares", str(shares),
    ]
    if stop is not None:
        args += ["--stop", str(stop)]
    if target is not None:
        args += ["--target", str(target)]
    if thesis:
        args += ["--thesis", thesis]
    return _run_logger(args)


def log_partial(
    trade_id: str,
    exit_price: float,
    shares: float,
    reason: str = "",
    notes: str = "",
) -> dict:
    """Record a partial exit. Trade stays OPEN unless this closes remaining shares."""
    args = [
        "partial",
        "--id", trade_id,
        "--exit", str(exit_price),
        "--shares", str(shares),
    ]
    if reason:
        args += ["--reason", reason]
    if notes:
        args += ["--notes", notes]
    return _run_logger(args)


def log_close(
    trade_id: str,
    exit_price: float,
    grade: str = "",
    reason: str = "",
    notes: str = "",
) -> dict:
    """Close remaining shares of a trade.

    Args:
        grade: 'A+'|'A'|'B'|'C'|'F' (optional).
        reason: 'target'|'stop'|'pattern'|'manual'|'time'|'news'|'trail'.
    """
    args = ["close", "--id", trade_id, "--exit", str(exit_price)]
    if grade:
        args += ["--grade", grade]
    if reason:
        args += ["--reason", reason]
    if notes:
        args += ["--notes", notes]
    return _run_logger(args)


def list_open_trades() -> dict:
    """List all OPEN trades from the trade log CSV."""
    result = _run_logger(["list-open"])
    return {**result, "csv_path": str(TRADE_CSV)}


def list_all_trades(limit: int = 50) -> dict:
    """List recent trades (open + closed). limit defaults to 50."""
    result = _run_logger(["list-all"])
    # Trim output to last N lines for readability
    if result.get("success") and result.get("stdout"):
        lines = result["stdout"].splitlines()
        result["stdout"] = "\n".join(lines[-limit:])
    return result


def trade_stats() -> dict:
    """Run trade_stats.py for TTS qualification + performance metrics (win rate, P&L, etc.)."""
    script = SCRIPTS_DIR / "trade_stats.py"
    if not script.exists():
        return {"success": False, "error": f"trade_stats.py not found at {script}",
                "hint": "Set RH_SCRIPTS_DIR env var."}
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as e:
        return {"success": False, "error": f"trade_stats failed: {e}"}
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr if result.returncode else "",
        "returncode": result.returncode,
    }
