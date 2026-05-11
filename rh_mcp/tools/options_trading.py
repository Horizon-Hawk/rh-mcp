"""Options trading — single-leg, vertical spreads, butterflies, condors, straddles, calendars.

Each named strategy is a thin wrapper that builds the correct leg list and calls the
universal `place_option_spread`. RH's API accepts arbitrary multi-leg orders via the
spread endpoint; we expose the common patterns by name plus the universal escape hatch.

Conventions:
- `expiry` is always 'YYYY-MM-DD'
- `option_type` is 'call' or 'put'
- `quantity` is the NUMBER OF SPREADS (e.g., 1 spread = 1 contract per leg)
- `limit_price` is the NET debit (positive cost to open) or NET credit (positive premium received)
- `direction` is 'debit' (you pay) or 'credit' (you receive). The named strategies set it correctly.
"""

import time

from rh_mcp.lib.rh_client import client
from rh_mcp.auth import default_account


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _leg(expiry: str, strike: float, option_type: str, action: str, effect: str = "open") -> dict:
    """Build a single leg dict in RH's expected format."""
    return {
        "expirationDate": expiry,
        "strike": float(strike),
        "optionType": option_type.lower(),
        "effect": effect.lower(),
        "action": action.lower(),
    }


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Universal multi-leg
# ---------------------------------------------------------------------------

def place_option_spread(
    ticker: str,
    legs: list,
    quantity: int,
    limit_price: float,
    direction: str = "debit",
    time_in_force: str = "gtc",
    account_number: str | None = None,
) -> dict:
    """Universal multi-leg options order. Any valid combination of legs.

    Args:
        ticker: Underlying symbol.
        legs: List of leg dicts, each: {expirationDate, strike, optionType, effect, action}.
              expirationDate='YYYY-MM-DD', optionType='call'|'put', effect='open'|'close', action='buy'|'sell'.
        quantity: Number of spreads (each leg contracts = quantity * ratio).
        limit_price: Net debit/credit per spread (positive number).
        direction: 'debit' (you pay) or 'credit' (you receive).
        time_in_force: 'gtc' | 'gfd' | 'ioc' | 'opg'.
    """
    rh = client()
    sym = ticker.strip().upper()
    if direction.lower() not in ("debit", "credit"):
        return {"success": False, "error": "direction must be 'debit' or 'credit'"}
    if not legs or not isinstance(legs, list):
        return {"success": False, "error": "legs must be a non-empty list"}

    result = rh.order_option_spread(
        direction=direction.lower(),
        price=float(limit_price),
        symbol=sym,
        quantity=int(quantity),
        spread=legs,
        account_number=account_number or default_account(),
        timeInForce=time_in_force,
    )
    if not result or "id" not in result:
        return {"success": False, "error": "order rejected", "raw": result, "legs": legs}
    return {
        "success": True,
        "order_id": result.get("id"),
        "symbol": sym,
        "direction": direction.lower(),
        "quantity": quantity,
        "limit_price": limit_price,
        "legs": legs,
        "state": result.get("state"),
    }


# ---------------------------------------------------------------------------
# Single-leg
# ---------------------------------------------------------------------------

def buy_option_to_open(
    ticker: str, expiry: str, strike: float, option_type: str,
    quantity: int, limit_price: float, time_in_force: str = "gtc",
    account_number: str | None = None,
) -> dict:
    """Buy a single call or put to open a long position."""
    rh = client()
    sym = ticker.strip().upper()
    result = rh.order_buy_option_limit(
        positionEffect="open", creditOrDebit="debit",
        price=float(limit_price), symbol=sym, quantity=int(quantity),
        expirationDate=expiry, strike=float(strike), optionType=option_type.lower(),
        account_number=account_number or default_account(), timeInForce=time_in_force,
    )
    if not result or "id" not in result:
        return {"success": False, "error": "order rejected", "raw": result}
    return {"success": True, "order_id": result.get("id"), "state": result.get("state"),
            "symbol": sym, "expiry": expiry, "strike": strike, "option_type": option_type,
            "quantity": quantity, "limit_price": limit_price}


