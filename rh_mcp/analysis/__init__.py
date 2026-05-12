"""In-process analysis modules — replace the legacy subprocess-spawning script wrappers.

Each module exports a single `analyze(...)` entry point that returns a dict (or list of
dicts for batched inputs) matching the JSON shape the legacy scripts emitted on their
`JSON: ...` line. The MCP tool wrappers in `rh_mcp.tools.analysis` call these directly.
"""
