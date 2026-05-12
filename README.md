# rh-mcp — Robinhood MCP server for Claude

A local-first Model Context Protocol server that lets Claude (Code, Desktop, or claude.ai with HTTP transport) read your Robinhood account and place trades.

Built because the existing third-party Robinhood MCPs expire mid-session, lack atomic entry-with-stop operations, and don't wrap the analysis tools you actually use to make decisions.

## What it does

**Account & market data**
- Portfolio, positions, open orders, multi-account
- Quotes (batch), intraday bars, market hours

**Order placement**
- Limit long/short with automatic shortable + borrow check
- **`place_with_stop`** — atomic: limit entry + native fixed stop-loss in one call
- **`place_with_trailing_stop`** — atomic: limit entry + native trailing stop in one call
- **`flatten_position`** — atomic: cancel open sell-side orders + market sell (matches the RH app's "Close Position" button)
- **`partial_with_trail_rearm`** — atomic +2R partial: cancel existing trail, sell partial, re-arm trail on remainder
- Native trailing stops on existing positions (the only protection that survives chain failures)

Note: Robinhood's API does **not** support true OCO bracket orders for stocks. Take-profit is handled via the partial-exit framework, not paired with the stop. Once shares are protected by a stop, RH won't accept a second sell-side order on the same shares — `partial_with_trail_rearm` exists because of this.

**Setup grading (wraps the analysis scripts you already trust)**
- Stack grade (float + RV + news)
- Order book scan with OBI signal
- Historicals breakout grade + measured move + stop suggestion
- Float / short-interest squeeze grade
- IV rank, options chain scan
- Recent news with catalyst classification

**Trade lifecycle**
- Structured trade log with partial-close support
- Alert management (read/add/deactivate price_alerts.json)
- Auto-stage trade-card alerts (BE / 1R / 2R / target / stop) at entry

## Why a custom MCP

Existing Robinhood MCPs:
- Auth tokens expire mid-session, breaking active trades
- No atomic entry-with-stop operations — you place entry, wait for fill, then manually set the stop (gap of unprotected exposure)
- No integration with the analysis scripts that drive setup grading
- No claude.ai HTTP transport for remote autonomous routines

This server fixes the first three. HTTP transport for claude.ai remote routines is on the roadmap (Phase 3).

## Install

```bash
git clone https://github.com/Horizon-Hawk/rh-mcp.git
cd rh-mcp
pip install -e .
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

## Optional: rh-monitor (alert injector + phone push)

`rh-monitor` is a separate companion process that polls Robinhood prices, news,
volume spikes, and scheduled messages. When something fires it:

1. Shows a Windows toast notification
2. Sends a phone push via ntfy.sh (optional — set `NTFY_TOPIC` to enable)
3. Auto-types the alert into the Claude Code window + Enter (Windows GUI)

This is a **Windows-only**, GUI-based companion. It uses `pyautogui` /
`pygetwindow` / `keyboard` to find the Claude Code window by title (Braille
prefix character) and paste alerts. It works for the original author's setup;
adapt for yours.

### Install + run

```bash
pip install -e ".[monitor]"
# write your alerts file (rh-monitor watches this):
echo '[]' > price_alerts.json
rh-monitor
```

### Configuration (all env vars)

- `RH_MONITOR_DATA_DIR` — where the monitor reads/writes its JSON state
  (default: current working directory)
- `RH_CONFIG_PATH` — same path rh-mcp uses for Robinhood credentials (shared)
- `CLAUDE_EXE` — full path to your Claude Code launcher
  (default: `%APPDATA%\\npm\\claude.cmd`)
- `NTFY_TOPIC` — set to an unguessable string and subscribe on your phone via
  the ntfy.sh app. Empty by default = no phone pushes.
- `NTFY_BASE_URL` — defaults to `https://ntfy.sh`; override for self-hosted
- Various polling intervals: `RH_MONITOR_POLL_SECS`, `RH_MONITOR_VOL_POLL_MINS`,
  `RH_MONITOR_NEWS_POLL_MINS`, `RH_MONITOR_DIGEST_MINS`,
  `RH_MONITOR_VOL_SPIKE_X` (see `rh_monitor/cli.py` for full list)

### State files (read from `RH_MONITOR_DATA_DIR`)

Required: `price_alerts.json` (array of alert dicts; the monitor adds
`fired`/`fired_at` fields when an alert triggers).

Optional: `scheduled_messages.json`, `stock_universe.txt`,
`fundamentals_cache.json`, `news_cache.json`, `alert_inbox.json`
(backlog of injections that failed because the Claude window was missing).

### Known limitations

- **Windows-first.** pyautogui works on macOS/Linux but the window-title
  detection (Braille prefix) is Claude-Code-on-Windows specific.
- **Brittle to window state.** Multi-monitor, fullscreen apps, display scaling,
  and "Claude is on virtual desktop 3 minimized" can defeat the focus path.
  Failed injections go to `alert_inbox.json` and drain on next successful
  injection.
- **Antivirus may flag the keystroke synthesis.** The `keyboard` library
  installs a global hook on Windows. This is a known false-positive pattern.

## Security

- `rh_config.json` is gitignored — never commit your credentials
- All trading actions require explicit tool calls from Claude
- No outbound network calls except Robinhood API
- Session lives only as long as the MCP process

## Status

🚧 **Pre-release** — scaffold and proof-of-pattern tools shipped. See `docs/STATUS.md` for tool roadmap.

## License

MIT — see [LICENSE](LICENSE).
