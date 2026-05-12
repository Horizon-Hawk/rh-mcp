"""Classify a new 8-K filing by nearest-signature lookup against the historical
corpus produced by edgar_history_builder.

Matching strategy (tiered, returns first non-empty result):
1. EXACT item_codes signature + body_keywords overlap (Jaccard >= 0.3)
2. EXACT item_codes signature (no body filter)
3. item_codes Jaccard overlap >= 0.5 (handles novel item combinations)

Aggregation: average next-day return across matched filings → direction.
  avg_return > +0.5%  → long
  avg_return < -0.5%  → short
  else                → neutral
Confidence = min(1.0, |avg_return| / 0.03) × min(1.0, n_matches / 5)

The size penalty ensures a 1-match outlier doesn't pretend to be a confident
classification. n=5+ is the threshold for full confidence on signal strength.
"""

from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from rh_mcp.analysis import edgar_client as _edgar
from rh_mcp.analysis import edgar_history_builder as _builder

CACHE_DIR = _edgar.CACHE_DIR / "history_classifications"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CSV_PATH = _builder.DEFAULT_CSV_PATH

# Aggregation thresholds
DIRECTION_THRESHOLD = 0.005   # |avg return| must exceed 0.5% to call a direction
FULL_SIGNAL_RETURN = 0.03     # 3% avg move = full signal-strength confidence
MIN_MATCHES_FOR_FULL_CONF = 5
MIN_MATCHES_FOR_ANY_CONF = 2  # under this, force neutral

# Tier 1 body keyword overlap threshold
BODY_JACCARD_THRESHOLD = 0.3
# Tier 3 item codes overlap threshold
ITEM_JACCARD_THRESHOLD = 0.5

# Lazy-loaded corpus
_corpus: list[dict] | None = None
_corpus_mtime: float = 0.0
_corpus_lock = threading.Lock()


def _load_corpus(csv_path: Path | None = None) -> list[dict]:
    """Load the corpus CSV. Re-reads if the file's mtime changed since last load."""
    global _corpus, _corpus_mtime
    path = Path(csv_path) if csv_path else DEFAULT_CSV_PATH
    with _corpus_lock:
        if not path.exists():
            _corpus = []
            _corpus_mtime = 0.0
            return _corpus
        mtime = path.stat().st_mtime
        if _corpus is not None and mtime == _corpus_mtime:
            return _corpus
        rows: list[dict] = []
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                # Cast numeric fields
                for k in ("t0_close", "t1_close", "t5_close",
                         "next_day_return", "five_day_return"):
                    v = r.get(k, "")
                    r[k] = float(v) if v not in (None, "") else None
                r["item_codes_set"] = frozenset(
                    c for c in (r.get("item_codes") or "").split("|") if c
                )
                r["body_keywords_set"] = frozenset(
                    k for k in (r.get("body_keywords") or "").split("|") if k
                )
                rows.append(r)
        _corpus = rows
        _corpus_mtime = mtime
        return _corpus


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _extract_features(
    filing_url: str | None,
    with_body_keywords: bool = True,
) -> tuple[frozenset, frozenset]:
    """Extract (item_codes, body_keywords) from a live filing URL."""
    items: frozenset = frozenset()
    body: frozenset = frozenset()
    if not filing_url:
        return items, body
    try:
        item_list = _edgar.filing_items(filing_url)
        items = frozenset(item_list)
    except Exception:
        pass
    if with_body_keywords:
        try:
            from rh_mcp.analysis import edgar_keywords as _kw
            hits = _kw.scan_filing_body(filing_url)
            body = frozenset(h.pattern_name for h in hits)
        except Exception:
            pass
    return items, body


