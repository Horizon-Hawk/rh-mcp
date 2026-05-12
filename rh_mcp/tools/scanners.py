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


def get_futures_quote(ticker: str) -> dict:
    """Get current quote for an RH futures contract (e.g. 'MNQM26').

    The ticker must be in the UUID cache first — register via
    futures_client.register_uuid(ticker, uuid). UUIDs are sourced by inspecting
    RH's web app network calls (the quote endpoint takes UUIDs, not tickers).
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        q = fc.get_quote_by_ticker(ticker)
        if q is None:
            uuid = fc.get_uuid(ticker)
            return {
                "success": False,
                "error": f"ticker {ticker} not in UUID cache. Register with "
                         f"futures_client.register_uuid(ticker, uuid). "
                         f"UUID currently {uuid!r}.",
            }
        return {"success": True, **q}
    except Exception as e:
        return {"success": False, "error": f"futures quote failed: {e}"}


def scan_futures_rsi2(
    tickers: list[str] | None = None,
    oversold_threshold: float = 5.0,
    overbought_threshold: float = 95.0,
) -> dict:
    """Futures RSI(2) mean reversion scanner. Default basket: GC/MNQ/NQ/ES/MES/RTY/
    CL/YM/SI (NG excluded — negative expectancy in backtest). Returns longs (oversold
    in uptrend) + shorts (overbought in downtrend), plus a multi_fire summary
    when correlated instruments fire simultaneously (e.g. MNQ + ES both oversold).

    Validated edge from 5y backtest: GC PF 2.01, MNQ PF 1.92, ES PF 1.78.
    """
    from rh_mcp.analysis import scan_futures_rsi2 as _sf
    try:
        return _sf.analyze(
            tickers=tickers,
            oversold_threshold=oversold_threshold,
            overbought_threshold=overbought_threshold,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_futures_rsi2 failed: {e}"}


def list_futures_accounts() -> dict:
    """List RH futures accounts on this user (separate from regular stock account).
    Returns the futures account UUIDs needed for positions/orders endpoints.
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        accts = fc.list_accounts()
        return {"success": True, "count": len(accts), "accounts": accts}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_futures_positions(account_id: str | None = None) -> dict:
    """Open futures positions for an RH futures account (uses default account if None)."""
    from rh_mcp.analysis import futures_client as fc
    try:
        positions = fc.get_positions(account_id)
        return {"success": True, "count": len(positions), "positions": positions}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_futures_orders(account_id: str | None = None, limit: int = 50) -> dict:
    """Futures order history (most recent first)."""
    from rh_mcp.analysis import futures_client as fc
    try:
        orders = fc.get_orders(account_id, limit=limit)
        return {"success": True, "count": len(orders), "orders": orders}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_futures_aggregated_positions(account_id: str | None = None) -> dict:
    """Aggregated futures positions (per-contract roll-ups with P&L context)."""
    from rh_mcp.analysis import futures_client as fc
    try:
        positions = fc.get_aggregated_positions(account_id)
        return {"success": True, "count": len(positions), "positions": positions}
    except Exception as e:
        return {"success": False, "error": str(e)}


