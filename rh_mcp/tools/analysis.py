"""Analysis script wrappers — wraps the existing Python scripts as MCP tools.

Each script is invoked as a subprocess. Scripts must print a final line starting
with 'JSON:' followed by the parseable JSON result. This matches the pattern in
the user's existing scripts (stack_grade.py, order_book.py, historicals.py, etc.).

Configurable via env var RH_SCRIPTS_DIR (default: C:\\Users\\algee\\).
For shared distribution, friends point this at their own scripts directory.
If the scripts don't exist, the tools return a clear "script not available" error.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(os.environ.get("RH_SCRIPTS_DIR", r"C:\Users\algee"))


def _run_script(script_name: str, args: list[str], timeout: int = 60) -> dict:
    """Run a script and parse its 'JSON: ...' final-line output."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {
            "success": False,
            "error": f"script not found: {script_path}",
            "hint": "Set RH_SCRIPTS_DIR env var to point at your scripts directory.",
        }
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"{script_name} timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": f"{script_name} failed to launch: {e}"}

    # Find the JSON: line
    json_payload = None
    for line in result.stdout.splitlines():
        if line.startswith("JSON:"):
            json_payload = line[len("JSON:"):].strip()
    if not json_payload:
        return {
            "success": False,
            "error": "no JSON output from script",
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
            "returncode": result.returncode,
        }
    try:
        data = json.loads(json_payload)
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse failed: {e}", "raw": json_payload[:500]}
    return {"success": True, "data": data}


def stack_grade(tickers: str) -> dict:
    """Combined float + relative volume + news catalyst grading.

    Returns per-ticker grade and STACK rating (A+/A/B/SKIP STACK).
    A+ requires all three legs (breakout body + squeeze setup + today catalyst).

    Args:
        tickers: One or more tickers, space- or comma-separated.
    """
    symbols = [t.strip().upper() for t in tickers.replace(",", " ").split() if t.strip()]
    return _run_script("stack_grade.py", symbols, timeout=90)


def order_book_scan(ticker: str, price: float, range_dollars: float | None = None, threshold: int | None = None) -> dict:
    """Order book depth + OBI signal at a target price.

    Returns: spread, range walls (bid/ask), OBI top-10/top-20, signal (BUY/SELL/BALANCED).

    Args:
        ticker: Stock symbol.
        price: Price level to center the scan around.
        range_dollars: Half-range in dollars (default auto: $2 if price<$20, $5 normal, $10 if >$100).
        threshold: Min share count to flag a wall (default 500-1000 depending on book depth).
    """
    args = [ticker.upper(), f"{price:.4f}"]
    if range_dollars is not None:
        args += ["--range", str(int(range_dollars))]
    if threshold is not None:
        args += ["--threshold", str(int(threshold))]
    return _run_script("order_book.py", args, timeout=45)


def historicals(ticker: str) -> dict:
    """Daily-bar breakout grading: volume ratio, body %, 20d SMA, ATR, MM target, suggested stop, R:R."""
    return _run_script("historicals.py", [ticker.upper()], timeout=60)


def float_check(ticker: str) -> dict:
    """Float + short-interest squeeze discriminator: SQUEEZE / ELEVATED / NORMAL / LOW_RISK_SHORT."""
    return _run_script("float_check.py", [ticker.upper()], timeout=45)


def iv_rank(ticker: str) -> dict:
    """Implied volatility rank — gauges whether options are expensive or cheap."""
    return _run_script("iv_rank.py", [ticker.upper()], timeout=60)


def options_scan(ticker: str) -> dict:
    """Options chain scan: ATM IV, max pain, OI walls, spread setup ideas."""
    return _run_script("options_scan.py", [ticker.upper()], timeout=90)


def spike_check(ticker: str, spike_price: float, ts: str) -> dict:
    """Validate a volume spike before entering: staleness, drift, fade, current price.

    Args:
        ticker: Stock symbol.
        spike_price: Price level when the spike was detected.
        ts: ISO timestamp 'YYYY-MM-DDTHH:MM:SS' of the spike event.
    """
    return _run_script("spike_check.py", [ticker.upper(), f"{spike_price:.4f}", ts], timeout=30)
