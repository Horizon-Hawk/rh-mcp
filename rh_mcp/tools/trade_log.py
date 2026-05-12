"""Trade-log MCP tool wrappers — calls in-process rh_mcp.analysis.trade_log directly."""

from __future__ import annotations

from rh_mcp.analysis import trade_log as _tl
from rh_mcp.analysis import trade_stats as _ts


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
        strategy: 'momentum_breakout', 'debit_spread', 'iron_condor', 'earnings_drift', 'ah_earnings',
                  'dead_cat_short', 'intraday', 'spike_entry'.
        entry: Fill price.
        shares: Quantity.
        stop: Stop-loss price (optional).
        target: Profit target price (optional).
        thesis: Free-text setup description.
    """
    try:
        trade_id = _tl.open_trade(
            account=account, ticker=ticker, direction=direction, strategy=strategy,
            entry_price=entry, shares=shares,
            stop_price=stop, target_price=target, thesis=thesis,
        )
    except Exception as e:
        return {"success": False, "error": f"log_open failed: {e}"}
    return {
        "success": True,
        "trade_id": trade_id,
        "ticker": ticker.upper(),
        "direction": direction.lower(),
        "shares": shares,
        "entry": entry,
        "stop": stop,
        "target": target,
        "csv_path": str(_tl.CSV_PATH),
    }


def log_partial(
    trade_id: str,
    exit_price: float,
    shares: float,
    reason: str = "",
    notes: str = "",
) -> dict:
    """Record a partial exit. Trade stays OPEN unless this closes remaining shares."""
    try:
        ok, remaining = _tl.partial_exit(trade_id, exit_price, shares, reason, notes)
    except Exception as e:
        return {"success": False, "error": f"log_partial failed: {e}"}
    if not ok:
        if remaining is None:
            return {"success": False, "error": f"trade {trade_id} not found or already closed"}
        return {"success": False, "error": f"requested {shares} > remaining {remaining}"}
    return {
        "success": True,
        "trade_id": trade_id,
        "remaining_shares": remaining,
        "auto_closed": (remaining or 0) < 0.0001,
    }


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
    try:
        ok = _tl.close_trade(trade_id, exit_price, grade=grade, exit_reason=reason, notes=notes)
    except Exception as e:
        return {"success": False, "error": f"log_close failed: {e}"}
    if not ok:
        return {"success": False, "error": f"trade {trade_id} not found or already closed"}
    return {"success": True, "trade_id": trade_id, "exit_price": exit_price, "grade": grade or None}


def list_open_trades() -> dict:
    """List all OPEN trades from the trade log CSV."""
    try:
        rows = _tl.get_open_trades()
    except Exception as e:
        return {"success": False, "error": f"list_open_trades failed: {e}"}
    return {"success": True, "count": len(rows), "trades": rows, "csv_path": str(_tl.CSV_PATH)}


def list_all_trades(limit: int = 50) -> dict:
    """List recent trades (open + closed). limit caps the return count from the end."""
    try:
        rows = _tl.get_all_trades()
    except Exception as e:
        return {"success": False, "error": f"list_all_trades failed: {e}"}
    trimmed = rows[-limit:] if limit and len(rows) > limit else rows
    return {"success": True, "count": len(trimmed), "total": len(rows), "trades": trimmed}


def trade_stats() -> dict:
    """TTS qualification + performance metrics (win rate, P&L, holding period, trade-day ratio)."""
    try:
        return _ts.analyze()
    except Exception as e:
        return {"success": False, "error": f"trade_stats failed: {e}"}
