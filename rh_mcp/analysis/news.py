"""News feed per ticker — catalyst-classified, age-flagged.

Ported from news.py CLI script. Uses robin_stocks (shared login via rh_mcp.auth)
and optionally a news cache file (populated by an external monitor process).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

# Configurable cache location — if a monitor process keeps this fresh, fetch_ticker_news
# uses it; otherwise falls back to live API.
NEWS_CACHE_PATH = Path(os.environ.get("RH_NEWS_CACHE", r"C:\Users\algee\news_cache.json"))


CATALYST_PATTERNS = [
    ("ACQUISITION", r"acqui[rs]|merger|merge with|takeover|buyout|to buy\b|bid for|offer to (buy|acquire)|deal to acquire|in talks to acquire|definitive agreement|going private|spin[- ]?off|divest"),
    ("DOWNGRADE", r"downgrad|cut.*target|lower.*target|target.*cut|target.*lower|reduc.*target"),
    ("UPGRADE",   r"upgrad|rais.*target|target.*rais|increas.*target|target.*increas"),
    ("WARNING",   r"warn|guidance.*cut|lower.*guidance|withdraw.*guidance|miss.*guidance|below.*expect"),
    ("BEAT",      r"\bbeat\b|top.*estimate|exceed.*expect|better.*expect|above.*estimate"),
    ("MISS",      r"\bmiss\b|below.*estimate|fell short|disappoint"),
    ("FDA",       r"\bfda\b|approval|regulatory|clinical|trial result"),
    ("INSIDER",   r"insider|10-[bk]\b|form 4|\bbuying\b.*share|\bselling\b.*share"),
]


def _load_news_cache() -> dict:
    if not NEWS_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(NEWS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def classify(title: str, preview: str) -> str:
    text = (title + " " + preview).lower()
    for label, pattern in CATALYST_PATTERNS:
        if re.search(pattern, text):
            return label
    return "NEWS"


def _age_label(published_at: str) -> tuple[str, int]:
    """Returns (label, hours_ago)."""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours = int((now - dt).total_seconds() / 3600)
        days = hours // 24
        if days == 0:
            return "TODAY", hours
        elif days <= 3:
            return "RECENT", hours
        else:
            return "OLD", hours
    except Exception:
        return "?", 0


def _from_cache(ticker: str, cache: dict) -> list[dict] | None:
    if not (cache and ticker in cache and cache[ticker]):
        return None
    out = []
    for item in cache[ticker]:
        pub = item.get("published", "")
        try:
            dt = datetime.fromisoformat(pub.replace(" ", "T") + ":00+00:00") if pub else None
            now = datetime.now(timezone.utc)
            hours = int((now - dt).total_seconds() / 3600) if dt else 999
        except Exception:
            hours = 999
        age = _age_label((pub.replace(" ", "T") + "Z") if pub else "")[0]
        out.append({
            "ticker": ticker,
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "preview": item.get("preview", ""),
            "published": item.get("published", ""),
            "hours_ago": hours,
            "age": age,
            "catalyst": item.get("catalyst", classify(item.get("title", ""), item.get("preview", ""))),
        })
    out.sort(key=lambda x: x["hours_ago"])
    return out


def _fetch_live(ticker: str) -> list[dict]:
    login()
    raw = rh.stocks.get_news(ticker) or []
    out = []
    for item in raw:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        preview = item.get("preview_text") or item.get("summary") or ""
        pub = item.get("published_at", "")
        age, hours = _age_label(pub)
        out.append({
            "ticker": ticker,
            "title": title,
            "source": item.get("source", ""),
            "preview": preview[:200].strip(),
            "published": pub[:10] if pub else "",
            "hours_ago": hours,
            "age": age,
            "catalyst": classify(title, preview),
        })
    out.sort(key=lambda x: x["hours_ago"])
    return out


def analyze(tickers: list[str] | str, count: int | None = None) -> list[dict]:
    """News headlines for one or more tickers, classified and age-flagged.

    Returns a flat list across all tickers (mirrors the legacy CLI's JSON shape).
    If ``count`` is given, each ticker's slice is trimmed to N items.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    cache = _load_news_cache()

    flat = []
    for t in cleaned:
        items = _from_cache(t, cache)
        if items is None:
            items = _fetch_live(t)
        if count is not None:
            items = items[:count]
        flat.extend(items)
    return flat
