"""Social sentiment tools — StockTwits (Cloudflare-blocked) + Apewisdom.

StockTwits unauth endpoint is now Cloudflare-walled — left in place but expect
HTTP 403. For working social data use the Apewisdom functions below.

Apewisdom (https://apewisdom.io/api/) aggregates Reddit (WSB, stocks, options,
penny stocks, etc.) plus light Twitter. No auth required, no documented rate
limit but treat as ~60 req/min to be safe.
"""

import requests


_STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
_APEWISDOM_BASE = "https://apewisdom.io/api/v1.0"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://stocktwits.com/",
    "Origin": "https://stocktwits.com",
}

_APEWISDOM_FILTERS = {
    "all-stocks", "all-crypto", "wallstreetbets", "stocks", "stockmarket",
    "options", "spacs", "shortsqueeze", "pennystocks", "robinhoodpennystocks",
    "cryptocurrency", "satoshistreetbets", "twitter", "yolo",
}


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


# ---------------------------------------------------------------------------
# Apewisdom — Reddit + light Twitter aggregator (no auth, works today)
# ---------------------------------------------------------------------------

def scan_apewisdom_trending(filter: str = "all-stocks", limit: int = 25) -> dict:
    """Top mentioned tickers on Reddit + Twitter via Apewisdom.

    Args:
        filter: Source filter. Common values:
                "all-stocks"  - all stock subreddits (default)
                "wallstreetbets"
                "stocks"
                "stockmarket"
                "options"
                "spacs"
                "shortsqueeze"
                "pennystocks"
                "robinhoodpennystocks"
                "twitter"  - light Twitter coverage
        limit:  Max tickers to return (Apewisdom returns 50/page; clamped to 50).

    Returns each ticker's rank, mentions, mentions_24h_ago (delta), upvotes,
    sentiment score (-1 to +1), and sentiment label.
    """
    filter = (filter or "all-stocks").strip().lower()
    if filter not in _APEWISDOM_FILTERS:
        return {
            "success": False,
            "error": f"unknown filter '{filter}'. Valid: {sorted(_APEWISDOM_FILTERS)}",
        }
    if limit < 1:
        limit = 25
    limit = min(limit, 50)

    url = f"{_APEWISDOM_BASE}/filter/{filter}/page/1"
    try:
        r = requests.get(url, headers={"User-Agent": _HEADERS["User-Agent"]}, timeout=10)
    except Exception as e:
        return {"success": False, "error": f"request failed: {e}"}
    if r.status_code != 200:
        return {"success": False, "error": f"HTTP {r.status_code}", "body": r.text[:200]}

    try:
        data = r.json()
    except Exception as e:
        return {"success": False, "error": f"json parse failed: {e}"}

    results = data.get("results", []) or []
    rows = []
    for x in results[:limit]:
        try:
            mentions = int(x.get("mentions") or 0)
            mentions_prior = int(x.get("mentions_24h_ago") or 0)
        except (TypeError, ValueError):
            mentions, mentions_prior = 0, 0
        mention_delta_pct = (
            round(100 * (mentions - mentions_prior) / mentions_prior, 1)
            if mentions_prior else None
        )
        try:
            rank = int(x.get("rank") or 0)
            rank_prior = int(x.get("rank_24h_ago") or 0)
        except (TypeError, ValueError):
            rank, rank_prior = 0, 0
        rank_delta = (rank_prior - rank) if (rank and rank_prior) else None

        rows.append({
            "ticker": x.get("ticker"),
            "name": x.get("name"),
            "rank": rank,
            "rank_24h_ago": rank_prior,
            "rank_delta": rank_delta,  # positive = climbing, negative = falling
            "mentions": mentions,
            "mentions_24h_ago": mentions_prior,
            "mention_delta_pct": mention_delta_pct,
            "upvotes": x.get("upvotes"),
            "sentiment_score": x.get("sentiment_score"),
            "sentiment": x.get("sentiment"),
        })

    return {
        "success": True,
        "filter": filter,
        "count": len(rows),
        "total_available": data.get("count"),
        "trending": rows,
    }


def get_apewisdom_ticker(ticker: str, filter: str = "all-stocks") -> dict:
    """Apewisdom data for a specific ticker — searches the trending list.

    If the ticker is in the top ~50 currently trending under the chosen filter,
    returns full sentiment + mention deltas. Otherwise returns success=False with
    rank info (not on the trending list).
    """
    sym = ticker.strip().upper()
    filter = (filter or "all-stocks").strip().lower()
    if filter not in _APEWISDOM_FILTERS:
        return {
            "success": False,
            "error": f"unknown filter '{filter}'. Valid: {sorted(_APEWISDOM_FILTERS)}",
        }

    # Apewisdom doesn't have a per-ticker endpoint; search the trending list.
    # Try first 2 pages (top 100) to give a reasonable chance of finding the ticker.
    found = None
    rank_seen = 0
    for page in (1, 2):
        url = f"{_APEWISDOM_BASE}/filter/{filter}/page/{page}"
        try:
            r = requests.get(url, headers={"User-Agent": _HEADERS["User-Agent"]}, timeout=10)
        except Exception as e:
            return {"success": False, "error": f"request failed: {e}"}
        if r.status_code != 200:
            return {"success": False, "error": f"HTTP {r.status_code}"}
        try:
            data = r.json()
        except Exception as e:
            return {"success": False, "error": f"json parse failed: {e}"}
        for x in data.get("results", []) or []:
            if (x.get("ticker") or "").upper() == sym:
                found = x
                break
            try:
                rank_seen = max(rank_seen, int(x.get("rank") or 0))
            except (TypeError, ValueError):
                pass
        if found:
            break

    if not found:
        return {
            "success": False,
            "symbol": sym,
            "filter": filter,
            "error": "ticker not in current top 100 trending list",
            "max_rank_searched": rank_seen,
            "interpretation": "no meaningful social signal — ticker is not catching attention",
        }

    try:
        mentions = int(found.get("mentions") or 0)
        mentions_prior = int(found.get("mentions_24h_ago") or 0)
        rank = int(found.get("rank") or 0)
        rank_prior = int(found.get("rank_24h_ago") or 0)
    except (TypeError, ValueError):
        mentions, mentions_prior, rank, rank_prior = 0, 0, 0, 0

    return {
        "success": True,
        "symbol": sym,
        "filter": filter,
        "name": found.get("name"),
        "rank": rank,
        "rank_24h_ago": rank_prior,
        "rank_delta": (rank_prior - rank) if (rank and rank_prior) else None,
        "mentions": mentions,
        "mentions_24h_ago": mentions_prior,
        "mention_delta_pct": (
            round(100 * (mentions - mentions_prior) / mentions_prior, 1)
            if mentions_prior else None
        ),
        "upvotes": found.get("upvotes"),
        "sentiment_score": found.get("sentiment_score"),
        "sentiment": found.get("sentiment"),
    }
