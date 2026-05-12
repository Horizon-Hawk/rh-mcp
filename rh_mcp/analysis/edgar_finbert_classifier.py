"""FinBERT-based 8-K filing classifier — replaces regex/keyword direction inference
on ambiguous filings. Runs entirely locally (no API key, no per-call cost).

Why FinBERT and not a general LLM:
- ProsusAI/finbert is fine-tuned on financial text — better signal-to-noise on
  filing language than general models.
- ~440MB download, runs on CPU in ~0.5-2s/chunk. Zero recurring cost.
- Apache 2.0 license, no terms-of-service shackles.

Architecture:
1. Fetch filing body via existing edgar_client (reuses cache + rate limiter).
2. Strip HTML, drop XBRL/data-heavy chunks.
3. Chunk into ~400-word windows (under 512-token BERT limit).
4. Run FinBERT inference per chunk → {positive, negative, neutral} probs.
5. Filter chunks dominated by neutral (legal boilerplate).
6. Aggregate remaining chunks → direction + confidence + top key clauses.
7. Cache result keyed by accession_no.

Dependencies: transformers, torch. Install with `pip install rh-mcp[finbert]`.
The module fails gracefully (returns success=False) if they're missing.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from rh_mcp.analysis import edgar_client as _edgar
from rh_mcp.analysis import edgar_keywords as _kw

CACHE_DIR = _edgar.CACHE_DIR / "finbert_classifications"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MODEL_NAME = "ProsusAI/finbert"

NEUTRAL_FILTER_THRESHOLD = 0.85
DIRECTION_MARGIN = 0.15
MIN_CHUNK_WORDS = 30
MAX_CHUNK_WORDS = 400
MAX_CHUNKS_PER_FILING = 25
TOP_CLAUSES = 3
MAX_DOCUMENTS = 3

_model = None
_tokenizer = None
_model_lock = threading.Lock()


def _ensure_model():
    """Lazy-load FinBERT. Raises RuntimeError if transformers/torch missing."""
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    with _model_lock:
        if _model is not None:
            return _model, _tokenizer
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "FinBERT requires transformers and torch. "
                "Install with: pip install transformers torch"
            ) from e
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
        model.eval()
        _tokenizer = tokenizer
        _model = model
    return _model, _tokenizer


def _resolve_filing_url(
    accession_no: str,
    ticker: str | None,
    filing_url: str | None,
    cik: str | None,
) -> str | None:
    if filing_url:
        return filing_url
    if not cik and ticker:
        try:
            cik = _edgar.get_ticker_to_cik().get(ticker.upper())
        except Exception:
            cik = None
    if not cik:
        return None
    cik_no_zeros = str(cik).lstrip("0") or "0"
    accession_no_clean = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
        f"{accession_no_clean}/{accession_no}-index.htm"
    )


def _chunk_text(text: str) -> list[str]:
    """Split text into ~MAX_CHUNK_WORDS chunks, breaking on sentence boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sent in sentences:
        words = sent.split()
        if not words:
            continue
        if current_len + len(words) > MAX_CHUNK_WORDS and current:
            joined = " ".join(current)
            if len(joined.split()) >= MIN_CHUNK_WORDS:
                chunks.append(joined)
            current = []
            current_len = 0
        current.append(sent)
        current_len += len(words)
    if current:
        joined = " ".join(current)
        if len(joined.split()) >= MIN_CHUNK_WORDS:
            chunks.append(joined)
    return chunks


def _looks_like_data_chunk(text: str) -> bool:
    """Detect XBRL/financial table chunks where digits dominate alphabetics —
    FinBERT can't extract sentiment from raw numbers without surrounding prose."""
    if not text:
        return True
    alpha = sum(1 for c in text if c.isalpha())
    digits = sum(1 for c in text if c.isdigit())
    return alpha < digits * 2


def _classify_chunks(chunks: list[str]) -> list[dict]:
    import torch
    model, tokenizer = _ensure_model()
    out: list[dict] = []
    # FinBERT label order from config: 0=positive, 1=negative, 2=neutral
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    with _model_lock, torch.no_grad():
        for chunk in chunks:
            inputs = tokenizer(
                chunk, return_tensors="pt", truncation=True, max_length=512,
            )
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0].tolist()
            row = {"text": chunk}
            for i, p in enumerate(probs):
                label = id2label.get(i, str(i)).lower()
                row[label] = float(p)
            row.setdefault("positive", 0.0)
            row.setdefault("negative", 0.0)
            row.setdefault("neutral", 0.0)
            out.append(row)
    return out


