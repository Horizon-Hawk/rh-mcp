"""Build a historical-reaction corpus of 8-K filings.

The thesis: ground-truth direction labels come from actual market reactions, not
from sentiment models or keyword heuristics. We pull every 8-K for a ticker
universe over a lookback window, compute next-day and 5-day forward returns,
label each filing A_long / B_long / neutral / B_short / A_short, and persist as
CSV. A separate classifier (edgar_history_classifier) does nearest-signature
lookup against this corpus at inference time.

Features captured per filing (the "signature" we match on later):
- item_codes      — pipe-separated like "1.01|9.01"
- body_keywords   — optional, names of edgar_keywords patterns that fired
                    (only when build with `with_body_keywords=True`)

Labels (default: based on next-day return; t+5 also recorded):
- A_long   : r > +3%
- B_long   : +1% < r <= +3%
- neutral  : -1% <= r <= +1%
- B_short  : -3% <= r < -1%
- A_short  : r < -3%

Build effort: ~500 tickers × ~10 filings each ≈ 5K rows. Item-codes-only build
takes ~15-25 min (mostly EDGAR rate limit at 6 req/s). With body keywords,
expect 45-60 min.

Output: ~/.edgar_cache/8k_history.csv (path configurable).
"""

from __future__ import annotations

import csv
import concurrent.futures
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from rh_mcp.analysis import edgar_client as _edgar

DEFAULT_CSV_PATH = _edgar.CACHE_DIR / "8k_history.csv"

CSV_FIELDS = [
    "accession_no", "ticker", "cik", "filed_at", "filing_date",
    "item_codes", "body_keywords",
    # Direction labels: same logic scan_8k uses at runtime — body keyword
    # synthesis if confidence >= 0.7, else item-code classification.
    "direction", "direction_confidence", "direction_source",
    "t0_close", "t1_close", "t5_close",
    "next_day_return", "five_day_return",
    "label_t1", "label_t5",
]

# Mirrors scan_8k's LONG/SHORT_ITEM_CODES for the item-code fallback.
_LONG_ITEM_CODES = {"1.01"}
_SHORT_ITEM_CODES = {"3.02", "4.01", "4.02"}


def _load_universe(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(tickers))


def _label_from_return(r: float | None) -> str:
    if r is None:
        return ""
    if r > 0.03:
        return "A_long"
    if r > 0.01:
        return "B_long"
    if r >= -0.01:
        return "neutral"
    if r >= -0.03:
        return "B_short"
    return "A_short"


def _ticker_8ks(cik: str, start_date: date, end_date: date) -> list[dict]:
    """Pull all 8-K filings for a CIK between start_date and end_date."""
    data = _edgar.company_submissions(cik)
    if not data:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    acc_nos = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    forms = recent.get("form") or []
    primary_docs = recent.get("primaryDocument") or []
    out: list[dict] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        fd_str = filing_dates[i] if i < len(filing_dates) else None
        if not fd_str:
            continue
        try:
            fd = datetime.strptime(fd_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if fd < start_date or fd > end_date:
            continue
        acc = acc_nos[i] if i < len(acc_nos) else None
        primary = primary_docs[i] if i < len(primary_docs) else None
        if not acc:
            continue
        cik_no_zeros = cik.lstrip("0") or "0"
        accession_clean = acc.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
            f"{accession_clean}/{acc}-index.htm"
        )
        out.append({
            "accession_no": acc,
            "cik": cik,
            "filing_date": fd_str,
            "filed_at": fd_str,
            "filing_url": filing_url,
            "primary_doc": primary,
        })
    return out


def _item_code_direction(item_codes: list[str]) -> str | None:
    items = set(item_codes)
    if items & _LONG_ITEM_CODES:
        return "long"
    if items & _SHORT_ITEM_CODES:
        return "short"
    return None