def flatten_futures_position(contract_uuid: str, account_id: str | None = None) -> dict:
    """Emergency close a futures position via market order. RH auto-determines
    side (sell longs / cover shorts) and quantity from current position. Fires
    POST /ceres/v1/accounts/{id}/flatten_position.

    IMPORTANT: places a MARKET order — fills at whatever price the book offers.
    Use only when you want immediate exit; for orderly exits use place_futures_order
    with a LIMIT instead.
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        return fc.flatten_position(contract_uuid=contract_uuid, account_id=account_id)
    except Exception as e:
        return {"success": False, "error": str(e)}


def place_futures_order(
    contract_uuid: str,
    side: str,
    quantity: int = 1,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "GFD",
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """Place a real futures order on Robinhood.

    PLACES REAL ORDERS. Verify all inputs before calling. The function generates
    a unique refId per call (RH dedupes by refId so accidental double-call is safe).

    side: 'BUY' or 'SELL'
    order_type: 'LIMIT' (default) or 'MARKET'. MARKET requires accept_market_risk=True.
    time_in_force: 'GFD' (day order, default) or 'GTC'.
    stop_price: optional. Setting it produces STOP or STOP_LIMIT order_trigger.

    Returns: {http_status, request_body, response} where response contains
    the order ID and derivedState (CONFIRMED / REJECTED / FILLED / CANCELLED).
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        return fc.place_order(
            contract_uuid=contract_uuid, side=side, quantity=quantity,
            order_type=order_type, limit_price=limit_price, stop_price=stop_price,
            time_in_force=time_in_force, account_id=account_id,
            accept_market_risk=accept_market_risk,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_buying_power_breakdown(account_number: str = "588784215") -> dict:
    """Per-category buying power breakdown: Cash, Margin total, Futures equity,
    Futures margin held, etc. Single source of truth for unified account capacity.
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        data = fc.get_buying_power_breakdown(account_number)
        # Extract futures-specific items for convenient consumption
        futures_items = {
            item["title"]: item["value"]
            for item in data.get("breakdown_items", [])
            if (item.get("category") or "").lower() == "futures"
        }
        return {
            "success": True,
            "account_number": data.get("account_number"),
            "account_type": data.get("account_type"),
            "futures_equity": futures_items.get("Futures equity"),
            "futures_margin_held": futures_items.get("Futures margin held"),
            "breakdown_items": data.get("breakdown_items", []),
            "raw": data,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_futures_history(ticker: str, period: str = "5y", interval: str = "1d") -> dict:
    """Historical bars for a futures contract via yfinance (independent of RH).

    Friendly tickers (MNQ, NQ, ES, CL, GC) map to yfinance =F symbols. Dated
    contracts (MNQM26) fall back to the continuous front-month series.

    period: '1d'..'10y'|'ytd'|'max'  |  interval: '1m'..'1mo'
    (note: 1m/5m only have ~7 days of history)
    """
    from rh_mcp.analysis import futures_history as fh
    try:
        return fh.get_bars(ticker, period=period, interval=interval)
    except Exception as e:
        return {"success": False, "error": f"futures history failed: {e}"}


def register_futures_uuid(ticker: str, uuid: str) -> dict:
    """Persist a ticker → UUID mapping for futures contracts. Get the UUID by
    inspecting RH web app network calls for the futures quote endpoint.
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        fc.register_uuid(ticker, uuid)
        return {"success": True, "ticker": ticker.upper().lstrip("/"), "uuid": uuid}
    except Exception as e:
        return {"success": False, "error": str(e)}


def scan_8k(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    item_codes: list[str] | None = None,
    lookback_minutes: int = 240,
    recent_filings_count: int = 100,
    top_n: int = 20,
    deep_scan: bool = False,
) -> dict:
    """SEC 8-K filing scanner: surfaces recent material filings with high-signal
    item codes. Default codes: 1.01 (Material Agreement, LONG bias), 3.02 (dilution),
    4.01 (auditor change), 4.02 (financial restatement) — last three SHORT bias.

    Set deep_scan=True to ALSO fetch filing bodies and run the keyword pattern
    library (going_concern, non_reliance, definitive_agreement, etc.). Body
    signals override item-code direction when confidence >= 0.7 — captures the
    "digestive drift" edge where market takes minutes to fully read the filing.

    Requires RH_EDGAR_USER_AGENT env var set per SEC policy.
    """
    from rh_mcp.analysis import scan_8k as _s8k
    try:
        return _s8k.analyze(
            tickers=tickers, universe_file=universe_file,
            item_codes=item_codes, lookback_minutes=lookback_minutes,
            recent_filings_count=recent_filings_count, top_n=top_n,
            deep_scan=deep_scan,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_8k failed: {e}"}


def backtest(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    strategies: list[str] | None = None,
) -> dict:
    """Walk-forward backtest of price-based strategies on 5y daily bars.

    Strategies: 'capitulation_reversal', 'rsi2_long', 'momentum_12_1', 'pead'.
    Pass strategies=None to run all four. Costs and slippage NOT modeled — see
    `caveats` field in response.
    """
    from rh_mcp.analysis import backtest as _bt
    try:
        return _bt.backtest(
            tickers=tickers, universe_file=universe_file, strategies=strategies,
        )
    except Exception as e:
        return {"success": False, "error": f"backtest failed: {e}"}


def scan_pead(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_days_since_earnings: int = 5,
    max_days_since_earnings: int = 30,
    min_eps_beat_pct: float = 5.0,
    min_gap_pct: float = 3.0,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    min_market_cap: float = 50_000_000_000,
    top_n: int = 15,
) -> dict:
    """Post-Earnings Announcement Drift: stocks 5-30 days past an earnings beat
    with gap-up confirmation, drift still intact. Strongest academic prior in
    the suite (Bernard & Thomas 1989).

    Default min_market_cap = $50B because tonight's tiered backtest showed the
    edge step-functions with size:
      Full universe: 52.7% win / +1.02% avg / PF 1.23 (essentially flat)
      $50B+ subset:  56.3% win / +2.47% avg / PF 1.74 (real edge, 314 names)

    Set min_market_cap=2_000_000_000 to capture a broader mid-cap pool, or 0 to
    disable the filter entirely (research mode).
    """
    from rh_mcp.analysis import pead
    try:
        return pead.analyze(
            tickers=tickers, universe_file=universe_file,
            min_days_since_earnings=min_days_since_earnings,
            max_days_since_earnings=max_days_since_earnings,
            min_eps_beat_pct=min_eps_beat_pct,
            min_gap_pct=min_gap_pct,
            min_price=min_price, min_avg_volume=min_avg_volume,
            min_market_cap=min_market_cap, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_pead failed: {e}"}


def scan_momentum_12_1(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    top_n: int = 20,
) -> dict:
    """Jegadeesh-Titman cross-sectional momentum: rank universe by 12-month
    return excluding the most recent month. Portfolio sleeve signal (hold
    1-3 months, monthly rebalance), not single-trade entry.
    """
    from rh_mcp.analysis import momentum_12_1
    try:
        return momentum_12_1.analyze(
            tickers=tickers, universe_file=universe_file,
            min_price=min_price, min_avg_volume=min_avg_volume, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_momentum_12_1 failed: {e}"}


def scan_capitulation_reversal(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_vol_ratio_yesterday: float = 3.0,
    min_decline_pct_yesterday: float = 5.0,
    min_price: float = 5.0,
    min_avg_volume: int = 500_000,
    top_n: int = 15,
) -> dict:
    """Two-bar capitulation+reversal: yesterday ≥3x vol on ≥5% decline with
    close in bottom 25% of range; today reversal (close in top 50% of range).
    """
    from rh_mcp.analysis import capitulation_reversal
    try:
        return capitulation_reversal.analyze(
            tickers=tickers, universe_file=universe_file,
            min_vol_ratio_yesterday=min_vol_ratio_yesterday,
            min_decline_pct_yesterday=min_decline_pct_yesterday,
            min_price=min_price, min_avg_volume=min_avg_volume, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_capitulation_reversal failed: {e}"}


def scan_rsi2_extremes(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    oversold_threshold: float = 5.0,
    overbought_threshold: float = 95.0,
    min_price: float = 5.0,
    top_n: int = 15,
) -> dict:
    """Connors RSI(2) mean reversion: oversold (RSI(2) < 5) in uptrend → long
    signal; overbought (RSI(2) > 95) in downtrend → short signal. 200-SMA
    trend filter mandatory.
    """
    from rh_mcp.analysis import rsi2_extremes
    try:
        return rsi2_extremes.analyze(
            tickers=tickers, universe_file=universe_file,
            oversold_threshold=oversold_threshold,
            overbought_threshold=overbought_threshold,
            min_price=min_price, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_rsi2_extremes failed: {e}"}


def scan_buyback_announcements(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    max_days_since_announcement: int = 14,
    min_price: float = 5.0,
    min_market_cap: float = 500_000_000,
    top_n: int = 15,
) -> dict:
    """Recent buyback announcements: stocks with new repurchase authorizations
    in the last N days. Ranked by buyback size as % of market cap when known.
    Long holding period (3-6 months) — treat as swing watch list.
    """
    from rh_mcp.analysis import buyback_announcements
    try:
        return buyback_announcements.analyze(
            tickers=tickers, universe_file=universe_file,
            max_days_since_announcement=max_days_since_announcement,
            min_price=min_price, min_market_cap=min_market_cap, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_buyback_announcements failed: {e}"}


def snapshot_oi(tickers: list[str]) -> dict:
    """Snapshot today's OI for the given tickers. Writes per-ticker JSON to
    RH_OI_HISTORY_DIR/YYYY-MM-DD/. Run daily after close to build the history
    that find_oi_spikes() compares against.
    """
    from rh_mcp.analysis import oi_history
    try:
        return oi_history.snapshot_universe(tickers)
    except Exception as e:
        return {"success": False, "error": f"snapshot_oi failed: {e}"}


def find_oi_spikes(
    tickers: list[str],
    days_back: int = 1,
    min_delta_pct: float = 50.0,
    min_delta_abs: int = 500,
) -> dict:
    """Compare today's OI vs a snapshot from N days ago. Flags strikes where
    OI grew by min_delta_abs contracts AND min_delta_pct percent.
    Requires prior snapshot_oi() runs to populate the comparison baseline.
    """
    from rh_mcp.analysis import oi_history
    try:
        return oi_history.find_oi_spikes(
            tickers=tickers,
            days_back=days_back,
            min_delta_pct=min_delta_pct,
            min_delta_abs=min_delta_abs,
        )
    except Exception as e:
        return {"success": False, "error": f"find_oi_spikes failed: {e}"}


def scan_failed_breakouts(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_breakout_pct: float = 0.3,
    min_fade_depth_pct: float = 0.5,
    min_price: float = 5.0,
    min_avg_volume: int = 500_000,
    top_n: int = 15,
) -> dict:
    """Find failed breakout short candidates: stocks that broke above prior-day
    high today, then faded back inside the prior range. Best mid-session
    (10:30 AM ET onwards). Ranked by fade depth from today's high.
    """
    from rh_mcp.analysis import failed_breakout
    try:
        return failed_breakout.analyze(
            tickers=tickers,
            universe_file=universe_file,
            min_breakout_pct=min_breakout_pct,
            min_fade_depth_pct=min_fade_depth_pct,
            min_price=min_price,
            min_avg_volume=min_avg_volume,
            top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_failed_breakouts failed: {e}"}


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
