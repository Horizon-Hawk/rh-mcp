"""yfinance wake-up + persistent session helper.

yfinance is reliable when warm but slow on cold calls due to:
  - DNS lookup to query1.finance.yahoo.com (~50-200ms)
  - Fresh TLS handshake (~200-500ms)
  - Yahoo server cold-cache response (1-10s, variable)

This module:
  1. Maintains a persistent requests.Session with connection pool (keepalive)
  2. Auto-fires a tiny warmup query in a background thread at import time
  3. Exposes warmup() for explicit pre-warming before known-hot scan windows
  4. Exposes a Ticker() factory that pins the session for reuse

Usage:
    from rh_mcp.analysis import yf_warmup
    df = yf_warmup.Ticker("MNQ=F").history(period="5d", interval="15m")
"""
from __future__ import annotations

import threading
import time
from typing import Optional

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

# yfinance >= 0.2.4x requires curl_cffi session and rejects requests.Session.
# We rely on yfinance's internal session (which already does pooling/keepalive)
# and use the warmup call to seed DNS + TLS + Yahoo server cache at the OS level.

# Warmup state
_WARMUP_LOCK = threading.Lock()
_WARMED_AT: Optional[float] = None
_WARMUP_RESULT: Optional[dict] = None


def Ticker(ticker: str):
    """yf.Ticker — uses yfinance's internal session (it handles its own pooling)."""
    if yf is None:
        raise ImportError("yfinance not installed")
    return yf.Ticker(ticker)


def _call_with_timeout(fn, timeout_s: float):
    """Run fn() on a daemon thread, return its result or raise TimeoutError if
    it exceeds timeout_s. The underlying call may continue running (Python
    threads can't be killed) but the caller is unblocked.
    """
    result: dict = {}

    def runner():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise TimeoutError(f"call exceeded {timeout_s}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def warmup(force: bool = False, timeout_s: float = 5.0) -> dict:
    """Fire a tiny yfinance call to warm DNS + TLS + Yahoo server cache.

    Hard timeout (default 5s) prevents Yahoo slowdowns from hanging the caller.
    Idempotent within 60s unless force=True.

    Returns dict with 'success', 'elapsed_s', 'cached'.
    """
    global _WARMED_AT, _WARMUP_RESULT
    now = time.time()
    if _WARMUP_RESULT is not None and not force and _WARMED_AT and (now - _WARMED_AT < 60):
        return {**_WARMUP_RESULT, "cached": True}

    with _WARMUP_LOCK:
        if _WARMUP_RESULT is not None and not force and _WARMED_AT and (time.time() - _WARMED_AT < 60):
            return {**_WARMUP_RESULT, "cached": True}

        if yf is None:
            res = {"success": False, "error": "yfinance not installed"}
            _WARMUP_RESULT = res
            _WARMED_AT = time.time()
            return res

        t0 = time.time()
        try:
            df = _call_with_timeout(
                lambda: yf.Ticker("^VIX").history(period="1d", interval="1d"),
                timeout_s=timeout_s,
            )
            elapsed = time.time() - t0
            res = {
                "success": True,
                "elapsed_s": round(elapsed, 2),
                "bars_returned": len(df) if df is not None else 0,
                "warmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except TimeoutError:
            elapsed = time.time() - t0
            res = {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": f"yahoo did not respond within {timeout_s}s — use massive.com for MNQ instead",
            }
        except Exception as e:
            elapsed = time.time() - t0
            res = {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": f"{type(e).__name__}: {e}",
            }
        _WARMUP_RESULT = res
        _WARMED_AT = time.time()
        return res


def _bg_warmup() -> None:
    """Background thread: fires warmup ~1s after import to seed the session
    without blocking import or the first real caller. Failures are swallowed.
    """
    try:
        time.sleep(1.0)  # let the import chain settle first
        warmup()
    except Exception:
        pass


# Fire background warmup on module import — non-blocking
_BG_THREAD = threading.Thread(target=_bg_warmup, daemon=True, name="yf_warmup")
_BG_THREAD.start()
