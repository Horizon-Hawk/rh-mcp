# rh-mcp Tools Reference

81 tools across 12 categories. Once the server is connected to Claude Code, each tool is callable as `mcp__rh-mcp__<tool_name>`.

All tools return a dict with at least `{"success": bool}` and either `{"error": str}` on failure or domain-specific keys on success.

---

## Account & Portfolio

### `get_portfolio(account_number?)`
Portfolio summary: equity, cash, buying power, position count, top 10 holdings with P&L.

### `get_positions(account_number?)`
Open stock positions with quantity, avg cost, current price, unrealized $ and %.

### `get_open_orders(account_number?)`
All pending stock orders. Returns symbol, side, quantity, type, state, prices.

### `list_accounts()`
List linked Robinhood accounts. Currently returns single account; multi-account support TBD.

### `get_account_status(account_number?)`
PDT flag, day trade count, buying power, margin balance, restrictions.

### `get_portfolio_history(span?, interval?, bounds?, account_number?)`
Historical portfolio equity curve. For phase-gate equity tracking.
- `span`: `'day' | 'week' | 'month' | '3month' | 'year' | '5year' | 'all'` (default `'year'`)
- `interval`: `'5minute' | '10minute' | 'hour' | 'day' | 'week'` (default `'day'`)
- `bounds`: `'regular' | 'extended'` (default `'regular'`)

---

## Quotes & Market Data

### `get_quote(tickers)`
Current quote(s) for one or many tickers (comma-separated, e.g. `'AAPL,NVDA,TSLA'`). Returns bid/ask/last/AH price.

### `get_latest_price(ticker)`
Fast single-ticker last-price lookup. Lighter than `get_quote`.

### `get_bars(ticker, interval?, span?)`
Historical bars (OHLCV + session info).
- `interval`: `'5minute' | '10minute' | 'hour' | 'day' | 'week'` (default `'5minute'`)
- `span`: `'day' | 'week' | 'month' | '3month' | 'year' | '5year'` (default `'day'`)

### `get_market_hours(date?)`
NYSE market hours for a date (`'YYYY-MM-DD'`). Omit for today. Returns `is_open`, `opens_at`, `closes_at`, AH hours.

---

## Orders — Placement

### `place_long(ticker, quantity, limit_price, account_number?, time_in_force?, extended_hours?)`
Buy limit order. `time_in_force`: `'gfd'` (default, day) or `'gtc'` (good till cancel). `extended_hours`: enable pre/post-market fills.

### `place_short(ticker, quantity, limit_price, account_number?, time_in_force?)`
Sell-short limit order. Call `check_short_availability` first.

### `close_position(ticker, quantity?, limit_price?, account_number?)`
Close a long position. Omit `quantity` to close all; omit `limit_price` for aggressive (bid - $0.05) fill.

---

## Orders — Native Stops (broker-enforced)

### `set_trailing_stop(ticker, quantity, trail_amount, trail_type?, time_in_force?)`
Native Robinhood trailing stop SELL order.
- `trail_type`: `'percentage'` (e.g. `3.0` = 3%) or `'price'` (dollar amount). Default `'percentage'`.
- Survives chain failures — broker enforces server-side.

### `set_stop_loss(ticker, quantity, stop_price, time_in_force?)`
Native fixed stop-loss SELL order at a specific price.

### `place_with_trailing_stop(ticker, quantity, limit_price, trail_percent?, account_number?, extended_hours?, fill_timeout_sec?)`
**ATOMIC**: buy limit + native trailing stop in one call. Waits for fill, then sets the trail. `trail_percent` defaults to `3.0`.

### `place_with_stop(ticker, quantity, limit_price, stop_price, account_number?, extended_hours?, fill_timeout_sec?)`
**ATOMIC**: buy limit + native fixed stop-loss. No take-profit leg — RH does not support OCO brackets; handle take-profit via partial-exit framework.

### `flatten_position(ticker, limit_price?, account_number?)`
**ATOMIC** full exit. Cancels all sell-side orders for ticker (stops, trails, limits), then market sells (or limit sells if `limit_price` provided). Matches the RH app's "Close Position" button.

### `partial_with_trail_rearm(ticker, partial_quantity, exit_limit_price?, trail_percent?, account_number?, fill_timeout_sec?)`
**ATOMIC** +2R partial exit. Cancels existing trail, sells partial, re-sets trailing stop on remainder. Required because RH locks shares to the first sell-side order.

