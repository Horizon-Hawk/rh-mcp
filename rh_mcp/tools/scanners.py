"""Market scanners — top movers, news, earnings, 52w breakouts."""

from rh_mcp.lib.rh_client import client


def scan_squeeze_breakouts(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    compression_percentile: float = 20.0,
    proximity_upper_pct: float = 2.0,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    top_n: int = 20,
) -> dict:
    """Find Bollinger-squeeze candidates: 20d bandwidth in bottom percentile
    of 6-month history AND price near upper band (ready to break).

    Precursor signal to the 52w-high scanner — catches setups ~$0.50 earlier.
    Output ranked by lowest bandwidth percentile (deepest compression first).
    """
    from rh_mcp.analysis import squeeze_breakout
    try:
        return squeeze_breakout.analyze(
            tickers=tickers,
            universe_file=universe_file,
            compression_percentile=compression_percentile,
            proximity_upper_pct=proximity_upper_pct,
            min_price=min_price,
            min_avg_volume=min_avg_volume,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_squeeze_breakouts failed: {e}"}


def scan_sympathy_laggards(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    leader_move_pct: float = 5.0,
    max_laggard_move_pct: float = 2.0,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    min_peers_per_industry: int = 3,
    top_n: int = 20,
) -> dict:
    """Find industries where a leader moved ≥ leader_move_pct today but peers
    in the same industry haven't moved much (< max_laggard_move_pct).

    Surfaces day-2 catch-up trades when sector themes propagate. Returns
    {industry, leader, laggards} groups ranked by leader's move size.
    """
    from rh_mcp.analysis import sympathy_laggards
    try:
        return sympathy_laggards.analyze(
            tickers=tickers,
            universe_file=universe_file,
            leader_move_pct=leader_move_pct,
            max_laggard_move_pct=max_laggard_move_pct,
            min_price=min_price,
            min_avg_volume=min_avg_volume,
            min_peers_per_industry=min_peers_per_industry,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_sympathy_laggards failed: {e}"}


def scan_52w_breakouts(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    proximity_pct: float = 0.0,
    min_price: float = 5.0,
    min_volume_ratio: float = 1.5,
    max_gain_today_pct: float = 5.0,
    require_spy_uptrend: bool = True,
    skip_earnings_within_days: int = 5,
    top_n: int = 20,
) -> dict:
    """Scan a ticker universe for stocks at-or-near 52-week highs with
    confirming volume, filtered by the momentum-breakout framework rules.

    Args:
        tickers: Explicit list of symbols. If omitted, reads `universe_file`.
        universe_file: Path to a whitespace-delimited ticker file
            (`# comments` stripped). Defaults to `stock_universe.txt` in cwd.
        proximity_pct: 0 = strict at-or-above 52w high; 0.5 = "within 0.5% of".
        min_price: Skip names below this price (framework rule, default $5).
        min_volume_ratio: Required pace-adjusted volume vs 20d avg (default 1.5x).
        max_gain_today_pct: If today's % move already exceeds this, downgrade
            grade to B (chased). Set to 100 to disable.
        require_spy_uptrend: Gate the scan off if SPY below 20d SMA.
        skip_earnings_within_days: Drop candidates with earnings inside this
            window (framework hard rule, default 5).
        top_n: Trim output to top N candidates by volume_ratio.

    Returns dict with `candidates` list ranked by volume_ratio descending.
    Each candidate carries everything an entry pipeline needs to grade.
    """
    from rh_mcp.analysis import high_breakout
    try:
        return high_breakout.analyze(
            tickers=tickers,
            universe_file=universe_file,
            proximity_pct=proximity_pct,
            min_price=min_price,
            min_volume_ratio=min_volume_ratio,
            max_gain_today_pct=max_gain_today_pct,
            require_spy_uptrend=require_spy_uptrend,
            skip_earnings_within_days=skip_earnings_within_days,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_52w_breakouts failed: {e}"}


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
