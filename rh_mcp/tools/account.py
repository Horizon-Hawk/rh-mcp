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
    """Get all open stock positions with quantity, avg cost, current price, P&L.

    Scopes to the specified account_number — necessary because rh.build_holdings()
    is account-agnostic and silently returns the primary account's holdings.
    """
    rh = client()
    acct = account_number or default_account()

    # Account-scoped positions endpoint. build_holdings() ignores account_number
    # so we hit /positions/ directly with the account filter.
    try:
        raw = rh.helper.request_get(
            f"https://api.robinhood.com/positions/?account_number={acct}&nonzero=true",
            "pagination",
        ) or []
    except Exception as e:
        return {"success": False, "error": f"positions fetch failed: {e}"}

    positions = []
    for p in raw:
        qty = _to_float(p.get("quantity"))
        if not qty or qty == 0:
            continue
        instrument_url = p.get("instrument")
        try:
            symbol = rh.stocks.get_symbol_by_url(instrument_url) if instrument_url else None
        except Exception:
            symbol = None
        if not symbol:
            continue
        avg_cost = _to_float(p.get("average_buy_price"))
        try:
            current_price = _to_float(rh.stocks.get_latest_price(symbol)[0])
        except Exception:
            current_price = None
        market_value = (qty * current_price) if (qty and current_price) else None
        gain_loss = (
            (current_price - avg_cost) * qty
            if (avg_cost is not None and current_price is not None)
            else None
        )
        pct_gain = (
            ((current_price - avg_cost) / avg_cost * 100)
            if (avg_cost and current_price)
            else None
        )
        positions.append({
            "symbol": symbol,
            "quantity": qty,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "market_value": market_value,
            "gain_loss": gain_loss,
            "pct_gain": pct_gain,
        })

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
    """List all linked Robinhood accounts (brokerage + IRA/retirement)."""
    rh = client()
    try:
        raw = rh.helper.request_get(
            "https://api.robinhood.com/accounts/", "pagination"
        ) or []
    except Exception as e:
        raw = []
        pagination_error = str(e)
    else:
        pagination_error = None

    if not raw:
        profile = rh.load_account_profile()
        if not profile:
            return {
                "success": False,
                "error": f"no accounts returned (pagination_error={pagination_error})",
            }
        raw = [profile]

    accounts = []
    for a in raw:
        accounts.append({
            "account_number": a.get("account_number"),
            "type": a.get("type"),
            "brokerage_account_type": a.get("brokerage_account_type"),
            "buying_power": _to_float(a.get("buying_power")),
            "cash": _to_float(a.get("cash")),
            "cash_held_for_orders": _to_float(a.get("cash_held_for_orders")),
            "day_trade_count": a.get("day_trade_count"),
            "pattern_day_trader": a.get("is_pinned"),
            "deactivated": a.get("deactivated"),
            "margin_balances": a.get("margin_balances"),
        })
    return {"success": True, "accounts": accounts}


def get_settled_cash(account_number: str | None = None) -> dict:
    """Return settled cash vs unsettled funds vs total BP for an account.

    Critical for cash accounts (incl. IRAs) where unsettled funds can be used
    to BUY but the resulting shares are LOCKED from selling until T+1.

    Returns:
      settled_cash:         actually deployable cash, no settlement violation risk
      unsettled_funds:      pending from recent sales (T+1)
      unsettled_debit:      pending from recent buys still settling
      cash_held_for_orders: locked by working limit orders
      buying_power:         total available (may include unsettled — misleading on IRA)
      account_type:         brokerage_account_type (e.g., individual, ira_roth)
    """
    rh = client()
    acct = account_number or default_account()

    try:
        accounts = rh.helper.request_get(
            "https://api.robinhood.com/accounts/", "pagination"
        ) or []
    except Exception as e:
        return {"success": False, "error": f"accounts fetch failed: {e}"}

    profile = None
    for a in accounts:
        if a.get("account_number") == acct:
            profile = a
            break
    if not profile:
        profile = rh.load_account_profile()
    if not profile:
        return {"success": False, "error": "no matching account profile"}

    margin = profile.get("margin_balances") or {}
    return {
        "success": True,
        "account_number": acct,
        "settled_cash": _to_float(margin.get("cash_available_for_withdrawal")),
        "unsettled_funds": _to_float(margin.get("unsettled_funds")),
        "unsettled_debit": _to_float(margin.get("unsettled_debit")),
        "cash_held_for_orders": _to_float(margin.get("cash_held_for_orders")),
        "buying_power": _to_float(profile.get("buying_power")),
        "account_type": profile.get("brokerage_account_type") or profile.get("type"),
    }


