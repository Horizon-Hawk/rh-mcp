"""rh-mcp server — registers all tools and runs stdio transport.

Run directly:
    python -m rh_mcp.server

Or via the installed entrypoint:
    rh-mcp
"""

from mcp.server.fastmcp import FastMCP

from rh_mcp import auth
from rh_mcp.tools import (
    quotes, account, orders, shorting, notifications, scanners,
    alerts, analysis, trade_log, sizing, pipeline,
    options_trading, social,
)


mcp = FastMCP("rh-mcp")


# ---------------------------------------------------------------------------
# Auth maintenance
# ---------------------------------------------------------------------------

@mcp.tool()
def force_relogin() -> dict:
    """Force a fresh Robinhood login. Call this when stock-side tools start
    returning empty results (the silent symptom of an expired RH token).
    Deletes the persisted session pickle and re-authenticates. Futures tools
    are unaffected since they use a separate bearer-token path.
    """
    return auth.force_relogin()


# ---------------------------------------------------------------------------
# Account & portfolio
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def get_portfolio(account_number: str | None = None) -> dict:
    """Portfolio summary: equity, cash, buying power, position count, top holdings."""
    return account.get_portfolio(account_number)


@mcp.tool()
@auth.auth_retry
def get_positions(account_number: str | None = None) -> dict:
    """Open stock positions with quantity, avg cost, current price, P&L."""
    return account.get_positions(account_number)


@mcp.tool()
@auth.auth_retry
def get_open_orders(account_number: str | None = None) -> dict:
    """All pending stock orders."""
    return account.get_open_orders(account_number)


@mcp.tool()
@auth.auth_retry
def get_option_positions(account_number: str | None = None) -> dict:
    """All open option contract positions: ticker, expiry, strike, type,
    contracts held, avg cost, mark, P&L. The portfolio summary's
    `build_holdings` excludes options — this is the only way to see
    contract holdings from the API.
    """
    return account.get_option_positions(account_number)


@mcp.tool()
@auth.auth_retry
def get_open_option_orders(account_number: str | None = None) -> dict:
    """All pending option orders (single-leg and multi-leg spreads). Each
    leg is enriched with strike/expiry/option_type so you can read the
    pending exposure without separately resolving instrument URLs.
    """
    return account.get_open_option_orders(account_number)


@mcp.tool()
def list_accounts() -> dict:
    """List linked Robinhood accounts."""
    return account.list_accounts()


@mcp.tool()
def get_account_status(account_number: str | None = None) -> dict:
    """Account status: PDT flag, day trades, buying power, margin info."""
    return account.get_account_status(account_number)


@mcp.tool()
def get_full_snapshot(account_number: str | None = None) -> dict:
    """ONE-CALL unified financial snapshot: BP, equity, all positions
    (stock/option/futures), all open orders. Replaces 4-6 sequential calls when
    you need a complete account view. Use for morning brief, capital check
    before sizing, or any 'what do I own + what can I deploy' question."""
    return account.get_full_snapshot(account_number)


@mcp.tool()
def get_settled_cash(account_number: str | None = None) -> dict:
    """Settled cash vs unsettled funds vs buying power for an account. Critical for cash accounts/IRAs — unsettled funds can buy but new shares are locked until T+1."""
    return account.get_settled_cash(account_number)


@mcp.tool()
def effective_basis(ticker: str, account_number: str | None = None) -> dict:
    """Effective cost basis adjusted for currently open option positions (covered calls, CSPs). RH's avg_cost is always the literal buy price — this computes what you'd actually break even on."""
    return account.effective_basis(ticker, account_number)


@mcp.tool()
def get_portfolio_history(span: str = "year", interval: str = "day", bounds: str = "regular", account_number: str | None = None) -> dict:
    """Historical portfolio equity curve. span='day|week|month|3month|year|5year|all', interval='5minute|10minute|hour|day|week'."""
    return account.get_portfolio_history(span, interval, bounds, account_number)


# ---------------------------------------------------------------------------
# Quotes & market data
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def get_quote(tickers: str) -> dict:
    """Current quote(s) for one or batched tickers (comma-separated). Includes AH price."""
    return quotes.get_quote(tickers)


@mcp.tool()
@auth.auth_retry
def get_latest_price(ticker: str) -> dict:
    """Fast single-ticker last-price lookup. Lighter than get_quote."""
    return quotes.get_latest_price(ticker)


@mcp.tool()
def get_bars(ticker: str, interval: str = "5minute", span: str = "day") -> dict:
    """Historical bars. interval: '5minute|10minute|hour|day|week'. span: 'day|week|month|3month|year|5year'."""
    return quotes.get_bars(ticker, interval, span)


@mcp.tool()
@auth.auth_retry
def get_market_hours(date: str | None = None) -> dict:
    """NYSE market hours for a date (YYYY-MM-DD). Omit for today."""
    return quotes.get_market_hours(date)


# ---------------------------------------------------------------------------
# Orders — placement
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def place_long(ticker: str, quantity: float, limit_price: float, account_number: str | None = None, time_in_force: str = "gfd", extended_hours: bool = False) -> dict:
    """Buy limit order. time_in_force: 'gfd' (day) or 'gtc'. extended_hours for AH."""
    return orders.place_long(ticker, quantity, limit_price, account_number, time_in_force, extended_hours)


@mcp.tool()
@auth.auth_retry
def place_short(ticker: str, quantity: float, limit_price: float, account_number: str | None = None, time_in_force: str = "gfd") -> dict:
    """Sell-short limit order. Check shortable + borrow fee first via check_short_availability."""
    return orders.place_short(ticker, quantity, limit_price, account_number, time_in_force)


@mcp.tool()
@auth.auth_retry
def close_position(ticker: str, quantity: float | None = None, limit_price: float | None = None, account_number: str | None = None) -> dict:
    """Close a long position. Omit quantity to close all. Omit limit_price for aggressive bid - $0.05."""
    return orders.close_position(ticker, quantity, limit_price, account_number)


@mcp.tool()
@auth.auth_retry
def close_all(ticker: str, account_number: str | None = None) -> dict:
    """Close the FULL position including any fractional residual. Splits into whole-share limit + fractional market orders. Returns both order IDs."""
    return orders.close_all(ticker, account_number)


# ---------------------------------------------------------------------------
# Orders — native stops (broker-enforced)
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def set_trailing_stop(ticker: str, quantity: float, trail_amount: float, trail_type: str = "percentage", time_in_force: str = "gtc") -> dict:
    """Native Robinhood trailing stop SELL order. trail_type='percentage' (3.0=3%) or 'price' ($). Survives chain failures."""
    return orders.set_trailing_stop(ticker, quantity, trail_amount, trail_type, time_in_force)


