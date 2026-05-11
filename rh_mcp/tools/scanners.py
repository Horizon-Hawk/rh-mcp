"""Market scanners — top movers, news, earnings."""

from rh_mcp.lib.rh_client import client


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def top_movers(direction: str = "up", scope: str = "sp500") -> dict:
    """Get top market movers.

    Args:
        direction: 'up' or 'down'.
        scope: 'sp500' (S&P 500 only) or 'all' (broader market).
    """
    rh = client()
    direction = direction.lower()
    scope = scope.lower()

    if scope == "sp500":
        raw = rh.get_top_movers_sp500(direction=direction) or []
    else:
        raw = rh.get_top_movers() or []

    symbols = [m.get("symbol") for m in raw if m.get("symbol")]
    if not symbols:
        return {"success": True, "direction": direction, "scope": scope, "count": 0, "movers": []}

    # The movers endpoint returns instruments only — enrich with batch quote
    quotes_data = rh.stocks.get_quotes(symbols) or []
    by_sym = {q.get("symbol"): q for q in quotes_data if q}

    out = []
    for sym in symbols:
        q = by_sym.get(sym, {})
        last = _to_float(q.get("last_trade_price"))
        prev = _to_float(q.get("previous_close"))
        pct = round((last - prev) / prev * 100, 2) if (last and prev) else None
        out.append({
            "symbol": sym,
            "last_price": last,
            "previous_close": prev,
            "pct_change": pct,
        })
    # Sort by abs pct_change for readability
    out.sort(key=lambda r: abs(r["pct_change"] or 0), reverse=True)
    return {"success": True, "direction": direction, "scope": scope, "count": len(out), "movers": out}


def get_news(ticker: str, count: int = 10) -> dict:
    """Get recent news for a ticker."""
    rh = client()
    sym = ticker.strip().upper()
    raw = rh.get_news(sym) or []
    out = []
    for n in raw[:count]:
        out.append({
            "title": n.get("title"),
            "source": n.get("source"),
            "preview": n.get("summary") or n.get("preview"),
            "url": n.get("url"),
            "published_at": n.get("published_at"),
            "updated_at": n.get("updated_at"),
        })
    return {"success": True, "symbol": sym, "count": len(out), "news": out}


def get_earnings(ticker: str) -> dict:
    """Get earnings history + next earnings date for a ticker."""
    rh = client()
    sym = ticker.strip().upper()
    raw = rh.get_earnings(sym) or []
    earnings = []
    upcoming = None
    for e in raw:
        rec = {
            "year": e.get("year"),
            "quarter": e.get("quarter"),
            "eps_estimate": _to_float((e.get("eps") or {}).get("estimate")),
            "eps_actual": _to_float((e.get("eps") or {}).get("actual")),
            "report_date": (e.get("report") or {}).get("date"),
            "report_timing": (e.get("report") or {}).get("timing"),
            "report_verified": (e.get("report") or {}).get("verified"),
        }
        earnings.append(rec)
        if rec["report_date"] and rec["eps_actual"] is None:
            if upcoming is None or rec["report_date"] < upcoming["report_date"]:
                upcoming = rec
    return {"success": True, "symbol": sym, "upcoming": upcoming, "history": earnings}


def get_fundamentals(ticker: str) -> dict:
    """Get fundamentals: market cap, float, short interest, P/E, sector, etc."""
    rh = client()
    sym = ticker.strip().upper()
    raw = rh.get_fundamentals(sym) or []
    if not raw or not raw[0]:
        return {"success": False, "error": "no fundamentals"}
    f = raw[0]
    return {
        "success": True,
        "symbol": sym,
        "market_cap": _to_float(f.get("market_cap")),
        "shares_outstanding": _to_float(f.get("shares_outstanding")),
        "float": _to_float(f.get("float")),
        "short_percent_of_float": _to_float(f.get("short_percent_of_float")),
        "pe_ratio": _to_float(f.get("pe_ratio")),
        "high_52w": _to_float(f.get("high_52_weeks")),
        "low_52w": _to_float(f.get("low_52_weeks")),
        "average_volume": _to_float(f.get("average_volume")),
        "average_volume_2_weeks": _to_float(f.get("average_volume_2_weeks")),
        "dividend_yield": _to_float(f.get("dividend_yield")),
        "sector": f.get("sector"),
        "industry": f.get("industry"),
        "description": f.get("description"),
    }