def _find_matches(
    item_codes: frozenset,
    body_keywords: frozenset,
    corpus: list[dict],
    exclude_accession: str | None = None,
) -> tuple[list[dict], str]:
    """Return (matches, tier_name). Tries tier 1 → 2 → 3."""
    if not corpus:
        return [], "empty_corpus"
    candidates = [
        r for r in corpus
        if r["accession_no"] != exclude_accession
        and r.get("next_day_return") is not None
    ]
    if not candidates:
        return [], "no_labeled_rows"

    # Tier 1: exact item codes + body keyword Jaccard >= threshold
    if item_codes and body_keywords:
        tier1 = [
            r for r in candidates
            if r["item_codes_set"] == item_codes
            and _jaccard(r["body_keywords_set"], body_keywords) >= BODY_JACCARD_THRESHOLD
        ]
        if len(tier1) >= MIN_MATCHES_FOR_ANY_CONF:
            return tier1, "tier1_exact_items_body_jaccard"

    # Tier 2: exact item codes (any body)
    if item_codes:
        tier2 = [r for r in candidates if r["item_codes_set"] == item_codes]
        if len(tier2) >= MIN_MATCHES_FOR_ANY_CONF:
            return tier2, "tier2_exact_items"

    # Tier 3: item codes Jaccard overlap
    if item_codes:
        tier3 = [
            r for r in candidates
            if _jaccard(r["item_codes_set"], item_codes) >= ITEM_JACCARD_THRESHOLD
        ]
        if len(tier3) >= MIN_MATCHES_FOR_ANY_CONF:
            return tier3, "tier3_items_jaccard"

    return [], "no_matches"


