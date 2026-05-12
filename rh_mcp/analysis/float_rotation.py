"""Float-rotation breakout scanner — small-cap edge stacking with 8-K signal.

Thesis: when a stock trades its entire public float in a single session it
means passive holders have been completely cycled out, replaced by aggressive
new buyers. On a small float (<50M shares) this rotation is usually
catalyst-driven — an 8-K filing is the most common trigger. Stocks that
combine BOTH a >1x float rotation AND a bullish 8-K classification tend to
follow through over the next 3-5 sessions because the new shareholder base
is unanchored by the prior price.

Universe selection inherits the standard challenge filter (price ≥ $5, avg
volume ≥ 500K). Cap is bounded ABOVE at $3B because the float-rotation
mechanic doesn't fire on mega-caps (a $50B name with 1B float would need
1B+ shares of volume — never happens).

Output: ranked candidates sorted by `rotation_score = vol_to_float_ratio ×
classification_confidence`. Top result is usually the most actionable.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

try:
    import yfinance as yf
except ImportError as e:
    raise ImportError("float_rotation requires yfinance — pip install yfinance") from e

from rh_mcp.analysis import edgar_client as _edgar
from rh_mcp.analysis import scan_8k as _scan_8k


MIN_PRICE = 5.0
MAX_MARKET_CAP = 3_000_000_000          # $3B ceiling — above this, mechanic fails
MAX_FLOAT_SHARES = 100_000_000          # 100M float cap
MIN_VOL_TO_FLOAT_RATIO = 1.0            # 1x float = complete rotation
DEFAULT_TOP_N = 10


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def _ticker_metrics(ticker: str) -> dict:
    """Pull today's volume, float, market cap, price for one ticker."""
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return {"ticker": ticker, "ok": False, "error": "yfinance fetch failed"}
    price = _to_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    market_cap = _to_float(info.get("marketCap"))
    float_shares = _to_float(info.get("floatShares"))
    # `regularMarketVolume` is today's running volume if mid-session;
    # `averageVolume` is the 30d baseline. We compute the ratio against float.
    today_volume = _to_float(info.get("regularMarketVolume") or info.get("volume"))
    avg_volume = _to_float(info.get("averageVolume"))
    return {
        "ticker": ticker,
        "ok": True,
        "price": price,
        "market_cap": market_cap,
        "float_shares": float_shares,
        "today_volume": today_volume,
        "avg_volume": avg_volume,
    }


def _passes_universe_filter(m: dict) -> tuple[bool, str]:
    if not m.get("ok"):
        return False, m.get("error", "fetch_failed")
    price = m.get("price") or 0
    if price < MIN_PRICE:
        return False, f"price<{MIN_PRICE}"
    market_cap = m.get("market_cap") or 0
    if market_cap > MAX_MARKET_CAP:
        return False, f"market_cap>{MAX_MARKET_CAP}"
    float_shares = m.get("float_shares") or 0
    if float_shares <= 0 or float_shares > MAX_FLOAT_SHARES:
        return False, f"float>{MAX_FLOAT_SHARES}_or_missing"
    return True, "ok"


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_vol_to_float_ratio: float = MIN_VOL_TO_FLOAT_RATIO,
    require_8k: bool = True,
    require_bullish_8k: bool = True,
    lookback_minutes: int = 480,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find float-rotation breakout candidates.

    Pipeline:
      1. Load universe, fetch metrics in parallel via yfinance
      2. Filter: price ≥ $5, market_cap ≤ $3B, float ≤ 100M
      3. Compute vol_to_float_ratio for each survivor
      4. Filter: ratio ≥ 1.0 (full float rotation)
      5. Cross-reference with recent 8-K filings (if require_8k=True)
      6. If require_bullish_8k: drop neutral/short 8-K candidates
      7. Score = ratio × 8k_confidence (or just ratio if no 8-K filter)
      8. Return top_n sorted by score

    Args:
        require_8k: if True, candidate must have an 8-K in `lookback_minutes`.
        require_bullish_8k: if True (and require_8k=True), direction must be
            'long' from scan_8k's three-layer classifier.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("C:/Users/algee/stock_universe.txt")
        tickers = _load_universe(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Step 1: metrics in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        metrics = list(ex.map(_ticker_metrics, tickers))

    # Steps 2-4: universe + rotation filter
    rotation_candidates: list[dict] = []
    for m in metrics:
        ok, _ = _passes_universe_filter(m)
        if not ok:
            continue
        vol = m.get("today_volume") or 0
        flt = m.get("float_shares") or 0
        if flt <= 0:
            continue
        ratio = vol / flt
        if ratio < min_vol_to_float_ratio:
            continue
        m["vol_to_float_ratio"] = round(ratio, 3)
        rotation_candidates.append(m)

    if not rotation_candidates:
        return {
            "success": True,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_scanned": len(tickers),
            "with_full_rotation": 0,
            "with_8k_match": 0,
            "candidates": [],
        }

    # Steps 5-6: 8-K cross-reference
    candidates_with_8k: list[dict] = []
    if require_8k:
        rotation_tickers = [c["ticker"] for c in rotation_candidates]
        try:
            scan_resp = _scan_8k.analyze(
                tickers=rotation_tickers,
                lookback_minutes=lookback_minutes,
                deep_scan=True,
                top_n=top_n * 5,  # generous before final ranking
            )
        except Exception as e:
            return {"success": False, "error": f"8-K scan failed: {e}"}
        if not scan_resp.get("success"):
            return {"success": False, "error": scan_resp.get("error")}

        # Index 8-K results by ticker (most recent only per ticker)
        by_ticker: dict[str, dict] = {}
        for c in scan_resp.get("candidates", []):
            t = c.get("ticker")
            if t and t not in by_ticker:
                by_ticker[t] = c

        for c in rotation_candidates:
            filing = by_ticker.get(c["ticker"])
            if not filing:
                continue
            direction = filing.get("direction")
            if require_bullish_8k and direction != "long":
                continue
            # Confidence used for scoring: prefer historical > body_keywords > 0.5 default
            conf = (
                filing.get("historical_confidence")
                or filing.get("body_confidence")
                or 0.5
            )
            c.update({
                "filing_accession_no": filing.get("accession_no"),
                "filing_url": filing.get("filing_url"),
                "filing_item_codes": filing.get("item_codes"),
                "filing_direction": direction,
                "filing_confidence": round(conf, 3),
                "filing_direction_source": filing.get("direction_source"),
                "rotation_score": round(c["vol_to_float_ratio"] * conf, 3),
            })
            candidates_with_8k.append(c)
    else:
        # No 8-K filter — score is just the rotation ratio
        for c in rotation_candidates:
            c["rotation_score"] = c["vol_to_float_ratio"]
            candidates_with_8k.append(c)

    # Step 7-8: rank
    candidates_with_8k.sort(key=lambda c: c.get("rotation_score", 0), reverse=True)
    if top_n and len(candidates_with_8k) > top_n:
        candidates_with_8k = candidates_with_8k[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "with_full_rotation": len(rotation_candidates),
        "with_8k_match": len(candidates_with_8k) if require_8k else None,
        "filter_settings": {
            "min_vol_to_float_ratio": min_vol_to_float_ratio,
            "require_8k": require_8k,
            "require_bullish_8k": require_bullish_8k,
            "lookback_minutes": lookback_minutes,
            "max_market_cap": MAX_MARKET_CAP,
            "max_float_shares": MAX_FLOAT_SHARES,
        },
        "candidates": candidates_with_8k,
    }
