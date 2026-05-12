"""Robinhood authentication — one-time login at server startup, session persists.

Reads credentials from rh_config.json adjacent to the cwd or via RH_CONFIG_PATH env var.

Supports optional unattended MFA via pyotp: set `mfa_secret` in rh_config.json to your
TOTP base32 secret (the one shown when you set up the authenticator app — not the 6-digit
code). With it set, the server logs in without prompting for a code.
"""

import json
import os
import sys
from pathlib import Path

try:
    import robin_stocks.robinhood as rh
except ImportError as e:
    raise SystemExit(
        "robin-stocks not installed. Run: pip install robin-stocks"
    ) from e

# CRITICAL: redirect robin_stocks output to stderr at import time.
# Several robin_stocks code paths (notably options.find_options_by_expiration)
# write a "Loading Market Data |/-\" spinner directly to sys.stdout. When this
# package runs as an MCP stdio server, sys.stdout is the JSON-RPC protocol
# channel — any non-protocol bytes there corrupt the stream and the MCP client
# disconnects mid-call ("Connection closed" error). Pointing robin_stocks at
# stderr instead keeps useful diagnostics visible to the wrapper's stderr log
# while protecting the stdio transport from corruption.
try:
    rh.helper.set_output(sys.stderr)
except Exception:
    # Older / newer robin_stocks versions may rename set_output — never let
    # this failure stop the package from importing.
    pass

try:
    import pyotp
    _HAS_PYOTP = True
except ImportError:
    _HAS_PYOTP = False


_CONFIG_CACHE: dict | None = None
_LOGGED_IN: bool = False


def _config_path() -> Path:
    """Resolve config path: env var first (if file exists), then cwd, then package dir.

    If RH_CONFIG_PATH is set but the file doesn't exist, fall through to candidates
    rather than locking the user into a bad path. Common cause: backslashes stripped
    by a JSON writer (`C:Usersfoo` instead of `C:\\Users\\foo`).
    """
    candidates = []
    env_path = os.environ.get("RH_CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        Path.cwd() / "rh_config.json",
        Path(__file__).parent.parent / "rh_config.json",
    ])
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # default for error message


def load_config() -> dict:
    """Load and cache rh_config.json.

    Raises RuntimeError (not SystemExit) on missing config — SystemExit inside a
    lazily-invoked tool call would kill the MCP server subprocess, leaving the
    client to see "server disconnected" with no diagnostic.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    path = _config_path()
    if not path.exists():
        env_hint = (
            f" (RH_CONFIG_PATH={os.environ['RH_CONFIG_PATH']!r})"
            if os.environ.get("RH_CONFIG_PATH") else ""
        )
        raise RuntimeError(
            f"rh_config.json not found at {path}{env_hint}. "
            f"Copy rh_config.example.json to rh_config.json and fill in your credentials."
        )
    _CONFIG_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _CONFIG_CACHE


def _current_mfa_code(cfg: dict) -> str | None:
    """Generate current TOTP code if mfa_secret is configured. Returns None otherwise."""
    secret = cfg.get("mfa_secret")
    if not secret:
        return None
    if not _HAS_PYOTP:
        # mfa_secret set but library missing — warn but don't crash; user may use interactive flow
        return None
    return pyotp.TOTP(secret.replace(" ", "")).now()


def login() -> None:
    """One-time Robinhood login. Idempotent — subsequent calls are no-ops.

    If `mfa_secret` is in rh_config.json AND `pyotp` is installed, generates the
    current 6-digit code automatically. Otherwise, falls back to robin-stocks'
    interactive prompt (which requires a human to type the code)."""
    global _LOGGED_IN
    if _LOGGED_IN:
        return
    cfg = load_config()
    mfa_code = _current_mfa_code(cfg)
    rh.login(
        username=cfg["username"],
        password=cfg["password"],
        store_session=True,
        mfa_code=mfa_code,
    )
    _LOGGED_IN = True


def default_account() -> str | None:
    """Return the default account number from config, if set."""
    return load_config().get("default_account")
