"""Robinhood authentication — one-time login at server startup, session persists.

Reads credentials from rh_config.json adjacent to the cwd or via RH_CONFIG_PATH env var.

Supports optional unattended MFA via pyotp: set `mfa_secret` in rh_config.json to your
TOTP base32 secret (the one shown when you set up the authenticator app — not the 6-digit
code). With it set, the server logs in without prompting for a code.

Auth-resilience features (added 2026-05-14 after a 401 storm):
- 401 response hook: any 401 from RH's API raises AuthExpired instead of being
  silently swallowed by robin_stocks (which used to return empty dicts on 401).
- auth_retry decorator: catches AuthExpired, forces a fresh login, retries once.
- Background refresh thread: re-logs in every 12h to prevent token expiry.
- force_relogin(): manual escape hatch when stock-side tools return empty.
"""

import functools
import json
import logging
import os
import sys
import threading
import time
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
_HOOK_INSTALLED: bool = False
_REFRESH_THREAD: threading.Thread | None = None

# Endpoints that should NEVER trigger 401-on-failure handling because they
# legitimately can return 401 during normal usage (e.g., the login endpoint
# itself returns 401 when MFA is required as a non-error state).
_HOOK_SKIP_PATTERNS = (
    "/oauth2/token/",     # login flow
    "/oauth2/revoke",     # logout
)

_log = logging.getLogger(__name__)


class AuthExpired(Exception):
    """Raised when an RH endpoint returns 401 — signal for auth_retry to refresh."""
    pass


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


def _install_stock_bearer_override() -> bool:
    """Inject a browser-captured Bearer token into robin_stocks' shared session.

    Mirror of RH_FUTURES_BEARER_TOKEN in futures_client.py. Bypasses
    robin_stocks' fragile rh.login() refresh path by hard-overriding the
    Authorization header on every request. Use a browser-captured Bearer JWT
    from a logged-in RH web session — they're valid for ~100 days.

    Token source priority:
      1. RH_STOCK_BEARER_TOKEN env var
      2. `stock_bearer_token` in rh_config.json

    Returns True if a token was installed, False otherwise.
    """
    token = os.environ.get("RH_STOCK_BEARER_TOKEN", "").strip()
    if not token:
        # Fallback: try rh_config.json. Use try/except so a missing config
        # doesn't break the no-override path.
        try:
            token = (load_config().get("stock_bearer_token") or "").strip()
        except Exception:
            token = ""
    if not token:
        return False
    try:
        from robin_stocks.robinhood import helper as rhh
    except ImportError:
        _log.warning("robin_stocks not importable — stock bearer override skipped")
        return False
    session = getattr(rhh, "SESSION", None)
    if session is None:
        _log.warning("robin_stocks helper.SESSION not found — stock bearer override skipped")
        return False
    # These match the headers RH's web client sends for stock/options endpoints.
    # x-robinhood-client-platform tags us as the black-widow web app session.
    session.headers["Authorization"] = f"Bearer {token}"
    session.headers["x-robinhood-client-platform"] = "black-widow"

    # Also flip robin_stocks' internal LOGGED_IN flag — required by their
    # @login_required decorator (gates get_open_orders, place_order, etc.).
    # The HTTP override alone gives us auth at the wire layer, but library-level
    # gates still check this flag.
    try:
        from robin_stocks.robinhood import helper as rhh
        rhh.set_login_state(True)
    except Exception as e:
        _log.warning(f"could not flip robin_stocks LOGGED_IN flag: {e}")

    _log.info("RH_STOCK_BEARER_TOKEN installed on robin_stocks.SESSION (override active)")
    return True


