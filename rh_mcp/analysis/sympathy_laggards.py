"""Sympathy / sector laggard scanner.

When a sector leader moves ≥`leader_move_pct` today, find peers in the same
industry that haven't moved much yet (`max_laggard_move_pct`). The thesis:
sector themes propagate over 1-3 days, so day-2 laggards often catch up
when institutional rotation completes.

Today's example use case (5/11): optics rallied — LITE +16.5%, COHR +13.2%,
GLW +10.9%, VRT +8.2%. POET was the only sympathy mover (+26.7% small-cap).
Names like CIEN, FN, IIVI stayed quiet — those would be tomorrow's catch-up
candidates if the theme persists.

Output: industries with strong leaders, each with the laggards ranked by
how far they trailed the leader.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

LEADER_MOVE_PCT = 5.0           # leader must move >= this to count
MAX_LAGGARD_MOVE_PCT = 2.0      # laggards moved < this today
MIN_PRICE = 5.00
MIN_AVG_VOLUME = 200_000
MIN_PEERS_PER_INDUSTRY = 3      # need a real industry, not 1-2 names

DEFAULT_TOP_N = 20
_BATCH_SIZE = 75
_BATCH_SLEEP = 0.1


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def _fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_quotes(chunk, info=None) or []
        except Exception:
            results = []
        for q in results:
            if not q:
                continue
            sym = (q.get("symbol") or "").upper()
            if not sym:
                continue
            price = _to_float(q.get("last_trade_price"))
            prev = _to_float(q.get("adjusted_previous_close")) or _to_float(q.get("previous_close"))
            if not price or not prev:
                continue
            out[sym] = {
                "price": price,
                "prev_close": prev,
                "pct_change": round((price - prev) / prev * 100, 2),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_fundamentals(chunk, info=None) or []
        except Exception:
            results = []
        for sym, f in zip(chunk, results):
            if not f:
                continue
            out[sym.upper()] = {
                "industry": f.get("industry"),
                "sector": f.get("sector"),
                "avg_volume": _to_float(f.get("average_volume")),
                "market_cap": _to_float(f.get("market_cap")),
                "high_52w": _to_float(f.get("high_52_weeks")),
            }
        time.sleep(_BATCH_SLEEP)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    leader_move_pct: float = LEADER_MOVE_PCT,
    max_laggard_move_pct: float = MAX_LAGGARD_MOVE_PCT,
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    min_peers_per_industry: int = MIN_PEERS_PER_INDUSTRY,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan for industries with strong leaders + sympathy laggards.

    Returns a list of {industry, leader, laggards} entries ranked by
    leader_move_pct descending. Laggards within each industry are ranked
    by gap (leader_pct - laggard_pct) descending — biggest catch-up
    opportunity first.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe — provide tickers list or universe_file"}

    login()
    scanned_at = datetime.now().isoformat(timespec="seconds")

    quotes = _fetch_quotes(tickers)
    funds = _fetch_fundamentals(tickers)

    # Group by industry
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for ticker in tickers:
        q = quotes.get(ticker)
        f = funds.get(ticker)
        if not q or not f:
            continue
        industry = f.get("industry")
        if not industry:
            continue
        avg_vol = f.get("avg_volume") or 0
        price = q["price"]
        if price < min_price or avg_vol < min_avg_volume:
            continue
        by_industry[industry].append({
            "ticker": ticker,
            "price": price,
            "pct_change": q["pct_change"],
            "industry": industry,
            "sector": f.get("sector"),
            "avg_volume": avg_vol,
            "market_cap": f.get("market_cap"),
            "high_52w": f.get("high_52w"),
        })

    industries_scanned = len(by_industry)
    industry_themes: list[dict] = []

    for industry, members in by_industry.items():
        if len(members) < min_peers_per_industry:
            continue
        # Sort members by today's move descending
        members.sort(key=lambda m: m["pct_change"], reverse=True)
        leader = members[0]
        if leader["pct_change"] < leader_move_pct:
            continue  # no leader strong enough

        # Find laggards: ones that moved < max_laggard_move_pct
        laggards = [m for m in members[1:] if m["pct_change"] < max_laggard_move_pct]
        if not laggards:
            continue

        # Rank laggards by gap to leader (biggest gap first = biggest catch-up potential)
        for m in laggards:
            m["gap_to_leader"] = round(leader["pct_change"] - m["pct_change"], 2)
        laggards.sort(key=lambda m: m["gap_to_leader"], reverse=True)

        industry_themes.append({
            "industry": industry,
            "sector": leader.get("sector"),
            "leader": {
                "ticker": leader["ticker"],
                "price": leader["price"],
                "pct_change": leader["pct_change"],
            },
            "leader_move_pct": leader["pct_change"],
            "peer_count": len(members),
            "laggards": laggards[:top_n],
        })

    industry_themes.sort(key=lambda x: x["leader_move_pct"], reverse=True)

    return {
        "success": True,
        "scanned_at": scanned_at,
        "total_scanned": len(tickers),
        "industries_scanned": industries_scanned,
        "themes_found": len(industry_themes),
        "themes": industry_themes,
    }