def _enrich_features(filing: dict, with_body_keywords: bool) -> dict:
    """Fetch item codes (mandatory) and optionally body keyword features.
    Also synthesizes a direction label using scan_8k's runtime logic so the
    corpus can be backtested per-direction (long vs short subsets).
    """
    try:
        items = _edgar.filing_items(filing["filing_url"])
    except Exception:
        items = []
    filing["item_codes"] = "|".join(items)

    body_direction: str | None = None
    body_confidence: float = 0.0
    if with_body_keywords and items:
        from rh_mcp.analysis import edgar_keywords as _kw
        try:
            hits = _kw.scan_filing_body(filing["filing_url"])
            filing["body_keywords"] = "|".join(sorted({h.pattern_name for h in hits}))
            body_direction, body_confidence = _kw.synthesize_direction(hits)
        except Exception:
            filing["body_keywords"] = ""
    else:
        filing["body_keywords"] = ""

    # Final direction: body if confidence >= 0.7 (matches scan_8k), else item code.
    item_direction = _item_code_direction(items)
    if body_direction in ("long", "short") and body_confidence >= 0.7:
        filing["direction"] = body_direction
        filing["direction_confidence"] = round(body_confidence, 3)
        filing["direction_source"] = "body_keywords"
    elif item_direction:
        filing["direction"] = item_direction
        filing["direction_confidence"] = 0.5  # item-code-only baseline confidence
        filing["direction_source"] = "item_codes"
    else:
        filing["direction"] = ""
        filing["direction_confidence"] = 0.0
        filing["direction_source"] = ""
    return filing


def _fetch_ticker_history(ticker: str, lookback_days: int):
    """One yfinance call per ticker — returns a date-indexed DataFrame of closes."""
    import yfinance as yf
    period = "2y" if lookback_days > 365 else "1y"
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


def _compute_returns(df, filing_date_str: str) -> dict:
    """For a given filing date, return {t0_close, t1_close, t5_close,
    next_day_return, five_day_return}. Uses close-of-filing-date through
    close-of-next-trading-day. Returns empty values if data unavailable."""
    import pandas as pd
    blank = {
        "t0_close": "", "t1_close": "", "t5_close": "",
        "next_day_return": "", "five_day_return": "",
    }
    if df is None or df.empty:
        return blank
    try:
        fd = pd.Timestamp(filing_date_str).tz_localize(None).normalize()
    except Exception:
        return blank
    # Index is timezone-aware DatetimeIndex from yfinance — normalize for comparison
    idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
    idx = idx.normalize()
    # Find the position of fd (or next available trading day on or after fd)
    pos_arr = idx.searchsorted(fd, side="left")
    pos = int(pos_arr)
    if pos >= len(idx):
        return blank
    # t0 = close on filing day (or earliest trading day >= fd)
    t0_close = float(df["Close"].iloc[pos])
    # t1 = next trading day after t0
    t1_close = float(df["Close"].iloc[pos + 1]) if pos + 1 < len(idx) else None
    # t5 = 5 trading days after t0
    t5_close = float(df["Close"].iloc[pos + 5]) if pos + 5 < len(idx) else None

    next_day_return = (t1_close / t0_close - 1) if t1_close else None
    five_day_return = (t5_close / t0_close - 1) if t5_close else None
    return {
        "t0_close": round(t0_close, 4),
        "t1_close": round(t1_close, 4) if t1_close else "",
        "t5_close": round(t5_close, 4) if t5_close else "",
        "next_day_return": round(next_day_return, 5) if next_day_return is not None else "",
        "five_day_return": round(five_day_return, 5) if five_day_return is not None else "",
    }