def sell_option_to_open(
    ticker: str, expiry: str, strike: float, option_type: str,
    quantity: int, limit_price: float, time_in_force: str = "gtc",
    account_number: str | None = None,
) -> dict:
    """Sell a single call or put to open (covered call, cash-secured put, naked short)."""
    rh = client()
    sym = ticker.strip().upper()
    result = rh.order_sell_option_limit(
        positionEffect="open", creditOrDebit="credit",
        price=float(limit_price), symbol=sym, quantity=int(quantity),
        expirationDate=expiry, strike=float(strike), optionType=option_type.lower(),
        account_number=account_number or default_account(), timeInForce=time_in_force,
    )
    if not result or "id" not in result:
        return {"success": False, "error": "order rejected", "raw": result}
    return {"success": True, "order_id": result.get("id"), "state": result.get("state"),
            "symbol": sym, "expiry": expiry, "strike": strike, "option_type": option_type,
            "quantity": quantity, "limit_price": limit_price}


def close_option_position(
    ticker: str, expiry: str, strike: float, option_type: str,
    quantity: int, limit_price: float, side: str = "sell",
    time_in_force: str = "gtc", account_number: str | None = None,
) -> dict:
    """Close an existing option position. side='sell' to close a long, 'buy' to close a short."""
    rh = client()
    sym = ticker.strip().upper()
    if side.lower() == "sell":
        result = rh.order_sell_option_limit(
            positionEffect="close", creditOrDebit="credit",
            price=float(limit_price), symbol=sym, quantity=int(quantity),
            expirationDate=expiry, strike=float(strike), optionType=option_type.lower(),
            account_number=account_number or default_account(), timeInForce=time_in_force,
        )
    else:
        result = rh.order_buy_option_limit(
            positionEffect="close", creditOrDebit="debit",
            price=float(limit_price), symbol=sym, quantity=int(quantity),
            expirationDate=expiry, strike=float(strike), optionType=option_type.lower(),
            account_number=account_number or default_account(), timeInForce=time_in_force,
        )
    if not result or "id" not in result:
        return {"success": False, "error": "order rejected", "raw": result}
    return {"success": True, "order_id": result.get("id"), "state": result.get("state")}


# ---------------------------------------------------------------------------
# Vertical Spreads (2 legs, same expiry)
# ---------------------------------------------------------------------------

