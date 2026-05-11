"""Short-sell utility tools."""

from rh_mcp.lib.rh_client import client


def check_short_availability(ticker: str) -> dict:
    """Check whether a ticker is shortable and the borrow fee.

    Returns shortable flag, borrow fee, and inventory level. Call before short_sell.
    """
    rh = client()
    sym = ticker.strip().upper()
    instruments = rh.get_instruments_by_symbols([sym])
    if not instruments or not instruments[0]:
        return {"success": False, "error": "instrument not found"}
    inst = instruments[0]
    fundamentals = rh.get_fundamentals(sym) or []
    fund = fundamentals[0] if fundamentals else {}
    return {
        "success": True,
        "symbol": sym,
        "shortable": inst.get("tradeable") and inst.get("type") == "stock",
        "tradeable": inst.get("tradeable"),
        "fractional_tradeable": inst.get("fractional_tradability") == "tradable",
        "margin_initial_ratio": inst.get("margin_initial_ratio"),
        "maintenance_ratio": inst.get("maintenance_ratio"),
        "day_trade_ratio": inst.get("day_trade_ratio"),
        "short_pct_of_float": fund.get("short_percent_of_float"),
        "shares_outstanding": fund.get("shares_outstanding"),
        "float_shares": fund.get("float"),
    }
