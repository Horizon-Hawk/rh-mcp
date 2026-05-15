"""Robinhood futures API client.

RH launched futures in 2025 (ES, NQ, MNQ, MES, RTY, CL, GC, etc.) but the
robin_stocks library doesn't expose them yet. This client uses the same
authenticated session as robin_stocks to call the raw futures endpoints
directly.

ENDPOINTS DISCOVERED:
- GET /marketdata/futures/quotes/v1/?ids=<UUID>   — quotes by contract UUID

ENDPOINTS STILL UNKNOWN (404'd on guessed paths):
- Contract search / list (need this to map ticker → UUID)
- Historical bars
- Positions / account balances
- Order placement
- Order management

Set RH_FUTURES_UUIDS in env (JSON dict: {"MNQM26": "uuid", ...}) to cache
known mappings until we discover the instruments endpoint.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

import robin_stocks.robinhood.helper as rhh

# UUID cache — seeded from env var, persisted to file for future sessions
_UUID_CACHE_FILE = os.environ.get(
    "RH_FUTURES_UUID_CACHE",
    os.path.expanduser("~/.rh_futures_uuids.json"),
)


def _futures_headers(extra: dict | None = None) -> dict | None:
    """Build per-request headers for futures endpoints.

    If RH_FUTURES_BEARER_TOKEN is set, injects a fresh Bearer override + required
    custom headers (rh-contract-protected, x-robinhood-client-platform). This
    bypasses the stale rh.login() session token when robin_stocks hasn't
    refreshed for the ceres namespace.

    Returns None when no override is set AND no extra headers passed (so
    requests falls back to session defaults).
    """
    token = os.environ.get("RH_FUTURES_BEARER_TOKEN")
    if not token and not extra:
        return None
    h: dict = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
        h["rh-contract-protected"] = "true"
        h["x-robinhood-client-platform"] = "black-widow"
        h["accept"] = "*/*"
    if extra:
        h.update(extra)
    return h


def _load_uuid_cache() -> dict[str, str]:
    """Load ticker → UUID mappings. Seeded from RH_FUTURES_UUIDS env var
    + persisted JSON file."""
    cache: dict[str, str] = {}
    env_seed = os.environ.get("RH_FUTURES_UUIDS")
    if env_seed:
        try:
            cache.update(json.loads(env_seed))
        except Exception:
            pass
    if os.path.exists(_UUID_CACHE_FILE):
        try:
            with open(_UUID_CACHE_FILE) as f:
                cache.update(json.load(f))
        except Exception:
            pass
    return cache


def _save_uuid_cache(cache: dict[str, str]) -> None:
    try:
        with open(_UUID_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def register_uuid(ticker: str, uuid: str) -> None:
    """Persist a ticker → UUID mapping. Use this when you discover a new
    contract via the RH web app or any other method."""
    cache = _load_uuid_cache()
    cache[ticker.upper().lstrip("/")] = uuid
    _save_uuid_cache(cache)


def get_uuid(ticker: str) -> str | None:
    """Look up a futures contract UUID by ticker (e.g. 'MNQM26' or '/MNQM26').

    For root-only inputs ('MNQ', 'ES', etc.), use resolve_front_month() instead —
    this function is a pure cache read.
    """
    return _load_uuid_cache().get(ticker.upper().lstrip("/"))


# ---------------------------------------------------------------------------
# Front-month auto-discovery
# ---------------------------------------------------------------------------
#
# Reverse-engineered 2026-05-13: the same /marketdata/futures/quotes/v1/ endpoint
# that takes ?ids=<UUID> also accepts ?symbols=/<TICKER>:<EXCHANGE> and returns
# the matching instrument_id in the payload. This gives us a symbol→UUID lookup
# without manual web-app inspection.
#
# Symbol format requirements (verified across CME, CBOT, COMEX, NYMEX):
#   - Leading "/" required
#   - Two-digit year required ("MNQM26", not "MNQM6")
#   - Exchange suffix required and uses CQG-style codes (XCME, XCBT, XCEC, XNYM)

# Quarterly cycle month codes — CME convention.
# H=Mar, M=Jun, U=Sep, Z=Dec.
_QUARTERLY_CODES = ("H", "M", "U", "Z")
_QUARTERLY_MONTHS = (3, 6, 9, 12)

# Roll convention: switch to the next quarterly ~14 days before the current
# expires. CME equity-index quarterlies expire on the third Friday of the
# delivery month — using day=14 as the cutover gives a safe ~1 week buffer
# before the actual expiration.
_QUARTERLY_ROLL_DAY = 14

# Root → exchange suffix. Equity-index futures roll on the standard quarterly
# cycle; metals (GC/SI) and energies (CL/NG) have richer monthly cycles that
# need a different resolver — not implemented here, callers fall back to
# manual register_uuid() or pass the full ticker explicitly.
KNOWN_ROOTS: dict[str, dict] = {
    # CME equity index — quarterly HMUZ
    "MNQ": {"exchange": "XCME", "cycle": "quarterly"},
    "NQ":  {"exchange": "XCME", "cycle": "quarterly"},
    "MES": {"exchange": "XCME", "cycle": "quarterly"},
    "ES":  {"exchange": "XCME", "cycle": "quarterly"},
    "RTY": {"exchange": "XCME", "cycle": "quarterly"},
    "M2K": {"exchange": "XCME", "cycle": "quarterly"},
    # CBOT (Dow) — quarterly HMUZ
    "YM":  {"exchange": "XCBT", "cycle": "quarterly"},
    "MYM": {"exchange": "XCBT", "cycle": "quarterly"},
    # COMEX metals — quarterly works for the listed front but liquidity
    # actually flows on bi-monthly (G/J/M/Q/V/Z). Quarterly resolves to a
    # listed contract; users wanting liquid month should pass ticker explicitly.
    "GC":  {"exchange": "XCEC", "cycle": "quarterly"},
    "MGC": {"exchange": "XCEC", "cycle": "quarterly"},
    "SI":  {"exchange": "XCEC", "cycle": "quarterly"},
    # NYMEX energies — quarterly resolves but liquid front is monthly. Same caveat.
    "CL":  {"exchange": "XNYM", "cycle": "quarterly"},
    "MCL": {"exchange": "XNYM", "cycle": "quarterly"},
    "NG":  {"exchange": "XNYM", "cycle": "quarterly"},
}


def front_month_ticker(root: str, ref_date=None) -> str:
    """Compute the active front-month ticker for a quarterly-cycle root.

    Returns e.g. 'MNQM26' for MNQ on 2026-05-13.

    Cycle: H(Mar)/M(Jun)/U(Sep)/Z(Dec). Rolls on day 14 of the expiry month —
    chosen as a safe ~1 week buffer before standard third-Friday expiration.

    Raises NotImplementedError for non-quarterly roots (would need a different
    resolver — caller should pass the full ticker explicitly).
    """
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except ImportError:
        ET = timezone(timedelta(hours=-4))

    root_u = root.upper().lstrip("/")
    spec = KNOWN_ROOTS.get(root_u)
    if spec is None:
        raise ValueError(
            f"Unknown root {root_u!r}. Add to KNOWN_ROOTS in futures_client.py "
            f"or pass a full ticker (e.g. 'MNQM26') instead of a root."
        )
    if spec.get("cycle") != "quarterly":
        raise NotImplementedError(
            f"Root {root_u} uses cycle {spec.get('cycle')!r} — only 'quarterly' "
            f"is implemented. Pass full ticker explicitly."
        )

    if ref_date is None:
        ref_date = datetime.now(tz=ET)

    y, m, d = ref_date.year, ref_date.month, ref_date.day
    # Pick first quarterly month we haven't rolled out of yet
    for code, expiry_m in zip(_QUARTERLY_CODES, _QUARTERLY_MONTHS):
        if (m, d) < (expiry_m, _QUARTERLY_ROLL_DAY):
            return f"{root_u}{code}{y % 100:02d}"
    # Past December roll → next year's H (March)
    next_y = (y + 1) % 100
    return f"{root_u}H{next_y:02d}"


def front_month_symbol(root: str, ref_date=None) -> str:
    """Build the full RH lookup symbol: '/{ticker}:{exchange}'."""
    root_u = root.upper().lstrip("/")
    spec = KNOWN_ROOTS.get(root_u)
    if spec is None:
        raise ValueError(f"Unknown root {root_u!r}. See KNOWN_ROOTS.")
    ticker = front_month_ticker(root_u, ref_date=ref_date)
    return f"/{ticker}:{spec['exchange']}"


def lookup_by_symbol(symbol: str) -> dict | None:
    """Resolve a contract symbol to its RH metadata (instrument_id, state, last).

    Symbol format: '/{TICKER}:{EXCHANGE}' — e.g. '/MNQM26:XCME', '/CLM26:XNYM'.

    Returns None if the contract doesn't exist or the lookup fails. Returns the
    full quote payload (including state='active'|'inactive') on success — caller
    can use state to detect rolled-out contracts.
    """
    sym = symbol if symbol.startswith("/") else f"/{symbol}"
    url = f"https://api.robinhood.com/marketdata/futures/quotes/v1/?symbols={sym}"
    try:
        r = rhh.SESSION.get(url, timeout=10, headers=_futures_headers())
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    data = body.get("data") or []
    if not data:
        return None
    first = data[0] or {}
    if first.get("status") != "SUCCESS":
        return None
    inner = first.get("data") or {}
    if not inner.get("instrument_id"):
        return None
    return inner


def resolve_front_month(root: str, persist: bool = True, ref_date=None) -> str | None:
    """Get the active front-month UUID for a root symbol.

    Flow:
      1. Compute candidate ticker from today's date (front_month_ticker).
      2. Cache hit on that ticker → return UUID immediately.
      3. Cache miss → look up symbol via /marketdata/futures/quotes/v1/.
      4. If active → optionally persist to cache, return UUID.
      5. If inactive (rolled past expiry) → advance one quarterly and retry once.
      6. If still no hit → return None.

    Args:
        root: 'MNQ', 'ES', 'MES', etc. Must be in KNOWN_ROOTS.
        persist: when True (default), saves resolved UUIDs to the cache file
            so subsequent calls in any session are instant.
        ref_date: override the reference date (for testing). Defaults to now.

    Returns the contract UUID, or None if the root can't be resolved.
    """
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except ImportError:
        ET = timezone(timedelta(hours=-4))

    root_u = root.upper().lstrip("/")

    if ref_date is None:
        ref_date = datetime.now(tz=ET)

    ticker = front_month_ticker(root_u, ref_date=ref_date)

    # Fast path: cache hit
    cached = get_uuid(ticker)
    if cached:
        return cached

    # Network path: resolve via symbol lookup
    symbol = front_month_symbol(root_u, ref_date=ref_date)
    info = lookup_by_symbol(symbol)

    if info and info.get("state") == "active":
        uid = info["instrument_id"]
        if persist:
            register_uuid(ticker, uid)
        return uid

    # Edge case: our date math picked a contract that's already rolled out
    # (e.g. running the brief on roll day before our cutover). Try the next
    # quarterly once.
    if info and info.get("state") == "inactive":
        bumped_date = ref_date.replace(day=28)  # forces (m,d) past any roll cutover
        bumped_ticker = front_month_ticker(root_u, ref_date=bumped_date)
        if bumped_ticker != ticker:
            bumped_symbol = front_month_symbol(root_u, ref_date=bumped_date)
            info2 = lookup_by_symbol(bumped_symbol)
            if info2 and info2.get("state") == "active":
                uid = info2["instrument_id"]
                if persist:
                    register_uuid(bumped_ticker, uid)
                return uid

    return None


_UUID_RE = None


def _looks_like_uuid(s: str) -> bool:
    """True if s matches the canonical UUID format (8-4-4-4-12 hex)."""
    global _UUID_RE
    if _UUID_RE is None:
        import re as _re
        _UUID_RE = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            _re.IGNORECASE,
        )
    return bool(_UUID_RE.match(s))


def resolve_contract_input(value: str) -> str | None:
    """Coerce any of [UUID | full ticker | root] to a contract UUID.

    Resolution order:
      1. UUID-shaped input → returned as-is (no validation).
      2. Known root in KNOWN_ROOTS → resolve_front_month() (uses dated-ticker cache).
      3. Cache hit on the literal string (full dated ticker like 'MNQM26').
      4. Otherwise None.

    Roots take precedence over a literal-string cache hit because the cache is
    keyed by dated ticker ('MNQM26'). Any cache entry on a bare root key is a
    stale alias from earlier manual registration and would route the wrong
    contract (e.g. 'GC' → MGC UUID seen 2026-05-13).
    """
    if not value:
        return None
    s = value.strip().lstrip("/").upper()
    if _looks_like_uuid(s.lower()):
        return s.lower()
    if s in KNOWN_ROOTS:
        return resolve_front_month(s)
    cached = get_uuid(s)
    if cached:
        return cached
    return None


def get_quotes(ids: str | Iterable[str]) -> dict:
    """Pull current quotes for one or more futures contracts by UUID.

    Returns the raw RH response payload, which is shaped:
      {"status": "SUCCESS", "data": [{"status": "...", "data": {<quote>}}, ...]}
    """
    if isinstance(ids, str):
        ids_param = ids
    else:
        ids_param = ",".join(ids)
    url = f"https://api.robinhood.com/marketdata/futures/quotes/v1/?ids={ids_param}"
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Futures account / positions / orders — `ceres` namespace
# ---------------------------------------------------------------------------

CERES_BASE = "https://api.robinhood.com/ceres/v1"


def list_accounts() -> list[dict]:
    """List futures accounts on this user. Typical user has 1-2 (one live, sometimes
    a duplicate for different status). Each account has its own UUID `id` field
    used as the path scope for positions/orders/balances.
    """
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/", timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json().get("results", []) or []


def get_default_account_id() -> str | None:
    """Convenience: pick the ACTIVE futures account that has activity (positions
    or order history). RH users typically have 2 futures accounts under the same
    accountNumber — pick the one that's actually being used. Override via env
    var RH_FUTURES_ACCOUNT_ID if you want to pin a specific one.
    """
    import os
    env_pin = os.environ.get("RH_FUTURES_ACCOUNT_ID")
    if env_pin:
        return env_pin
    active = [a for a in list_accounts() if (a.get("status") or "").upper() == "ACTIVE"]
    if not active:
        return None
    if len(active) == 1:
        return active[0].get("id")
    # Multi-account: pick the one with recent orders. Fall back to first if both empty.
    for a in active:
        try:
            r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{a['id']}/orders/", timeout=10, headers=_futures_headers())
            if r.status_code == 200 and r.json().get("results"):
                return a["id"]
        except Exception:
            pass
    return active[0].get("id")


def get_positions(account_id: str | None = None) -> list[dict]:
    """Open futures positions for the given account (or default)."""
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return []
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/positions/", timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json().get("results", []) or []


def get_orders(
    account_id: str | None = None,
    limit: int = 50,
    contract_type: str = "OUTRIGHT",
    rhs_account_number: str = "588784215",
) -> list[dict]:
    """Order history with full schema (most recent first).

    Uses the root-level /ceres/v1/orders endpoint which returns richer data
    than the account-scoped one — includes orderState/derivedState, fees,
    executions, realizedPnl, limitPrice/stopPrice/averagePrice, etc.

    contract_type: OUTRIGHT (single-leg) or SPREAD (multi-leg).
    """
    url = (
        f"{CERES_BASE}/orders?pageSize={limit}"
        f"&contractType={contract_type}"
        f"&rhsAccountNumber={rhs_account_number}"
    )
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    rows = r.json().get("results", []) or []
    return rows


def get_orders_account_scoped(account_id: str | None = None, limit: int = 50) -> list[dict]:
    """Legacy account-scoped orders endpoint. Less data than get_orders() — use
    only if you need the original API shape for compatibility.
    """
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return []
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/orders/", timeout=15, headers=_futures_headers())
    r.raise_for_status()
    rows = r.json().get("results", []) or []
    return rows[:limit] if limit else rows


def get_aggregated_positions(account_id: str | None = None) -> list[dict]:
    """Aggregated positions per contract with P&L / cost basis context.

    Different from get_positions(): aggregated rolls multiple fills into a
    single per-contract row (typical "Positions" view in RH web app).
    """
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return []
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/aggregated_positions", timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json().get("results", []) or []


def get_order_validation_rules(account_id: str | None = None) -> dict:
    """Server-side order validation rules (max quantity, etc.) for this account.

    GET on /ceres/v1/accounts/{id}/presubmit_order_validation returns the
    config — e.g. {"globalMaxOrderQuantity": "500"}. The POST version validates
    a specific order body against these rules without actually placing it.
    """
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return {}
    url = f"{CERES_BASE}/accounts/{account_id}/presubmit_order_validation"
    r = rhh.SESSION.get(url, timeout=10, headers=_futures_headers())
    r.raise_for_status()
    return r.json()


def get_order_detail(order_id: str, asset_type: str = "FUTURES", account_number: str = "588784215") -> dict:
    """Fetch full detail for a specific order by ID via /wormhole/bw/orders/{id}.

    asset_type: 'FUTURES' | 'EQUITY' | 'OPTION' | 'CRYPTO'
    """
    url = (
        f"https://api.robinhood.com/wormhole/bw/orders/{order_id}"
        f"?assetType={asset_type}&rhsAccountNumber={account_number}"
    )
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json()


def get_recent_orders_unified(account_number: str = "588784215") -> list[dict]:
    """Recent orders across ALL asset types (stocks, options, futures, crypto).

    Uses /wormhole/bw/orders/recent — RH's unified order display. Each row has
    `assetType` (FUTURES, EQUITY, OPTION, CRYPTO) so you can filter client-side.
    Useful for tracking new fills/rejects across the whole account at once.
    """
    url = f"https://api.robinhood.com/wormhole/bw/orders/recent?accountNumber={account_number}"
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json().get("results", []) or []


def place_order(
    contract_uuid: str,
    side: str,
    quantity: int = 1,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "GFD",
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """Place a futures order via POST /ceres/v1/orders.

    Args:
        contract_uuid: Futures contract UUID (e.g. MNQM26 → "64cda3df-...").
        side: "BUY" or "SELL".
        quantity: Number of contracts (default 1).
        order_type: "LIMIT" or "MARKET". MARKET requires accept_market_risk=True.
        limit_price: Required for LIMIT orders. Float, sent as string to RH.
        stop_price: Optional. Triggers STOP/STOP_LIMIT order_trigger if set.
        time_in_force: "GFD" (day) or "GTC". Default GFD (matches RH web app).
        account_id: Futures account UUID. Uses default if None.
        accept_market_risk: Safety gate — MARKET orders execute immediately at
            whatever price the book offers. False raises ValueError; explicit
            True acknowledges you understand market-order fill risk.

    Returns the raw POST response — includes order ID, derivedState (CONFIRMED,
    REJECTED, FILLED, CANCELLED), and any rejection reason.

    Each call generates a unique refId for idempotency — accidental double-call
    will not double-submit (RH dedupes by refId).
    """
    import uuid as _uuid

    side = side.strip().upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

    order_type = order_type.strip().upper()
    if order_type not in ("LIMIT", "MARKET"):
        raise ValueError(f"order_type must be 'LIMIT' or 'MARKET', got {order_type!r}")

    if order_type == "MARKET" and not accept_market_risk:
        raise ValueError(
            "MARKET orders fill at whatever price the book offers — pass "
            "accept_market_risk=True to acknowledge."
        )

    if order_type == "LIMIT" and limit_price is None:
        raise ValueError("LIMIT order requires a limit_price")

    time_in_force = time_in_force.strip().upper()
    if time_in_force not in ("GFD", "GTC"):
        raise ValueError(f"time_in_force must be 'GFD' or 'GTC', got {time_in_force!r}")

    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        raise RuntimeError("No futures account ID available. Pass account_id explicitly.")

    # Determine orderTrigger from stop_price + order_type
    if stop_price is not None:
        order_trigger = "STOP_LIMIT" if order_type == "LIMIT" else "STOP"
    else:
        order_trigger = "IMMEDIATE"

    body = {
        "accountId": account_id,
        "legs": [{
            "contractType": "OUTRIGHT",
            "contractId": contract_uuid,
            "ratioQuantity": 1,
            "orderSide": side,
        }],
        "quantity": str(quantity),
        "orderType": order_type,
        "orderTrigger": order_trigger,
        "timeInForce": time_in_force,
        "refId": str(_uuid.uuid4()),
    }
    if limit_price is not None:
        body["limitPrice"] = str(limit_price)
    if stop_price is not None:
        body["stopPrice"] = str(stop_price)

    headers = {
        "content-type": "application/json",
        "rh-contract-protected": "true",
        "x-robinhood-client-platform": "black-widow",
    }

    url = f"{CERES_BASE.replace('/v1', '/v1')}/orders"  # /ceres/v1/orders
    r = rhh.SESSION.post(url, json=body, headers=_futures_headers(headers), timeout=15)
    # Don't raise_for_status — we want to return the response even on 400 (RH
    # rejection includes useful error detail). Caller checks status_code / derivedState.
    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": r.text}
    return {
        "http_status": r.status_code,
        "request_body": body,
        "response": payload,
    }


def get_order_status(order_id: str, account_id: str | None = None) -> dict | None:
    """Look up a single futures order's current state by ID.

    Polls /ceres/v1/orders (most recent 50) and filters for the matching orderId.
    Returns the full order dict or None if not found in the recent window.

    Useful for the place_with_stop polling loop — gives derivedState plus the
    leg-level averagePrice (the actual fill price) and filledQuantity.
    """
    rows = get_orders(account_id=account_id, limit=50)
    for row in rows:
        if row.get("orderId") == order_id:
            return row
    return None


def _parse_fill(order_row: dict) -> tuple[float | None, int]:
    """Extract (avg_fill_price, filled_quantity) from an order row.

    averagePrice on legs[0] is empty string when nothing filled.
    filledQuantity is a string number on the order itself.
    """
    if not order_row:
        return None, 0
    try:
        filled_qty = int(order_row.get("filledQuantity") or "0")
    except (ValueError, TypeError):
        filled_qty = 0
    legs = order_row.get("orderLegs") or order_row.get("legs") or []
    avg = None
    if legs:
        avg_str = (legs[0].get("averagePrice") or "").strip()
        if avg_str:
            try:
                avg = float(avg_str)
            except ValueError:
                pass
    return avg, filled_qty


def place_with_stop(
    contract_uuid: str,
    side: str,
    quantity: int = 1,
    limit_price: float | None = None,
    stop_price: float | None = None,
    stop_distance_points: float | None = None,
    order_type: str = "LIMIT",
    time_in_force: str = "GFD",
    stop_time_in_force: str = "GTC",
    fill_timeout_sec: float = 60.0,
    poll_interval_sec: float = 2.0,
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """Atomic entry + stop placement.

    Places the entry order, polls until it fills (or timeout), then immediately
    places an opposite-side stop on the filled quantity. Closes the unprotected
    window between fill and manual stop placement.

    Args:
        contract_uuid: Contract UUID. Use higher-level scanners.place_with_stop
            if you want root/ticker auto-resolution.
        side: 'BUY' or 'SELL' — entry direction. Stop side is auto-derived.
        quantity: Contracts requested.
        limit_price: Required for LIMIT entry; ignored for MARKET.
        stop_price: Absolute stop price. Either this OR stop_distance_points
            must be set (not both).
        stop_distance_points: Distance in points from fill price (long: stop
            below fill, short: stop above). Computed after fill — useful for
            MARKET entries when fill price is unknown upfront.
        order_type: 'LIMIT' (default) or 'MARKET' for the entry.
        time_in_force: 'GFD' or 'GTC' for the entry. Default GFD (intraday).
        stop_time_in_force: 'GFD' or 'GTC' for the stop. Default GTC (survives
            session — required for swing positions).
        fill_timeout_sec: How long to wait for the entry to fill before giving
            up and returning. Default 60s — entry stays live (LIMIT GFD), user
            decides whether to cancel.
        poll_interval_sec: Seconds between fill checks. Default 2s.
        account_id: Defaults to the active futures account.
        accept_market_risk: Required True for MARKET entry.

    Returns:
        {
            success: bool,
            stage: 'rejected_entry' | 'fill_timeout' | 'rejected_stop' | 'complete',
            entry: {order_id, state, fill_price, fill_quantity, response},
            stop: {order_id, stop_price, state, response} | None,
            elapsed_sec: float,
        }
    """
    import time as _time

    side_u = side.strip().upper()
    if side_u not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    stop_side = "SELL" if side_u == "BUY" else "BUY"

    if (stop_price is None) == (stop_distance_points is None):
        raise ValueError(
            "Specify exactly one of stop_price or stop_distance_points "
            "(stop_price for absolute, stop_distance for from-fill computation)."
        )
    if stop_distance_points is not None and stop_distance_points <= 0:
        raise ValueError("stop_distance_points must be positive (direction is auto-derived)")

    started = _time.time()

    # Phase 1: place entry
    entry_resp = place_order(
        contract_uuid=contract_uuid,
        side=side_u,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
        account_id=account_id,
        accept_market_risk=accept_market_risk,
    )
    entry_body = entry_resp.get("response") or {}
    entry_id = entry_body.get("orderId")
    initial_state = (entry_body.get("derivedState") or "").upper()

    if entry_resp.get("http_status") not in (200, 201) or not entry_id:
        return {
            "success": False,
            "stage": "rejected_entry",
            "entry": {"response": entry_resp, "state": initial_state or "ERROR"},
            "stop": None,
            "elapsed_sec": round(_time.time() - started, 2),
        }
    if initial_state == "REJECTED":
        return {
            "success": False,
            "stage": "rejected_entry",
            "entry": {"order_id": entry_id, "state": "REJECTED", "response": entry_resp},
            "stop": None,
            "elapsed_sec": round(_time.time() - started, 2),
        }

    # Phase 2: poll for fill
    deadline = started + fill_timeout_sec
    fill_price, fill_qty = _parse_fill(entry_body)
    final_state = initial_state

    while fill_qty < 1 and _time.time() < deadline:
        _time.sleep(poll_interval_sec)
        row = get_order_status(entry_id, account_id=account_id)
        if row is None:
            continue
        final_state = (row.get("derivedState") or "").upper()
        fill_price, fill_qty = _parse_fill(row)
        if final_state in ("CANCELLED", "CANCELED", "REJECTED"):
            break

    if fill_qty < 1:
        return {
            "success": False,
            "stage": "fill_timeout" if final_state in ("CONFIRMED", "") else f"entry_{final_state.lower()}",
            "entry": {"order_id": entry_id, "state": final_state or "PENDING", "fill_quantity": 0},
            "stop": None,
            "elapsed_sec": round(_time.time() - started, 2),
            "note": (
                f"entry {entry_id} not filled within {fill_timeout_sec}s. "
                f"State={final_state or 'unknown'}. Working order is still live — "
                f"call cancel_order({entry_id!r}) to abandon, or wait."
            ),
        }

    # Phase 3: compute stop_price if distance-based
    if stop_price is None:
        assert stop_distance_points is not None and fill_price is not None
        stop_price = (
            fill_price - stop_distance_points if side_u == "BUY"
            else fill_price + stop_distance_points
        )

    # Phase 4: place stop (opposite side, market-on-trigger)
    stop_resp = place_order(
        contract_uuid=contract_uuid,
        side=stop_side,
        quantity=fill_qty,
        order_type="MARKET",
        stop_price=stop_price,
        time_in_force=stop_time_in_force,
        account_id=account_id,
        accept_market_risk=True,
    )
    stop_body = stop_resp.get("response") or {}
    stop_id = stop_body.get("orderId")
    stop_state = (stop_body.get("derivedState") or "").upper()
    stop_ok = stop_resp.get("http_status") in (200, 201) and stop_state != "REJECTED"

    return {
        "success": stop_ok,
        "stage": "complete" if stop_ok else "rejected_stop",
        "entry": {
            "order_id": entry_id,
            "state": final_state,
            "fill_price": fill_price,
            "fill_quantity": fill_qty,
        },
        "stop": {
            "order_id": stop_id,
            "stop_price": stop_price,
            "side": stop_side,
            "state": stop_state,
            "response": stop_resp,
        },
        "elapsed_sec": round(_time.time() - started, 2),
    }


def replace_order(
    old_order_id: str,
    contract_uuid: str,
    side: str,
    quantity: int = 1,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "GTC",
    account_id: str | None = None,
    accept_market_risk: bool = False,
) -> dict:
    """Atomic order modify — POST /ceres/v1/orders/{old_id}/replace.

    Submits a brand-new full order body; RH atomically cancels the old order
    and posts the new one. Use for stop-tightening on a working position so
    there's no naked window between cancel and re-place.

    The new order body has the same shape as place_order. The old_order_id is
    only used in the URL path — the new body fully describes the replacement.

    Use only on orders with derivedState=CONFIRMED. Calling on already-cancelled
    orders crashes the RH server (500 nil pointer).
    """
    import uuid as _uuid

    side = side.strip().upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    order_type = order_type.strip().upper()
    if order_type not in ("LIMIT", "MARKET"):
        raise ValueError(f"order_type must be 'LIMIT' or 'MARKET', got {order_type!r}")
    if order_type == "MARKET" and not accept_market_risk:
        raise ValueError(
            "MARKET orders fill at whatever price the book offers — pass "
            "accept_market_risk=True to acknowledge."
        )
    if order_type == "LIMIT" and limit_price is None:
        raise ValueError("LIMIT order requires a limit_price")
    time_in_force = time_in_force.strip().upper()
    if time_in_force not in ("GFD", "GTC"):
        raise ValueError(f"time_in_force must be 'GFD' or 'GTC', got {time_in_force!r}")

    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        raise RuntimeError("No futures account ID available. Pass account_id explicitly.")

    if stop_price is not None:
        order_trigger = "STOP_LIMIT" if order_type == "LIMIT" else "STOP"
    else:
        order_trigger = "IMMEDIATE"

    body = {
        "accountId": account_id,
        "legs": [{
            "contractType": "OUTRIGHT",
            "contractId": contract_uuid,
            "ratioQuantity": 1,
            "orderSide": side,
        }],
        "quantity": str(quantity),
        "orderType": order_type,
        "orderTrigger": order_trigger,
        "timeInForce": time_in_force,
        "refId": str(_uuid.uuid4()),
    }
    if limit_price is not None:
        body["limitPrice"] = str(limit_price)
    if stop_price is not None:
        body["stopPrice"] = str(stop_price)

    headers = {
        "content-type": "application/json",
        "rh-contract-protected": "true",
        "x-robinhood-client-platform": "black-widow",
    }
    url = f"{CERES_BASE}/orders/{old_order_id}/replace"
    r = rhh.SESSION.post(url, json=body, headers=_futures_headers(headers), timeout=15)
    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": r.text}
    return {
        "http_status": r.status_code,
        "old_order_id": old_order_id,
        "request_body": body,
        "response": payload,
    }


def find_open_stop_for_contract(
    contract_uuid: str,
    account_id: str | None = None,
) -> dict | None:
    """Find a working STOP order on the given contract. Returns the order row
    or None if no open stop exists.

    Filters get_orders() for derivedState=CONFIRMED + orderTrigger=STOP/STOP_LIMIT
    + matching contractId. Returns the most recent one (in case of duplicates).
    """
    rows = get_orders(account_id=account_id, limit=50)
    candidates = []
    for row in rows:
        if (row.get("derivedState") or "").upper() != "CONFIRMED":
            continue
        trigger = (row.get("orderTrigger") or "").upper()
        if trigger not in ("STOP", "STOP_LIMIT"):
            continue
        legs = row.get("orderLegs") or row.get("legs") or []
        if not legs:
            continue
        if legs[0].get("contractId") != contract_uuid:
            continue
        candidates.append(row)
    if not candidates:
        return None
    # Most recent first (orders endpoint returns newest first by default)
    return candidates[0]


def cancel_order(order_id: str, account_id: str | None = None) -> dict:
    """Cancel a pending (unfilled) futures order.

    POST /ceres/v1/orders/{order_id}/cancel with body {"accountId": "..."}.
    Use for resting orders that haven't filled yet. For closing FILLED positions,
    use flatten_position() instead.
    """
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        raise RuntimeError("No futures account ID available.")
    headers = {
        "content-type": "application/json",
        "rh-contract-protected": "true",
        "x-robinhood-client-platform": "black-widow",
    }
    url = f"{CERES_BASE}/orders/{order_id}/cancel"
    r = rhh.SESSION.post(url, json={"accountId": account_id}, headers=_futures_headers(headers), timeout=15)
    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": r.text}
    return {
        "http_status": r.status_code,
        "order_id": order_id,
        "response": payload,
    }


def flatten_position(contract_uuid: str, account_id: str | None = None) -> dict:
    """Emergency close a futures position via market order.

    POSTs to /ceres/v1/accounts/{id}/flatten_position. RH auto-determines side
    (sell longs / cover shorts) and quantity from the current position — caller
    only specifies the contract.

    IMPORTANT: this places a MARKET order to close. Will fill at whatever price
    the book offers. Use only when you want immediate exit (bail signal, stop
    breach, etc.) — for orderly exits use place_order with a LIMIT instead.

    Returns {http_status, request_body, response}.
    """
    import uuid as _uuid
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        raise RuntimeError("No futures account ID available.")
    body = {
        "contractId": contract_uuid,
        "refId": str(_uuid.uuid4()),
    }
    headers = {
        "content-type": "application/json",
        "rh-contract-protected": "true",
        "x-robinhood-client-platform": "black-widow",
    }
    url = f"{CERES_BASE}/accounts/{account_id}/flatten_position"
    r = rhh.SESSION.post(url, json=body, headers=_futures_headers(headers), timeout=15)
    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": r.text}
    return {
        "http_status": r.status_code,
        "request_body": body,
        "response": payload,
    }


# ---------------------------------------------------------------------------
# Market data streaming — dxFeed dxLink token exchange
# ---------------------------------------------------------------------------
#
# Discovered 2026-05-14. RH exchanges the user JWT for a short-lived (4hr)
# dxFeed token via GET /marketdata/token/v1/. The dxFeed token is what goes in
# the WS AUTH frame; the user JWT does NOT authenticate the WS by itself.
# See memory/project_rh_marketdata_streaming.md for the full protocol notes.

_MD_TOKEN_CACHE: dict | None = None
_MD_TOKEN_REFRESH_BUFFER_SEC = 30 * 60  # refresh 30min before expiry


def get_market_data_token(force_refresh: bool = False) -> dict:
    """Exchange the user JWT for a dxFeed market data token.

    Cached in-memory with auto-refresh ~30min before the 4-hour expiry. Pass
    force_refresh=True to bypass the cache (e.g. after an AUTH failure that
    suggests the cached token went stale early).

    Returns:
      {
        token: str,              # the value to put in dxLink AUTH frame
        wss_url: str,            # WebSocket URL to connect to
        expiration: str,         # ISO8601
        expires_at: float,       # unix seconds (derived for cache logic)
        ttl_ms: int,
        dxfeed_id: str,
        cached: bool,            # True if from cache, False if freshly fetched
      }
    """
    import time
    global _MD_TOKEN_CACHE

    now = time.time()
    if (not force_refresh
            and _MD_TOKEN_CACHE
            and (_MD_TOKEN_CACHE["expires_at"] - now) > _MD_TOKEN_REFRESH_BUFFER_SEC):
        return {**_MD_TOKEN_CACHE, "cached": True}

    import uuid as _uuid
    import requests
    from datetime import datetime, timezone

    token = os.environ.get("RH_FUTURES_BEARER_TOKEN")
    if not token:
        raise RuntimeError(
            "RH_FUTURES_BEARER_TOKEN not set — required to exchange for "
            "market data token."
        )

    session_id = str(_uuid.uuid4())
    url = (
        "https://api.robinhood.com/marketdata/token/v1/"
        f"?session_id={session_id}&session_type=blackwidow"
    )
    # Critical: Origin header is a CORS gate. Without it, returns 401.
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "origin": "https://robinhood.com",
        "referer": "https://robinhood.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/147.0.0.0 Safari/537.36",
        "x-robinhood-client-platform": "black-widow",
    }
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    body = r.json()

    # Response is double-nested: {status, data:{status, data:{...}}}
    inner = ((body.get("data") or {}).get("data")) or {}
    if not inner.get("token"):
        raise RuntimeError(f"unexpected token response shape: {body!r}")

    # Compute absolute expiry from ttl
    ttl_ms = int(inner.get("ttl_ms") or 0)
    expires_at = now + (ttl_ms / 1000.0)

    result = {
        "token": inner["token"],
        "wss_url": inner.get("wss_url") or
                   "wss://api.robinhood.com/marketdata/streaming/legend/v2/",
        "expiration": inner.get("expiration"),
        "expires_at": expires_at,
        "ttl_ms": ttl_ms,
        "dxfeed_id": inner.get("dxfeed_id"),
        "session_id": session_id,
    }
    _MD_TOKEN_CACHE = result
    return {**result, "cached": False}


def clear_market_data_token_cache() -> None:
    """Drop any cached market data token. Use when you suspect the cached
    token has been invalidated server-side (e.g. AUTH frame rejected)."""
    global _MD_TOKEN_CACHE
    _MD_TOKEN_CACHE = None


def get_buying_power_breakdown(account_number: str = "588784215") -> dict:
    """Per-category buying power breakdown including futures equity + margin held.

    Returns categorized line items: Cash, Margin total, Futures equity,
    Futures margin held, Short cash, etc. The 'category' field is one of
    'None', 'Futures', etc. Useful for showing where capacity is being used.
    """
    url = f"https://api.robinhood.com/accounts/{account_number}/buying_power_breakdown"
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    return r.json()


def get_contract_quantity(contract_uuid: str, account_id: str | None = None) -> dict:
    """Held / pending / net quantity for a specific futures contract."""
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return {}
    url = f"{CERES_BASE}/accounts/{account_id}/contract_quantities?contractIds={contract_uuid}"
    r = rhh.SESSION.get(url, timeout=15, headers=_futures_headers())
    r.raise_for_status()
    results = r.json().get("results", []) or []
    return results[0] if results else {}


def get_quote_by_ticker(ticker: str) -> dict | None:
    """Convenience: resolve to UUID, fetch quote, unwrap to flat dict.

    Accepts any of: UUID, full dated ticker ('MNQM26'), or root ('MNQ').
    Root inputs resolve to the active front-month via resolve_front_month()
    and auto-populate the local cache.

    Returns None if the input can't be resolved to a valid contract.
    """
    uuid = resolve_contract_input(ticker)
    if not uuid:
        return None
    raw = get_quotes(uuid)
    items = raw.get("data") or []
    if not items:
        return None
    first = items[0].get("data") or {}
    if not first:
        return None
    # Normalize floats
    def _f(k):
        v = first.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return {
        "ticker": ticker.upper().lstrip("/"),
        "symbol": first.get("symbol"),
        "instrument_id": first.get("instrument_id"),
        "bid": _f("bid_price"),
        "ask": _f("ask_price"),
        "last": _f("last_trade_price"),
        "bid_size": first.get("bid_size"),
        "ask_size": first.get("ask_size"),
        "last_size": first.get("last_trade_size"),
        "updated_at": first.get("updated_at"),
        "state": first.get("state"),
        "out_of_band": first.get("out_of_band"),
    }