def build_corpus(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    lookback_days: int = 365,
    output_path: str | Path | None = None,
    with_body_keywords: bool = False,
    max_tickers: int | None = None,
    progress_every: int = 25,
) -> dict:
    """Build the 8-K → reaction corpus.

    Args:
        tickers: explicit ticker list. Falls back to universe_file if None.
        universe_file: path to a stock_universe.txt-style file.
        lookback_days: how far back to pull 8-Ks (default 365).
        output_path: CSV destination. Defaults to ~/.edgar_cache/8k_history.csv.
        with_body_keywords: if True, also fetch each filing body and run the
            keyword pattern library — produces richer features at ~5x build cost.
        max_tickers: cap on tickers processed (useful for first-build smoke tests).
        progress_every: emit a progress log line every N tickers.

    Returns: summary dict {success, rows_written, tickers_processed, output_path, ...}.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("C:/Users/algee/stock_universe.txt")
        tickers = _load_universe(Path(universe_file))
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    if max_tickers:
        tickers = tickers[:max_tickers]
    if not tickers:
        return {"success": False, "error": "no tickers"}

    out_path = Path(output_path) if output_path else DEFAULT_CSV_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)

    today = date.today()
    start_date = today - timedelta(days=lookback_days)
    started_at = datetime.utcnow().isoformat(timespec="seconds")

    try:
        ticker_to_cik = _edgar.get_ticker_to_cik()
    except Exception as e:
        return {"success": False, "error": f"ticker->cik map failed: {e}"}

    rows_written = 0
    tickers_processed = 0
    tickers_with_filings = 0
    skipped_no_cik = 0

    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for ticker_idx, ticker in enumerate(tickers):
            tickers_processed += 1
            cik = ticker_to_cik.get(ticker)
            if not cik:
                skipped_no_cik += 1
                continue

            try:
                filings = _ticker_8ks(cik, start_date, today)
            except Exception:
                continue
            if not filings:
                continue

            # Enrich features in parallel (HTTP-bound, EDGAR rate limiter serializes anyway)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                enriched = list(ex.map(lambda f: _enrich_features(f, with_body_keywords), filings))

            # One yfinance call per ticker
            df = _fetch_ticker_history(ticker, lookback_days)
            if df is None:
                continue

            tickers_with_filings += 1

            for f in enriched:
                if not f.get("item_codes"):
                    continue
                ret = _compute_returns(df, f["filing_date"])
                if ret["next_day_return"] == "":
                    # No price data for this filing — skip
                    continue
                row = {
                    "accession_no": f["accession_no"],
                    "ticker": ticker,
                    "cik": cik,
                    "filed_at": f["filed_at"],
                    "filing_date": f["filing_date"],
                    "item_codes": f["item_codes"],
                    "body_keywords": f["body_keywords"],
                    "direction": f.get("direction", ""),
                    "direction_confidence": f.get("direction_confidence", 0.0),
                    "direction_source": f.get("direction_source", ""),
                    "t0_close": ret["t0_close"],
                    "t1_close": ret["t1_close"],
                    "t5_close": ret["t5_close"],
                    "next_day_return": ret["next_day_return"],
                    "five_day_return": ret["five_day_return"],
                    "label_t1": _label_from_return(
                        ret["next_day_return"] if ret["next_day_return"] != "" else None
                    ),
                    "label_t5": _label_from_return(
                        ret["five_day_return"] if ret["five_day_return"] != "" else None
                    ),
                }
                writer.writerow(row)
                rows_written += 1

            if progress_every and (ticker_idx + 1) % progress_every == 0:
                fh.flush()
                print(
                    f"[corpus] {ticker_idx + 1}/{len(tickers)} tickers, "
                    f"{rows_written} rows so far",
                    flush=True,
                )

    finished_at = datetime.utcnow().isoformat(timespec="seconds")
    return {
        "success": True,
        "started_at": started_at,
        "finished_at": finished_at,
        "tickers_input": len(tickers),
        "tickers_processed": tickers_processed,
        "tickers_with_filings": tickers_with_filings,
        "tickers_no_cik": skipped_no_cik,
        "rows_written": rows_written,
        "output_path": str(out_path),
        "lookback_days": lookback_days,
        "with_body_keywords": with_body_keywords,
    }
