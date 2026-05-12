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

    # Robinhood's `equity` field includes options market value, but
    # `build_holdings` is stock-only. Surface option exposure separately so
    # the caller can see contracts vs shares + the live unrealized P&L.
    option_count = 0
    option_long_value = 0.0
    option_short_liability = 0.0
    option_unrealized_pnl = 0.0
    try:
        opt_resp = get_option_positions(account_number=acct)
        if opt_resp.get("success"):
            option_count = opt_resp.get("count", 0)
            option_long_value = opt_resp.get("total_long_value", 0.0) or 0.0
            option_short_liability = opt_resp.get("total_short_liability", 0.0) or 0.0
            option_unrealized_pnl = opt_resp.get("net_unrealized_pnl", 0.0) or 0.0
    except Exception:
        pass

    return {
        "success": True,
        "account_number": acct,
        "equity": equity,
        "cash": _to_float(profile.get("withdrawable_amount")),
        "buying_power": _to_float(profile.get("market_value")),
        "num_stock_positions": len(holdings),
        "num_option_positions": option_count,
        "option_long_value": option_long_value,
        "option_short_liability": option_short_liability,
        "option_unrealized_pnl": option_unrealized_pnl,
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
        trailing_peg = o.get("trailing_peg")
        last_trail = o.get("last_trail_price")
        last_trail_amt = _to_float(last_trail.get("amount")) if isinstance(last_trail, dict) else None
        orders.append({
            "id": o.get("id"),
            "symbol": sym,
            "side": o.get("side"),
            "quantity": _to_float(o.get("quantity")),
            "price": _to_float(o.get("price")),
            "stop_price": _to_float(o.get("stop_price")),
            "type": o.get("type"),
            "trigger": o.get("trigger"),
            "trailing_peg": trailing_peg,
            "last_trail_price": last_trail_amt,
            "time_in_force": o.get("time_in_force"),
            "state": o.get("state"),
            "extended_hours": o.get("extended_hours"),
            "created_at": o.get("created_at"),
        })
    return {"success": True, "count": len(orders), "orders": orders}


# ---------------------------------------------------------------------------
# Options visibility — positions and orders
# ---------------------------------------------------------------------------

def _option_id_from_url(url: str | None) -> str | None:
    """Extract UUID from an options instrument URL like
    https://api.robinhood.com/options/instruments/<UUID>/."""
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def _enrich_option_instrument(rh, opt_id: str | None) -> dict:
    """Fetch strike / expiry / option_type / chain_symbol for a given option_id.
    Returns an empty dict on any failure — caller decides how strict to be."""
    if not opt_id:
        return {}
    try:
        instr = rh.options.get_option_instrument_data_by_id(opt_id)
    except Exception:
        return {}
    if not isinstance(instr, dict):
        return {}
    return instr


def _option_mark(rh, opt_id: str | None) -> float | None:
    """Fetch current mark price for an option_id. None on failure."""
    if not opt_id:
        return None
    try:
        md = rh.options.get_option_market_data_by_id(opt_id)
    except Exception:
        return None
    if isinstance(md, list) and md:
        md = md[0]
    if isinstance(md, list) and md:
        md = md[0]
    if not isinstance(md, dict):
        return None
    return _to_float(md.get("mark_price")) or _to_float(md.get("adjusted_mark_price"))


def get_option_positions(account_number: str | None = None) -> dict:
    """Get all open option contract positions enriched with strike/expiry/type
    and current mark + P&L. Robinhood's build_holdings excludes options, so
    this is the only way to see contract holdings from the API.

    Each entry includes:
        ticker, expiry, strike, option_type ('call'/'put'),
        position_type ('long'/'short'), quantity (contracts),
        average_price (per-share cost basis), mark_price, market_value,
        cost_basis, gain_loss, gain_loss_pct, option_id.
    Closed positions (quantity == 0) are filtered out.
    """
    rh = client()
    acct = account_number or default_account()
    try:
        raw = rh.options.get_open_option_positions(account_number=acct) or []
    except Exception as e:
        return {"success": False, "error": f"open option positions fetch failed: {e}"}

    positions = []
    for p in raw:
        qty = _to_float(p.get("quantity")) or 0.0
        if qty <= 0:
            continue
        opt_id = p.get("option_id") or _option_id_from_url(p.get("option"))
        instr = _enrich_option_instrument(rh, opt_id)
        mark = _option_mark(rh, opt_id)
        position_type = (p.get("type") or "").lower()  # 'long' or 'short'

        # Robinhood reports `average_price` as total dollars per CONTRACT
        # (premium × 100). For SHORT positions it's signed NEGATIVE to
        # represent the credit received on open. We normalise to absolute
        # magnitudes here and rely on `position_type` for direction.
        avg_price_raw = _to_float(p.get("average_price"))
        avg_price_abs = abs(avg_price_raw) if avg_price_raw is not None else None
        avg_per_share = (avg_price_abs / 100.0) if avg_price_abs is not None else None
        cost_basis_abs = (avg_price_abs * qty) if avg_price_abs is not None else None
        market_value = (mark * qty * 100) if mark is not None else None

        gain_loss = None
        gain_loss_pct = None
        if market_value is not None and cost_basis_abs is not None and cost_basis_abs != 0:
            if position_type == "short":
                # Short: received credit on open. Profit if mark < entry.
                gain_loss = cost_basis_abs - market_value
            else:
                # Long: paid debit on open. Profit if mark > entry.
                gain_loss = market_value - cost_basis_abs
            gain_loss_pct = round(gain_loss / cost_basis_abs * 100, 2)

        positions.append({
            "option_id": opt_id,
            "ticker": p.get("chain_symbol") or instr.get("chain_symbol"),
            "expiry": instr.get("expiration_date"),
            "strike": _to_float(instr.get("strike_price")),
            "option_type": (instr.get("type") or "").lower() or None,
            "position_type": position_type or None,
            "quantity": qty,
            # Per-share entry price (always positive). Whether it's a debit
            # paid or a credit received is signaled by position_type.
            "average_price_per_share": round(avg_per_share, 4) if avg_per_share is not None else None,
            "average_cost_per_contract": round(avg_price_abs, 2) if avg_price_abs is not None else None,
            "mark_price": round(mark, 4) if mark is not None else None,
            "market_value": round(market_value, 2) if market_value is not None else None,
            # For long: debit paid (capital deployed). For short: credit received.
            "cost_basis": round(cost_basis_abs, 2) if cost_basis_abs is not None else None,
            "gain_loss": round(gain_loss, 2) if gain_loss is not None else None,
            "gain_loss_pct": gain_loss_pct,
        })

    total_long_value = round(
        sum(p["market_value"] for p in positions
            if p["market_value"] and p["position_type"] == "long"),
        2,
    )
    total_short_liability = round(
        sum(p["market_value"] for p in positions
            if p["market_value"] and p["position_type"] == "short"),
        2,
    )
    net_unrealized_pnl = round(
        sum(p["gain_loss"] for p in positions if p["gain_loss"] is not None),
        2,
    )
    return {
        "success": True,
        "account_number": acct,
        "count": len(positions),
        # Long positions are assets — market value is what they'd sell for.
        "total_long_value": total_long_value,
        # Short positions are liabilities — market value is what it'd cost to close.
        "total_short_liability": total_short_liability,
        # Sum of per-position gain_loss (signed correctly for each side).
        "net_unrealized_pnl": net_unrealized_pnl,
        "positions": positions,
    }


def get_open_option_orders(account_number: str | None = None) -> dict:
    """Get all pending option orders (single-leg and multi-leg spreads).

    Each order's legs are enriched with strike/expiry/option_type so you can
    read what's actually pending without re-resolving instrument URLs.
    """
    rh = client()
    acct = account_number or default_account()
    try:
        raw = rh.orders.get_all_open_option_orders(account_number=acct) or []
    except Exception as e:
        return {"success": False, "error": f"open option orders fetch failed: {e}"}

    orders = []
    for o in raw:
        legs_out = []
        for leg in (o.get("legs") or []):
            opt_id = _option_id_from_url(leg.get("option"))
            instr = _enrich_option_instrument(rh, opt_id)
            # Fill price (if leg partially executed) lives under executions[*].price
            executions = leg.get("executions") or []
            filled_qty = sum(_to_float(ex.get("quantity")) or 0 for ex in executions)
            legs_out.append({
                "option_id": opt_id,
                "side": leg.get("side"),                  # 'buy' / 'sell'
                "position_effect": leg.get("position_effect"),  # 'open' / 'close'
                "ratio_quantity": leg.get("ratio_quantity"),
                "ticker": instr.get("chain_symbol"),
                "expiry": instr.get("expiration_date"),
                "strike": _to_float(instr.get("strike_price")),
                "option_type": (instr.get("type") or "").lower() or None,
                "filled_quantity": filled_qty or None,
            })
        orders.append({
            "id": o.get("id"),
            "ticker": o.get("chain_symbol"),
            "direction": o.get("direction"),         # 'debit' / 'credit'
            "type": o.get("type"),                   # 'limit' / 'market'
            "quantity": _to_float(o.get("quantity")),
            "price": _to_float(o.get("price")),      # net debit/credit per spread
            "premium": _to_float(o.get("premium")),
            "processed_premium": _to_float(o.get("processed_premium")),
            "state": o.get("state"),
            "time_in_force": o.get("time_in_force"),
            "trigger": o.get("trigger"),
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
            "legs": legs_out,
        })
    return {"success": True, "account_number": acct, "count": len(orders), "orders": orders}


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
    # NOTE: robin_stocks.get_historical_portfolio() does NOT accept an account_number kwarg —
    # it uses the default account internally. The account_number arg on this MCP tool is
    # accepted for API uniformity but currently has no effect.
    _ = account_number or default_account()
    raw = rh.get_historical_portfolio(
        interval=interval, span=span, bounds=bounds,
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
