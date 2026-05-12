"""8-K filing scanner — surfaces recent SEC filings with high-signal item codes.

Hypothesis: market takes 1-5 days to fully digest 8-K material events.
Specific item codes have known directional bias:

  1.01 Entry into Material Definitive Agreement       — LONG signal (deal news)
  2.02 Results of Operations / Financial Condition    — Earnings (already covered by scan_pead)
  3.02 Unregistered Sales of Equity Securities        — SHORT signal (dilution)
  4.01 Changes in Registrant's Certifying Accountant  — SHORT signal (auditor change)
  4.02 Non-Reliance on Previously Issued Financials   — SHORT signal (restatement)
  5.02 Departure of Directors or Officers             — Context-dependent
  8.01 Other Events                                   — Catch-all, needs NLP

This scanner polls EDGAR's recent 8-K Atom feed, filters by item codes,
cross-references with the input ticker list (only tradable names), and
returns candidates classified long/short.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rh_mcp.analysis import edgar_client as _edgar

# Direction mapping per item code
LONG_ITEM_CODES = {"1.01"}      # Material Definitive Agreement = positive deal
SHORT_ITEM_CODES = {"3.02", "4.01", "4.02"}  # Dilution / Auditor / Restatement
DEFAULT_ITEM_CODES = sorted(LONG_ITEM_CODES | SHORT_ITEM_CODES)

DEFAULT_LOOKBACK_MINUTES = 240   # last 4 hours by default
DEFAULT_RECENT_FILINGS_COUNT = 100  # cap on Atom feed pull

DEFAULT_TOP_N = 20


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


def _classify(item_codes: list[str]) -> str | None:
    """Direction classification. Long-bias item beats short-bias on tied filings."""
    items = set(item_codes)
    if items & LONG_ITEM_CODES:
        return "long"
    if items & SHORT_ITEM_CODES:
        return "short"
    return None


def _enrich_with_items(filing: dict) -> dict:
    """Fetch item codes for one filing — slow call (HTTP per filing)."""
    items = _edgar.filing_items(filing.get("filing_url", ""))
    filing["item_codes"] = items
    filing["direction"] = _classify(items)
    return filing


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    item_codes: list[str] | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    recent_filings_count: int = DEFAULT_RECENT_FILINGS_COUNT,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan recent 8-K filings for high-signal item codes on tradable tickers.

    Filter chain:
    1. Pull recent_filings_count most recent 8-Ks from EDGAR Atom feed
    2. Drop filings older than lookback_minutes
    3. Cross-reference with the input ticker universe (only names you can trade)
    4. Fetch item codes for each surviving filing (HTTP per filing — bounded)
    5. Filter by item_codes (default: 1.01, 3.02, 4.01, 4.02)
    6. Classify direction (long if 1.01 present, else short for the others)
    """
    # Load + normalize universe
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = {t.strip().upper() for t in tickers if t and t.strip()}

    if item_codes is None:
        item_codes = DEFAULT_ITEM_CODES
    item_codes_set = set(item_codes)

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Step 1: pull recent 8-Ks
    try:
        recent = _edgar.recent_8k_filings(count=recent_filings_count)
    except Exception as e:
        return {"success": False, "error": f"EDGAR feed fetch failed: {e}"}

    # Step 2: filter by lookback window
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    fresh: list[dict] = []
    for f in recent:
        ts = f.get("filed_at")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except Exception:
            dt = None
        if dt is None or dt < cutoff:
            continue
        fresh.append(f)

    # Step 3: ticker filter via CIK lookup
    try:
        cik_to_ticker = _edgar.get_cik_to_ticker()
    except Exception:
        cik_to_ticker = {}
    tradable: list[dict] = []
    for f in fresh:
        cik = f.get("cik")
        if not cik:
            continue
        ticker = cik_to_ticker.get(cik)
        if not ticker or ticker not in tickers:
            continue
        f["ticker"] = ticker
        tradable.append(f)

    # Step 4: enrich with item codes (HTTP per filing — parallel-bounded)
    enriched: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_enrich_with_items, f) for f in tradable]
        for fut in concurrent.futures.as_completed(futures):
            enriched.append(fut.result())

    # Step 5: item code filter
    matched: list[dict] = []
    for f in enriched:
        if not f.get("item_codes"):
            continue
        if not (set(f["item_codes"]) & item_codes_set):
            continue
        matched.append(f)

    # Step 6: sort by filing time descending (newest first)
    matched.sort(key=lambda f: f.get("filed_at", ""), reverse=True)
    if top_n and len(matched) > top_n:
        matched = matched[:top_n]

    longs = [m for m in matched if m.get("direction") == "long"]
    shorts = [m for m in matched if m.get("direction") == "short"]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recent_filings_pulled": len(recent),
        "in_lookback_window": len(fresh),
        "in_ticker_universe": len(tradable),
        "matched_item_codes": len(matched),
        "long_count": len(longs),
        "short_count": len(shorts),
        "candidates": matched,
    }
