# rh-mcp — Robinhood MCP server for Claude

A local-first Model Context Protocol server that lets Claude (Code, Desktop, or claude.ai via HTTP transport) read your Robinhood account, run validated edge scanners against live data, and execute trades — with multi-account support including IRAs.

138 tools across account, orders, options, scanners, analysis, alerts, and social-sentiment domains.

## Why this exists

Existing third-party Robinhood MCPs share three problems:

1. **Auth tokens expire mid-session,** breaking active trades.
2. **No atomic entry-with-stop operations** — you place the entry, wait for fill, then manually set the stop. The gap is unprotected exposure.
3. **No integration with the analysis tools** that drive setup grading (news, earnings proximity, order book, IV rank).

rh-mcp fixes all three, adds backtest-validated scanner edges directly callable as tools, and supports both individual brokerage and IRA accounts in one server.

## Validated edges (4-year stress-tested backtests)

The four core scanners ship with their backtest provenance documented in code:

```
Scanner                       4yr Return     Max DD    Win Rate    Hold
─────────────────────────────────────────────────────────────────────────
scan_bullish_8k (stack)       +372.92%       20.8%     varies      5d
scan_buyback_announcements    +84.91%        8.4%      —           5d
scan_pead (mega-cap)          —              —         56.3%       5-30d
scan_pead_negative            +241.30%       18.3%     50.4%       4d
```

Each scanner has documented sector exclusions tuned via additional backtests (e.g., dropping Industrials lifts buyback returns from +43% to +85%). The validation methodology and parameter tuning are visible in the scanner docstrings and analysis modules.

## What it does

**Account & market data**
- Multi-account: brokerage + IRA in one server, account-scoped position/portfolio/orders queries
- Settled cash vs unsettled funds breakdown (critical for cash-account settlement rules)
- Quotes (batch), intraday bars, market hours, fundamentals
- Effective basis: stock cost basis adjusted for currently open short option premium

**Order placement**
- Limit long/short with automatic shortable + borrow check
- `place_with_stop` — atomic: limit entry + native fixed stop-loss in one call
- `place_with_trailing_stop` — atomic: limit entry + native trailing stop
- `flatten_position` — atomic: cancel open sell-side orders + market sell
- `close_all` — full position close including fractional residual (splits into whole-share limit + fractional market)
- `partial_with_trail_rearm` — atomic +2R partial: cancel existing trail, sell partial, re-arm trail on remainder
- `stage_stop_safe` — places stop with graceful settlement-lockup detection (returns `deferred=True` instead of silent failure when broker blocks for T+1 settlement)
- Native trailing stops on existing positions

Robinhood's API does **not** support true OCO bracket orders for stocks. Take-profit is handled via the partial-exit framework, not paired with the stop.

**Options trading**
- All common spreads: bull/bear call/put, iron condor, iron butterfly, calendars, diagonals, straddles, strangles, covered call, cash-secured put
- Multi-leg via `place_option_spread` (any leg combination, any account — brokerage only; IRAs reject multi-leg per Robinhood policy)
- Single-leg open/close for IRAs

**Setup grading (in-process, no external scripts)**
- Stack grade (float + RV + news)
- Order book scan with OBI signal — note: RH's L2 is internalized retail flow, not consolidated NBBO; use for spread/liquidity, not directional signal on liquid ETFs
- Historicals breakout grade + measured move + stop suggestion
- Float / short-interest squeeze grade
- IV rank, options chain scan
- Recent news with catalyst classification (UPGRADE / DOWNGRADE / BEAT / MISS / WARNING)

**Edge scanners (validated)**
- `scan_bullish_8k` — 8-K filings tagged bullish, small + mid-cap stack
- `scan_buyback_announcements` — new authorizations, % of market cap ranked
- `scan_pead` — post-earnings drift, $50B+ mega-caps with gap-up confirmation
- `scan_pead_negative` — post-earnings flush bounce, top EV strategy
- `scan_stack` — runs the 4 above sequentially as a background job (with per-scanner hang detection), returns job_id immediately, poll `scan_stack_status(job_id)`

