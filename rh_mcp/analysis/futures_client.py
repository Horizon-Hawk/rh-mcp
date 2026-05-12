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


def get_orders(account_id: str | None = None, limit: int = 50) -> list[dict]:
    """Order history (most recent first). `limit` is server-side paging — fetch
    multiple pages by following the `next` token if larger history is needed.
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
