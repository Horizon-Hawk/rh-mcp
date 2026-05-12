"""SEC EDGAR API client — foundation for 8-K monitoring + future SEC-data scanners.

EDGAR access rules (read these before debugging mysterious failures):
- User-Agent header is MANDATORY. SEC blocks requests without it. Set
  RH_EDGAR_USER_AGENT env var to "Your Name your@email.com" or similar.
- Rate limit: 10 req/sec, enforced server-side. We rate-limit client-side too.
- All endpoints free, no API key, no auth.

Endpoints this client wraps:
1. company_tickers — CIK to ticker mapping (cached daily)
2. recent_8k_feed — Atom RSS of latest 8-K filings across all companies
3. filing_items — parse Item codes from a specific filing's index page
4. company_submissions — per-ticker filing history (used by future scanners)

Cache layout: $RH_EDGAR_CACHE_DIR/<cache_key>.json (default ~/.edgar_cache).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

DEFAULT_USER_AGENT = "rh-mcp researcher noreply@example.com"
EDGAR_BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"

USER_AGENT = os.environ.get("RH_EDGAR_USER_AGENT", DEFAULT_USER_AGENT)
CACHE_DIR = Path(os.environ.get("RH_EDGAR_CACHE_DIR", Path.home() / ".edgar_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Client-side rate limit — SEC ceiling is 10 req/sec
_MIN_REQUEST_INTERVAL_SECS = 0.15  # ~6 req/sec, conservative
_last_request_time: float = 0.0


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL_SECS:
        time.sleep(_MIN_REQUEST_INTERVAL_SECS - elapsed)
    _last_request_time = time.time()


def _http_get(url: str, accept: str = "*/*") -> bytes:
    """Make a rate-limited GET request with required EDGAR headers."""
    _rate_limit()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            # No Accept-Encoding — urllib doesn't auto-decompress and adding
            # decompression for marginal bandwidth gain isn't worth the code.
            "Host": urllib.parse.urlparse(url).hostname,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _cached_json(cache_key: str, ttl_hours: float, fetch_fn) -> dict:
    """Read from cache if fresh, else call fetch_fn and write to cache."""
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < ttl_hours:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
    data = fetch_fn()
    try:
        cache_path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return data


# ---------------------------------------------------------------------------
# CIK ↔ ticker mapping
# ---------------------------------------------------------------------------

def get_ticker_to_cik() -> dict[str, str]:
    """Return {ticker: cik_zero_padded_10_digits}. Cached for 24h."""
    def _fetch():
        raw = _http_get(f"{EDGAR_BASE}/files/company_tickers.json", accept="application/json")
        data = json.loads(raw)
        # File structure: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        return data

    data = _cached_json("company_tickers", ttl_hours=24, fetch_fn=_fetch)
    out: dict[str, str] = {}
    for row in data.values():
        t = (row.get("ticker") or "").upper()
        cik = row.get("cik_str")
        if t and cik is not None:
            out[t] = str(cik).zfill(10)
    return out


def get_cik_to_ticker() -> dict[str, str]:
    return {cik: ticker for ticker, cik in get_ticker_to_cik().items()}


# ---------------------------------------------------------------------------
# Recent 8-K feed
# ---------------------------------------------------------------------------

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def recent_8k_filings(count: int = 100) -> list[dict]:
    """Pull the most recent 8-K filings across all SEC filers from the Atom feed.

    Returns list of {accession_no, cik, form, title, filed_at (ISO), filing_url}.
    Does NOT include item codes — call filing_items() per accession to get those.
    """
    url = (
        f"{EDGAR_BASE}/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
        f"&company=&dateb=&owner=include&count={count}&output=atom"
    )
    raw = _http_get(url, accept="application/atom+xml")
    root = ET.fromstring(raw)
    out: list[dict] = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=_ATOM_NS) or "").strip()
        link_el = entry.find("a:link", _ATOM_NS)
        href = link_el.get("href") if link_el is not None else ""
        # Title format: "8-K - <Company Name> (0001234567) (Filer)" — CIK is the
        # parenthesized 10-digit (or shorter, zero-padded) number near the end.
        m = re.search(r"\((\d{7,10})\)\s*\(Filer\)?", title)
        cik = m.group(1).zfill(10) if m else None
        # Accession number from URL — e.g. ".../1234567-25-001234-index.htm"
        m2 = re.search(r"/(\d+-\d+-\d+)-index", href)
        accession_no = m2.group(1) if m2 else None
        out.append({
            "accession_no": accession_no,
            "cik": cik,
            "form": "8-K",
            "title": title,
            "filed_at": updated,
            "filing_url": href,
        })
    return out


# ---------------------------------------------------------------------------
# Item code extraction
# ---------------------------------------------------------------------------

_ITEM_PATTERN = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)


def filing_items(filing_url: str) -> list[str]:
    """Fetch an 8-K filing's index page and extract its Item codes.

    Index pages list 'Item X.XX Description' for each item filed. We scrape
    these and return sorted unique codes like ['1.01', '9.01'].
    """
    if not filing_url:
        return []
    try:
        html = _http_get(filing_url, accept="text/html").decode("utf-8", errors="ignore")
    except Exception:
        return []
    found = set(_ITEM_PATTERN.findall(html))
    return sorted(found)


# ---------------------------------------------------------------------------
# Per-company submission history (for future scanners)
# ---------------------------------------------------------------------------

def company_submissions(cik: str) -> dict | None:
    """Pull recent filings JSON for a CIK. Caller is responsible for caching
    if querying many companies — this endpoint is rate-limit-friendly but
    high-frequency polling will hit limits.
    """
    if not cik:
        return None
    cik_padded = cik.zfill(10)
    url = f"{DATA_BASE}/submissions/CIK{cik_padded}.json"
    try:
        raw = _http_get(url, accept="application/json")
        return json.loads(raw)
    except Exception:
        return None