**Additional scanners**
- `scan_52w_breakouts`, `scan_squeeze_breakouts`, `scan_sympathy_laggards`, `scan_failed_breakouts`
- `scan_premium_sellers`, `scan_cheap_premium_buyers`, `scan_iv_crush_drift`
- `scan_unusual_oi` + `snapshot_oi` / `find_oi_spikes`
- `scan_futures_rsi2`, `scan_rsi2_extremes`, `scan_gap_and_go`, `scan_float_rotation`, `scan_capitulation_reversal`
- `scan_frd` — failed reversal day shorts

**Social sentiment**
- `scan_apewisdom_trending` — top mentioned tickers across WSB / stocks / options / penny / Twitter
- `get_apewisdom_ticker` — search top 100 trending for a specific ticker (useful for validating alerts: organic notice vs late-stage FOMO)
- StockTwits stubs (currently Cloudflare-blocked on unauth API; dev-key support pending)

**Trade lifecycle**
- Structured trade log with partial-close support
- Alert management (read/add/deactivate price_alerts.json)
- Auto-stage trade-card alerts (BE / 1R / 2R / target / stop) at entry
- `get_portfolio_earnings` — earnings dates for all positions in one call, flags positions with earnings inside warn window

**Futures**
- Aggregated positions, quotes, history, orders for the futures product

## Install

```bash
git clone https://github.com/Horizon-Hawk/rh-mcp.git
cd rh-mcp
pip install -e ".[analysis]"          # base + scanners (yfinance + scipy)
# optional: pip install -e ".[analysis,monitor]"  # adds GUI alert injector
cp rh_config.example.json rh_config.json
# edit rh_config.json with your RH credentials
```

## Connect to Claude Code

Add to your Claude Code MCP settings (see `claude_mcp_config.example.json`):

```json
{
  "mcpServers": {
    "rh-mcp": {
      "command": "python",
      "args": ["-m", "rh_mcp.server"],
      "cwd": "/absolute/path/to/rh-mcp"
    }
  }
}
```

## rh-monitor (optional alert injector)

`rh-monitor` is a separate companion process that polls Robinhood prices, news, volume spikes, and scheduled messages. When something fires it:

1. Shows a Windows toast notification
2. Sends a phone push via ntfy.sh (optional — set `NTFY_TOPIC` to enable)
3. Auto-types the alert into the Claude Code window + Enter (Windows GUI)

Windows-only (uses pyautogui/keyboard for window detection). Adapt for your setup.

```bash
pip install -e ".[monitor]"
echo '[]' > price_alerts.json
rh-monitor
```

Configuration via env vars: `RH_MONITOR_DATA_DIR`, `RH_CONFIG_PATH`, `NTFY_TOPIC`, polling intervals (see `rh_monitor/cli.py`).

## Security & opsec

- `rh_config.json` is gitignored — never commit credentials
- All trading actions require explicit tool calls from Claude
- No outbound network calls except Robinhood, Apewisdom, news sources
- Session lives only as long as the MCP process
- Multi-account access requires the account number explicitly — no implicit cross-account leakage

## Known limitations

- **IRAs reject multi-leg option orders** at Robinhood — single-leg only on retirement accounts (`stage_stop_safe` exists partly to handle this and the settlement-lockup case gracefully)
- **Cash account settlement rule:** buying with unsettled funds locks the new shares from selling until T+1 — error message is misleading ("PDT designation"), the real cause is the good-faith violation rule
- **Stock scanners run sequentially, not in parallel** — parallel scanner calls hang due to shared API/login resources. `scan_stack` enforces serial execution with per-scanner timeouts
- **Robinhood L2 is internalized retail flow,** not consolidated NBBO. Use order book for spread/wall structure on illiquid names; do not infer market-wide directional pressure on liquid ETFs

## Status

Production — running real trades across two accounts with documented P&L. Scanner edges validated on 4-year backtests. ~138 tools registered.

## License

MIT — see [LICENSE](LICENSE).