### `cover_short(ticker, quantity, limit_price?, cancel_existing?, account_number?)`
**ATOMIC** short cover. Cancels buy-side orders for ticker, then BUYs to close (market by default).

### `adjust_trailing_stop(ticker, new_trail_percent, account_number?)`
Tighten/loosen trail on existing long. Cancels current trail, re-sets at new percentage.

---

## Orders — Cancellation & Status

### `cancel_order(order_id)`
Cancel a specific open stock order by ID.

### `cancel_all_orders(account_number?)`
Cancel ALL open stock orders. Use with caution.

### `get_order_status(order_id)`
State, fills, prices for a specific order.

---

## Shorting

### `check_short_availability(ticker)`
Tradeable flag, margin ratios, float/SI from fundamentals. Call before `place_short`.

---

## Notifications & Watchlists

### `get_notifications(count?)`
Recent Robinhood notifications. Native price alert fires appear here (read-only — setting native alerts via API is not supported).

### `get_watchlists()`
List Robinhood watchlists.

### `add_to_watchlist(watchlist_name, ticker)` / `remove_from_watchlist(watchlist_name, ticker)`
Watchlist management.

---

## Market Scanners

### `top_movers(direction?, scope?)`
Top market movers, enriched with batch quotes. `direction`: `'up'|'down'`. `scope`: `'sp500'|'all'`.

### `get_news(ticker, count?)`
Recent news headlines for a ticker (default 10).

### `get_earnings(ticker)`
Earnings history + next earnings date.

### `get_fundamentals(ticker)`
Market cap, float, short interest, P/E, 52-week range, sector, industry.

---

## Local Alert Management

Alerts live in `price_alerts.json`, consumed by the user's local monitor process. Configurable via `RH_ALERTS_FILE` env var.

### `list_alerts(active_only?, ticker?)`
List alerts. `active_only=True` (default) hides deactivated.

### `add_alert(ticker, target, direction, grade?, note?, active?)`
Add a price alert. `direction`: `'above'|'below'`. Picked up by monitor on next cycle.

### `deactivate_alert(ticker, target?, grade?)`
Set `active=False` on matching alerts (no delete). Omit `target` to deactivate all for ticker.

### `delete_alert(ticker, target, direction)`
Hard delete a specific alert.

### `auto_stage_trade_card(ticker, entry, stop, target?, shares?, direction?, grade?, thesis?)`
**ATOMIC** stages the full trade-card alerts for a new entry: STOP, +1R signal, +2R signal, TARGET. Closes the gap where R-multiples lived only in memory and never fired.

---

## Setup Analysis (wraps user scripts)

Each tool wraps a Python script in the user's scripts directory (configurable via `RH_SCRIPTS_DIR`). Tools return `{"success": True, "data": {...}}` on success.

### `stack_grade(tickers)`
Combined float + relative volume + news catalyst grade. Returns `'A+'`, `'A'`, `'B'`, or `'SKIP STACK'` per ticker.

### `order_book_scan(ticker, price, range_dollars?, threshold?)`
Order book depth + OBI signal at a target price. Spread, walls (bid/ask), top-10/20 OBI ratio, BUY/SELL/BALANCED signal.

### `historicals(ticker)`
Daily-bar breakout grade: volume ratio, body %, 20d SMA, ATR, measured-move target, suggested stop, R:R.

### `float_check(ticker)`
Float + short-interest squeeze grade: `'SQUEEZE'`, `'ELEVATED'`, `'NORMAL'`, `'LOW_RISK_SHORT'`.

### `iv_rank(ticker)`
Implied volatility rank — gauges options pricing.

### `options_scan(ticker)`
Options chain scan: ATM IV, max pain, OI walls, spread setup ideas (bull call / bear put).

### `spike_check(ticker, spike_price, ts)`
Validate a volume spike entry: staleness, drift, fade, current price. `ts` is ISO `'YYYY-MM-DDTHH:MM:SS'`.

---

## Trade Logging

CSV-backed trade log at `RH_TRADE_CSV` (default `robinhood_trades.csv`). Supports partial closes — see `log_partial`.

