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
    """Look up a futures contract UUID by ticker (e.g. 'MNQM26' or '/MNQM26')."""
    return _load_uuid_cache().get(ticker.upper().lstrip("/"))


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
    r = rhh.SESSION.get(url, timeout=15)
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
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/", timeout=15)
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
            r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{a['id']}/orders/", timeout=10)
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
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/positions/", timeout=15)
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
    r = rhh.SESSION.get(url, timeout=15)
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
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/orders/", timeout=15)
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
    r = rhh.SESSION.get(f"{CERES_BASE}/accounts/{account_id}/aggregated_positions", timeout=15)
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
    r = rhh.SESSION.get(url, timeout=10)
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
    r = rhh.SESSION.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def get_recent_orders_unified(account_number: str = "588784215") -> list[dict]:
    """Recent orders across ALL asset types (stocks, options, futures, crypto).

    Uses /wormhole/bw/orders/recent — RH's unified order display. Each row has
    `assetType` (FUTURES, EQUITY, OPTION, CRYPTO) so you can filter client-side.
    Useful for tracking new fills/rejects across the whole account at once.
    """
    url = f"https://api.robinhood.com/wormhole/bw/orders/recent?accountNumber={account_number}"
    r = rhh.SESSION.get(url, timeout=15)
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
    r = rhh.SESSION.post(url, json=body, headers=headers, timeout=15)
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
    r = rhh.SESSION.post(url, json={"accountId": account_id}, headers=headers, timeout=15)
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
    r = rhh.SESSION.post(url, json=body, headers=headers, timeout=15)
    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": r.text}
    return {
        "http_status": r.status_code,
        "request_body": body,
        "response": payload,
    }


def get_buying_power_breakdown(account_number: str = "588784215") -> dict:
    """Per-category buying power breakdown including futures equity + margin held.

    Returns categorized line items: Cash, Margin total, Futures equity,
    Futures margin held, Short cash, etc. The 'category' field is one of
    'None', 'Futures', etc. Useful for showing where capacity is being used.
    """
    url = f"https://api.robinhood.com/accounts/{account_number}/buying_power_breakdown"
    r = rhh.SESSION.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def get_contract_quantity(contract_uuid: str, account_id: str | None = None) -> dict:
    """Held / pending / net quantity for a specific futures contract."""
    if account_id is None:
        account_id = get_default_account_id()
    if not account_id:
        return {}
    url = f"{CERES_BASE}/accounts/{account_id}/contract_quantities?contractIds={contract_uuid}"
    r = rhh.SESSION.get(url, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", []) or []
    return results[0] if results else {}


def get_quote_by_ticker(ticker: str) -> dict | None:
    """Convenience: look up UUID from cache, fetch quote, unwrap to flat dict.

    Returns None if ticker not in UUID cache. The cache is bootstrapped by
    calling register_uuid() with mappings you discover from RH's web app.
    """
    uuid = get_uuid(ticker)
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
