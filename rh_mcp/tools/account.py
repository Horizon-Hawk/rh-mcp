"""Account and portfolio tools."""

from rh_mcp.lib.rh_client import client
from rh_mcp.auth import default_account


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_portfolio(account_number: str | None = None) -> dict:
    """Get portfolio summary: equity, cash, buying power, positions count, top holdings.

    Args:
        account_number: Robinhood account number. Omit for default account from rh_config.json.
    """
    rh = client()
    acct = account_number or default_account()
    profile = rh.load_portfolio_profile(account_number=acct)
    if not profile:
        return {"success": False, "error": "no portfolio data returned"}

    holdings = rh.build_holdings() or {}
    top = sorted(
        [
            {
                "symbol": sym,
                "quantity": _to_float(h.get("quantity")),
                "avg_cost": _to_float(h.get("average_buy_price")),
                "current_price": _to_float(h.get("price")),
                "market_value": _to_float(h.get("equity")),
                "gain_loss": _to_float(h.get("equity_change")),
                "pct_gain": _to_float(h.get("percent_change")),
            }
            for sym, h in holdings.items()
        ],
        key=lambda r: r["market_value"] or 0,
        reverse=True,
    )

    equity = _to_float(profile.get("equity")) or _to_float(profile.get("extended_hours_equity"))
    return {
        "success": True,
        "account_number": acct,
        "equity": equity,
        "cash": _to_float(profile.get("withdrawable_amount")),
        "buying_power": _to_float(profile.get("market_value")),
        "num_positions": len(holdings),
        "top_holdings": top[:10],
    }


def get_positions(account_number: str | None = None) -> dict:
    """Get all open stock positions with quantity, avg cost, current price, P&L."""
    rh = client()
    acct = account_number or default_account()
    holdings = rh.build_holdings() or {}
    positions = [
        {
            "symbol": sym,
            "quantity": _to_float(h.get("quantity")),
            "avg_cost": _to_float(h.get("average_buy_price")),
            "current_price": _to_float(h.get("price")),
            "market_value": _to_float(h.get("equity")),
            "gain_loss": _to_float(h.get("equity_change")),
            "pct_gain": _to_float(h.get("percent_change")),
        }
        for sym, h in holdings.items()
    ]
    return {"success": True, "account_number": acct, "count": len(positions), "positions": positions}


def get_open_orders(account_number: str | None = None) -> dict:
    """Get all pending stock orders."""
    rh = client()
    raw = rh.get_all_open_stock_orders(account_number=account_number) or []
    orders = []
    for o in raw:
        # Resolve instrument URL → symbol
        sym = None
        if o.get("instrument"):
            inst = rh.get_instrument_by_url(o["instrument"])
            sym = (inst or {}).get("symbol")
        orders.append({
            "id": o.get("id"),
            "symbol": sym,
            "side": o.get("side"),
            "quantity": _to_float(o.get("quantity")),
            "price": _to_float(o.get("price")),
            "stop_price": _to_float(o.get("stop_price")),
            "type": o.get("type"),
            "trigger": o.get("trigger"),
            "time_in_force": o.get("time_in_force"),
            "state": o.get("state"),
            "extended_hours": o.get("extended_hours"),
            "created_at": o.get("created_at"),
        })
    return {"success": True, "count": len(orders), "orders": orders}


def list_accounts() -> dict:
    """List all linked Robinhood accounts."""
    rh = client()
    profile = rh.load_account_profile()
    if not profile:
        return {"success": False, "error": "no account profile returned"}
    # robin_stocks returns a single account dict; for multi-account a different endpoint is needed
    return {
        "success": True,
        "accounts": [{
            "account_number": profile.get("account_number"),
            "type": profile.get("type"),
            "buying_power": _to_float(profile.get("buying_power")),
            "cash_held_for_orders": _to_float(profile.get("cash_held_for_orders")),
            "day_trade_count": profile.get("day_trade_count"),
            "pattern_day_trader": profile.get("is_pinned"),
            "margin_balances": profile.get("margin_balances"),
        }],
    }


def get_portfolio_history(
    span: str = "year",
    interval: str = "day",
    bounds: str = "regular",
    account_number: str | None = None,
) -> dict:
    """Get historical portfolio equity for drawing the equity curve.

    Args:
        span: 'day' | 'week' | 'month' | '3month' | 'year' | '5year' | 'all'.
        interval: '5minute' | '10minute' | 'hour' | 'day' | 'week'.
                  Coarser intervals required for longer spans.
        bounds: 'regular' (RTH only) | 'extended' (incl. AH).
    """
    rh = client()
    acct = account_number or default_account()
    raw = rh.get_historical_portfolio(
        account_number=acct, interval=interval, span=span, bounds=bounds,
    )
    if not raw:
        return {"success": False, "error": "no historical data"}
    points = []
    for p in raw.get("equity_historicals", []) if isinstance(raw, dict) else raw:
        points.append({
            "begins_at": p.get("begins_at"),
            "session": p.get("session"),
            "adjusted_open_equity": _to_float(p.get("adjusted_open_equity")),
            "adjusted_close_equity": _to_float(p.get("adjusted_close_equity")),
            "open_equity": _to_float(p.get("open_equity")),
            "close_equity": _to_float(p.get("close_equity")),
            "net_return": _to_float(p.get("net_return")),
        })
    summary = {
        "success": True,
        "span": span, "interval": interval, "bounds": bounds,
        "count": len(points),
        "first": points[0] if points else None,
        "last": points[-1] if points else None,
        "points": points,
    }
    if isinstance(raw, dict):
        summary["start_equity"] = _to_float(raw.get("start_date_close_equity"))
        summary["total_return"] = _to_float(raw.get("total_return"))
        summary["use_24_hour_market"] = raw.get("use_24_hour_market")
    return summary


def get_account_status(account_number: str | None = None) -> dict:
    """Get account status: PDT flag, day trades, restrictions, margin info."""
    rh = client()
    profile = rh.load_account_profile()
    if not profile:
        return {"success": False, "error": "no account profile"}
    margin = profile.get("margin_balances") or {}
    return {
        "success": True,
        "account_number": profile.get("account_number"),
        "type": profile.get("type"),
        "day_trade_count": profile.get("day_trade_count"),
        "buying_power": _to_float(profile.get("buying_power")),
        "cash": _to_float(profile.get("cash")),
        "cash_available_for_withdrawal": _to_float(profile.get("cash_available_for_withdrawal")),
        "uncleared_deposits": _to_float(profile.get("uncleared_deposits")),
        "margin_buying_power": _to_float(margin.get("overnight_buying_power")),
        "margin_used": _to_float(margin.get("margin_limit")),
        "deactivated": profile.get("deactivated"),
        "max_ach_early_access_amount": _to_float(profile.get("max_ach_early_access_amount")),
    }
