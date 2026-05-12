"""Low-IV debit-spread candidate scanner.

Takes a list of tickers (typically from scan_all output) and filters by
IV rank < threshold. The intersection — "stock has a directional setup
AND options are cheap" — is the highest-edge zone for debit spreads:
leveraged directional exposure at low premium cost, defined risk.

Output: per-ticker IV details + a hint on spread direction (long calls
if pct_to_52w >= -2, otherwise neutral — caller decides).
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime

from rh_mcp.analysis import iv_rank as _iv_rank
from rh_mcp.auth import login

MAX_IV_RANK = 30.0          # premium cheap below this
MIN_PRICE = 10.0            # options liquidity floor
MAX_IV_PARALLEL = 4

DEFAULT_TOP_N = 15


def _iv_rank_one(ticker: str) -> dict:
    try:
        result = _iv_rank.analyze(ticker)
        if isinstance(result, list) and result:
            return result[0]
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    return {"ticker": ticker, "error": "no data"}


def analyze(
    tickers: list[str],
    max_iv_rank: float = MAX_IV_RANK,
    min_price: float = MIN_PRICE,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Filter tickers down to low-IV-rank candidates suitable for debit spreads.

    Designed to be fed directly from scan_all / scan_squeeze / scan_52w output.
    Pass the candidate tickers as a list; we IV-rank each in parallel and
    return only the ones below the threshold.
    """
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "no tickers provided"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IV_PARALLEL) as ex:
        futures = {ex.submit(_iv_rank_one, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            ivr = fut.result()
            if ivr.get("error"):
                continue
            iv_rank = ivr.get("iv_rank")
            current_price = ivr.get("current_price")
            if iv_rank is None or current_price is None:
                continue
            if current_price < min_price:
                continue
            if iv_rank > max_iv_rank:
                continue
            candidates.append({
                "ticker": t,
                "price": current_price,
                "iv_rank": iv_rank,
                "iv_percentile": ivr.get("iv_percentile"),
                "current_iv": ivr.get("current_iv"),
                "hv30": ivr.get("hv30"),
                "iv_premium": ivr.get("iv_premium"),
                "suggested_play": (
                    "long call debit spread" if (ivr.get("iv_premium") or 0) < 0
                    else "long call debit spread (note: IV > HV, premium fair)"
                ),
            })

    # Sort by lowest IV rank (cheapest premium first)
    candidates.sort(key=lambda x: x["iv_rank"])
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_iv_threshold": len(candidates),
        "candidates": candidates,
    }
