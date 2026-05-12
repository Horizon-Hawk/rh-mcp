"""Level 2 order book scan: walls, OBI, spread, imbalance.

Ported from order_book.py CLI. Calls the Robinhood pricebook snapshot endpoint
directly via robin_stocks' authenticated session (requires Gold subscription).
"""

from __future__ import annotations

import robin_stocks.robinhood as rh
import robin_stocks.robinhood.helper as rhh

from rh_mcp.auth import login

PRICEBOOK_URL = "https://api.robinhood.com/marketdata/pricebook/snapshots/{instrument_id}/"
DEFAULT_RANGE = 5.0
DEFAULT_WALL = 10_000


def _get_instrument_id(ticker: str) -> str | None:
    instruments = rh.stocks.get_instruments_by_symbols(ticker, info=None)
    if not instruments:
        return None
    return instruments[0]["id"]


def _fetch_pricebook(instrument_id: str) -> dict:
    url = PRICEBOOK_URL.format(instrument_id=instrument_id)
    r = rhh.SESSION.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"pricebook returned {r.status_code}: {r.text[:200]}")
    return r.json()


def _parse_book(data: dict) -> dict:
    book = {"bids": {}, "asks": {}}
    for side in ("bids", "asks"):
        for entry in data.get(side, []):
            try:
                price = float(entry["price"]["amount"])
                size = float(entry["quantity"])
                if size > 0:
                    book[side][price] = size
            except (KeyError, ValueError, TypeError):
                pass
    return book


def _calc_obi(book: dict, depth: int = 10) -> dict:
    bid_levels = sorted(book["bids"].items(), key=lambda x: x[0], reverse=True)[:depth]
    ask_levels = sorted(book["asks"].items(), key=lambda x: x[0])[:depth]
    bid_size = sum(s for _, s in bid_levels)
    ask_size = sum(s for _, s in ask_levels)

    if ask_size == 0 and bid_size == 0:
        return {"depth": depth, "bid_size": 0, "ask_size": 0, "ratio": None, "signal": "no_data"}

    ratio = float("inf") if ask_size == 0 else bid_size / ask_size

    if ratio == float("inf") or ratio >= 3.0:
        signal = "STRONG BUY PRESSURE"
    elif ratio >= 2.0:
        signal = "BUY PRESSURE"
    elif ratio <= 0.33:
        signal = "STRONG SELL PRESSURE"
    elif ratio <= 0.5:
        signal = "SELL PRESSURE"
    else:
        signal = "BALANCED"

    return {
        "depth": depth,
        "bid_size": int(bid_size),
        "ask_size": int(ask_size),
        "ratio": round(ratio, 2) if ratio != float("inf") else None,
        "ratio_inf": ratio == float("inf"),
        "signal": signal,
        "levels_used_bid": len(bid_levels),
        "levels_used_ask": len(ask_levels),
    }


def _grade(book: dict, ticker: str, target: float, price_range: float, wall_threshold: int) -> dict:
    low = target - price_range
    high = target + price_range
    bids_in = {p: s for p, s in book["bids"].items() if low <= p <= high}
    asks_in = {p: s for p, s in book["asks"].items() if low <= p <= high}

    bid_walls = sorted(
        [(p, s) for p, s in bids_in.items() if s >= wall_threshold],
        key=lambda x: x[0], reverse=True,
    )
    ask_walls = sorted(
        [(p, s) for p, s in asks_in.items() if s >= wall_threshold],
        key=lambda x: x[0],
    )

    total_bid = sum(bids_in.values())
    total_ask = sum(asks_in.values())
    if total_bid + total_ask > 0:
        bid_pct = total_bid / (total_bid + total_ask) * 100
        imbalance = f"bid {bid_pct:.0f}% / ask {100 - bid_pct:.0f}%"
    else:
        imbalance = "no data in range"

    top_resistance = max(ask_walls, key=lambda x: x[1]) if ask_walls else None
    top_support = max(bid_walls, key=lambda x: x[1]) if bid_walls else None

    best_bid = max(book["bids"].keys()) if book["bids"] else None
    best_ask = min(book["asks"].keys()) if book["asks"] else None
    spread = round(best_ask - best_bid, 4) if best_bid and best_ask else None

    return {
        "ticker": ticker,
        "target_price": target,
        "range": f"${low:.2f}–${high:.2f}",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "ask_walls": [{"price": p, "size": int(s)} for p, s in ask_walls],
        "bid_walls": [{"price": p, "size": int(s)} for p, s in bid_walls],
        "total_bid_size": int(total_bid),
        "total_ask_size": int(total_ask),
        "imbalance": imbalance,
        "obi_10": _calc_obi(book, depth=10),
        "obi_20": _calc_obi(book, depth=20),
        "top_resistance": {"price": top_resistance[0], "size": int(top_resistance[1])} if top_resistance else None,
        "top_support": {"price": top_support[0], "size": int(top_support[1])} if top_support else None,
        "levels_in_range": len(bids_in) + len(asks_in),
        "total_book_levels": len(book["bids"]) + len(book["asks"]),
    }


def analyze(
    ticker: str,
    price: float,
    range_dollars: float | None = None,
    threshold: int | None = None,
) -> dict:
    """L2 order book scan at a target price. Returns walls, OBI, spread."""
    ticker = ticker.upper()
    price_range = DEFAULT_RANGE if range_dollars is None else range_dollars
    wall_threshold = DEFAULT_WALL if threshold is None else threshold

    login()
    instrument_id = _get_instrument_id(ticker)
    if not instrument_id:
        return {"ticker": ticker, "error": "no instrument found"}
    raw = _fetch_pricebook(instrument_id)
    book = _parse_book(raw)
    return _grade(book, ticker, price, price_range, wall_threshold)