@mcp.tool()
@auth.auth_retry
def set_stop_loss(ticker: str, quantity: float, stop_price: float, time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Native fixed stop-loss SELL order at a specific price."""
    return orders.set_stop_loss(ticker, quantity, stop_price, time_in_force, account_number)


@mcp.tool()
@auth.auth_retry
def place_with_trailing_stop(ticker: str, quantity: float, limit_price: float, trail_percent: float = 3.0, account_number: str | None = None, extended_hours: bool = False, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC: buy limit + native trailing stop in one call. Eliminates the fill-to-stop gap."""
    return orders.place_with_trailing_stop(ticker, quantity, limit_price, trail_percent, account_number, extended_hours, fill_timeout_sec)


@mcp.tool()
@auth.auth_retry
def place_with_stop(ticker: str, quantity: float, limit_price: float, stop_price: float, account_number: str | None = None, extended_hours: bool = False, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC: buy limit + native fixed stop-loss in one call. For trailing stops use place_with_trailing_stop. (RH does not support true OCO; take-profit is handled via partial-exit framework, not a paired order.)"""
    return orders.place_with_stop(ticker, quantity, limit_price, stop_price, account_number, extended_hours, fill_timeout_sec)


@mcp.tool()
@auth.auth_retry
def flatten_position(ticker: str, limit_price: float | None = None, account_number: str | None = None) -> dict:
    """ATOMIC full exit: cancel all open sell-side orders for ticker (stops, trails, limits), then sell remaining shares. Use when you want to fully close a stop-protected position."""
    return orders.flatten_position(ticker, limit_price, account_number)


@mcp.tool()
@auth.auth_retry
def partial_with_trail_rearm(ticker: str, partial_quantity: float, exit_limit_price: float | None = None, trail_percent: float = 3.0, account_number: str | None = None, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC +2R partial exit: cancel existing trail/stop, sell partial, re-set trailing stop on remainder. Required because RH locks shares to the first sell-side order."""
    return orders.partial_with_trail_rearm(ticker, partial_quantity, exit_limit_price, trail_percent, account_number, fill_timeout_sec)


@mcp.tool()
@auth.auth_retry
def cover_short(ticker: str, quantity: float, limit_price: float | None = None, cancel_existing: bool = True, account_number: str | None = None) -> dict:
    """ATOMIC short cover: cancel buy-side orders for ticker, then BUY to close. Defaults to market BUY (matches RH app)."""
    return orders.cover_short(ticker, quantity, limit_price, cancel_existing, account_number)


@mcp.tool()
@auth.auth_retry
def adjust_trailing_stop(ticker: str, new_trail_percent: float, account_number: str | None = None) -> dict:
    """Tighten/loosen the trailing stop on an existing long position. Cancels current trail, re-sets at new percentage."""
    return orders.adjust_trailing_stop(ticker, new_trail_percent, account_number)


# ---------------------------------------------------------------------------
# Orders — cancellation & status
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def cancel_order(order_id: str) -> dict:
    """Cancel a specific open order. Auto-detects stock vs option from the ID."""
    return orders.cancel_order(order_id)


@mcp.tool()
@auth.auth_retry
def cancel_option_order(order_id: str) -> dict:
    """Cancel a specific open OPTION order. Explicit alternative to
    cancel_order's auto-dispatch — skips the stock-first probe.
    """
    return orders.cancel_option_order(order_id)


@mcp.tool()
@auth.auth_retry
def cancel_all_orders(account_number: str | None = None) -> dict:
    """Cancel ALL open orders — both stock and option. Use with caution."""
    return orders.cancel_all_orders(account_number)


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Status of a specific order: state, fills, prices. Auto-detects stock
    vs option from the ID (tries stock first, falls back to option).
    """
    return orders.get_order_status(order_id)


@mcp.tool()
def get_orders_status_batch(order_ids: list[str]) -> dict:
    """Status for multiple orders (stock or option) in one call. Returns dict keyed by order_id."""
    return orders.get_orders_status_batch(order_ids)


@mcp.tool()
def stage_stop_safe(ticker: str, quantity: float, stop_price: float, time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Place a stop-loss with graceful handling of settlement-lockup rejections (PDT/good-faith). Returns deferred=True if blocked by settlement, deferred=False on real errors."""
    return orders.stage_stop_safe(ticker, quantity, stop_price, time_in_force, account_number)


@mcp.tool()
def get_option_order_status(order_id: str) -> dict:
    """Status of a specific OPTION order including all legs and per-leg
    fills. Explicit alternative to get_order_status's auto-dispatch.
    """
    return orders.get_option_order_status(order_id)


# ---------------------------------------------------------------------------
# Shorting
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def check_short_availability(ticker: str) -> dict:
    """Shortable + borrow fee + float/SI data. Call before place_short."""
    return shorting.check_short_availability(ticker)


# ---------------------------------------------------------------------------
# Notifications & watchlists
# ---------------------------------------------------------------------------

@mcp.tool()
def get_notifications(count: int = 20) -> dict:
    """Recent Robinhood notifications. Native price alert fires appear here."""
    return notifications.get_notifications(count)


@mcp.tool()
@auth.auth_retry
def get_watchlists() -> dict:
    """List Robinhood watchlists."""
    return notifications.get_watchlists()


@mcp.tool()
@auth.auth_retry
def add_to_watchlist(watchlist_name: str, ticker: str) -> dict:
    """Add a ticker to a named watchlist."""
    return notifications.add_to_watchlist(watchlist_name, ticker)


@mcp.tool()
@auth.auth_retry
def remove_from_watchlist(watchlist_name: str, ticker: str) -> dict:
    """Remove a ticker from a named watchlist."""
    return notifications.remove_from_watchlist(watchlist_name, ticker)


# ---------------------------------------------------------------------------
# Market scanners
# ---------------------------------------------------------------------------

@mcp.tool()
def top_movers(direction: str = "up", scope: str = "sp500") -> dict:
    """Top market movers. direction='up'|'down'. scope='sp500'|'all'."""
    return scanners.top_movers(direction, scope)


@mcp.tool()
@auth.auth_retry
def get_news(ticker: str, count: int = 10) -> dict:
    """Recent news headlines for a ticker."""
    return scanners.get_news(ticker, count)


@mcp.tool()
@auth.auth_retry
def get_earnings(ticker: str) -> dict:
    """Earnings history + next earnings date."""
    return scanners.get_earnings(ticker)


@mcp.tool()
def get_portfolio_earnings(account_number: str | None = None, warn_within_days: int = 14) -> dict:
    """Earnings dates for all current stock positions in one call. Flags any earnings within warn_within_days for morning-brief highlighting."""
    return scanners.get_portfolio_earnings(account_number, warn_within_days)


@mcp.tool()
@auth.auth_retry
def get_fundamentals(ticker: str) -> dict:
    """Fundamentals: market cap, float, short interest, P/E, 52w range, sector."""
    return scanners.get_fundamentals(ticker)


@mcp.tool()
def scan_premium_sellers(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_days_to_earnings: int = 7,
    max_days_to_earnings: int = 30,
    min_iv_rank: float = 70.0,
    min_price: float = 10.0,
    top_n: int = 15,
) -> dict:
    """Iron-condor / premium-selling candidates: earnings 7-30d out AND
    iv_rank > 70. Sells elevated IV before earnings.
    """
    return scanners.scan_premium_sellers(
        tickers=tickers,
        universe_file=universe_file,
        min_days_to_earnings=min_days_to_earnings,
        max_days_to_earnings=max_days_to_earnings,
        min_iv_rank=min_iv_rank,
        min_price=min_price,
        top_n=top_n,
    )


@mcp.tool()
def scan_cheap_premium_buyers(
    tickers: list[str],
    max_iv_rank: float = 30.0,
    min_price: float = 10.0,
    top_n: int = 15,
) -> dict:
    """Filter tickers (typically scan_all output) to low-IV-rank candidates
    for debit spreads. Stock has directional setup + options are cheap = best
    leverage zone.
    """
    return scanners.scan_cheap_premium_buyers(
        tickers=tickers,
        max_iv_rank=max_iv_rank,
        min_price=min_price,
        top_n=top_n,
    )


@mcp.tool()
def scan_iv_crush_drift(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    max_days_since_earnings: int = 5,
    max_iv_rank: float = 30.0,
    min_price: float = 10.0,
    min_pct_since_earnings: float = 1.0,
    top_n: int = 15,
) -> dict:
    """Post-earnings IV-crush drift: earnings reported in last N days, IV
    crushed below threshold, stock still above 20d SMA. Cheap options +
    intact drift thesis.
    """
    return scanners.scan_iv_crush_drift(
        tickers=tickers,
        universe_file=universe_file,
        max_days_since_earnings=max_days_since_earnings,
        max_iv_rank=max_iv_rank,
        min_price=min_price,
        min_pct_since_earnings=min_pct_since_earnings,
        top_n=top_n,
    )


@mcp.tool()
def scan_unusual_oi(
    tickers: list[str],
    min_strike_oi: int = 100,
    concentration_multiple: float = 5.0,
    min_turnover_ratio: float = 0.5,
    top_n: int = 15,
) -> dict:
    """Unusual options activity: per-strike volume/OI turnover and OI
    concentration anomalies. Focused-list tool — pass scan_all output or
    watchlist tickers, NOT a full universe.
    """
    return scanners.scan_unusual_oi(
        tickers=tickers,
        min_strike_oi=min_strike_oi,
        concentration_multiple=concentration_multiple,
        min_turnover_ratio=min_turnover_ratio,
        top_n=top_n,
    )


@mcp.tool()
def get_futures_quote(ticker: str) -> dict:
    """Get current quote for an RH futures contract.
    Accepts: root ('MNQ', 'ES', 'GC', ...) auto-resolves to active front month;
    full dated ticker ('MNQM26'); or contract UUID. Roots resolve via the
    /marketdata/futures/quotes/v1/ endpoint and persist to ~/.rh_futures_uuids.json
    on first lookup — no manual register_futures_uuid step needed.
    """
    return scanners.get_futures_quote(ticker)


@mcp.tool()
def scan_futures_rsi2(
    tickers: list[str] | None = None,
    oversold_threshold: float = 5.0,
    overbought_threshold: float = 95.0,
) -> dict:
    """Futures RSI(2) mean reversion scanner. Default basket excludes NG (no edge).
    Returns longs (oversold + uptrend) and shorts (overbought + downtrend) with
    multi-fire flags when correlated instruments align (e.g. MNQ + ES both fire).
    Validated edge: GC PF 2.01, MNQ PF 1.92, ES PF 1.78 over 5y daily backtest.
    """
    return scanners.scan_futures_rsi2(
        tickers=tickers,
        oversold_threshold=oversold_threshold,
        overbought_threshold=overbought_threshold,
    )


@mcp.tool()
def warmup_yfinance(force: bool = False) -> dict:
    """Pre-warm yfinance HTTP session — fire once at start of session for fast
    follow-up scan_orb_mnq calls. Idempotent within 60s unless force=True.
    """
    return scanners.warmup_yfinance(force=force)


@mcp.tool()
def orb_breadth_check(date_et: str | None = None, window: str = "breakout") -> dict:
    """Mega-cap breadth at OR window or breakout bar (Phase 3.6 soft signal).
    Pulls 8 NQ-100 mega-caps (AAPL/NVDA/MSFT/AMZN/META/GOOGL/TSLA/AVGO) and counts
    directional confirmation. 5+/8 confirming at the breakout bar correlated with
    ORB failures in 60d exploratory sample. NOT a hard skip — informational.
    window: 'or' (09:30 ET bar) or 'breakout' (09:45 ET bar).
    """
    return scanners.orb_breadth_check(date_et=date_et, window=window)


@mcp.tool()
def manage_orb_trail(
    entry_time_et: str | None = None,
    current_stop: float | None = None,
) -> dict:
    """Given an open MNQ position, compute the 25pt trailing-stop level (Phase 3.6).
    Tracks MFE since entry, recommends new stop level. Pass current_stop to get
    explicit tighten/hold instruction. entry_time_et defaults to today's 09:45 ET.
    """
    return scanners.manage_orb_trail(entry_time_et=entry_time_et, current_stop=current_stop)


@mcp.tool()
def scan_orb_futures(root: str = "MNQ") -> dict:
    """ORB scanner for any CME equity-index root (MNQ, MES, NQ, ES, RTY, M2K).
    Same state machine and rules as scan_orb_mnq, just parameterized for
    different instruments. Stop/trail points are pre-tuned per instrument for
    similar dollar risk.

    Tiers: MNQ = A (validated PF 1.93, 2y backtest). All others = BETA — same
    pattern but per-root backtest is pending (task #19). Use validated=True
    flag on response to gate live trading.
    """
    return scanners.scan_orb_futures(root=root)


@mcp.tool()
def scan_orb_mnq() -> dict:
    """MNQ Opening Range Breakout scanner (Phase 3.6). Call any time during
    the trading day; returns current state of today's setup. Validated on 2y of
    real 15m MNQ data: PF 1.93 with 25pt trail, $5,500/yr at 1 contract, max DD $558,
    profitable in every year tested (2024/2025/2026 YTD).

    States: pre_or | or_forming | or_set | long_trigger | short_trigger |
    window_closed_no_signal | skipped_vix_deadzone | off_session.

    When a trigger fires, returns entry_price_expected + init_stop + trail rule +
    explicit instructions. Does NOT auto-fire orders.
    """
    return scanners.scan_orb_mnq()


@mcp.tool()
def scan_mnq_cvd() -> dict:
    """MNQ CVD divergence scanner. Reads MNQ tape state captured by NT8's
    TC_CVDExporter indicator (NT8 acts as data feed; trading still happens on RH).

    Detects:
      - bearish_divergence: price HH but CVD LH (buyers absorbed -> reversal risk)
      - bullish_divergence: price LL but CVD HL (sellers absorbed -> reversal risk)
      - absorption_buy:     heavy buy volume in current bar with no price advance
      - absorption_sell:    heavy sell volume in current bar with no price decline

    Returns bias call + action suggestion. Use during ORB / RSI(2) trade
    management to spot exhaustion before price confirms. Requires NT8 to be
    open with TC_CVDExporter loaded on a 15m MNQ chart; flags stale=True if
    the local state file hasn't refreshed in >90s.
    """
    return scanners.scan_mnq_cvd()


@mcp.tool()
def list_futures_accounts() -> dict:
    """List RH futures accounts (separate UUID from regular stock account number).
    Each account ID is needed as scope for positions/orders endpoints.
    """
    return scanners.list_futures_accounts()


@mcp.tool()
def get_futures_positions(account_id: str | None = None) -> dict:
    """Open RH futures positions. Uses default ACTIVE account if account_id omitted."""
    return scanners.get_futures_positions(account_id)


@mcp.tool()
def get_futures_orders(account_id: str | None = None, limit: int = 50) -> dict:
    """RH futures order history (most recent first)."""
    return scanners.get_futures_orders(account_id, limit=limit)


@mcp.tool()
def get_futures_aggregated_positions(account_id: str | None = None) -> dict:
    """Aggregated RH futures positions — per-contract roll-ups with P&L context.
    Different from get_futures_positions(): aggregates multiple fills into a single
    row per contract (matches the typical Positions view in RH web).
    """
    return scanners.get_futures_aggregated_positions(account_id)


@mcp.tool()
def cancel_futures_order(order_id: str, account_id: str | None = None) -> dict:
    """Cancel a pending unfilled futures order by ID. For closing FILLED
    positions use flatten_futures_position instead.
    """
    return scanners.cancel_futures_order(order_id=order_id, account_id=account_id)


@mcp.tool()
def flatten_futures_position(contract: str, account_id: str | None = None) -> dict:
    """EMERGENCY CLOSE a futures position via market order. RH auto-determines
    side and quantity from current position. Fills at whatever price the book offers.
    For orderly exits prefer place_futures_order with LIMIT.

    contract: root ('MNQ'), full ticker ('MNQM26'), or UUID.
    """
    return scanners.flatten_futures_position(contract=contract, account_id=account_id)


@mcp.tool()
def place_futures_order(
    contract: str,
    side: str,
    quantity: int = 1,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "GFD",
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """PLACES A REAL FUTURES ORDER ON ROBINHOOD.

    contract: any of:
      - root ('MNQ', 'ES', 'MES', 'GC', ...) → auto-resolves to active front month
      - full ticker ('MNQM26', 'ESU26', ...) → cache lookup
      - contract UUID (legacy)

    side: 'BUY' or 'SELL'. order_type: 'LIMIT' (default) or 'MARKET'.
    MARKET orders require accept_market_risk=True. time_in_force: 'GFD' or 'GTC'.
    Returns http_status + the order response with derivedState (REJECTED, CONFIRMED, FILLED),
    plus contract_input + resolved_contract_uuid so the caller can verify which
    contract actually got hit. Each call generates unique refId — RH dedupes
    accidental double-submits.
    """
    return scanners.place_futures_order(
        contract=contract, side=side, quantity=quantity,
        order_type=order_type, limit_price=limit_price, stop_price=stop_price,
        time_in_force=time_in_force, account_id=account_id,
        accept_market_risk=accept_market_risk,
    )


@mcp.tool()
def get_futures_ladder(
    contract: str,
    levels: int = 10,
    duration_sec: float = 3.0,
    source: str = "GLBX",
) -> dict:
    """Live L2 order book snapshot for a futures contract — the 'Ladder' view.

    Opens a real-time WebSocket subscription to the CME Globex order book via
    RH's dxFeed feed, collects Order add/modify/delete events for duration_sec
    (1700+ events/sec for active MNQ), then aggregates the resulting book state
    by price and returns the top N levels per side.

    contract: root ('MNQ'), full ticker ('MNQM26'), or UUID.
    levels: how many price levels per side (default 10).
    duration_sec: collect window (default 3s — enough for initial snapshot).
    source: dxFeed source. 'GLBX' covers all CME Globex products (MNQ, MES, NQ,
            ES, RTY, M2K and the futures we trade).

    Returns ladder with best_bid, best_ask, mid, spread, bids[], asks[] (each
    level: {price, size, n_orders}), plus active_orders count.
    """
    return scanners.get_futures_ladder(
        contract=contract, levels=levels,
        duration_sec=duration_sec, source=source,
    )


@mcp.tool()
def get_futures_contract_quantity(contract: str, account_id: str | None = None) -> dict:
    """Held / pending / net quantity for a specific futures contract.
    contract: root ('MNQ'), full ticker ('MNQM26'), or UUID. Use to verify
    actual position size before placing exit orders.
    """
    return scanners.get_futures_contract_quantity(contract=contract, account_id=account_id)


@mcp.tool()
def get_futures_order_validation_rules(account_id: str | None = None) -> dict:
    """Server-side order validation rules for the futures account — max order
    quantity, etc. Check before placing large/unusual orders to catch caps the
    server will reject.
    """
    return scanners.get_futures_order_validation_rules(account_id=account_id)


@mcp.tool()
def get_futures_order_detail(
    order_id: str,
    asset_type: str = "FUTURES",
    account_number: str = "588784215",
) -> dict:
    """Single-order full detail via wormhole /orders/{id}. Richer than the
    ceres list view (productSymbol, lastDayToTrade, full executions, fees).
    asset_type: 'FUTURES' (default) / 'EQUITY' / 'OPTION' / 'CRYPTO'.
    """
    return scanners.get_futures_order_detail(
        order_id=order_id, asset_type=asset_type, account_number=account_number,
    )


@mcp.tool()
def get_recent_orders_unified(account_number: str = "588784215") -> dict:
    """Recent orders across ALL asset types (stock, options, futures, crypto)
    in one call. Returns per-asset and per-state breakdowns plus the full rows.
    Use for a unified 'what's working / what just filled' view.
    """
    return scanners.get_recent_orders_unified(account_number=account_number)


@mcp.tool()
def monitor_orb_position(
    entry_time_et: str | None = None,
    moc_minute_offset: int = 0,
    min_tighten_pts: float = 1.0,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict:
    """Single-iteration ORB monitor (Phase 3.6 auto-management).

    PERFORMS REAL ORDER ACTIONS unless dry_run=True. Each call:
      1. Reads open MNQ position + recommended trail stop (via orb_trail logic)
      2. If past 15:55 ET → flattens position (MOC exit)
      3. Else if recommended stop is tighter than current → atomic /replace
      4. Else → no action

    Drive via ScheduleWakeup every 2 minutes once a position is open. The
    response includes next_wakeup_seconds (None means stop the loop —
    no_position or moc_exit). Each call is idempotent — a missed wakeup
    just means the next call catches up.

    States: no_position | hold | tightened | moc_exit | no_open_stop |
            data_error | action_failed

    Args:
      entry_time_et: ISO timestamp of entry (defaults to today's 09:45 ET)
      moc_minute_offset: shift the 15:55 MOC fire by N minutes (testing)
      min_tighten_pts: skip tightens smaller than this (default 1.0pt = 4 ticks)
      dry_run: compute action but don't call RH
    """
    return scanners.monitor_orb_position(
        entry_time_et=entry_time_et,
        moc_minute_offset=moc_minute_offset,
        min_tighten_pts=min_tighten_pts,
        dry_run=dry_run,
        account_id=account_id,
    )


@mcp.tool()
def place_futures_with_stop(
    contract: str,
    side: str,
    quantity: int = 1,
    limit_price: float | None = None,
    stop_price: float | None = None,
    stop_distance_points: float | None = None,
    order_type: str = "LIMIT",
    time_in_force: str = "GFD",
    stop_time_in_force: str = "GTC",
    fill_timeout_sec: float = 60.0,
    poll_interval_sec: float = 2.0,
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """ATOMIC FUTURES BRACKET — places entry, polls until filled, then immediately
    places opposite-side stop on the filled quantity.

    PLACES REAL ORDERS. Closes the unprotected window between fill and manual
    stop placement. Use this for any A-grade entry where you want guaranteed
    stop coverage without depending on a follow-up tool call.

    contract: root ('MNQ', 'ES', ...), full ticker ('MNQM26'), or UUID.
    side: 'BUY' or 'SELL' for the entry. Stop side is auto-derived (opposite).
    Specify exactly one of:
      - stop_price: absolute trigger price
      - stop_distance_points: distance from fill (e.g. 50 for the Phase 3.6 ORB
        50-point initial stop). Use this with MARKET entries when fill is unknown.

    Stop is placed as a STOP_MARKET (triggers a market order at stop_price)
    with stop_time_in_force defaulting to GTC so it survives the session.

    Returns:
      stage: 'complete' (entry+stop both placed) | 'rejected_entry' |
             'fill_timeout' (entry working, no stop placed) | 'rejected_stop' |
             'entry_cancelled' | 'entry_rejected'
      entry: {order_id, state, fill_price, fill_quantity}
      stop:  {order_id, stop_price, side, state, response} | None
    """
    return scanners.place_futures_with_stop(
        contract=contract, side=side, quantity=quantity,
        limit_price=limit_price, stop_price=stop_price,
        stop_distance_points=stop_distance_points,
        order_type=order_type, time_in_force=time_in_force,
        stop_time_in_force=stop_time_in_force,
        fill_timeout_sec=fill_timeout_sec,
        poll_interval_sec=poll_interval_sec,
        account_id=account_id, accept_market_risk=accept_market_risk,
    )


@mcp.tool()
def get_buying_power_breakdown(account_number: str = "588784215") -> dict:
    """Per-category buying power breakdown — Cash, Margin total, Futures equity,
    Futures margin held, Short cash. The 'futures_equity' and 'futures_margin_held'
    fields are surfaced separately at the top level for convenience.
    """
    return scanners.get_buying_power_breakdown(account_number)


@mcp.tool()
def get_futures_history(ticker: str, period: str = "5y", interval: str = "1d") -> dict:
    """Historical bars for a futures contract (NQ, MNQ, ES, CL, GC etc.) via yfinance.
    Returns OHLCV bars suitable for backtesting and EOD analysis. RH's futures
    historicals endpoint isn't reverse-engineered yet — this is the independent
    data source until that lands.
    """
    return scanners.get_futures_history(ticker, period=period, interval=interval)


@mcp.tool()
def register_futures_uuid(ticker: str, uuid: str) -> dict:
    """Persist a futures ticker → contract UUID mapping.

    For roots in KNOWN_ROOTS (MNQ, NQ, MES, ES, RTY, M2K, YM, MYM, GC, MGC, SI,
    CL, MCL, NG) you generally don't need this — resolve_futures_contract will
    auto-discover and persist via the symbol lookup endpoint. Use register_uuid
    only for non-listed roots or to override a stale resolution.

    Cached to ~/.rh_futures_uuids.json so future sessions inherit the mappings.
    """
    return scanners.register_futures_uuid(ticker, uuid)


@mcp.tool()
def resolve_futures_contract(value: str) -> dict:
    """Resolve any futures input (root, full ticker, or UUID) to a contract UUID
    plus its current quote. Use to preview which contract place_futures_order
    will actually hit before placing the order.

    Input examples: 'MNQ' (root → active front month), 'MNQM26' (cached or
    live-looked-up), or a UUID (returned as-is + quote pulled).

    Returns: {success, input, resolved_uuid, ticker_resolved, symbol, state,
    bid, ask, last, ...} on hit; {success: False, ...} on miss.
    """
    from rh_mcp.analysis import futures_client as fc
    try:
        uuid = fc.resolve_contract_input(value)
        if not uuid:
            return {
                "success": False,
                "input": value,
                "error": f"could not resolve {value!r}. Pass a root, full ticker, or UUID.",
                "known_roots": list(fc.KNOWN_ROOTS),
            }
        # Pull a fresh quote for verification
        raw = fc.get_quotes(uuid)
        data = (raw.get("data") or [{}])[0]
        inner = data.get("data") or {}
        return {
            "success": True,
            "input": value,
            "resolved_uuid": uuid,
            "symbol": inner.get("symbol"),
            "state": inner.get("state"),
            "bid": inner.get("bid_price"),
            "ask": inner.get("ask_price"),
            "last": inner.get("last_trade_price"),
            "updated_at": inner.get("updated_at"),
        }
    except Exception as e:
        return {"success": False, "input": value, "error": str(e)}


@mcp.tool()
def scan_stack(
    cap_range: str = "stack",
    lookback_minutes: int = 1440,
    buyback_days: int = 14,
    pead_min_market_cap: float = 50_000_000_000,
    top_n_per_strategy: int = 10,
    per_scanner_timeout_sec: int = 120,
) -> dict:
    """START the 4-scanner stack (bullish_8k, buyback, pead, pead_negative) as a BACKGROUND job. Returns a job_id immediately — poll scan_stack_status(job_id) for progress + results. Per-scanner hang detection: any scanner exceeding per_scanner_timeout_sec is marked 'hung' and skipped."""
    return scanners.scan_stack(
        cap_range=cap_range,
        lookback_minutes=lookback_minutes,
        buyback_days=buyback_days,
        pead_min_market_cap=pead_min_market_cap,
        top_n_per_strategy=top_n_per_strategy,
        per_scanner_timeout_sec=per_scanner_timeout_sec,
    )


@mcp.tool()
def scan_stack_status(job_id: str) -> dict:
    """Poll a scan_stack background job. Returns per-scanner status (pending|running|completed|failed|hung), elapsed times, aggregated candidates so far. Hang warnings appear on running scanners that exceed 2× their timeout."""
    return scanners.scan_stack_status(job_id)


@mcp.tool()
def scan_bullish_8k(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    cap_range: str = "small",
    lookback_minutes: int = 720,
    top_n: int = 10,
    exclude_sectors: list[str] | None = None,
) -> dict:
    """Scan for the validated 8-K bullish edge. `cap_range` selects
    universe: 'small' ($300M-$2B, +143.94% / 22.7% DD), 'mid' ($2B-$10B,
    +147.95% / 21.6% DD), or 'stack' (both — regime-complementary;
    mid-cap dominates bears, small-cap dominates bulls; +372.92% / 20.8%
    DD on 4yr stack backtest at max 5 concurrent). Deep_scan direction
    classifier runs (keyword + historical lookup, FinBERT disabled for
    speed). Returns direction='long' only. Stack candidates tagged with
    their universe of origin.

    OPERATING RULE (small-cap only): exit at t+1 close if position is
    down >=5% from entry. Adds +8.6pp return, -0.6pp DD on small-cap
    backtests. DO NOT apply on mid-cap or stack — it inflates DD there.

    `exclude_sectors`: defaults to ["Industrials"] per validated finding
    (41.9% win rate on that sector, drags total return). Pass [] to take
    everything; sectors are pulled per-candidate from yfinance.
    """
    return scanners.scan_bullish_8k(
        tickers=tickers, universe_file=universe_file,
        cap_range=cap_range,
        lookback_minutes=lookback_minutes, top_n=top_n,
        exclude_sectors=exclude_sectors,
    )


@mcp.tool()
def scan_gap_and_go(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_gap_pct: float = 0.20,
    top_n: int = 25,
) -> dict:
    """Live scan for gap-and-go signals: >=20% gap up, >=2x avg volume,
    close above open. Defaults to small_cap_universe.txt — validated edge
    on Finviz $300M-$2B caps. Penny universe shows weaker / shorter edge
    (use 1-day hold for penny, 5-day for small-cap).
    """
    return scanners.scan_gap_and_go(
        tickers=tickers, universe_file=universe_file,
        min_gap_pct=min_gap_pct, top_n=top_n,
    )


@mcp.tool()
def scan_frd(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    top_n: int = 25,
) -> dict:
    """Live scan for First Red Day signals (3+ green closes, ≥30% run,
    then first red close). Trade direction is COUNTER-INTUITIVE: on
    Finviz small-cap universe these BOUNCE for +117% over 12 months at
    5-day hold (FRD-LONG). Penny stocks fade instead — do NOT trade FRD
    signals on penny universe.
    """
    return scanners.scan_frd(
        tickers=tickers, universe_file=universe_file, top_n=top_n,
    )


@mcp.tool()
def classify_8k(
    accession_no: str,
    ticker: str | None = None,
    filing_url: str | None = None,
    cik: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """FinBERT-based sentiment classifier for a single 8-K filing. Returns
    direction (long/short/neutral), confidence (0-1), reasoning, and key
    clauses extracted from the filing body. Result is cached per accession_no.
    Provide at least one of: filing_url (best), cik, or ticker — needed to
    locate the filing on EDGAR. Requires `transformers` and `torch` installed
    (pip install rh-mcp[finbert]).
    """
    return scanners.classify_8k(
        accession_no=accession_no, ticker=ticker,
        filing_url=filing_url, cik=cik, force_refresh=force_refresh,
    )


@mcp.tool()
def classify_8k_historical(
    accession_no: str,
    ticker: str | None = None,
    filing_url: str | None = None,
    cik: str | None = None,
    csv_path: str | None = None,
    with_body_keywords: bool = True,
    force_refresh: bool = False,
) -> dict:
    """Classify an 8-K by nearest-signature lookup against the historical
    market-reaction corpus. Direction comes from the average next-day return
    of historical filings sharing the same item-codes + body-keyword
    signature. Requires the corpus built first via build_8k_history.
    """
    return scanners.classify_8k_historical(
        accession_no=accession_no, ticker=ticker,
        filing_url=filing_url, cik=cik,
        csv_path=csv_path, with_body_keywords=with_body_keywords,
        force_refresh=force_refresh,
    )


@mcp.tool()
def build_8k_history(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    lookback_days: int = 365,
    output_path: str | None = None,
    with_body_keywords: bool = False,
    max_tickers: int | None = None,
) -> dict:
    """Build the 8-K → market-reaction CSV corpus that powers
    classify_8k_historical. Walks per-ticker 8-K history via EDGAR, computes
    next-day and 5-day returns from yfinance, labels each filing. Slow first
    build (~15-25 min item-codes-only, 45-60 min with body keywords) — run
    periodically (monthly) thereafter to keep fresh. Default output:
    ~/.edgar_cache/8k_history.csv.
    """
    return scanners.build_8k_history(
        tickers=tickers, universe_file=universe_file,
        lookback_days=lookback_days, output_path=output_path,
        with_body_keywords=with_body_keywords, max_tickers=max_tickers,
    )


@mcp.tool()
def history_corpus_stats(csv_path: str | None = None) -> dict:
    """Summary of the loaded 8-K history corpus: row count, label
    distribution, top item codes. Use to verify build coverage."""
    return scanners.history_corpus_stats(csv_path=csv_path)


@mcp.tool()
def scan_float_rotation(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_vol_to_float_ratio: float = 1.0,
    require_8k: bool = True,
    require_bullish_8k: bool = True,
    lookback_minutes: int = 480,
    top_n: int = 10,
) -> dict:
    """Float-rotation breakout scanner — finds small caps (≤$3B, ≤100M
    float) that have already traded ≥1x their float today AND have a
    bullish 8-K filing in `lookback_minutes`. Edge stacks: forced supply
    exhaustion + fundamental catalyst. Rank by vol/float × 8-K confidence.
    """
    from rh_mcp.analysis import float_rotation as _fr
    try:
        return _fr.analyze(
            tickers=tickers, universe_file=universe_file,
            min_vol_to_float_ratio=min_vol_to_float_ratio,
            require_8k=require_8k, require_bullish_8k=require_bullish_8k,
            lookback_minutes=lookback_minutes, top_n=top_n,
        )
    except Exception as e:
        return {"success": False, "error": f"scan_float_rotation failed: {e}"}


@mcp.tool()
def quality_check(
    tickers: list[str],
    min_gross_margin: float = 0.30,
    min_roe: float = 0.08,
    max_debt_equity: float = 2.0,
    min_profit_margin: float = 0.02,
) -> dict:
    """Quality overlay for any setup: gross margin, ROE, D/E, profit margin.
    Returns is_quality + failing_checks per ticker. Use to gate small-cap
    entries — junk small caps that pass technicals fail this filter."""
    from rh_mcp.analysis import quality_filter as _qf
    try:
        return _qf.analyze(
            tickers, min_gross_margin=min_gross_margin, min_roe=min_roe,
            max_debt_equity=max_debt_equity, min_profit_margin=min_profit_margin,
        )
    except Exception as e:
        return {"success": False, "error": f"quality_check failed: {e}"}


@mcp.tool()
def liquidity_check(
    ticker: str,
    price: float,
    side: str = "long",
    market_cap: float | None = None,
) -> dict:
    """Pre-entry order-book sanity check. Stricter thresholds for small
    caps (<$1B): spread ≤0.75%, depth ≥30 levels, entry-side wall ≥0.35%
    away. Call before any small-cap order_id placement."""
    from rh_mcp.analysis import liquidity_guard as _lg
    try:
        return _lg.check(ticker, price, side=side, market_cap=market_cap)
    except Exception as e:
        return {"success": False, "error": f"liquidity_check failed: {e}"}


@mcp.tool()
def scan_8k(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    item_codes: list[str] | None = None,
    lookback_minutes: int = 240,
    recent_filings_count: int = 100,
    top_n: int = 20,
    deep_scan: bool = False,
) -> dict:
    """SEC 8-K filing scanner. Default codes: 1.01 (long bias material agreement),
    3.02/4.01/4.02 (short bias dilution/auditor change/restatement). Set deep_scan=True
    to fetch filing bodies + run keyword pattern library — catches "digestive drift"
    signals (non-reliance, going concern, definitive agreement, etc.) that override
    item-code direction when confidence >= 0.7.
    """
    return scanners.scan_8k(
        tickers=tickers, universe_file=universe_file,
        item_codes=item_codes, lookback_minutes=lookback_minutes,
        recent_filings_count=recent_filings_count, top_n=top_n,
        deep_scan=deep_scan,
    )


@mcp.tool()
def backtest(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    strategies: list[str] | None = None,
) -> dict:
    """Walk-forward backtest of price-based strategies on 5y daily bars.
    Returns per-strategy win rate, avg return, profit factor, Sharpe-per-trade.
    Strategies: capitulation_reversal, rsi2_long, momentum_12_1, pead.
    """
    return scanners.backtest(tickers=tickers, universe_file=universe_file, strategies=strategies)


@mcp.tool()
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
    """Post-Earnings Announcement Drift: stocks 5-30 days past an earnings
    beat with gap-up confirmation. Default min_market_cap=$50B (per backtest:
    edge step-functions with size — $50B+ gives 56% win, +2.47% avg, PF 1.74).
    """
    return scanners.scan_pead(
        tickers=tickers, universe_file=universe_file,
        min_days_since_earnings=min_days_since_earnings,
        max_days_since_earnings=max_days_since_earnings,
        min_eps_beat_pct=min_eps_beat_pct, min_gap_pct=min_gap_pct,
        min_price=min_price, min_avg_volume=min_avg_volume,
        min_market_cap=min_market_cap, top_n=top_n,
    )


@mcp.tool()
def scan_pead_negative(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_days_since_earnings: int = 1,
    max_days_since_earnings: int = 10,
    min_neg_gap_pct: float = 3.0,
    max_extended_drop_pct: float = 15.0,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    min_market_cap: float = 300_000_000,
    top_n: int = 15,
    exclude_sectors: list[str] | None = None,
) -> dict:
    """PEAD NEGATIVE BOUNCE — TOP EV: +141.66% / 4yr cumulative,
    50.4% win, +1.18% avg per trade (558 trades, max DD 29.4%, +33% in 2022 bear).

    Counter-intuitive: buys oversold post-earnings flushes. Filters: earnings
    1-10d ago, t+1 close DOWN ≥3% from pre-earnings close, today still below
    pre-earnings close (bounce intact), not extended past -15% (skip falling
    knives). Mechanical 4-day hold per backtest spec.

    `exclude_sectors`: defaults to ["Industrials","Energy","Basic Materials",
    "Real Estate","Consumer Defensive"] — the 5 sectors flagged as drags on
    PEAD by the 4yr backtest. Skipping these lifts return +141.66% -> +241.30%
    and DD 29.4% -> 18.3%. Pass [] to disable.
    """
    return scanners.scan_pead_negative(
        tickers=tickers, universe_file=universe_file,
        min_days_since_earnings=min_days_since_earnings,
        max_days_since_earnings=max_days_since_earnings,
        min_neg_gap_pct=min_neg_gap_pct,
        max_extended_drop_pct=max_extended_drop_pct,
        min_price=min_price, min_avg_volume=min_avg_volume,
        min_market_cap=min_market_cap, top_n=top_n,
        exclude_sectors=exclude_sectors,
    )


@mcp.tool()
def scan_momentum_12_1(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_price: float = 5.0,
    min_avg_volume: int = 200_000,
    top_n: int = 20,
) -> dict:
    """Jegadeesh-Titman 12-1 cross-sectional momentum. Portfolio-construction
    signal — top names ranked by 12-month return excluding most recent month.
    Hold 1-3 months, monthly rebalance.
    """
    return scanners.scan_momentum_12_1(
        tickers=tickers, universe_file=universe_file,
        min_price=min_price, min_avg_volume=min_avg_volume, top_n=top_n,
    )


@mcp.tool()
def scan_capitulation_reversal(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_vol_ratio_yesterday: float = 3.0,
    min_decline_pct_yesterday: float = 5.0,
    min_price: float = 5.0,
    min_avg_volume: int = 500_000,
    top_n: int = 15,
) -> dict:
    """Two-bar capitulation+reversal: yesterday ≥3x vol on ≥5% decline + close
    in bottom 25% of range; today reversal candle. Bounce setup, 2:1 R:R with
    stop below yesterday's low.
    """
    return scanners.scan_capitulation_reversal(
        tickers=tickers, universe_file=universe_file,
        min_vol_ratio_yesterday=min_vol_ratio_yesterday,
        min_decline_pct_yesterday=min_decline_pct_yesterday,
        min_price=min_price, min_avg_volume=min_avg_volume, top_n=top_n,
    )


@mcp.tool()
def scan_rsi2_extremes(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    oversold_threshold: float = 5.0,
    overbought_threshold: float = 95.0,
    min_price: float = 5.0,
    top_n: int = 15,
) -> dict:
    """Connors RSI(2) mean reversion: oversold in uptrend (long) or overbought
    in downtrend (short). 200-SMA trend filter applied. Returns separate
    longs[] and shorts[] lists.
    """
    return scanners.scan_rsi2_extremes(
        tickers=tickers, universe_file=universe_file,
        oversold_threshold=oversold_threshold,
        overbought_threshold=overbought_threshold,
        min_price=min_price, top_n=top_n,
    )


@mcp.tool()
def scan_buyback_announcements(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    max_days_since_announcement: int = 14,
    min_price: float = 5.0,
    min_market_cap: float = 500_000_000,
    top_n: int = 15,
    exclude_sectors: list[str] | None = None,
) -> dict:
    """Recent buyback announcements: stocks with new repurchase authorizations
    in last N days. Ranked by buyback size as % of market cap. Long holding
    period (3-6 months) — swing watch list, not direct entry.

    `exclude_sectors`: defaults to ["Industrials","Consumer Cyclical",
    "Healthcare"] per 4yr backtest. Healthcare buybacks uniquely weak
    (40% win — likely "weak R&D pipeline" signal). Skipping these lifts
    buyback returns +43.19% -> +84.91% and DD 18.6% -> 8.4%. Pass [] to
    disable.
    """
    return scanners.scan_buyback_announcements(
        tickers=tickers, universe_file=universe_file,
        max_days_since_announcement=max_days_since_announcement,
        min_price=min_price, min_market_cap=min_market_cap, top_n=top_n,
        exclude_sectors=exclude_sectors,
    )


@mcp.tool()
def snapshot_oi(tickers: list[str]) -> dict:
    """Snapshot today's OI for tickers. Writes JSON to RH_OI_HISTORY_DIR/YYYY-MM-DD/.
    Run daily after close to build the OI delta history.
    """
    return scanners.snapshot_oi(tickers)


@mcp.tool()
def find_oi_spikes(
    tickers: list[str],
    days_back: int = 1,
    min_delta_pct: float = 50.0,
    min_delta_abs: int = 500,
) -> dict:
    """Find strikes where OI grew significantly vs N days ago. Requires
    snapshot_oi() runs to populate the comparison baseline.
    """
    return scanners.find_oi_spikes(
        tickers=tickers, days_back=days_back,
        min_delta_pct=min_delta_pct, min_delta_abs=min_delta_abs,
    )


@mcp.tool()
def scan_failed_breakouts(
    tickers: list[str] | None = None,
    universe_file: str | None = None,
    min_breakout_pct: float = 0.3,
    min_fade_depth_pct: float = 0.5,
    min_price: float = 5.0,
    min_avg_volume: int = 500_000,
    top_n: int = 15,
) -> dict:
    """Failed breakout shorts: stocks that broke prior-day high today then faded
    back inside. Best mid-session. Ranked by fade depth from today's high.
    """
    return scanners.scan_failed_breakouts(
        tickers=tickers,
        universe_file=universe_file,
        min_breakout_pct=min_breakout_pct,
        min_fade_depth_pct=min_fade_depth_pct,
        min_price=min_price,
        min_avg_volume=min_avg_volume,
        top_n=top_n,
    )


@mcp.tool()
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
    """Composite morning-brief scanner — runs 52w-high, Bollinger squeeze, and
    sympathy-laggard scanners in parallel and reconciles overlaps. Multi-signal
    tickers appear first in the candidates list (highest conviction setups).
    """
    return scanners.scan_all(
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


@mcp.tool()
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

    Precursor signal to the 52w-high scanner. APLS-style setups surface
    here at $40.50 before they hit the 52w level at $41.40.
    """
    return scanners.scan_squeeze_breakouts(
        tickers=tickers,
        universe_file=universe_file,
        compression_percentile=compression_percentile,
        proximity_upper_pct=proximity_upper_pct,
        min_price=min_price,
        min_avg_volume=min_avg_volume,
        top_n=top_n,
    )


@mcp.tool()
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
    """Find industries with a strong leader today + lagging peers (catch-up
    trades). Returns {industry, leader, laggards} groups ranked by leader
    move size.
    """
    return scanners.scan_sympathy_laggards(
        tickers=tickers,
        universe_file=universe_file,
        leader_move_pct=leader_move_pct,
        max_laggard_move_pct=max_laggard_move_pct,
        min_price=min_price,
        min_avg_volume=min_avg_volume,
        min_peers_per_industry=min_peers_per_industry,
        top_n=top_n,
    )


@mcp.tool()
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
    """Scan a universe for stocks at-or-near 52-week highs with confirming volume.

    Filters by the momentum-breakout framework (SPY uptrend, no earnings within
    5d, min $5 price, 1.5x volume). Returns ranked candidates with pct_to_52w,
    volume_ratio, today's pct change, sector, market_cap, and an A/B grade
    suitable for the entry pipeline.
    """
    return scanners.scan_52w_breakouts(
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


# ---------------------------------------------------------------------------
# Local alert management (price_alerts.json)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_alerts(active_only: bool = True, ticker: str | None = None) -> dict:
    """List price alerts from price_alerts.json. active_only=True hides deactivated."""
    return alerts.list_alerts(active_only, ticker)


@mcp.tool()
def add_alert(ticker: str, target: float, direction: str, grade: str = "watch", note: str = "", active: bool = True) -> dict:
    """Add a price alert. direction='above'|'below'. Picked up by monitor on its next cycle."""
    return alerts.add_alert(ticker, target, direction, grade, note, active)


@mcp.tool()
def deactivate_alert(ticker: str, target: float | None = None, grade: str | None = None) -> dict:
    """Deactivate matching alerts (sets active=False). Omit target to deactivate all for ticker."""
    return alerts.deactivate_alert(ticker, target, grade)


@mcp.tool()
def delete_alert(ticker: str, target: float, direction: str) -> dict:
    """Hard delete a specific alert."""
    return alerts.delete_alert(ticker, target, direction)


@mcp.tool()
def auto_stage_trade_card(ticker: str, entry: float, stop: float, target: float | None = None, shares: float | None = None, direction: str = "long", grade: str = "A", thesis: str = "") -> dict:
    """ATOMIC: write the full trade-card alerts (STOP, +1R, +2R, TARGET) for a new entry.
    Closes the gap where R-multiples lived only in memory and never fired."""
    return alerts.auto_stage_trade_card(ticker, entry, stop, target, shares, direction, grade, thesis)


# ---------------------------------------------------------------------------
# Setup analysis (wraps scripts: stack_grade, order_book, historicals, etc.)
# ---------------------------------------------------------------------------

@mcp.tool()
def stack_grade(tickers: str) -> dict:
    """Combined float + RV + news catalyst grade. Returns A+/A/B/SKIP STACK per ticker."""
    return analysis.stack_grade(tickers)


@mcp.tool()
@auth.auth_retry
def order_book_scan(ticker: str, price: float, range_dollars: float | None = None, threshold: int | None = None) -> dict:
    """Order book depth + OBI signal at a target price. Returns spread, walls, BUY/SELL/BALANCED signal."""
    return analysis.order_book_scan(ticker, price, range_dollars, threshold)


@mcp.tool()
def historicals(ticker: str) -> dict:
    """Daily-bar breakout grade: volume ratio, body %, 20d SMA, MM target, suggested stop, R:R."""
    return analysis.historicals(ticker)


@mcp.tool()
def float_check(ticker: str) -> dict:
    """Float + short-interest squeeze grade: SQUEEZE / ELEVATED / NORMAL / LOW_RISK_SHORT."""
    return analysis.float_check(ticker)


@mcp.tool()
def iv_rank(ticker: str) -> dict:
    """Implied volatility rank — gauge whether options are expensive or cheap."""
    return analysis.iv_rank(ticker)


@mcp.tool()
def options_scan(ticker: str) -> dict:
    """Options chain scan: ATM IV, max pain, OI walls, spread setup ideas."""
    return analysis.options_scan(ticker)


@mcp.tool()
def spike_check(ticker: str, spike_price: float, ts: str) -> dict:
    """Validate a volume spike entry: staleness, drift, fade, current price. ts='YYYY-MM-DDTHH:MM:SS'."""
    return analysis.spike_check(ticker, spike_price, ts)


# ---------------------------------------------------------------------------
# Trade logging (wraps trade_logger.py)
# ---------------------------------------------------------------------------

@mcp.tool()
def log_open(account: str, ticker: str, direction: str, strategy: str, entry: float, shares: float, stop: float | None = None, target: float | None = None, thesis: str = "") -> dict:
    """Open a trade in the structured log. strategy='momentum_breakout|debit_spread|earnings_drift|ah_earnings|dead_cat_short|intraday|spike_entry'."""
    return trade_log.log_open(account, ticker, direction, strategy, entry, shares, stop, target, thesis)


@mcp.tool()
def log_partial(trade_id: str, exit_price: float, shares: float, reason: str = "", notes: str = "") -> dict:
    """Record a partial exit. Trade stays OPEN unless this closes remaining shares."""
    return trade_log.log_partial(trade_id, exit_price, shares, reason, notes)


@mcp.tool()
def log_close(trade_id: str, exit_price: float, grade: str = "", reason: str = "", notes: str = "") -> dict:
    """Close remaining shares of a trade. grade='A+|A|B|C|F'. reason='target|stop|pattern|manual|time|news|trail'."""
    return trade_log.log_close(trade_id, exit_price, grade, reason, notes)


@mcp.tool()
def list_open_trades() -> dict:
    """List all OPEN trades from the trade log CSV."""
    return trade_log.list_open_trades()


@mcp.tool()
def list_all_trades(limit: int = 50) -> dict:
    """List recent trades (open + closed). Last N by default."""
    return trade_log.list_all_trades(limit)


@mcp.tool()
def trade_stats() -> dict:
    """TTS qualification + performance metrics (win rate, P&L, holding period, trade-day ratio)."""
    return trade_log.trade_stats()


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

@mcp.tool()
def position_size_helper(entry: float, stop: float, risk_pct: float | None = None, grade: str | None = None, equity: float | None = None, max_position_pct: float = 25.0, account_number: str | None = None) -> dict:
    """Calculate position size from entry+stop+risk. Provide risk_pct OR grade (A+/A/B → 5/3/1.5%). Equity auto-fetched if omitted."""
    return sizing.position_size_helper(entry, stop, risk_pct, grade, equity, max_position_pct, account_number)


# ---------------------------------------------------------------------------
# Pipeline — composite tools
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_alert(ticker: str, entry: float, stop: float, target: float | None = None, direction: str = "long", grade: str = "A", risk_pct: float | None = None, trail_percent: float = 3.0, strategy: str = "momentum_breakout", thesis: str = "", account_number: str | None = None, deactivate_alert: bool = True, fill_timeout_sec: int = 90) -> dict:
    """FULL PIPELINE: size position → place entry → wait fill → set native trail → stage BE/+1R/+2R/target alerts → log open → deactivate source alert. One call replaces 8 manual steps."""
    return pipeline.execute_alert(ticker, entry, stop, target, direction, grade, risk_pct, trail_percent, strategy, thesis, account_number, deactivate_alert, fill_timeout_sec)


@mcp.tool()
def monitor_position(ticker: str, direction: str = "long", entry_price: float | None = None, interval: str = "5minute", account_number: str | None = None) -> dict:
    """Pattern scanner for an open position. Returns hold/exit decision based on framework patterns (double top/bottom, two counter-candles, strong reversal at best level)."""
    return pipeline.monitor_position(ticker, direction, entry_price, interval, account_number)


@mcp.tool()
def pre_market_scan(account_number: str | None = None) -> dict:
    """One-call morning brief: portfolio + positions + open orders + active alerts + top movers up/down + market hours. Errors in sub-components surface as null fields with _errors list."""
    return pipeline.pre_market_scan(account_number)


@mcp.tool()
def auto_grade_setup(ticker: str, price: float | None = None) -> dict:
    """Composite grader: stack_grade + historicals + order_book + earnings + news → final A+/A/B/SKIP with reasoning. Use to grade a setup with one call."""
    return pipeline.auto_grade_setup(ticker, price)


@mcp.tool()
def eod_brief(account_number: str | None = None) -> dict:
    """End-of-day summary: realized P&L today, open positions, win rate, alerts fired vs unfired. Reads from trade CSV + price_alerts.json + RH portfolio."""
    return pipeline.eod_brief(account_number)


# ---------------------------------------------------------------------------
# Options — chain helpers
# ---------------------------------------------------------------------------

@mcp.tool()
def get_option_chain(ticker: str, expiry: str | None = None, option_type: str | None = None) -> dict:
    """List tradable options for a ticker. Filter by expiry ('YYYY-MM-DD') and/or type ('call'|'put')."""
    return options_trading.get_option_chain(ticker, expiry, option_type)


@mcp.tool()
def get_option_quote(ticker: str, expiry: str, strike: float, option_type: str) -> dict:
    """Market data for a specific option: bid, ask, last, mark, IV, delta/gamma/theta/vega/rho, OI, volume."""
    return options_trading.get_option_quote(ticker, expiry, strike, option_type)


# ---------------------------------------------------------------------------
# Options — single leg
# ---------------------------------------------------------------------------

@mcp.tool()
def buy_option_to_open(ticker: str, expiry: str, strike: float, option_type: str, quantity: int, limit_price: float, time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Buy a single call or put to OPEN a long position. option_type='call'|'put'."""
    return options_trading.buy_option_to_open(ticker, expiry, strike, option_type, quantity, limit_price, time_in_force, account_number)


@mcp.tool()
def sell_option_to_open(ticker: str, expiry: str, strike: float, option_type: str, quantity: int, limit_price: float, time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Sell a single call or put to OPEN (covered call, cash-secured put, naked short)."""
    return options_trading.sell_option_to_open(ticker, expiry, strike, option_type, quantity, limit_price, time_in_force, account_number)


@mcp.tool()
@auth.auth_retry
def close_option_position(ticker: str, expiry: str, strike: float, option_type: str, quantity: int, limit_price: float, side: str = "sell", time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Close an existing option position. side='sell' to close a long, 'buy' to close a short."""
    return options_trading.close_option_position(ticker, expiry, strike, option_type, quantity, limit_price, side, time_in_force, account_number)


# ---------------------------------------------------------------------------
# Options — universal multi-leg
# ---------------------------------------------------------------------------

@mcp.tool()
@auth.auth_retry
def place_option_spread(ticker: str, legs: list, quantity: int, limit_price: float, direction: str = "debit", time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Universal multi-leg options order. Each leg: {expirationDate, strike, optionType, effect, action}. direction='debit'|'credit'."""
    return options_trading.place_option_spread(ticker, legs, quantity, limit_price, direction, time_in_force, account_number)


# ---------------------------------------------------------------------------
# Options — vertical spreads (2 legs, same expiry)
# ---------------------------------------------------------------------------

@mcp.tool()
def bull_call_spread(ticker: str, expiry: str, long_strike: float, short_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """BULLISH DEBIT: buy lower-strike call + sell higher-strike call. long_strike < short_strike."""
    return options_trading.bull_call_spread(ticker, expiry, long_strike, short_strike, quantity, limit_price, account_number)


@mcp.tool()
def bear_put_spread(ticker: str, expiry: str, long_strike: float, short_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """BEARISH DEBIT: buy higher-strike put + sell lower-strike put. long_strike > short_strike."""
    return options_trading.bear_put_spread(ticker, expiry, long_strike, short_strike, quantity, limit_price, account_number)


@mcp.tool()
def bull_put_spread(ticker: str, expiry: str, short_strike: float, long_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """BULLISH CREDIT: sell higher-strike put + buy lower-strike put. short_strike > long_strike."""
    return options_trading.bull_put_spread(ticker, expiry, short_strike, long_strike, quantity, limit_price, account_number)


@mcp.tool()
def bear_call_spread(ticker: str, expiry: str, short_strike: float, long_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """BEARISH CREDIT: sell lower-strike call + buy higher-strike call. long_strike > short_strike."""
    return options_trading.bear_call_spread(ticker, expiry, short_strike, long_strike, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Options — condors & butterflies
# ---------------------------------------------------------------------------

@mcp.tool()
def iron_condor(ticker: str, expiry: str, put_long_strike: float, put_short_strike: float, call_short_strike: float, call_long_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL CREDIT: 4 legs. Profits if underlying stays between short strikes. put_long < put_short < call_short < call_long."""
    return options_trading.iron_condor(ticker, expiry, put_long_strike, put_short_strike, call_short_strike, call_long_strike, quantity, limit_price, account_number)


@mcp.tool()
def long_call_butterfly(ticker: str, expiry: str, low_strike: float, mid_strike: float, high_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL DEBIT (calls): buy 1 low + sell 2 mid + buy 1 high. Equidistant strikes. Profits if underlying lands near mid_strike at expiry."""
    return options_trading.long_call_butterfly(ticker, expiry, low_strike, mid_strike, high_strike, quantity, limit_price, account_number)


@mcp.tool()
def long_put_butterfly(ticker: str, expiry: str, low_strike: float, mid_strike: float, high_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL DEBIT (puts): mirror of call butterfly with puts."""
    return options_trading.long_put_butterfly(ticker, expiry, low_strike, mid_strike, high_strike, quantity, limit_price, account_number)


@mcp.tool()
def iron_butterfly(ticker: str, expiry: str, low_strike: float, mid_strike: float, high_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL CREDIT: short straddle at mid_strike + long wings. Equidistant. Tighter profit zone than condor, higher credit."""
    return options_trading.iron_butterfly(ticker, expiry, low_strike, mid_strike, high_strike, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Options — straddles & strangles (vol plays)
# ---------------------------------------------------------------------------

@mcp.tool()
def long_straddle(ticker: str, expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """LONG VOL DEBIT: buy call + buy put at same strike (typically ATM). Profits on big move either direction."""
    return options_trading.long_straddle(ticker, expiry, strike, quantity, limit_price, account_number)


@mcp.tool()
def long_strangle(ticker: str, expiry: str, put_strike: float, call_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """LONG VOL DEBIT: buy OTM put + buy OTM call (put_strike < call_strike). Cheaper than straddle, wider break-evens."""
    return options_trading.long_strangle(ticker, expiry, put_strike, call_strike, quantity, limit_price, account_number)


@mcp.tool()
def short_straddle(ticker: str, expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """SHORT VOL CREDIT (NAKED, unlimited risk both sides): sell call + sell put at same strike. Requires significant margin. Use with extreme caution."""
    return options_trading.short_straddle(ticker, expiry, strike, quantity, limit_price, account_number)


@mcp.tool()
def short_strangle(ticker: str, expiry: str, put_strike: float, call_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """SHORT VOL CREDIT (NAKED, unlimited risk): sell OTM put + sell OTM call. Wider profit zone than short straddle. Requires significant margin."""
    return options_trading.short_strangle(ticker, expiry, put_strike, call_strike, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Options — calendar & diagonal spreads (different expirations)
# ---------------------------------------------------------------------------

@mcp.tool()
def long_calendar_call(ticker: str, near_expiry: str, far_expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL DEBIT: sell near-month call + buy far-month call, same strike. Profits from time-decay differential."""
    return options_trading.long_calendar_call(ticker, near_expiry, far_expiry, strike, quantity, limit_price, account_number)


@mcp.tool()
def long_calendar_put(ticker: str, near_expiry: str, far_expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """NEUTRAL DEBIT: mirror of long_calendar_call with puts."""
    return options_trading.long_calendar_put(ticker, near_expiry, far_expiry, strike, quantity, limit_price, account_number)


@mcp.tool()
def diagonal_call_spread(ticker: str, near_expiry: str, far_expiry: str, short_strike: float, long_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """DIRECTIONAL + TIME-DECAY: sell near-month short_strike call + buy far-month long_strike call. Mixes calendar + vertical."""
    return options_trading.diagonal_call_spread(ticker, near_expiry, far_expiry, short_strike, long_strike, quantity, limit_price, account_number)


@mcp.tool()
def diagonal_put_spread(ticker: str, near_expiry: str, far_expiry: str, short_strike: float, long_strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """DIRECTIONAL + TIME-DECAY: mirror with puts."""
    return options_trading.diagonal_put_spread(ticker, near_expiry, far_expiry, short_strike, long_strike, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Options — stock + option overlays
# ---------------------------------------------------------------------------

@mcp.tool()
def covered_call(ticker: str, expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """Sell a covered call against existing long stock. Requires 100 shares per contract held. Same as sell_option_to_open with type='call'."""
    return options_trading.covered_call(ticker, expiry, strike, quantity, limit_price, account_number)


@mcp.tool()
def cash_secured_put(ticker: str, expiry: str, strike: float, quantity: int, limit_price: float, account_number: str | None = None) -> dict:
    """Sell a cash-secured put. Requires cash to buy at strike if assigned (strike × 100 × quantity)."""
    return options_trading.cash_secured_put(ticker, expiry, strike, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Social sentiment (StockTwits)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stocktwits_pulse(ticker: str, limit: int = 30) -> dict:
    """Recent StockTwits messages for a ticker with bull/bear sentiment summary. Public endpoint, ~200 req/hr rate limit per IP. Returns net_bullish_pct: +100 = all bull, -100 = all bear, 0 = balanced."""
    return social.get_stocktwits_pulse(ticker, limit)


@mcp.tool()
def scan_stocktwits_trending(limit: int = 15) -> dict:
    """Currently trending tickers on StockTwits (highest activity in last ~5 min). Returns symbols with watchlist counts. NOTE: StockTwits unauth API is now Cloudflare-blocked — expect HTTP 403."""
    return social.scan_stocktwits_trending(limit)


@mcp.tool()
def scan_apewisdom_trending(filter: str = "all-stocks", limit: int = 25) -> dict:
    """Top mentioned tickers on Reddit + light Twitter via Apewisdom. filter options: all-stocks, wallstreetbets, stocks, stockmarket, options, spacs, shortsqueeze, pennystocks, robinhoodpennystocks, twitter. Returns rank, mentions, mention_delta_pct, sentiment_score (-1 to +1), sentiment label."""
    return social.scan_apewisdom_trending(filter, limit)


@mcp.tool()
def get_apewisdom_ticker(ticker: str, filter: str = "all-stocks") -> dict:
    """Apewisdom data for a specific ticker — searches the top 100 trending list. Returns rank, mention delta, sentiment if the ticker is currently trending; otherwise success=False with 'not in trending list'."""
    return social.get_apewisdom_ticker(ticker, filter)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server on stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
