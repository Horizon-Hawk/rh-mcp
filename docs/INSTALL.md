# Installing rh-mcp

## Prerequisites

- Python 3.10 or higher
- A Robinhood account
- Claude Code CLI installed (or Claude Desktop with MCP support)

## Install steps

### 1. Clone and install

```bash
git clone https://github.com/Horizon-Hawk/rh-mcp.git
cd rh-mcp
pip install -e .
```

Or without git:

```bash
pip install -r requirements.txt
```

### 2. Configure Robinhood credentials

```bash
cp rh_config.example.json rh_config.json
```

Edit `rh_config.json`:

```json
{
  "username": "your-email@example.com",
  "password": "your-password",
  "default_account": "123456789",
  "mfa_method": "sms"
}
```

**The file is gitignored** — never commit it.

### 3. First-time MFA

The first time `rh-mcp` runs, robin-stocks may prompt for an MFA code from your phone. After successful login, `robinhood.pickle` is created and reused for subsequent sessions.

If you have a `pyotp` MFA secret, you can paste it as `mfa_secret` in `rh_config.json` for unattended login.

### 4. Connect to Claude Code

Locate your Claude Code MCP settings file:

- **Windows**: `%USERPROFILE%\.claude\settings.json`
- **macOS/Linux**: `~/.claude/settings.json`

Add the `rh-mcp` server entry:

```json
{
  "mcpServers": {
    "rh-mcp": {
      "command": "python",
      "args": ["-m", "rh_mcp.server"],
      "cwd": "C:\\Users\\<your-windows-username>\\TraderMCP-RH"
    }
  }
}
```

Update `cwd` to the absolute path where you cloned the repo.

### 5. Restart Claude Code

The server appears as `mcp__rh-mcp__*` tools when Claude Code restarts.

### Verify

In a Claude Code session, ask:

> Run get_quote on AAPL

Claude should call `mcp__rh-mcp__get_quote` and return a price.

## Troubleshooting

**"rh_config.json not found"** — check that the file exists in the same directory as `pyproject.toml`, or set `RH_CONFIG_PATH` env var.

**MFA loop** — delete `robinhood.pickle` and re-run; you'll be prompted to enter a fresh MFA code.

**Tools not appearing in Claude** — restart Claude Code completely after editing settings.json. Run `/mcp` in chat to check connection status.