### `log_open(account, ticker, direction, strategy, entry, shares, stop?, target?, thesis?)`
Record a new trade. `strategy` is one of `'momentum_breakout'`, `'debit_spread'`, `'earnings_drift'`, `'ah_earnings'`, `'dead_cat_short'`, `'intraday'`, `'spike_entry'`.

### `log_partial(trade_id, exit_price, shares, reason?, notes?)`
Record a partial exit. Trade stays OPEN unless this closes remaining shares.

### `log_close(trade_id, exit_price, grade?, reason?, notes?)`
Close remaining shares. `grade`: `'A+'|'A'|'B'|'C'|'F'`. `reason`: `'target'|'stop'|'pattern'|'manual'|'time'|'news'|'trail'`.

### `list_open_trades()`
List all OPEN trades.

### `list_all_trades(limit?)`
List last N trades (open + closed).

### `trade_stats()`
TTS qualification metrics: trade count, win rate, avg holding period, gross turnover, trade-day ratio.

---

## Position Sizing

### `position_size_helper(entry, stop, risk_pct?, grade?, equity?, max_position_pct?, account_number?)`
Calculate shares from risk parameters. Provide `risk_pct` (explicit %) OR `grade` (`'A+'|'A'|'B'` → tiered 5/3/1.5%). `equity` auto-fetched if omitted. Respects 25% max position cap.

---

## Pipeline — Composite Tools

### `execute_alert(ticker, entry, stop, target?, direction?, grade?, risk_pct?, trail_percent?, strategy?, thesis?, account_number?, deactivate_alert?, fill_timeout_sec?)`
**FULL PIPELINE**: size position → place entry → wait for fill → set native trailing stop → stage BE/+1R/+2R/TARGET alerts → log open → deactivate source alert. One call replaces 8 manual steps. Returns structured response with `stage` field naming where any failure occurred.

### `monitor_position(ticker, direction?, entry_price?, interval?, account_number?)`
Pattern scanner for an open position. Detects: double top (long) / double bottom (short), two consecutive 60%+ counter-candles, strong reversal candle (70%+ body) at the trade's best level. Returns `recommended_action` of `'hold'` or `'close'`. Per framework, only acts when in profit.

### `pre_market_scan(account_number?)`
One-call morning brief. Bundles portfolio + positions + open orders + active alerts + top movers (up/down) + market hours into a structured report. Per-component failures surface as null fields with `_errors` list — never crashes the whole brief.

### `auto_grade_setup(ticker, price?)`
Composite grader: `stack_grade` + `historicals` + `order_book_scan` + `get_earnings` + `get_news` → final `A+ / A / B / SKIP` grade with reasoning. Use to grade a setup with one call instead of 5.

### `eod_brief(account_number?)`
End-of-day summary. Reads the trade CSV for today's closes, computes realized P&L and win rate, lists open positions, and surfaces which alerts fired today vs unfired.

---

## Options Trading

24 tools spanning chain lookups, single-leg orders, vertical spreads, condors, butterflies, straddles/strangles, calendars/diagonals, and stock+option overlays.

### Chain helpers

#### `get_option_chain(ticker, expiry?, option_type?)`
List tradable option contracts for a ticker. Filter by expiry (`'YYYY-MM-DD'`) and/or type (`'call'|'put'`).

#### `get_option_quote(ticker, expiry, strike, option_type)`
Market data for a specific contract: bid, ask, last, mark, IV, delta/gamma/theta/vega/rho, open interest, volume.

### Single-leg

#### `buy_option_to_open(ticker, expiry, strike, option_type, quantity, limit_price, time_in_force?, account_number?)`
Buy a single call or put to open a long position.

#### `sell_option_to_open(ticker, expiry, strike, option_type, quantity, limit_price, time_in_force?, account_number?)`
Sell a single call or put to open. Used by `covered_call` and `cash_secured_put`.

#### `close_option_position(ticker, expiry, strike, option_type, quantity, limit_price, side?, time_in_force?, account_number?)`
Close an existing option position. `side='sell'` to close a long, `'buy'` to close a short.

### Universal multi-leg

#### `place_option_spread(ticker, legs, quantity, limit_price, direction?, time_in_force?, account_number?)`
Universal multi-leg orders. `legs` is a list of dicts: `{expirationDate, strike, optionType, effect, action}`. `direction='debit'` (you pay) or `'credit'` (you receive). Use named strategies below for the common patterns.

### Vertical spreads (2 legs, same expiry)

