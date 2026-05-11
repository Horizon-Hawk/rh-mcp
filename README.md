# rh-mcp — Robinhood MCP server for Claude

A local-first Model Context Protocol server that lets Claude (Code, Desktop, or claude.ai with HTTP transport) read your Robinhood account and place trades.

Built because the existing third-party Robinhood MCPs expire mid-session, lack atomic bracket operations, and don't wrap the analysis tools you actually use to make decisions.

## What it does

**Account & market data**
- Portfolio, positions, open orders, multi-account
- Quotes (batch), intraday bars, market hours

**Order placement**
- Limit long/short with automatic shortable + borrow check
- **Atomic place-with-stop** (limit entry + native trailing stop in one call)
- **Atomic place-with-bracket** (entry + stop + target)
- Native trailing stops on existing positions (the only protection that survives chain failures)

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
- No atomic bracket operations — you place entry, then stop, then target, each separately
- No integration with the analysis scripts that drive setup grading
- No claude.ai HTTP transport for remote autonomous routines

This server fixes all four.

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

## Security

- `rh_config.json` is gitignored — never commit your credentials
- All trading actions require explicit tool calls from Claude
- No outbound network calls except Robinhood API
- Session lives only as long as the MCP process

## Status

🚧 **Pre-release** — scaffold and proof-of-pattern tools shipped. See `docs/STATUS.md` for tool roadmap.

## License

MIT — see [LICENSE](LICENSE).
