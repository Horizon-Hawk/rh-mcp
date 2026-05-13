"""Social sentiment tools — StockTwits integration.

Public endpoints, no auth required. Rate limit is roughly 200 requests per hour
per IP for unauthenticated access. For higher volume, register for a free dev
key at https://api.stocktwits.com and pass via env var (not yet wired).
"""

import requests


_STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
_HEADERS = {"User-Agent": "rh-mcp/1.0 (trading-research)"}


def get_stocktwits_pulse(ticker: str, limit: int = 30) -> dict:
    """Recent StockTwits messages for a ticker plus bull/bear sentiment summary.

    Args:
        ticker: Stock symbol.
        limit:  Max messages to return (1-30 typical; StockTwits caps at 30).

    Returns:
        success, symbol, message_count, bull/bear/neutral counts + percentages,
        and a list of recent messages with author, sentiment tag, follower count.
    """
    sym = ticker.strip().upper()
    url = f"{_STOCKTWITS_BASE}/streams/symbol/{sym}.json"

    try:
        response = requests.get(url, params={"limit": limit}, headers=_HEADERS, timeout=10)
    except Exception as e:
        return {"success": False, "error": f"request failed: {e}"}

    if response.status_code == 429:
        return {"success": False, "error": "rate limited (~200/hr per IP); try again in ~1 min"}
    if response.status_code == 404:
        return {"success": False, "error": f"ticker {sym} not found on StockTwits"}
    if response.status_code != 200:
        return {"success": False, "error": f"HTTP {response.status_code}", "body": response.text[:200]}

    try:
        data = response.json()
    except Exception as e:
        return {"success": False, "error": f"json parse failed: {e}"}

    messages = data.get("messages", []) or []

    bull_count = 0
    bear_count = 0
    neutral_count = 0
    out_messages = []
    for m in messages[:limit]:
        sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if sentiment == "Bullish":
            bull_count += 1
        elif sentiment == "Bearish":
            bear_count += 1
        else:
            neutral_count += 1
        user = m.get("user") or {}
        out_messages.append({
            "id": m.get("id"),
            "created_at": m.get("created_at"),
            "body": (m.get("body") or "")[:280],
            "sentiment": sentiment,
            "username": user.get("username"),
            "followers": user.get("followers"),
            "official": user.get("official"),
        })

    total = len(messages)
    tagged = bull_count + bear_count
    bull_pct = round(100 * bull_count / total, 1) if total else 0.0
    bear_pct = round(100 * bear_count / total, 1) if total else 0.0
    net_bull_pct = round(100 * (bull_count - bear_count) / tagged, 1) if tagged else 0.0

    return {
        "success": True,
        "symbol": sym,
        "message_count": total,
        "bullish_count": bull_count,
        "bearish_count": bear_count,
        "neutral_count": neutral_count,
        "bullish_pct": bull_pct,
        "bearish_pct": bear_pct,
        "net_bullish_pct": net_bull_pct,  # +100 = all bull, -100 = all bear, 0 = split
        "messages": out_messages,
    }


def scan_stocktwits_trending(limit: int = 15) -> dict:
    """Currently trending tickers on StockTwits.

    Returns the symbols with the highest activity in the last ~5 minutes.
    """
    url = f"{_STOCKTWITS_BASE}/trending/symbols.json"

    try:
        response = requests.get(url, params={"limit": limit}, headers=_HEADERS, timeout=10)
    except Exception as e:
        return {"success": False, "error": f"request failed: {e}"}

    if response.status_code == 429:
        return {"success": False, "error": "rate limited (~200/hr per IP); try again in ~1 min"}
    if response.status_code != 200:
        return {"success": False, "error": f"HTTP {response.status_code}", "body": response.text[:200]}

    try:
        data = response.json()
    except Exception as e:
        return {"success": False, "error": f"json parse failed: {e}"}

    symbols = data.get("symbols", []) or []

    return {
        "success": True,
        "count": len(symbols),
        "trending": [
            {
                "symbol": s.get("symbol"),
                "title": s.get("title"),
                "watchlist_count": s.get("watchlist_count"),
                "industry": s.get("industry"),
            }
            for s in symbols[:limit]
        ],
    }