#### `bull_call_spread(ticker, expiry, long_strike, short_strike, quantity, limit_price, account_number?)`
Bullish DEBIT. Buy lower-strike call + sell higher-strike call. `long_strike < short_strike`.

#### `bear_put_spread(ticker, expiry, long_strike, short_strike, quantity, limit_price, account_number?)`
Bearish DEBIT. Buy higher-strike put + sell lower-strike put. `long_strike > short_strike`.

#### `bull_put_spread(ticker, expiry, short_strike, long_strike, quantity, limit_price, account_number?)`
Bullish CREDIT. Sell higher-strike put + buy lower-strike put. `short_strike > long_strike`.

#### `bear_call_spread(ticker, expiry, short_strike, long_strike, quantity, limit_price, account_number?)`
Bearish CREDIT. Sell lower-strike call + buy higher-strike call. `long_strike > short_strike`.

### Condors & butterflies

#### `iron_condor(ticker, expiry, put_long_strike, put_short_strike, call_short_strike, call_long_strike, quantity, limit_price, account_number?)`
Neutral CREDIT, 4 legs. Profits if underlying stays between short strikes. Defined risk.

#### `long_call_butterfly(ticker, expiry, low_strike, mid_strike, high_strike, quantity, limit_price, account_number?)`
Neutral DEBIT (calls). Buy 1 low + sell 2 mid + buy 1 high. Equidistant strikes. Profits if underlying lands near mid_strike at expiry.

#### `long_put_butterfly(...)`
Same as `long_call_butterfly` but with puts.

#### `iron_butterfly(ticker, expiry, low_strike, mid_strike, high_strike, quantity, limit_price, account_number?)`
Neutral CREDIT. Short straddle at mid_strike + long wings. Tighter profit zone than condor, higher credit.

### Straddles & strangles (volatility plays)

#### `long_straddle(ticker, expiry, strike, quantity, limit_price, account_number?)`
Long-volatility DEBIT. Buy call + buy put at same strike (typically ATM).

#### `long_strangle(ticker, expiry, put_strike, call_strike, quantity, limit_price, account_number?)`
Long-volatility DEBIT. Buy OTM put + buy OTM call. Cheaper than straddle, wider break-evens.

#### `short_straddle(ticker, expiry, strike, quantity, limit_price, account_number?)`
**NAKED CREDIT, unlimited risk both sides**. Sell call + sell put at same strike. Significant margin required.

#### `short_strangle(ticker, expiry, put_strike, call_strike, quantity, limit_price, account_number?)`
**NAKED CREDIT, unlimited risk**. Sell OTM put + sell OTM call. Wider profit zone than short straddle.

### Calendars & diagonals (different expirations)

#### `long_calendar_call(ticker, near_expiry, far_expiry, strike, quantity, limit_price, account_number?)`
Neutral DEBIT. Sell near-month call + buy far-month call at same strike. Profits from time-decay differential.

#### `long_calendar_put(...)`
Same with puts.

#### `diagonal_call_spread(ticker, near_expiry, far_expiry, short_strike, long_strike, quantity, limit_price, account_number?)`
Directional + time-decay combo. Sell near-month short_strike call + buy far-month long_strike call.

#### `diagonal_put_spread(...)`
Same with puts.

### Stock + option overlays

#### `covered_call(ticker, expiry, strike, quantity, limit_price, account_number?)`
Sell a covered call against existing long stock. Requires 100 shares per contract held.

#### `cash_secured_put(ticker, expiry, strike, quantity, limit_price, account_number?)`
Sell a cash-secured put. Requires cash to buy at strike if assigned (`strike × 100 × quantity`).

---

## Environment Variables

- `RH_CONFIG_PATH` — path to `rh_config.json` with credentials. Default: cwd or package dir.
- `RH_ALERTS_FILE` — path to `price_alerts.json`. Default: `C:\Users\algee\price_alerts.json`.
- `RH_SCRIPTS_DIR` — directory containing the analysis scripts (`stack_grade.py`, etc.). Default: `C:\Users\algee\`.
- `RH_TRADE_CSV` — path to trade log CSV. Default: `C:\Users\algee\robinhood_trades.csv`.

For your own install, point these at your own files. Tools that wrap optional scripts return a graceful `{"success": False, "error": "script not found at ..."}` if the script is missing, rather than crashing.
