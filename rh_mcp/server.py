"""rh-mcp server — registers all tools and runs stdio transport.

Run directly:
    python -m rh_mcp.server

Or via the installed entrypoint:
    rh-mcp
"""

from mcp.server.fastmcp import FastMCP

from rh_mcp.tools import (
    quotes, account, orders, shorting, notifications, scanners,
    alerts, analysis, trade_log, sizing, pipeline,
    options_trading,
)


mcp = FastMCP("rh-mcp")


# ---------------------------------------------------------------------------
# Account & portfolio
# ---------------------------------------------------------------------------

@mcp.tool()
def get_portfolio(account_number: str | None = None) -> dict:
    """Portfolio summary: equity, cash, buying power, position count, top holdings."""
    return account.get_portfolio(account_number)


@mcp.tool()
def get_positions(account_number: str | None = None) -> dict:
    """Open stock positions with quantity, avg cost, current price, P&L."""
    return account.get_positions(account_number)


@mcp.tool()
def get_open_orders(account_number: str | None = None) -> dict:
    """All pending stock orders."""
    return account.get_open_orders(account_number)


@mcp.tool()
def list_accounts() -> dict:
    """List linked Robinhood accounts."""
    return account.list_accounts()


@mcp.tool()
def get_account_status(account_number: str | None = None) -> dict:
    """Account status: PDT flag, day trades, buying power, margin info."""
    return account.get_account_status(account_number)


@mcp.tool()
def get_portfolio_history(span: str = "year", interval: str = "day", bounds: str = "regular", account_number: str | None = None) -> dict:
    """Historical portfolio equity curve. span='day|week|month|3month|year|5year|all', interval='5minute|10minute|hour|day|week'."""
    return account.get_portfolio_history(span, interval, bounds, account_number)


# ---------------------------------------------------------------------------
# Quotes & market data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_quote(tickers: str) -> dict:
    """Current quote(s) for one or batched tickers (comma-separated). Includes AH price."""
    return quotes.get_quote(tickers)


@mcp.tool()
def get_latest_price(ticker: str) -> dict:
    """Fast single-ticker last-price lookup. Lighter than get_quote."""
    return quotes.get_latest_price(ticker)


@mcp.tool()
def get_bars(ticker: str, interval: str = "5minute", span: str = "day") -> dict:
    """Historical bars. interval: '5minute|10minute|hour|day|week'. span: 'day|week|month|3month|year|5year'."""
    return quotes.get_bars(ticker, interval, span)


@mcp.tool()
def get_market_hours(date: str | None = None) -> dict:
    """NYSE market hours for a date (YYYY-MM-DD). Omit for today."""
    return quotes.get_market_hours(date)


# ---------------------------------------------------------------------------
# Orders — placement
# ---------------------------------------------------------------------------

@mcp.tool()
def place_long(ticker: str, quantity: float, limit_price: float, account_number: str | None = None, time_in_force: str = "gfd", extended_hours: bool = False) -> dict:
    """Buy limit order. time_in_force: 'gfd' (day) or 'gtc'. extended_hours for AH."""
    return orders.place_long(ticker, quantity, limit_price, account_number, time_in_force, extended_hours)


@mcp.tool()
def place_short(ticker: str, quantity: float, limit_price: float, account_number: str | None = None, time_in_force: str = "gfd") -> dict:
    """Sell-short limit order. Check shortable + borrow fee first via check_short_availability."""
    return orders.place_short(ticker, quantity, limit_price, account_number, time_in_force)


@mcp.tool()
def close_position(ticker: str, quantity: float | None = None, limit_price: float | None = None, account_number: str | None = None) -> dict:
    """Close a long position. Omit quantity to close all. Omit limit_price for aggressive bid - $0.05."""
    return orders.close_position(ticker, quantity, limit_price, account_number)


# ---------------------------------------------------------------------------
# Orders — native stops (broker-enforced)
# ---------------------------------------------------------------------------

@mcp.tool()
def set_trailing_stop(ticker: str, quantity: float, trail_amount: float, trail_type: str = "percentage", time_in_force: str = "gtc") -> dict:
    """Native Robinhood trailing stop SELL order. trail_type='percentage' (3.0=3%) or 'price' ($). Survives chain failures."""
    return orders.set_trailing_stop(ticker, quantity, trail_amount, trail_type, time_in_force)


@mcp.tool()
def set_stop_loss(ticker: str, quantity: float, stop_price: float, time_in_force: str = "gtc") -> dict:
    """Native fixed stop-loss SELL order at a specific price."""
    return orders.set_stop_loss(ticker, quantity, stop_price, time_in_force)


