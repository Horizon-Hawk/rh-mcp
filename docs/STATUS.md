# Tool roadmap and status

**81 tools shipped as of 2026-05-11 night.** Phase 1, Phase 2 (analysis wrappers + pipeline tools), Path 1 distribution polish, full options trading (24 strategies), AND composite workflow tools (`monitor_position`, `pre_market_scan`, `auto_grade_setup`, `eod_brief`) — all in a single session.

## Account & Portfolio
| Tool | Status | Notes |
|------|--------|-------|
| `get_portfolio` | ✅ | equity, cash, BP, top holdings |
| `get_positions` | ✅ | qty, avg cost, current price, P&L per position |
| `get_open_orders` | ✅ | pending stock orders, symbol-resolved |
| `list_accounts` | ✅ | single-account currently (RH multi-account TBD) |
| `get_account_status` | ✅ | PDT flag, day trades, margin balance |

## Quotes & Market Data
| Tool | Status | Notes |
|------|--------|-------|
| `get_quote` | ✅ | batch, includes AH price |
| `get_latest_price` | ✅ | single-ticker fast path |
| `get_bars` | ✅ | 5min/10min/hour/day/week intervals |
| `get_market_hours` | ✅ | NYSE hours by date |

## Orders — Placement
| Tool | Status | Notes |
|------|--------|-------|
| `place_long` | ✅ | buy limit |
| `place_short` | ✅ | sell limit (verify shortable first) |
| `close_position` | ✅ | auto-prices bid-0.05 if no limit given |

## Orders — Native Stops (the broker-enforced kind)
| Tool | Status | Notes |
|------|--------|-------|
| **`set_trailing_stop`** | ✅ | **percentage or dollar trail; survives chain failures** |
| `set_stop_loss` | ✅ | fixed stop-loss order |
| **`place_with_trailing_stop`** | ✅ | **ATOMIC: limit entry + trailing stop in one call** |

## Orders — Cancellation & Status
| Tool | Status | Notes |
|------|--------|-------|
| `cancel_order` | ✅ | by order ID |
| `cancel_all_orders` | ✅ | bulk |
| `get_order_status` | ✅ | state, fills, prices |

## Shorting
| Tool | Status | Notes |
|------|--------|-------|
| `check_short_availability` | ✅ | tradeable, margin ratios, SI/float |

## Notifications & Watchlists
| Tool | Status | Notes |
|------|--------|-------|
| `get_notifications` | ✅ | reads native RH push notifications (price alert fires appear here) |
| `get_watchlists` | ✅ | list all |
| `add_to_watchlist` | ✅ | by name |
| `remove_from_watchlist` | ✅ | by name |

## Market Scanners
| Tool | Status | Notes |
|------|--------|-------|
| `top_movers` | ✅ | enriched with batch quotes; SP500 or all |
| `get_news` | ✅ | Robinhood news per ticker |
| `get_earnings` | ✅ | history + upcoming date |
| `get_fundamentals` | ✅ | market cap, float, SI, P/E, 52w |

## Note: Native price alerts (the kind you set in the app)

Robinhood does NOT expose a public API endpoint for SETTING price alerts. `get_notifications` reads alerts that already fired, but there's no documented way to programmatically create them.

**Alternatives:**
1. Use `add_to_watchlist` + the existing `price_alert_monitor.py` for monitoring (already working)
2. Integrate ntfy.sh, Pushover, or Twilio for push notifications when our JSON alerts fire
3. Reverse-engineer the internal RH alerts API (fragile, not in scope)

## Coming next (Phase 2)

| Tool | Notes |
|------|-------|
| ~~`place_with_bracket`~~ | NOT POSSIBLE — RH does not support OCO brackets. Shipped `place_with_stop` (entry + fixed stop) and `place_with_trailing_stop` (entry + trail) instead; take-profit handled via partial-exit framework. |
| `order_book_scan` | Wrap order_book.py with OBI signal |
| `stack_grade` | Wrap stack_grade.py (float + RV + news combined) |
| `historicals` | Wrap historicals.py (breakout grade, MM target, stop suggestion) |
| `float_check` | Wrap float_check.py (squeeze grading) |
| `options_scan` / `iv_rank` | Wrap options analysis scripts |
| `spike_check` | Volume spike entry evaluation |
| `log_open`, `log_partial`, `log_close` | Trade logger integration |
| `list_alerts`, `add_alert`, `deactivate_alert` | Our price_alerts.json management |
| `auto_stage_trade_card` | Write BE/1R/2R/target/stop alerts atomically at entry |

## Phase 3 (later)

- HTTP transport (FastMCP supports it) for claude.ai connector
- Hosting (Cloudflare Workers / Fly.io) with OAuth
- `execute_alert` and `manage_2R_partial` convenience tools (full pipelines)
