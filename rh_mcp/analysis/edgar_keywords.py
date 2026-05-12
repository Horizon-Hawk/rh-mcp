"""8-K filing body keyword extraction.

The thesis (per user research): markets overreact to headlines but take
minutes-to-hours to digest fine print in exhibits. LLMs parse legal jargon
faster than humans but slower than HFT headline scrapers — the sweet spot is
the 30s-15min "digestive drift" window where prices reset to reflect body
content the HFT bots missed.

This module extracts specific high-signal phrases from filing bodies that
carry directional implications beyond what item codes alone reveal.

Pattern library is intentionally conservative — start with high-confidence
phrases, expand based on backtest results.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from rh_mcp.analysis import edgar_client as _edgar


class KeywordHit(NamedTuple):
    pattern_name: str
    direction: str       # 'long', 'short', or 'flag' (info-only)
    weight: float        # 0-1 confidence
    context: str         # ~200 char snippet around match


# Pattern library. Each entry: (name, regex, direction, weight)
# Regexes are case-insensitive and word-boundary-aware where appropriate.
PATTERN_LIB: list[tuple[str, str, str, float]] = [
    # --- SHORT signals: financial / regulatory distress ---
    ("going_concern", r"\bgoing\s+concern\b", "short", 0.95),
    ("material_adverse_change", r"\bmaterial\s+adverse\s+(change|effect)\b", "short", 0.70),
    ("covenant_violation", r"\b(covenant\s+(violation|breach)|event\s+of\s+default|in\s+default\s+of)\b", "short", 0.85),
    ("non_reliance", r"\b(non[- ]?reliance|should\s+no\s+longer\s+be\s+relied\s+upon)\b", "short", 0.95),
    ("restate", r"\b(restate(d|ment)?|materially\s+misstated)\b", "short", 0.85),
    ("sec_subpoena", r"\b(SEC\s+(subpoena|investigation|inquiry)|Wells\s+notice|DOJ\s+(subpoena|investigation))\b", "short", 0.90),
    ("qualified_audit_opinion", r"\bqualified\s+(audit\s+)?opinion\b", "short", 0.85),
    ("auditor_resignation", r"\bauditor\s+(resigned|resignation|dismissed|terminated)\b", "short", 0.80),
    ("material_weakness", r"\bmaterial\s+weakness\b", "short", 0.75),
    ("ceo_departure_unplanned", r"\b(CEO|chief\s+executive)\s+(resigned\s+effective\s+immediately|terminated|removed|departed)\b", "short", 0.75),
    ("guidance_withdrawn", r"\b(withdraw(s|n|ing)?|suspend(s|ed|ing)?|rescind(s|ed)?)\s+(guidance|outlook|forecast)\b", "short", 0.80),
    ("delisting_notice", r"\b(delisting\s+notice|notice\s+of\s+delisting|continued\s+listing\s+(requirement|standard))\b", "short", 0.80),

    # --- LONG signals: positive deal / capital return ---
    # Require an acquisition verb form ("to acquire" / "will acquire" / "to be
    # acquired" / "will be acquired") within ~150 non-period chars of the
    # "definitive agreement" phrase. Original `.*acquir` bridged unrelated
    # clauses and matched "acquiring person" definitional language in PIPE/SPA
    # exhibits, false-positiving on dilutive filings.
    ("definitive_agreement_acquisition",
     r"\bdefinitive\s+(merger\s+|purchase\s+)?agreement\b[^.]{0,150}\b(to\s+acquire|will\s+acquire|will\s+be\s+acquired|to\s+be\s+acquired)\b",
     "long", 0.70),
    ("acquired_by", r"\b(to\s+be\s+acquired\s+by|agreed?\s+to\s+sell\s+the\s+company)\b", "long", 0.90),
    ("buyback_authorized", r"\b(repurchase|buyback)\s+(program|authorization|of\s+up\s+to)\b", "long", 0.70),
    ("dividend_increase", r"\b(increase[ds]?\s+(quarterly\s+)?(dividend|distribution)|dividend\s+raised)\b", "long", 0.65),
    ("partnership_strategic", r"\b(strategic\s+(partnership|alliance|collaboration))\b", "long", 0.50),
    ("fda_approval", r"\bFDA\s+(approval|approved|clearance|granted)\b", "long", 0.85),
    ("tender_offer_received", r"\b(tender\s+offer\s+(commenced|received)|unsolicited\s+(bid|proposal))\b", "long", 0.85),
    ("guidance_raised", r"\b(raised?|increase[ds]?)\s+(guidance|outlook|forecast)\b", "long", 0.75),

    # --- Info / flag (directional but context-dependent) ---
    ("10b5_1_plan_adopted", r"\b10b5-?1\s+(trading\s+)?plan\s+(adopted|established)\b", "flag", 0.50),
    ("credit_facility_amendment", r"\bamendment\s+(no\.?\s*\d+\s+)?to\s+(credit|loan)\s+(agreement|facility)\b", "flag", 0.40),
    ("convertible_debt_issuance", r"\bconvertible\s+(senior\s+)?notes?\b", "flag", 0.50),
    ("share_dilution_pipe", r"\b(PIPE|private\s+placement)\s+(financing|transaction)\b", "short", 0.65),

    # --- Dilutive offering structure (used as features, not just direction) ---
    # These appear in 1.01 filings that ARE dilutive — registered directs, follow-ons,
    # underwritten offerings, shelf takedowns. The keyword scanner alone is too weak to
    # bet on these (1.01 acquisitions use similar phrasing for the deal itself), but they
    # are very useful as FEATURES in the historical-reaction classifier.
    ("registered_direct_offering", r"\bregistered\s+direct\s+offering\b", "short", 0.80),
    ("underwritten_offering", r"\bunderwritten\s+(public\s+)?offering\b", "short", 0.75),
    ("follow_on_offering", r"\bfollow[- ]?on\s+offering\b", "short", 0.75),
    ("shelf_takedown", r"\bprospectus\s+supplement\b", "flag", 0.50),
    ("securities_purchase_agreement", r"\bsecurities\s+purchase\s+agreement\b", "flag", 0.55),
    ("issue_and_sell_shares", r"\bissue\s+and\s+sell\b[^.]{0,200}\bshares\b", "short", 0.70),
    ("at_the_market_offering", r"\bat[- ]the[- ]market\s+(offering|equity\s+offering\s+program|sales\s+agreement)\b", "short", 0.75),
]

# Compile once
_COMPILED = [
    (name, re.compile(pattern, re.IGNORECASE), direction, weight)
    for name, pattern, direction, weight in PATTERN_LIB
]


def _strip_html(html: str) -> str:
    """Quick text extraction from filing HTML. Not perfect, but good enough for
    keyword pattern matching across body + exhibits."""
    # Drop script/style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace tags with spaces (preserves word boundaries)
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Decode common entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#160;", " ")
        .replace("&#8217;", "'")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
    )
    return text.strip()


def _filing_documents_from_index(index_url: str) -> list[str]:
    """Given an 8-K filing's index page URL, return URLs of the actual document
    files ordered for narrative extraction: 8-K body first, then EX-99 (press
    release), then other exhibits. Drops graphics and unrelated files.

    Handles two EDGAR quirks:
    1. iXBRL-wrapped 8-K bodies are linked as `/ix?doc=/Archives/...`. That URL
       returns a JS-only viewer page with no narrative text. We rewrite to the
       underlying document path.
    2. The index page table has a Type column ('8-K', 'EX-5.1', 'EX-99.1',
       'GRAPHIC', etc.) we parse for priority ordering.
    """
    try:
        html = _edgar._http_get(index_url, accept="text/html").decode("utf-8", errors="ignore")
    except Exception:
        return []

    # Parse the Document Format Files table — each <tr> holds Seq, Description,
    # Document (with <a href>), Type, Size. We need (href, type) pairs.
    docs: list[tuple[str, str]] = []
    # Match each row's href + the Type cell that follows within the same row
    row_pattern = re.compile(
        r'<tr[^>]*>.*?<a\s+href="([^"]+\.html?)"[^>]*>.*?</a>.*?'
        r'<td[^>]*>\s*([A-Za-z0-9.\-/]+)\s*</td>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in row_pattern.finditer(html):
        href, doc_type = m.group(1), (m.group(2) or "").strip().upper()
        if "Archives/edgar" not in href or "-index" in href:
            continue
        if doc_type in ("GRAPHIC", "ZIP", "XML"):
            continue
        # Unwrap iXBRL viewer URLs — `/ix?doc=/path` -> `/path`
        if "/ix?doc=" in href:
            href = href.split("/ix?doc=", 1)[1]
        if href.startswith("/"):
            href = "https://www.sec.gov" + href
        elif not href.startswith("http"):
            continue
        docs.append((href, doc_type))

    # Fallback: if table parsing yielded nothing (unusual format), use the
    # legacy href-only scan so older filings still work.
    if not docs:
        for h in re.findall(r'href="([^"]+\.html?)"', html, re.IGNORECASE):
            if "-index" in h or "Archives/edgar" not in h:
                continue
            if "/ix?doc=" in h:
                h = h.split("/ix?doc=", 1)[1]
            if h.startswith("/"):
                h = "https://www.sec.gov" + h
            elif not h.startswith("http"):
                continue
            docs.append((h, ""))

    def _priority(doc_type: str) -> int:
        t = doc_type.upper()
        if t == "8-K":
            return 0          # the body itself — always first
        if t.startswith("EX-99"):
            return 1          # press releases / supplemental info — high signal
        if t.startswith("EX-10"):
            return 2          # material contracts — often the actual agreement
        if t.startswith("EX-"):
            return 3          # legal opinions, consents, etc.
        return 4

    docs.sort(key=lambda d: _priority(d[1]))

    seen: set[str] = set()
    out: list[str] = []
    for href, _ in docs:
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out[:5]


# Negation patterns to suppress — most legal boilerplate uses these forms.
# If the regex match is within ~40 chars after one of these phrases, skip it.
_NEGATION_PRECEDENTS = re.compile(
    r"\b(no|not|absence\s+of|without|no\s+such|free\s+of|will\s+not|"
    r"shall\s+not|do\s+not|does\s+not|did\s+not|absent|"
    r"in\s+the\s+absence\s+of|except\s+as|other\s+than)\b",
    re.IGNORECASE,
)


def _is_negated(text: str, match_start: int) -> bool:
    """Look back ~50 chars from the match start. If a negation precedes it
    within that window, consider the match negated (boilerplate)."""
    look_start = max(0, match_start - 60)
    preceding = text[look_start:match_start]
    return bool(_NEGATION_PRECEDENTS.search(preceding))


def scan_filing_body(filing_index_url: str, max_docs: int = 3) -> list[KeywordHit]:
    """Fetch a filing's body documents and extract keyword hits.

    Returns sorted list of KeywordHit by direction weight (highest confidence first).
    Negation-aware: hits like "no material weakness" or "absence of restated"
    are dropped as boilerplate noise.
    """
    doc_urls = _filing_documents_from_index(filing_index_url)
    if not doc_urls:
        return []

    all_text_parts: list[str] = []
    for url in doc_urls[:max_docs]:
        try:
            html = _edgar._http_get(url, accept="text/html").decode("utf-8", errors="ignore")
        except Exception:
            continue
        all_text_parts.append(_strip_html(html))
    full_text = " ".join(all_text_parts)
    if not full_text:
        return []

    hits: list[KeywordHit] = []
    for name, regex, direction, weight in _COMPILED:
        for match in regex.finditer(full_text):
            # Negation guard: "no material weakness" should NOT count as a hit
            if _is_negated(full_text, match.start()):
                continue
            start = max(0, match.start() - 80)
            end = min(len(full_text), match.end() + 80)
            context = full_text[start:end].strip()
            hits.append(KeywordHit(name, direction, weight, context))
            break  # one hit per pattern is enough — multiple hits on same pattern aren't more informative

    hits.sort(key=lambda h: h.weight, reverse=True)
    return hits


def synthesize_direction(hits: list[KeywordHit]) -> tuple[str | None, float]:
    """Aggregate per-pattern hits into a single direction + confidence score.

    Strategy:
    - Sum weights by direction
    - Net direction wins (long-sum minus short-sum determines sign)
    - Confidence = max single-pattern weight on the winning side
    - 'flag' hits don't vote but appear in output
    """
    long_score = sum(h.weight for h in hits if h.direction == "long")
    short_score = sum(h.weight for h in hits if h.direction == "short")
    if long_score == 0 and short_score == 0:
        return None, 0.0
    if long_score > short_score:
        max_long_weight = max((h.weight for h in hits if h.direction == "long"), default=0.0)
        return "long", round(max_long_weight, 2)
    if short_score > long_score:
        max_short_weight = max((h.weight for h in hits if h.direction == "short"), default=0.0)
        return "short", round(max_short_weight, 2)
    return "conflict", 0.5