def _install_401_hook() -> None:
    """Install a response hook on robin_stocks' shared requests.Session that
    raises AuthExpired on any 401. Without this, robin_stocks swallows 401s
    and returns empty dicts — making expired tokens look like 'no data'.

    Idempotent: safe to call multiple times.
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        from robin_stocks.robinhood import helper as rhh
    except ImportError:
        _log.warning("robin_stocks.helper not importable — 401 hook NOT installed")
        return

    session = getattr(rhh, "SESSION", None)
    if session is None:
        _log.warning("robin_stocks helper.SESSION not found — 401 hook NOT installed")
        return

    def _check_401(response, *args, **kwargs):
        if response.status_code != 401:
            return
        url = response.url or ""
        # Don't raise during the login flow itself
        for skip in _HOOK_SKIP_PATTERNS:
            if skip in url:
                return
        _log.warning(f"AuthExpired: 401 on {url}")
        raise AuthExpired(f"401 Unauthorized on {url}")

    # Append rather than replace so we don't clobber any existing hooks
    hooks = session.hooks.setdefault("response", [])
    if _check_401 not in hooks:
        hooks.append(_check_401)
    _HOOK_INSTALLED = True


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
    # Install the 401 hook AFTER login so robin_stocks has initialized urls.SESSION
    _install_401_hook()
    # If RH_STOCK_BEARER_TOKEN is set, override robin_stocks' session token with
    # the browser-captured one (~100 day TTL, bypasses the brittle re-auth path)
    _install_stock_bearer_override()
    # Start the background refresh thread on first login (idempotent)
    _start_refresh_thread()


def force_relogin() -> dict:
    """Clear cached session + token and force a fresh Robinhood login.

    Use this when stock-side tools start returning empty results, which is the
    silent symptom of an expired RH token. Deletes the persisted session pickle
    so robin_stocks can't reuse the stale credentials.

    Returns a result dict so MCP tool callers get a clean success/error response.
    """
    global _LOGGED_IN
    _LOGGED_IN = False

    # Wipe the persisted session pickle (path is robin_stocks' default).
    pickle_path = Path.home() / ".tokens" / "robinhood.pickle"
    pickle_removed = False
    try:
        if pickle_path.exists():
            pickle_path.unlink()
            pickle_removed = True
    except Exception as e:
        _log.warning(f"could not delete pickle {pickle_path}: {e}")

    try:
        login()
        # Check if bearer override actually got installed (env var OR config).
        # _install_stock_bearer_override() reads both — verify by inspecting
        # the SESSION header rather than just the env var.
        bearer_active = False
        try:
            from robin_stocks.robinhood import helper as rhh
            auth_header = rhh.SESSION.headers.get("Authorization", "")
            bearer_active = auth_header.startswith("Bearer ") and "Bearer " in auth_header
        except Exception:
            pass
        return {
            "success": True,
            "message": "Re-authenticated to Robinhood",
            "pickle_removed": pickle_removed,
            "bearer_override_active": bearer_active,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Re-login failed: {e}",
            "pickle_removed": pickle_removed,
        }


def auth_retry(fn):
    """Decorator: catch AuthExpired (raised by the 401 hook), force re-login,
    and retry the wrapped function once.

    Apply to any tool that hits a stock-side Robinhood endpoint via robin_stocks.
    Futures-only tools don't need it (they use the separate bearer-token path).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthExpired as e:
            _log.warning(f"auth_retry: AuthExpired caught in {fn.__name__}: {e} — relogging in")
            result = force_relogin()
            if not result.get("success"):
                # Re-login itself failed — surface the original error context
                raise RuntimeError(
                    f"AuthExpired and force_relogin() failed: {result.get('error')}"
                ) from e
            # One retry only — if it 401s again, let the exception propagate
            return fn(*args, **kwargs)
    return wrapper


def _start_refresh_thread(interval_hours: float = 12.0) -> None:
    """Spawn a daemon thread that re-logs in every `interval_hours` to prevent
    silent token expiry mid-session. Idempotent.
    """
    global _REFRESH_THREAD
    if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
        return

    def _refresh_loop():
        while True:
            try:
                time.sleep(interval_hours * 3600)
                _log.info(f"auth refresh: periodic re-login (every {interval_hours}h)")
                force_relogin()
            except Exception as e:
                # Never let the thread die — log and continue
                _log.error(f"auth refresh loop error: {e}")

    t = threading.Thread(
        target=_refresh_loop,
        daemon=True,
        name=f"rh-auth-refresh-{interval_hours}h",
    )
    t.start()
    _REFRESH_THREAD = t


def default_account() -> str | None:
    """Return the default account number from config, if set."""
    return load_config().get("default_account")
