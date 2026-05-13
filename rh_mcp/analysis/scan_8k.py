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


def _enrich_with_items(filing: dict, with_body_keywords: bool = False) -> dict:
    """Fetch item codes for one filing — slow call (HTTP per filing).

    If with_body_keywords=True, ALSO fetch the filing body documents and run
    the keyword pattern library against them. Adds `body_signals` field with
    per-pattern hits and `body_direction` / `body_confidence` synthesis.
    """
    url = filing.get("filing_url", "")
    items = _edgar.filing_items(url)
    filing["item_codes"] = items
    filing["item_direction"] = _classify(items)

    if with_body_keywords:
        from rh_mcp.analysis import edgar_keywords as _kw
        hits = _kw.scan_filing_body(url)
        filing["body_signals"] = [
            {"pattern": h.pattern_name, "direction": h.direction, "weight": h.weight, "context": h.context[:160]}
            for h in hits
        ]
        body_dir, body_conf = _kw.synthesize_direction(hits)
        filing["body_direction"] = body_dir
        filing["body_confidence"] = body_conf
    else:
        filing["body_signals"] = []
        filing["body_direction"] = None
        filing["body_confidence"] = 0.0

    # Final direction: prefer body signal if confidence >= 0.7, else item code direction
    if filing["body_direction"] and filing["body_confidence"] >= 0.7:
        filing["direction"] = filing["body_direction"]
        filing["direction_source"] = "body_keywords"
    else:
        filing["direction"] = filing["item_direction"]
        filing["direction_source"] = "item_codes"

    return filing


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    item_codes: list[str] | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    recent_filings_count: int = DEFAULT_RECENT_FILINGS_COUNT,
    top_n: int = DEFAULT_TOP_N,
    deep_scan: bool = False,
    use_finbert: bool = True,
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

    # Step 4: enrich with item codes (HTTP per filing — parallel-bounded).
    # If deep_scan=True, also pull filing bodies + run keyword pattern library
    # to identify high-signal phrases like "non-reliance", "going concern",
    # "material adverse change" — the digestive-drift edge layer.
    enriched: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_enrich_with_items, f, deep_scan) for f in tradable]
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

    # Step 7 (deep_scan only): historical-reaction classifier is the primary
    # arbiter, FinBERT is fallback. The historical classifier asks: "how did
    # the market react to past filings with this same item-codes + body-keyword
    # signature?" — ground-truth labels, not inferred sentiment. FinBERT only
    # gets a vote when no historical matches exist.
    if deep_scan and matched:
        from rh_mcp.analysis import edgar_history_classifier as _hc
        for f in matched:
            acc = f.get("accession_no")
            if not acc:
                continue
            hc_res = _hc.classify_8k_historical(
                accession_no=acc,
                ticker=f.get("ticker"),
                filing_url=f.get("filing_url"),
                cik=f.get("cik"),
            )
            if hc_res.get("success"):
                f["historical_direction"] = hc_res.get("direction")
                f["historical_confidence"] = hc_res.get("confidence")
                f["historical_reasoning"] = hc_res.get("reasoning")
                f["historical_stats"] = hc_res.get("stats")
                if (
                    hc_res.get("direction") in ("long", "short")
                    and hc_res.get("confidence", 0) >= 0.5
                ):
                    f["direction"] = hc_res["direction"]
                    f["direction_source"] = "historical_reactions"
                    continue
            else:
                f["historical_error"] = hc_res.get("error")

            # Fallback: FinBERT sentiment on the filing body. Off by default in
            # live-scan paths (use_finbert=False) because the model load +
            # per-chunk CPU inference adds 30-90 sec per ambiguous filing —
            # unacceptable when scan_bullish_8k is called interactively.
            # Backtest / batch paths can re-enable it for fuller coverage.
            if not use_finbert:
                continue
            from rh_mcp.analysis import edgar_finbert_classifier as _fb
            fb_res = _fb.classify_8k_filing(
                accession_no=acc, ticker=f.get("ticker"),
                filing_url=f.get("filing_url"), cik=f.get("cik"),
            )
            if not fb_res.get("success"):
                f["finbert_error"] = fb_res.get("error")
                continue
            f["finbert_direction"] = fb_res.get("direction")
            f["finbert_confidence"] = fb_res.get("confidence")
            f["finbert_reasoning"] = fb_res.get("reasoning")
            if (
                fb_res.get("direction") in ("long", "short")
                and fb_res.get("confidence", 0) >= 0.7
            ):
                f["direction"] = fb_res["direction"]
                f["direction_source"] = "finbert_fallback"

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
