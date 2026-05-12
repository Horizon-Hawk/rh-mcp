"""rh-monitor — optional companion process that watches Robinhood prices, news,
volume spikes, and scheduled messages, then injects alerts into the Claude Code
window via OS-level keystrokes (and sends phone pushes via ntfy.sh as backup).

This package is intentionally separate from `rh_mcp/` — the MCP server is a
clean stdio server, while `rh_monitor` does GUI automation that's Windows-first
and brittle across environments. Install with `pip install rh-mcp[monitor]`.
Run with `rh-monitor` (or `python -m rh_monitor`).
"""