def _aggregate(matches: list[dict]) -> dict:
    if not matches:
        return {
            "n_matches": 0,
            "avg_next_day_return": 0.0,
            "avg_five_day_return": None,
            "median_next_day_return": 0.0,
            "label_distribution": {},
        }
    t1_returns = [r["next_day_return"] for r in matches if r.get("next_day_return") is not None]
    t5_returns = [r["five_day_return"] for r in matches if r.get("five_day_return") is not None]
    avg_t1 = sum(t1_returns) / len(t1_returns) if t1_returns else 0.0
    avg_t5 = sum(t5_returns) / len(t5_returns) if t5_returns else None
    sorted_t1 = sorted(t1_returns)
    median_t1 = (
        sorted_t1[len(sorted_t1) // 2] if len(sorted_t1) % 2 == 1
        else (sorted_t1[len(sorted_t1) // 2 - 1] + sorted_t1[len(sorted_t1) // 2]) / 2
    ) if sorted_t1 else 0.0
    dist: dict[str, int] = {}
    for r in matches:
        label = r.get("label_t1") or "unknown"
        dist[label] = dist.get(label, 0) + 1
    return {
        "n_matches": len(matches),
        "avg_next_day_return": avg_t1,
        "avg_five_day_return": avg_t5,
        "median_next_day_return": median_t1,
        "label_distribution": dist,
    }


def _direction_from_stats(stats: dict) -> tuple[str, float]:
    n = stats["n_matches"]
    if n < MIN_MATCHES_FOR_ANY_CONF:
        return "neutral", 0.0
    avg = stats["avg_next_day_return"]
    if avg > DIRECTION_THRESHOLD:
        direction = "long"
    elif avg < -DIRECTION_THRESHOLD:
        direction = "short"
    else:
        return "neutral", round(min(1.0, n / MIN_MATCHES_FOR_FULL_CONF) * 0.3, 3)
    signal_strength = min(1.0, abs(avg) / FULL_SIGNAL_RETURN)
    size_penalty = min(1.0, n / MIN_MATCHES_FOR_FULL_CONF)
    confidence = signal_strength * size_penalty
    return direction, round(confidence, 3)


def _build_reasoning(direction: str, confidence: float, stats: dict, tier: str) -> str:
    n = stats["n_matches"]
    if n == 0:
        return f"No historical matches in corpus ({tier})."
    avg = stats["avg_next_day_return"]
    dist = stats["label_distribution"]
    dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items()))
    base = (
        f"{n} matching historical 8-Ks ({tier}). "
        f"Avg next-day return: {avg*100:+.2f}%. "
        f"Distribution: {dist_str}."
    )
    if direction == "neutral":
        return base + " No directional edge."
    return base + f" Direction: {direction} (confidence {confidence:.2f})."


def classify_8k_historical(
    accession_no: str,
    ticker: str | None = None,
    filing_url: str | None = None,
    cik: str | None = None,
    csv_path: str | None = None,
    with_body_keywords: bool = True,
    force_refresh: bool = False,
) -> dict:
    """Classify an 8-K filing by matching its signature against the historical
    reaction corpus.

    Returns: {success, accession_no, ticker, direction, confidence, reasoning,
    signature, matches_summary, top_matches, stats, source}.
    """
    cache_path = CACHE_DIR / f"{accession_no}.json"
    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except Exception:
            pass

    corpus = _load_corpus(Path(csv_path) if csv_path else None)
    if not corpus:
        return {
            "success": False,
            "error": (
                "no corpus loaded — run build_8k_history first "
                f"(expected at {DEFAULT_CSV_PATH})"
            ),
            "accession_no": accession_no,
        }

    # Resolve filing URL same way the FinBERT classifier does
    if not filing_url:
        if not cik and ticker:
            try:
                cik = _edgar.get_ticker_to_cik().get(ticker.upper())
            except Exception:
                cik = None
        if cik:
            cik_no_zeros = str(cik).lstrip("0") or "0"
            accession_clean = accession_no.replace("-", "")
            filing_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
                f"{accession_clean}/{accession_no}-index.htm"
            )

    if not filing_url:
        return {
            "success": False,
            "error": "could not resolve filing URL — provide filing_url, cik, or ticker",
            "accession_no": accession_no,
        }

    item_codes, body_keywords = _extract_features(filing_url, with_body_keywords)
    if not item_codes:
        return {
            "success": False,
            "error": "no item codes extracted from filing",
            "accession_no": accession_no,
            "filing_url": filing_url,
        }

    matches, tier = _find_matches(item_codes, body_keywords, corpus, exclude_accession=accession_no)
    stats = _aggregate(matches)
    direction, confidence = _direction_from_stats(stats)
    reasoning = _build_reasoning(direction, confidence, stats, tier)

    top_matches = [
        {
            "accession_no": m["accession_no"],
            "ticker": m["ticker"],
            "filing_date": m["filing_date"],
            "item_codes": m["item_codes"],
            "body_keywords": m["body_keywords"],
            "next_day_return": round(m["next_day_return"], 4) if m["next_day_return"] is not None else None,
            "label_t1": m["label_t1"],
        }
        for m in sorted(
            matches,
            key=lambda r: abs(r["next_day_return"]) if r.get("next_day_return") is not None else 0,
            reverse=True,
        )[:10]
    ]

    result = {
        "success": True,
        "accession_no": accession_no,
        "ticker": ticker.upper() if ticker else None,
        "filing_url": filing_url,
        "direction": direction,
        "confidence": confidence,
        "reasoning": reasoning,
        "signature": {
            "item_codes": sorted(item_codes),
            "body_keywords": sorted(body_keywords),
        },
        "stats": {
            "n_matches": stats["n_matches"],
            "avg_next_day_return": round(stats["avg_next_day_return"], 5),
            "median_next_day_return": round(stats["median_next_day_return"], 5),
            "avg_five_day_return": round(stats["avg_five_day_return"], 5) if stats["avg_five_day_return"] is not None else None,
            "label_distribution": stats["label_distribution"],
            "match_tier": tier,
        },
        "top_matches": top_matches,
        "source": "historical_reactions",
        "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cached": False,
    }

    try:
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass

    return result


def corpus_stats(csv_path: str | None = None) -> dict:
    """Quick summary of the loaded corpus — for debugging coverage gaps."""
    corpus = _load_corpus(Path(csv_path) if csv_path else None)
    if not corpus:
        return {"success": False, "error": "no corpus loaded"}
    by_label: dict[str, int] = {}
    by_item: dict[str, int] = {}
    for r in corpus:
        lbl = r.get("label_t1") or "unknown"
        by_label[lbl] = by_label.get(lbl, 0) + 1
        for c in r["item_codes_set"]:
            by_item[c] = by_item.get(c, 0) + 1
    return {
        "success": True,
        "rows": len(corpus),
        "labels": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "top_item_codes": dict(sorted(by_item.items(), key=lambda kv: -kv[1])[:15]),
    }