def _aggregate(chunk_results: list[dict]) -> dict:
    if not chunk_results:
        return {
            "direction": "neutral", "confidence": 0.0,
            "avg_positive": 0.0, "avg_negative": 0.0,
            "signal_chunks": 0, "total_chunks": 0, "key_clauses": [],
        }

    signal = [c for c in chunk_results if c["neutral"] <= NEUTRAL_FILTER_THRESHOLD]
    if not signal:
        return {
            "direction": "neutral", "confidence": 0.0,
            "avg_positive": round(sum(c["positive"] for c in chunk_results) / len(chunk_results), 3),
            "avg_negative": round(sum(c["negative"] for c in chunk_results) / len(chunk_results), 3),
            "signal_chunks": 0, "total_chunks": len(chunk_results), "key_clauses": [],
        }

    avg_pos = sum(c["positive"] for c in signal) / len(signal)
    avg_neg = sum(c["negative"] for c in signal) / len(signal)

    if avg_neg - avg_pos > DIRECTION_MARGIN:
        direction = "short"
        confidence = round(avg_neg, 3)
        ranked = sorted(signal, key=lambda c: c["negative"], reverse=True)
    elif avg_pos - avg_neg > DIRECTION_MARGIN:
        direction = "long"
        confidence = round(avg_pos, 3)
        ranked = sorted(signal, key=lambda c: c["positive"], reverse=True)
    else:
        direction = "neutral"
        confidence = round(max(avg_pos, avg_neg), 3)
        ranked = sorted(signal, key=lambda c: max(c["positive"], c["negative"]), reverse=True)

    key_clauses = [
        {
            "text": c["text"][:280],
            "positive": round(c["positive"], 3),
            "negative": round(c["negative"], 3),
        }
        for c in ranked[:TOP_CLAUSES]
    ]

    return {
        "direction": direction,
        "confidence": confidence,
        "avg_positive": round(avg_pos, 3),
        "avg_negative": round(avg_neg, 3),
        "signal_chunks": len(signal),
        "total_chunks": len(chunk_results),
        "key_clauses": key_clauses,
    }


def _build_reasoning(agg: dict) -> str:
    if agg["total_chunks"] == 0:
        return "No analyzable narrative content found in filing."
    if agg["signal_chunks"] == 0:
        return (
            f"All {agg['total_chunks']} chunks dominated by neutral language "
            "(legal boilerplate / structured data) — no directional signal."
        )
    base = (
        f"FinBERT analyzed {agg['signal_chunks']} signal chunks "
        f"(of {agg['total_chunks']} total). "
        f"Avg negative={agg['avg_negative']:.2f}, avg positive={agg['avg_positive']:.2f}."
    )
    if agg["direction"] == "short":
        return base + " Negative sentiment dominates — bearish."
    if agg["direction"] == "long":
        return base + " Positive sentiment dominates — bullish."
    return base + " Sentiment balanced — neutral."


def classify_8k_filing(
    accession_no: str,
    ticker: str | None = None,
    filing_url: str | None = None,
    cik: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Classify an 8-K filing's directional sentiment using FinBERT.

    At least one of (filing_url, cik, ticker) is required to locate the filing.
    Results are cached forever (filings don't change post-publication) at
    ~/.edgar_cache/finbert_classifications/<accession_no>.json.

    Returns: {success, accession_no, ticker, direction (long|short|neutral),
    confidence (0-1), reasoning, key_clauses, filing_type, stats, model, cached}.
    """
    cache_path = CACHE_DIR / f"{accession_no}.json"
    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except Exception:
            pass

    resolved_url = _resolve_filing_url(accession_no, ticker, filing_url, cik)
    if not resolved_url:
        return {
            "success": False,
            "error": "could not resolve filing URL — provide filing_url, cik, or a valid ticker",
            "accession_no": accession_no,
        }

    doc_urls = _kw._filing_documents_from_index(resolved_url)
    if not doc_urls:
        return {
            "success": False,
            "error": "no documents found at filing index",
            "accession_no": accession_no,
            "filing_url": resolved_url,
        }

    text_parts: list[str] = []
    for url in doc_urls[:MAX_DOCUMENTS]:
        try:
            html = _edgar._http_get(url, accept="text/html").decode("utf-8", errors="ignore")
        except Exception:
            continue
        text_parts.append(_kw._strip_html(html))
    full_text = " ".join(text_parts).strip()
    if not full_text:
        return {
            "success": False,
            "error": "filing body extracted to empty string",
            "accession_no": accession_no,
            "filing_url": resolved_url,
        }

    chunks = [c for c in _chunk_text(full_text) if not _looks_like_data_chunk(c)]
    if not chunks:
        return {
            "success": False,
            "error": "no narrative chunks survived filtering",
            "accession_no": accession_no,
            "filing_url": resolved_url,
        }
    if len(chunks) > MAX_CHUNKS_PER_FILING:
        chunks = chunks[:MAX_CHUNKS_PER_FILING]

    try:
        chunk_results = _classify_chunks(chunks)
    except RuntimeError as e:
        return {"success": False, "error": str(e), "accession_no": accession_no}
    except Exception as e:
        return {
            "success": False,
            "error": f"FinBERT inference failed: {e}",
            "accession_no": accession_no,
        }

    agg = _aggregate(chunk_results)

    result = {
        "success": True,
        "accession_no": accession_no,
        "ticker": ticker.upper() if ticker else None,
        "filing_url": resolved_url,
        "filing_type": "8-K",
        "direction": agg["direction"],
        "confidence": agg["confidence"],
        "reasoning": _build_reasoning(agg),
        "key_clauses": agg["key_clauses"],
        "stats": {
            "total_chunks": agg["total_chunks"],
            "signal_chunks": agg["signal_chunks"],
            "avg_positive": agg["avg_positive"],
            "avg_negative": agg["avg_negative"],
        },
        "model": MODEL_NAME,
        "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cached": False,
    }

    try:
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass

    return result