@mcp.tool()
def place_with_trailing_stop(ticker: str, quantity: float, limit_price: float, trail_percent: float = 3.0, account_number: str | None = None, extended_hours: bool = False, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC: buy limit + native trailing stop in one call. Eliminates the fill-to-stop gap."""
    return orders.place_with_trailing_stop(ticker, quantity, limit_price, trail_percent, account_number, extended_hours, fill_timeout_sec)


@mcp.tool()
def place_with_stop(ticker: str, quantity: float, limit_price: float, stop_price: float, account_number: str | None = None, extended_hours: bool = False, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC: buy limit + native fixed stop-loss in one call. For trailing stops use place_with_trailing_stop. (RH does not support true OCO; take-profit is handled via partial-exit framework, not a paired order.)"""
    return orders.place_with_stop(ticker, quantity, limit_price, stop_price, account_number, extended_hours, fill_timeout_sec)


@mcp.tool()
def flatten_position(ticker: str, limit_price: float | None = None, account_number: str | None = None) -> dict:
    """ATOMIC full exit: cancel all open sell-side orders for ticker (stops, trails, limits), then sell remaining shares. Use when you want to fully close a stop-protected position."""
    return orders.flatten_position(ticker, limit_price, account_number)


@mcp.tool()
def partial_with_trail_rearm(ticker: str, partial_quantity: float, exit_limit_price: float | None = None, trail_percent: float = 3.0, account_number: str | None = None, fill_timeout_sec: int = 60) -> dict:
    """ATOMIC +2R partial exit: cancel existing trail/stop, sell partial, re-set trailing stop on remainder. Required because RH locks shares to the first sell-side order."""
    return orders.partial_with_trail_rearm(ticker, partial_quantity, exit_limit_price, trail_percent, account_number, fill_timeout_sec)


@mcp.tool()
def cover_short(ticker: str, quantity: float, limit_price: float | None = None, cancel_existing: bool = True, account_number: str | None = None) -> dict:
    """ATOMIC short cover: cancel buy-side orders for ticker, then BUY to close. Defaults to market BUY (matches RH app)."""
    return orders.cover_short(ticker, quantity, limit_price, cancel_existing, account_number)


@mcp.tool()
def adjust_trailing_stop(ticker: str, new_trail_percent: float, account_number: str | None = None) -> dict:
    """Tighten/loosen the trailing stop on an existing long position. Cancels current trail, re-sets at new percentage."""
    return orders.adjust_trailing_stop(ticker, new_trail_percent, account_number)


# ---------------------------------------------------------------------------
# Orders — cancellation & status
# ---------------------------------------------------------------------------

@mcp.tool()
def cancel_order(order_id: str) -> dict:
    """Cancel a specific open stock order."""
    return orders.cancel_order(order_id)


@mcp.tool()
def cancel_all_orders(account_number: str | None = None) -> dict:
    """Cancel ALL open stock orders. Use with caution."""
    return orders.cancel_all_orders(account_number)


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Status of a specific order: state, fills, prices."""
    return orders.get_order_status(order_id)


# ---------------------------------------------------------------------------
# Shorting
# ---------------------------------------------------------------------------

@mcp.tool()
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
def get_watchlists() -> dict:
    """List Robinhood watchlists."""
    return notifications.get_watchlists()


@mcp.tool()
def add_to_watchlist(watchlist_name: str, ticker: str) -> dict:
    """Add a ticker to a named watchlist."""
    return notifications.add_to_watchlist(watchlist_name, ticker)


@mcp.tool()
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
def get_news(ticker: str, count: int = 10) -> dict:
    """Recent news headlines for a ticker."""
    return scanners.get_news(ticker, count)


@mcp.tool()
def get_earnings(ticker: str) -> dict:
    """Earnings history + next earnings date."""
    return scanners.get_earnings(ticker)


@mcp.tool()
def get_fundamentals(ticker: str) -> dict:
    """Fundamentals: market cap, float, short interest, P/E, 52w range, sector."""
    return scanners.get_fundamentals(ticker)


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
def close_option_position(ticker: str, expiry: str, strike: float, option_type: str, quantity: int, limit_price: float, side: str = "sell", time_in_force: str = "gtc", account_number: str | None = None) -> dict:
    """Close an existing option position. side='sell' to close a long, 'buy' to close a short."""
    return options_trading.close_option_position(ticker, expiry, strike, option_type, quantity, limit_price, side, time_in_force, account_number)


# ---------------------------------------------------------------------------
# Options — universal multi-leg
# ---------------------------------------------------------------------------

@mcp.tool()
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
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server on stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
