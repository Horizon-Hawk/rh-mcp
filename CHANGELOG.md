# Changelog

## [Unreleased] — 2026-05-11

Major buildout transforming rh-mcp from "MCP wrapper around external scripts"
to a fully self-contained trading toolkit. No external script files needed —
`pip install rh-mcp[analysis]` gives you everything.

### Added

**Setup scanner suite (9 scanners total):**
- `scan_52w_breakouts` — at-or-near 52-week high with confirming volume
- `scan_squeeze_breakouts` — Bollinger bandwidth in bottom percentile + price near upper band
- `scan_sympathy_laggards` — industries with strong leader + lagging peers
- `scan_failed_breakouts` — broke prior-day high today, faded back inside (short setup)
- `scan_all` — composite that fans out the directional scanners in parallel and ranks by signal overlap
- `scan_premium_sellers` — iron condor candidates (pre-earnings high IV + liquidity check)
- `scan_cheap_premium_buyers` — debit spread candidates (low IV + directional setup)
- `scan_iv_crush_drift` — post-earnings cheap options with intact drift thesis
- `scan_unusual_oi` — turnover + concentration anomalies on a focused ticker list
- `snapshot_oi` / `find_oi_spikes` — daily OI history + delta detection for true unusual-activity v2

**In-process analysis package (`rh_mcp.analysis.*`):**
All formerly-external scripts now ship inside the package as importable Python
modules — no more subprocess hop, no more "script not found" errors for GitHub
users. Ports: float_check, news, earnings, historicals, order_book, iv_rank,
options_scan, spike_check, stack_grade, trade_log, trade_stats.

**Synthetic-today daily bar:**
`historicals.analyze()` now appends today's intraday-aggregated bar when RH
hasn't yet published the daily — fixes mid-session and just-after-close
grading on stale data.

**Optional alert-monitor companion (`rh_monitor`):**
Separate package (`pip install rh-mcp[monitor]`) that watches price/news/volume/schedule
events and injects them into a Claude Code window via GUI automation + sends phone
push via ntfy.sh. Windows-only in practice. `rh-monitor` CLI entry point.

**Trade post-mortem template** at `docs/TRADE_POSTMORTEM_TEMPLATE.md`.

### Changed

- `auth.py` redirects robin_stocks output to stderr at import time — protects
  the MCP stdio JSON-RPC channel from being corrupted by the library's
  "Loading Market Data" spinner. Previously caused mid-call disconnects on
  options-heavy tools.
- `add_alert` now fetches current price and returns a `warning` field when the
  target is on the trigger side of current price (would fire immediately on
  next monitor poll).
- `trade_log.log_open` now accepts `'iron_condor'` as a strategy value.
- `scan_premium_sellers` includes an ATM liquidity check (min OI, max bid/ask
  spread) — drops untradeable names like BZ even when IV rank looks great.

### Fixed

- Subprocess stdin deadlock on Windows: every spawned-script tool now passes
  `stdin=subprocess.DEVNULL` so the child doesn't block on the MCP server's
  inherited stdin pipe. (Resolved before subprocess code was deleted entirely
  in the in-process port; preserved in the historicals fallback path.)
- `auth.py` `load_config()` raises RuntimeError instead of SystemExit on
  missing config — SystemExit inside a tool call killed the server. The MCP
  framework now reports a normal error response.
- `auth.py` env-var fallback: if `RH_CONFIG_PATH` is set but the file doesn't
  exist (e.g. backslashes stripped by a JSON writer), fall through to the
  candidate paths instead of locking in a bad path.
- `get_open_orders` surfaces `trailing_peg` and `last_trail_price` — previously
  trailing stops were indistinguishable from fixed stops in the output.
- `get_portfolio_history` stopped passing `account_number=` to a function that
  rejected the kwarg.

### Project structure

```
rh_mcp/
├── analysis/              # in-process setup graders (was: external scripts)
│   ├── float_check.py
│   ├── news.py
│   ├── earnings.py
│   ├── historicals.py
│   ├── order_book.py
│   ├── iv_rank.py
│   ├── options_scan.py
│   ├── spike_check.py
│   ├── stack_grade.py
│   ├── trade_log.py
│   ├── trade_stats.py
│   ├── high_breakout.py       # 52w scanner
│   ├── squeeze_breakout.py    # Bollinger squeeze scanner
│   ├── sympathy_laggards.py   # sector laggard scanner
│   ├── failed_breakout.py     # failed-breakout short scanner
│   ├── scan_all.py            # composite directional scanner
│   ├── premium_sellers.py     # iron condor scanner
│   ├── cheap_premium_buyers.py  # debit spread scanner
│   ├── iv_crush_drift.py      # post-earnings cheap options
│   ├── unusual_oi.py          # OI anomaly (focused list)
│   └── oi_history.py          # OI snapshot + delta v2
├── tools/                 # MCP tool wrappers
├── auth.py                # shared robin_stocks login
└── server.py              # MCP entry point

rh_monitor/                # optional GUI alert injector (pip install [monitor])
└── cli.py
```

## [0.1.0] — Earlier

Initial release: stdio MCP server with 81 tools wrapping Robinhood operations
(quotes, positions, orders, options, alerts, trade log) and external analysis
scripts at `C:\Users\algee\*.py`. See `b02702b` and earlier history.
