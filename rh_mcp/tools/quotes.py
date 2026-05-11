"""Quote and market data tools."""

from rh_mcp.lib.rh_client import client


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_quote(tickers: str) -> dict:
    """Get current quote(s) for one or more tickers.

    Args:
        tickers: Single ticker ('AAPL') or comma-separated batch ('AAPL,NVDA,TSLA').

    Returns bid/ask/last/AH/previous_close for each.
    """
    rh = client()
    symbols = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not symbols:
        return {"success": False, "error": "no tickers provided"}

    raw = rh.stocks.get_quotes(symbols)
    if not raw:
        return {"success": False, "error": "no quotes returned"}

    quotes = []
    for q in raw:
        if not q:
            continue
        quotes.append({
            "symbol": q.get("symbol"),
            "bid_price": _to_float(q.get("bid_price")),
            "bid_size": q.get("bid_size"),
            "ask_price": _to_float(q.get("ask_price")),
            "ask_size": q.get("ask_size"),
            "last_trade_price": _to_float(q.get("last_trade_price")),
            "last_extended_hours_trade_price": _to_float(q.get("last_extended_hours_trade_price")),
            "previous_close": _to_float(q.get("previous_close")),
            "previous_close_date": q.get("previous_close_date"),
            "trading_halted": q.get("trading_halted"),
            "updated_at": q.get("updated_at"),
        })

    return {"success": True, "count": len(quotes), "quotes": quotes}


def get_latest_price(ticker: str) -> dict:
    """Fast single-ticker last-price lookup. Lighter than get_quote."""
    rh = client()
    sym = ticker.strip().upper()
    if not sym:
        return {"success": False, "error": "ticker required"}
    prices = rh.get_latest_price([sym])
    if not prices:
        return {"success": False, "error": "no price"}
    return {"success": True, "symbol": sym, "last_price": _to_float(prices[0])}


def get_bars(ticker: str, interval: str = "5minute", span: str = "day") -> dict:
    """Get historical bars for a ticker.

    Args:
        ticker: Stock symbol.
        interval: '5minute', '10minute', 'hour', 'day', 'week'.
        span: 'day', 'week', 'month', '3month', 'year', '5year'.
    """
    rh = client()
    sym = ticker.strip().upper()
    bars = rh.get_stock_historicals(sym, interval=interval, span=span)
    if bars is None:
        return {"success": False, "error": "no bars returned"}
    out = []
    for b in bars:
        out.append({
            "begins_at": b.get("begins_at"),
            "open": _to_float(b.get("open_price")),
            "high": _to_float(b.get("high_price")),
            "low": _to_float(b.get("low_price")),
            "close": _to_float(b.get("close_price")),
            "volume": int(b.get("volume") or 0),
            "session": b.get("session"),
            "interpolated": b.get("interpolated"),
        })
    return {"success": True, "symbol": sym, "interval": interval, "span": span, "count": len(out), "bars": out}


def get_market_hours(date: str | None = None) -> dict:
    """Get NYSE market hours.

    Args:
        date: ISO date 'YYYY-MM-DD'. Omit for today.
    """
    rh = client()
    if date:
        hours = rh.get_market_hours("XNYS", date)
    else:
        hours = rh.get_market_today_hours("XNYS")
    if not hours:
        return {"success": False, "error": "no market hours data"}
    return {
        "success": True,
        "date": hours.get("date"),
        "is_open": hours.get("is_open"),
        "opens_at": hours.get("opens_at"),
        "closes_at": hours.get("closes_at"),
        "extended_opens_at": hours.get("extended_opens_at"),
        "extended_closes_at": hours.get("extended_closes_at"),
        "next_open_date": hours.get("next_open_date"),
    }
