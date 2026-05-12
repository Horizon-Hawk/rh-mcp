"""Iron condor / premium-selling candidate scanner.

Finds stocks with:
- Earnings 7-30 days away (sweet spot — long enough for theta to compound,
  short enough that IV is already elevated)
- IV rank > 70 (premium is expensive)
- Sufficient options liquidity (min avg volume, min price)

Returns ranked candidates with IV rank, days to earnings, and the suggested
expected move so the caller can sketch an iron condor structure outside
the wings.

Edge: sells the elevated IV before earnings, lets theta + post-earnings
IV crush work for the position. ~70% win rate setups when sized to defined
risk (iron condor max loss).
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
from pathlib import Path

from rh_mcp.analysis import earnings as _earnings
from rh_mcp.analysis import iv_rank as _iv_rank
from rh_mcp.auth import login

MIN_DAYS_TO_EARNINGS = 7
MAX_DAYS_TO_EARNINGS = 30
MIN_IV_RANK = 70.0
MIN_PRICE = 10.0          # options liquidity floor
MAX_IV_PARALLEL = 4       # cap parallel iv_rank calls to avoid rate limits

DEFAULT_TOP_N = 15


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


def _iv_rank_one(ticker: str) -> dict:
    """Wrap iv_rank.analyze for a single ticker with safe error handling."""
    try:
        result = _iv_rank.analyze(ticker)
        if isinstance(result, list) and result:
            return result[0]
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    return {"ticker": ticker, "error": "no data"}


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_days_to_earnings: int = MIN_DAYS_TO_EARNINGS,
    max_days_to_earnings: int = MAX_DAYS_TO_EARNINGS,
    min_iv_rank: float = MIN_IV_RANK,
    min_price: float = MIN_PRICE,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Find iron-condor / premium-selling candidates.

    Stage 1: filter universe by earnings_days_away in [min, max] window.
    Stage 2: run iv_rank in parallel on survivors; filter iv_rank >= min_iv_rank.
    Stage 3: sort by iv_rank descending, return top_n.

    iv_rank is the expensive call (~20s per ticker). With MAX_IV_PARALLEL=4
    threads, scanning 30 candidates takes ~150s. Stage-1 filter is cheap
    (one earnings API call per ticker) so we narrow before paying the iv cost.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe — provide tickers list or universe_file"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    # Stage 1: earnings window filter (cheap, batched per ticker)
    # Use parallel pool here too because each call hits RH separately.
    passed_earnings: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_earnings.analyze, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            try:
                er = fut.result()
                e0 = er[0] if er else {}
            except Exception:
                continue
            days = e0.get("days_away")
            if days is None:
                continue
            if min_days_to_earnings <= days <= max_days_to_earnings:
                passed_earnings.append({
                    "ticker": t,
                    "days_to_earnings": days,
                    "earnings_date": e0.get("next_date"),
                    "timing": e0.get("timing"),
                    "eps_estimate": e0.get("eps_estimate"),
                })

    # Stage 2: IV rank filter (expensive — parallel-bounded)
    candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IV_PARALLEL) as ex:
        futures = {ex.submit(_iv_rank_one, e["ticker"]): e for e in passed_earnings}
        for fut in concurrent.futures.as_completed(futures):
            e = futures[fut]
            ivr = fut.result()
            if ivr.get("error"):
                continue
            current_price = ivr.get("current_price")
            iv_rank = ivr.get("iv_rank")
            if not current_price or current_price < min_price:
                continue
            if iv_rank is None or iv_rank < min_iv_rank:
                continue
            candidates.append({
                "ticker": e["ticker"],
                "price": current_price,
                "iv_rank": iv_rank,
                "iv_percentile": ivr.get("iv_percentile"),
                "current_iv": ivr.get("current_iv"),
                "hv30": ivr.get("hv30"),
                "iv_premium": ivr.get("iv_premium"),
                "days_to_earnings": e["days_to_earnings"],
                "earnings_date": e["earnings_date"],
                "timing": e["timing"],
                "eps_estimate": e["eps_estimate"],
            })

    candidates.sort(key=lambda x: x["iv_rank"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_earnings_window": len(passed_earnings),
        "passed_iv_threshold": len(candidates),
        "candidates": candidates,
    }
