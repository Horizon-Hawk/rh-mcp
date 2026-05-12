"""Buyback announcement drift scanner.

Stocks that announce share buybacks ≥3% of market cap drift higher 3-6 months
post-announcement (Ikenberry, Lakonishok, Vermaelen 1995 + replications).
8-12% excess return over 4 years, with ~3-5% in the first 6 months.

Detection:
- Scan recent news for each ticker, look for BUYBACK catalyst
- Filter for "≥X% authorization" or named dollar amounts
- Optional: weight by company size / debt trajectory

Caveats:
- News classification has false positives (any "buyback" mention triggers)
- Want NEW authorizations, not continuations or completions
- Long holding period (3-6 months) doesn't fit Phase 3 short-hold style
  cleanly — treat output as a "swing watch list" not direct entry triggers
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

MAX_DAYS_SINCE_ANNOUNCEMENT = 14   # only recent announcements
MIN_PRICE = 5.0
MIN_MARKET_CAP = 500_000_000       # filter penny stocks / shell companies

DEFAULT_TOP_N = 15
_BATCH_SIZE = 75
_BATCH_SLEEP = 0.1

# Patterns that indicate a NEW buyback authorization (not continuation/completion)
BUYBACK_NEW_PATTERNS = [
    r"announc[es]+ (?:new )?(?:share )?(?:repurchase|buyback) (?:program|authoriz)",
    r"board (?:approves?|authorizes?) (?:new )?(?:share )?(?:repurchase|buyback)",
    r"(?:new )?(?:share )?(?:repurchase|buyback) of \$\d+",
    r"\$\d+(?:\.\d+)?\s*(?:billion|million)\s*(?:share )?(?:repurchase|buyback)",
    r"increase[ds] (?:share )?(?:repurchase|buyback) (?:program|authoriz)",
]
BUYBACK_NEW_RE = re.compile("|".join(BUYBACK_NEW_PATTERNS), re.IGNORECASE)

# Patterns to EXCLUDE (continuations, completions, generic mentions)
BUYBACK_EXCLUDE_PATTERNS = [
    r"complete[ds]? (?:share )?(?:repurchase|buyback)",
    r"(?:share )?(?:repurchase|buyback) of \$\d+\s*million completed",
]
BUYBACK_EXCLUDE_RE = re.compile("|".join(BUYBACK_EXCLUDE_PATTERNS), re.IGNORECASE)


def _load_universe_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(tickers))


def _extract_amount(text: str) -> float | None:
    """Pull dollar amount from headline like '$10 billion buyback'. Returns USD amount."""
    m = re.search(r"\$(\d+(?:\.\d+)?)\s*(billion|million|b|m)\b", text, re.IGNORECASE)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("b"):
        return amount * 1_000_000_000
    return amount * 1_000_000


def _scan_news_one(ticker: str, max_days: int) -> dict | None:
    """Find buyback announcement in this ticker's recent news, if any."""
    try:
        raw = rh.stocks.get_news(ticker) or []
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    best = None
    for art in raw:
        title = (art.get("title") or "").strip()
        preview = (art.get("preview_text") or art.get("summary") or "").strip()
        text = f"{title} {preview}"
        if not BUYBACK_NEW_RE.search(text):
            continue
        if BUYBACK_EXCLUDE_RE.search(text):
            continue
        pub_raw = art.get("published_at", "")
        try:
            dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            days_ago = (now - dt).total_seconds() / 86400
        except Exception:
            continue
        if days_ago > max_days:
            continue
        amount_usd = _extract_amount(text)
        if best is None or days_ago < best["days_ago"]:
            best = {
                "title": title,
                "source": art.get("source", ""),
                "published_at": pub_raw,
                "days_ago": round(days_ago, 1),
                "amount_usd": amount_usd,
            }
    return best


def _fetch_market_data(tickers: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # Fundamentals for market cap
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_fundamentals(chunk, info=None) or []
            quotes = rh.stocks.get_quotes(chunk, info=None) or []
        except Exception:
            results, quotes = [], []
        quote_by_sym = {(q.get("symbol") or "").upper(): q for q in quotes if q}
        for sym, f in zip(chunk, results):
            if not f:
                continue
            try:
                market_cap = float(f.get("market_cap") or 0)
            except (ValueError, TypeError):
                market_cap = 0
            q = quote_by_sym.get(sym.upper(), {})
            try:
                price = float(q.get("last_trade_price") or 0)
            except (ValueError, TypeError):
                price = 0
            out[sym.upper()] = {
                "market_cap": market_cap,
                "price": price,
                "sector": f.get("sector"),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    max_days_since_announcement: int = MAX_DAYS_SINCE_ANNOUNCEMENT,
    min_price: float = MIN_PRICE,
    min_market_cap: float = MIN_MARKET_CAP,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find recent buyback announcements with size context."""
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")
    market = _fetch_market_data(tickers)

    # Parallel news scan — biggest API cost
    found: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_scan_news_one, t, max_days_since_announcement): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            r = fut.result()
            if r:
                found.append({"ticker": t, **r})

    candidates: list[dict] = []
    for f in found:
        m = market.get(f["ticker"], {})
        price = m.get("price", 0)
        market_cap = m.get("market_cap", 0)
        if price < min_price or market_cap < min_market_cap:
            continue
        # Size as % of market cap when amount is known
        size_pct_of_cap = None
        if f.get("amount_usd") and market_cap > 0:
            size_pct_of_cap = round(f["amount_usd"] / market_cap * 100, 2)
        candidates.append({
            "ticker": f["ticker"],
            "price": price,
            "market_cap": market_cap,
            "announcement_days_ago": f["days_ago"],
            "headline": f["title"],
            "source": f["source"],
            "amount_usd": f.get("amount_usd"),
            "size_pct_of_market_cap": size_pct_of_cap,
            "sector": m.get("sector"),
        })

    # Rank by size as % of market cap (larger = stronger signal). Names without
    # an extractable amount go last.
    candidates.sort(key=lambda x: (x.get("size_pct_of_market_cap") or 0), reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "candidates": candidates,
        "note": "Holding period 3-6 months per literature. Treat as swing watch list, not direct entry trigger.",
    }
