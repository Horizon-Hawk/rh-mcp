"""Market scanners — top movers, news, earnings, 52w breakouts."""

from rh_mcp.lib.rh_client import client


def scan_premium_sellers(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_days_to_earnings: int = 7,
    max_days_to_earnings: int = 30,
    min_iv_rank: float = 70.0,
    min_price: float = 10.0,
    top_n: int = 15,
) -> dict:
    """Find iron-condor / premium-selling candidates: earnings 7-30d out
    AND iv_rank > 70. Sells elevated IV before earnings; theta + post-earnings
    IV crush work for the position. ~70% win rate setups with defined risk.
    """
    from rh_mcp.analysis import premium_sellers
    try:
        return premium_sellers.analyze(
            tickers=tickers,
            universe_file=universe_file,
            min_days_to_earnings=min_days_to_earnings,
            max_days_to_earnings=max_days_to_earnings,
            min_iv_rank=min_iv_rank,
            min_price=min_price,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_premium_sellers failed: {e}"}


def scan_cheap_premium_buyers(
    tickers: list[str],
    max_iv_rank: float = 30.0,
    min_price: float = 10.0,
    top_n: int = 15,
) -> dict:
    """Filter tickers down to low-IV-rank candidates for debit spreads.
    Designed to be fed scan_all / scan_squeeze / scan_52w output.
    """
    from rh_mcp.analysis import cheap_premium_buyers
    try:
        return cheap_premium_buyers.analyze(
            tickers=tickers,
            max_iv_rank=max_iv_rank,
            min_price=min_price,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_cheap_premium_buyers failed: {e}"}


def scan_iv_crush_drift(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    max_days_since_earnings: int = 5,
    max_iv_rank: float = 30.0,
    min_price: float = 10.0,
    min_pct_since_earnings: float = 1.0,
    top_n: int = 15,
) -> dict:
    """Post-earnings IV-crush drift scanner: earnings reported in last N days,
    IV rank dropped to bottom range, stock still in uptrend. Buys cheap
    post-earnings options while drift thesis is intact.
    """
    from rh_mcp.analysis import iv_crush_drift
    try:
        return iv_crush_drift.analyze(
            tickers=tickers,
            universe_file=universe_file,
            max_days_since_earnings=max_days_since_earnings,
            max_iv_rank=max_iv_rank,
            min_price=min_price,
            min_pct_since_earnings=min_pct_since_earnings,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_iv_crush_drift failed: {e}"}


def scan_unusual_oi(
    tickers: list[str],
    min_strike_oi: int = 100,
    concentration_multiple: float = 5.0,
    min_turnover_ratio: float = 0.5,
    top_n: int = 15,
) -> dict:
    """Unusual options activity scanner: per-strike turnover (volume/OI) anomalies
    + OI concentration vs. median strike. Focused-list tool — pass scan_all or
    watchlist tickers, NOT a full universe.
    """
    from rh_mcp.analysis import unusual_oi
    try:
        return unusual_oi.analyze(
            tickers=tickers,
            min_strike_oi=min_strike_oi,
            concentration_multiple=concentration_multiple,
            min_turnover_ratio=min_turnover_ratio,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_unusual_oi failed: {e}"}


def scan_all(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    top_n_per_scanner: int = 25,
    top_n_overall: int = 30,
    proximity_pct: float = 2.0,
    require_spy_uptrend: bool = True,
    compression_percentile: float = 20.0,
    proximity_upper_pct: float = 2.0,
    leader_move_pct: float = 5.0,
    max_laggard_move_pct: float = 2.0,
) -> dict:
    """Composite morning-brief scanner: runs 52w-high, Bollinger squeeze, and
    sympathy-laggard scanners in parallel and reconciles overlaps.

    Multi-signal tickers (showing in 2-3 scanners) are highest conviction and
    appear first in the candidates list. APLS-style stacks (deep compression +
    near 52w high) are the highest-quality setups this composite finds.
    """
    from rh_mcp.analysis import scan_all as _scan_all
    try:
        return _scan_all.analyze(
            tickers=tickers,
            universe_file=universe_file,
            top_n_per_scanner=top_n_per_scanner,
            top_n_overall=top_n_overall,
            proximity_pct=proximity_pct,
            require_spy_uptrend=require_spy_uptrend,
            compression_percentile=compression_percentile,
            proximity_upper_pct=proximity_upper_pct,
            leader_move_pct=leader_move_pct,
            max_laggard_move_pct=max_laggard_move_pct,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_all failed: {e}"}


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
