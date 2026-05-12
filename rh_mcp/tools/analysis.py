"""Analysis MCP tool wrappers — thin shims over rh_mcp.analysis.* modules.

All analysis runs in-process inside the MCP server. The legacy subprocess-based
script-spawning path was removed in the script-to-module refactor; the env var
RH_SCRIPTS_DIR is no longer consulted by these wrappers.
"""

from __future__ import annotations


def stack_grade(tickers: str) -> dict:
    """Combined float + relative volume + news catalyst grading.

    Returns per-ticker grade and STACK rating (A+/A/B/SKIP STACK).
    A+ requires all three legs (breakout body + squeeze setup + today catalyst).

    Args:
        tickers: One or more tickers, space- or comma-separated.
    """
    symbols = [t.strip().upper() for t in tickers.replace(",", " ").split() if t.strip()]
    try:
        from rh_mcp.analysis import stack_grade as _stack
    except ImportError as e:
        return {"success": False, "error": f"in-process stack_grade unavailable: {e}"}
    try:
        data = _stack.analyze(symbols)
    except Exception as e:
        return {"success": False, "error": f"stack_grade failed: {e}"}
    return {"success": True, "data": data}


def order_book_scan(ticker: str, price: float, range_dollars: float | None = None, threshold: int | None = None) -> dict:
    """Order book depth + OBI signal at a target price.

    Returns: spread, range walls (bid/ask), OBI top-10/top-20, signal (BUY/SELL/BALANCED).

    Args:
        ticker: Stock symbol.
        price: Price level to center the scan around.
        range_dollars: Half-range in dollars (default auto: $2 if price<$20, $5 normal, $10 if >$100).
        threshold: Min share count to flag a wall (default 500-1000 depending on book depth).
    """
    try:
        from rh_mcp.analysis import order_book as _ob
    except ImportError as e:
        return {"success": False, "error": f"in-process order_book unavailable: {e}"}
    try:
        data = _ob.analyze(ticker.upper(), float(price), range_dollars, threshold)
    except Exception as e:
        return {"success": False, "error": f"order_book failed: {e}"}
    return {"success": True, "data": data}


def historicals(ticker: str) -> dict:
    """Daily-bar breakout grading: volume ratio, body %, 20d SMA, ATR, MM target, suggested stop, R:R."""
    try:
        from rh_mcp.analysis import historicals as _hist
    except ImportError as e:
        return {"success": False, "error": f"in-process historicals unavailable: {e}"}
    try:
        data = _hist.analyze(ticker.upper())
    except Exception as e:
        return {"success": False, "error": f"historicals failed: {e}"}
    return {"success": True, "data": data}


def float_check(ticker: str) -> dict:
    """Float + short-interest squeeze discriminator: SQUEEZE / ELEVATED / NORMAL / LOW_RISK_SHORT."""
    try:
        from rh_mcp.analysis import float_check as _float_check
    except ImportError as e:
        return {"success": False, "error": f"in-process float_check unavailable: {e}"}
    try:
        data = _float_check.analyze(ticker.upper())
    except Exception as e:
        return {"success": False, "error": f"float_check failed: {e}"}
    return {"success": True, "data": data}


def iv_rank(ticker: str) -> dict:
    """Implied volatility rank — gauges whether options are expensive or cheap."""
    try:
        from rh_mcp.analysis import iv_rank as _iv
    except ImportError as e:
        return {"success": False, "error": f"in-process iv_rank unavailable: {e}"}
    try:
        data = _iv.analyze(ticker.upper())
    except Exception as e:
        return {"success": False, "error": f"iv_rank failed: {e}"}
    return {"success": True, "data": data}


def options_scan(ticker: str) -> dict:  # noqa: D401
    """Options chain scan: ATM IV, max pain, OI walls, spread setup ideas."""
    try:
        from rh_mcp.analysis import options_scan as _opts
    except ImportError as e:
        return {"success": False, "error": f"in-process options_scan unavailable: {e}"}
    try:
        data = _opts.analyze(ticker.upper())
    except Exception as e:
        return {"success": False, "error": f"options_scan failed: {e}"}
    return {"success": True, "data": data}


def spike_check(ticker: str, spike_price: float, ts: str) -> dict:
    """Validate a volume spike before entering: staleness, drift, fade, current price.

    Args:
        ticker: Stock symbol.
        spike_price: Price level when the spike was detected.
        ts: ISO timestamp 'YYYY-MM-DDTHH:MM:SS' of the spike event.
    """
    try:
        from rh_mcp.analysis import spike_check as _spike
    except ImportError as e:
        return {"success": False, "error": f"in-process spike_check unavailable: {e}"}
    try:
        data = _spike.analyze(ticker.upper(), spike_price, ts)
    except Exception as e:
        return {"success": False, "error": f"spike_check failed: {e}"}
    return {"success": True, "data": data}