def bull_call_spread(
    ticker: str, expiry: str, long_strike: float, short_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Bullish DEBIT spread. Buy lower-strike call + sell higher-strike call.
    Max profit = (short_strike - long_strike - debit) * 100 per spread."""
    if long_strike >= short_strike:
        return {"success": False, "error": f"bull call: long_strike ({long_strike}) must be < short_strike ({short_strike})"}
    legs = [
        _leg(expiry, long_strike, "call", "buy"),
        _leg(expiry, short_strike, "call", "sell"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def bear_put_spread(
    ticker: str, expiry: str, long_strike: float, short_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Bearish DEBIT spread. Buy higher-strike put + sell lower-strike put.
    Max profit = (long_strike - short_strike - debit) * 100 per spread."""
    if long_strike <= short_strike:
        return {"success": False, "error": f"bear put: long_strike ({long_strike}) must be > short_strike ({short_strike})"}
    legs = [
        _leg(expiry, long_strike, "put", "buy"),
        _leg(expiry, short_strike, "put", "sell"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def bull_put_spread(
    ticker: str, expiry: str, short_strike: float, long_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Bullish CREDIT spread. Sell higher-strike put + buy lower-strike put.
    Max profit = credit. Max loss = (short - long - credit) * 100."""
    if short_strike <= long_strike:
        return {"success": False, "error": f"bull put: short_strike ({short_strike}) must be > long_strike ({long_strike})"}
    legs = [
        _leg(expiry, short_strike, "put", "sell"),
        _leg(expiry, long_strike, "put", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


def bear_call_spread(
    ticker: str, expiry: str, short_strike: float, long_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Bearish CREDIT spread. Sell lower-strike call + buy higher-strike call.
    Max profit = credit. Max loss = (long - short - credit) * 100."""
    if long_strike <= short_strike:
        return {"success": False, "error": f"bear call: long_strike ({long_strike}) must be > short_strike ({short_strike})"}
    legs = [
        _leg(expiry, short_strike, "call", "sell"),
        _leg(expiry, long_strike, "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Iron Condor — neutral, defined-risk, 4 legs (CREDIT)
# ---------------------------------------------------------------------------

def iron_condor(
    ticker: str, expiry: str,
    put_long_strike: float, put_short_strike: float,
    call_short_strike: float, call_long_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Iron Condor — neutral CREDIT, 4 legs. Profit if underlying stays between short strikes.
    Sell put spread (short put higher, long put lower) + sell call spread (short call lower, long call higher).

    Strike layout (lowest to highest):
        put_long_strike < put_short_strike < (current price) < call_short_strike < call_long_strike
    """
    if not (put_long_strike < put_short_strike < call_short_strike < call_long_strike):
        return {"success": False,
                "error": f"strikes must satisfy put_long ({put_long_strike}) < put_short ({put_short_strike}) < call_short ({call_short_strike}) < call_long ({call_long_strike})"}
    legs = [
        _leg(expiry, put_long_strike,  "put",  "buy"),
        _leg(expiry, put_short_strike, "put",  "sell"),
        _leg(expiry, call_short_strike, "call", "sell"),
        _leg(expiry, call_long_strike,  "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Butterflies
# ---------------------------------------------------------------------------

def long_call_butterfly(
    ticker: str, expiry: str,
    low_strike: float, mid_strike: float, high_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Call Butterfly — neutral DEBIT, profits if underlying lands near mid_strike at expiry.
    Buy 1 low-strike call, sell 2 mid-strike calls, buy 1 high-strike call. Equidistant strikes."""
    if not (low_strike < mid_strike < high_strike):
        return {"success": False, "error": "strikes must be low < mid < high"}
    if abs((mid_strike - low_strike) - (high_strike - mid_strike)) > 0.01:
        return {"success": False, "error": "butterfly requires equidistant strikes"}
    # RH represents the 2x short leg by sending two separate sell legs (ratio handled this way for spread API)
    legs = [
        _leg(expiry, low_strike,  "call", "buy"),
        _leg(expiry, mid_strike,  "call", "sell"),
        _leg(expiry, mid_strike,  "call", "sell"),
        _leg(expiry, high_strike, "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def long_put_butterfly(
    ticker: str, expiry: str,
    low_strike: float, mid_strike: float, high_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Put Butterfly — neutral DEBIT, profits near mid_strike. Mirror of call butterfly with puts."""
    if not (low_strike < mid_strike < high_strike):
        return {"success": False, "error": "strikes must be low < mid < high"}
    if abs((mid_strike - low_strike) - (high_strike - mid_strike)) > 0.01:
        return {"success": False, "error": "butterfly requires equidistant strikes"}
    legs = [
        _leg(expiry, low_strike,  "put", "buy"),
        _leg(expiry, mid_strike,  "put", "sell"),
        _leg(expiry, mid_strike,  "put", "sell"),
        _leg(expiry, high_strike, "put", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def iron_butterfly(
    ticker: str, expiry: str,
    low_strike: float, mid_strike: float, high_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Iron Butterfly — neutral CREDIT, profits near mid_strike. Short straddle at mid + long wings.
    Buy low_strike put + sell mid_strike put + sell mid_strike call + buy high_strike call.
    Equidistant strikes required."""
    if not (low_strike < mid_strike < high_strike):
        return {"success": False, "error": "strikes must be low < mid < high"}
    if abs((mid_strike - low_strike) - (high_strike - mid_strike)) > 0.01:
        return {"success": False, "error": "iron butterfly requires equidistant wings"}
    legs = [
        _leg(expiry, low_strike,  "put",  "buy"),
        _leg(expiry, mid_strike,  "put",  "sell"),
        _leg(expiry, mid_strike,  "call", "sell"),
        _leg(expiry, high_strike, "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Straddles & Strangles
# ---------------------------------------------------------------------------

def long_straddle(
    ticker: str, expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Straddle — volatility play DEBIT, profits on big move either direction.
    Buy call + buy put at same strike (typically ATM)."""
    legs = [
        _leg(expiry, strike, "call", "buy"),
        _leg(expiry, strike, "put",  "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def long_strangle(
    ticker: str, expiry: str, put_strike: float, call_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Strangle — volatility play DEBIT, cheaper than straddle, wider break-evens.
    Buy OTM put + buy OTM call. put_strike < call_strike."""
    if put_strike >= call_strike:
        return {"success": False, "error": "strangle: put_strike must be < call_strike"}
    legs = [
        _leg(expiry, put_strike,  "put",  "buy"),
        _leg(expiry, call_strike, "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def short_straddle(
    ticker: str, expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Short Straddle — NAKED CREDIT, unlimited risk both sides. Profits if underlying stays at strike.
    Sell call + sell put at same strike. REQUIRES significant margin. Use with caution."""
    legs = [
        _leg(expiry, strike, "call", "sell"),
        _leg(expiry, strike, "put",  "sell"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


def short_strangle(
    ticker: str, expiry: str, put_strike: float, call_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Short Strangle — NAKED CREDIT, unlimited risk. Wider profit zone than straddle.
    Sell OTM put + sell OTM call. REQUIRES significant margin."""
    if put_strike >= call_strike:
        return {"success": False, "error": "strangle: put_strike must be < call_strike"}
    legs = [
        _leg(expiry, put_strike,  "put",  "sell"),
        _leg(expiry, call_strike, "call", "sell"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="credit",
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Calendar Spreads (different expirations, same strike)
# ---------------------------------------------------------------------------

def long_calendar_call(
    ticker: str, near_expiry: str, far_expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Calendar Call — neutral DEBIT, profits from time decay differential.
    Sell near-month call + buy far-month call at same strike. near_expiry < far_expiry."""
    if near_expiry >= far_expiry:
        return {"success": False, "error": "near_expiry must precede far_expiry"}
    legs = [
        _leg(near_expiry, strike, "call", "sell"),
        _leg(far_expiry,  strike, "call", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def long_calendar_put(
    ticker: str, near_expiry: str, far_expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Long Calendar Put — mirror with puts."""
    if near_expiry >= far_expiry:
        return {"success": False, "error": "near_expiry must precede far_expiry"}
    legs = [
        _leg(near_expiry, strike, "put", "sell"),
        _leg(far_expiry,  strike, "put", "buy"),
    ]
    return place_option_spread(ticker, legs, quantity, limit_price, direction="debit",
                                account_number=account_number)


def diagonal_call_spread(
    ticker: str, near_expiry: str, far_expiry: str,
    short_strike: float, long_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Diagonal Call Spread — calendar + vertical combo. Sell near-month short_strike + buy far-month long_strike.
    For directional + time-decay plays."""
    if near_expiry >= far_expiry:
        return {"success": False, "error": "near_expiry must precede far_expiry"}
    legs = [
        _leg(near_expiry, short_strike, "call", "sell"),
        _leg(far_expiry,  long_strike,  "call", "buy"),
    ]
    direction = "debit" if long_strike <= short_strike else "credit"
    return place_option_spread(ticker, legs, quantity, limit_price, direction=direction,
                                account_number=account_number)


def diagonal_put_spread(
    ticker: str, near_expiry: str, far_expiry: str,
    short_strike: float, long_strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Diagonal Put Spread — mirror with puts."""
    if near_expiry >= far_expiry:
        return {"success": False, "error": "near_expiry must precede far_expiry"}
    legs = [
        _leg(near_expiry, short_strike, "put", "sell"),
        _leg(far_expiry,  long_strike,  "put", "buy"),
    ]
    direction = "debit" if long_strike >= short_strike else "credit"
    return place_option_spread(ticker, legs, quantity, limit_price, direction=direction,
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Stock + option strategies (notational helpers)
# ---------------------------------------------------------------------------

def covered_call(
    ticker: str, expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Sell a covered call against existing long stock. Each contract = 100 shares of cover required.
    Just `sell_option_to_open` with optionType='call'. Verify share coverage beforehand."""
    return sell_option_to_open(ticker, expiry, strike, "call", quantity, limit_price,
                                account_number=account_number)


def cash_secured_put(
    ticker: str, expiry: str, strike: float,
    quantity: int, limit_price: float, account_number: str | None = None,
) -> dict:
    """Sell a cash-secured put. Requires cash to buy at strike if assigned (strike × 100 × quantity).
    Just `sell_option_to_open` with optionType='put'. Verify cash available."""
    return sell_option_to_open(ticker, expiry, strike, "put", quantity, limit_price,
                                account_number=account_number)


# ---------------------------------------------------------------------------
# Chain helpers
# ---------------------------------------------------------------------------

def get_option_chain(
    ticker: str, expiry: str | None = None, option_type: str | None = None,
) -> dict:
    """List tradable options for a ticker. Filter by expiry and/or option type.

    Args:
        ticker: Stock symbol.
        expiry: 'YYYY-MM-DD' (optional — omit to list all expirations).
        option_type: 'call' | 'put' (optional).
    """
    rh = client()
    sym = ticker.strip().upper()
    raw = rh.find_tradable_options(symbol=sym, expirationDate=expiry, optionType=option_type)
    if not raw:
        return {"success": True, "count": 0, "options": []}
    out = []
    for o in raw:
        out.append({
            "id": o.get("id"),
            "symbol": o.get("chain_symbol"),
            "expiration_date": o.get("expiration_date"),
            "strike_price": _to_float(o.get("strike_price")),
            "type": o.get("type"),
            "state": o.get("state"),
            "tradability": o.get("tradability"),
        })
    return {"success": True, "count": len(out), "options": out}


def get_option_quote(
    ticker: str, expiry: str, strike: float, option_type: str,
) -> dict:
    """Get market data for a specific option contract: bid, ask, last, IV, greeks."""
    rh = client()
    sym = ticker.strip().upper()
    raw = rh.get_option_market_data(
        inputSymbols=sym, expirationDate=expiry,
        strikePrice=str(strike), optionType=option_type.lower(),
    )
    if not raw:
        return {"success": False, "error": "no data"}
    # robin_stocks returns a nested list; flatten to the first match
    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        raw = raw[0]
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return {"success": False, "error": "unexpected response shape", "raw": raw}
    return {
        "success": True,
        "ticker": sym, "expiry": expiry, "strike": strike, "option_type": option_type,
        "bid_price": _to_float(raw.get("bid_price")),
        "ask_price": _to_float(raw.get("ask_price")),
        "last_trade_price": _to_float(raw.get("last_trade_price")),
        "mark_price": _to_float(raw.get("mark_price")),
        "open_interest": int(raw.get("open_interest") or 0),
        "volume": int(raw.get("volume") or 0),
        "implied_volatility": _to_float(raw.get("implied_volatility")),
        "delta": _to_float(raw.get("delta")),
        "gamma": _to_float(raw.get("gamma")),
        "theta": _to_float(raw.get("theta")),
        "vega": _to_float(raw.get("vega")),
        "rho": _to_float(raw.get("rho")),
        "previous_close": _to_float(raw.get("previous_close_price")),
    }
