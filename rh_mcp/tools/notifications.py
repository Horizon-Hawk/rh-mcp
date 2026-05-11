"""Notifications and watchlist tools.

Note on native price alerts: Robinhood's public API does NOT expose a documented
endpoint for SETTING price alerts (the kind you configure in the app). They may
exist on an internal endpoint but reverse-engineering is fragile.

What IS supported:
- READ incoming notifications (price alert fires show up here)
- Watchlist add/remove (a proxy for the kind of monitoring alerts provide)

For reliable phone push notifications when alerts fire, integrate ntfy.sh,
Pushover, or Twilio with the existing price_alert_monitor.py.
"""

from rh_mcp.lib.rh_client import client


def get_notifications(count: int = 20) -> dict:
    """Get the most recent Robinhood notifications. Price alert fires appear here."""
    rh = client()
    notifs = rh.get_notifications() or []
    out = []
    for n in notifs[:count]:
        out.append({
            "id": n.get("id"),
            "title": n.get("title"),
            "message": n.get("message"),
            "type": n.get("type"),
            "fired_at": n.get("time"),
            "read": n.get("read"),
            "action_url": n.get("action"),
        })
    return {"success": True, "count": len(out), "notifications": out}


def get_watchlists() -> dict:
    """List all Robinhood watchlists."""
    rh = client()
    wls = rh.get_all_watchlists() or {}
    results = wls.get("results", []) if isinstance(wls, dict) else wls
    return {
        "success": True,
        "count": len(results),
        "watchlists": [
            {
                "name": w.get("display_name") or w.get("name"),
                "id": w.get("id"),
                "url": w.get("url"),
            }
            for w in results
        ],
    }


def add_to_watchlist(watchlist_name: str, ticker: str) -> dict:
    """Add a ticker to a named watchlist."""
    rh = client()
    sym = ticker.strip().upper()
    result = rh.post_symbols_to_watchlist(sym, name=watchlist_name)
    return {"success": True, "watchlist": watchlist_name, "added": sym, "raw": result}


def remove_from_watchlist(watchlist_name: str, ticker: str) -> dict:
    """Remove a ticker from a named watchlist."""
    rh = client()
    sym = ticker.strip().upper()
    result = rh.delete_symbols_from_watchlist(sym, name=watchlist_name)
    return {"success": True, "watchlist": watchlist_name, "removed": sym, "raw": result}