def effective_basis(ticker: str, account_number: str | None = None) -> dict:
    """Compute effective cost basis adjusted for currently open option positions.

    Pulls the stock position and any currently OPEN short option positions on
    the ticker (covered calls, CSPs). Adjusts the stock cost basis by the
    *open* short premium received.

    NOTE: This does NOT pull closed historical option premium (expired,
    bought-back). For full historical accounting, manual records are required.
    The displayed RH avg_cost is always the literal stock buy price — it does
    NOT adjust for option activity.

    Returns:
      original_basis:    literal stock avg_cost from RH
      shares:            position quantity
      open_short_premium: net premium currently held from open short options
      effective_basis:   original_basis - (open_short_premium / shares)
      open_options:      detail of contributing option positions
    """
    rh = client()
    acct = account_number or default_account()
    sym = ticker.strip().upper()

    # Get stock position via account-scoped /positions/
    try:
        raw_positions = rh.helper.request_get(
            f"https://api.robinhood.com/positions/?account_number={acct}&nonzero=true",
            "pagination",
        ) or []
    except Exception as e:
        return {"success": False, "error": f"positions fetch failed: {e}"}

    shares = 0.0
    original_basis = None
    for p in raw_positions:
        try:
            psym = rh.stocks.get_symbol_by_url(p.get("instrument")) if p.get("instrument") else None
        except Exception:
            psym = None
        if psym == sym:
            shares = float(p.get("quantity") or 0)
            original_basis = _to_float(p.get("average_buy_price"))
            break

    if shares == 0 or original_basis is None:
        return {"success": False, "error": f"no stock position in {sym}"}

    # Get open option positions on this ticker
    try:
        opt_positions = rh.options.get_open_option_positions(account_number=acct) or []
    except Exception as e:
        return {"success": False, "error": f"option positions fetch failed: {e}"}

    open_short_premium = 0.0
    open_options = []
    for op in opt_positions:
        op_ticker = (op.get("chain_symbol") or "").upper()
        if op_ticker != sym:
            continue
        op_type = op.get("type")  # 'long' or 'short'
        if op_type != "short":
            continue
        qty = float(op.get("quantity") or 0)
        avg_price = _to_float(op.get("average_price"))
        if not qty or avg_price is None:
            continue
        # average_price is per-share; per-contract = avg_price * 100
        # For shorts, this is credit collected (positive for the account)
        # Note: RH stores avg_price as negative for shorts in some versions; normalize
        premium = abs(avg_price) * qty * 100
        open_short_premium += premium
        open_options.append({
            "option_id": op.get("option_id") or op.get("option"),
            "quantity": qty,
            "avg_price": abs(avg_price),
            "premium_collected": premium,
        })

    effective = original_basis - (open_short_premium / shares) if shares else original_basis

    return {
        "success": True,
        "symbol": sym,
        "account_number": acct,
        "shares": shares,
        "original_basis": original_basis,
        "open_short_premium": round(open_short_premium, 2),
        "effective_basis": round(effective, 4),
        "open_options": open_options,
        "note": "Effective basis here only reflects CURRENTLY OPEN short option positions. Closed/expired historical premium is not included.",
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
