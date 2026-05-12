"""Liquidity guard — pre-entry order-book sanity check for small caps.

Small caps (< $1B market cap) have thin books; spreads blow out, walls hide
on either side, and aggressive market orders eat 1-3% to fill. This guard
runs the existing order_book.py scan with tighter thresholds and reports
pass/fail with a reason — designed to be called by the alert pipeline
right before placing an order, or as a standalone manual check.

Returns {success, ok, ticker, price, market_cap, spread_pct, depth_levels,
ask_wall_distance, bid_wall_distance, reason}.
"""

from __future__ import annotations

from rh_mcp.analysis import order_book as _ob

try:
    import yfinance as yf
except ImportError as e:
    raise ImportError("liquidity_guard requires yfinance — pip install yfinance") from e


# Tighter than the standard order_book defaults — small caps deserve stricter
# vetting because slippage is worse and the alert pipeline auto-fires orders.
SMALL_CAP_THRESHOLD = 1_000_000_000        # below this = small cap mode
SMALL_CAP_MAX_SPREAD_PCT = 0.75            # 0.75% max spread to greenlight entry
SMALL_CAP_MIN_DEPTH_LEVELS = 30            # at least 30 levels of book depth
SMALL_CAP_MIN_WALL_DISTANCE_PCT = 0.35     # entry-side wall must be ≥0.35% away
LARGE_CAP_MAX_SPREAD_PCT = 1.0
LARGE_CAP_MIN_DEPTH_LEVELS = 20


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _market_cap(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker.strip().upper()).info
        return _to_float(info.get("marketCap"))
    except Exception:
        return None


def check(
    ticker: str,
    price: float,
    side: str = "long",
    market_cap: float | None = None,
) -> dict:
    """Run order-book check with small-cap-aware thresholds.

    Args:
        ticker: stock symbol.
        price: current price (used to size scan range).
        side: 'long' (check ask walls) or 'short' (check bid walls).
        market_cap: pass in if already known. Auto-fetched if omitted.

    Returns:
        ok=True if entry is safe; otherwise a `reason` explains the failure.
    """
    sym = ticker.strip().upper()
    if market_cap is None:
        market_cap = _market_cap(sym)
    is_small_cap = market_cap is not None and market_cap < SMALL_CAP_THRESHOLD
    max_spread = SMALL_CAP_MAX_SPREAD_PCT if is_small_cap else LARGE_CAP_MAX_SPREAD_PCT
    min_depth = SMALL_CAP_MIN_DEPTH_LEVELS if is_small_cap else LARGE_CAP_MIN_DEPTH_LEVELS
    min_wall_dist = SMALL_CAP_MIN_WALL_DISTANCE_PCT if is_small_cap else 0.0

    # Range: scale with price (cheap stock → tight, expensive stock → wide).
    # Mirrors order_book.py convention (it takes `range_dollars`).
    if price < 20:
        range_dollars = 2.0
    elif price < 100:
        range_dollars = 5.0
    else:
        range_dollars = 10.0

    # Lower wall threshold for thinly traded names — 1K shares is a real
    # wall on a $500M cap, not a normal 10K threshold.
    wall_threshold = 1_000 if is_small_cap else 5_000

    try:
        scan = _ob.analyze(
            ticker=sym, price=price,
            range_dollars=range_dollars, threshold=wall_threshold,
        )
    except Exception as e:
        return {
            "success": False, "ok": False, "ticker": sym, "price": price,
            "error": f"order_book scan failed: {e}",
        }
    if "error" in scan:
        return {
            "success": False, "ok": False, "ticker": sym, "price": price,
            "error": scan["error"],
        }

    best_bid = _to_float(scan.get("best_bid"))
    best_ask = _to_float(scan.get("best_ask"))
    spread = _to_float(scan.get("spread"))
    spread_pct = (spread / price * 100) if (spread is not None and price) else None
    depth_levels = scan.get("total_book_levels") or scan.get("levels_in_range") or 0

    # Entry-side wall distance: for longs, distance to nearest ASK wall above
    # the current price; for shorts, distance to nearest BID wall below.
    wall_dist_pct = None
    if side == "long":
        for w in (scan.get("ask_walls") or []):
            wprice = _to_float(w.get("price"))
            if wprice is None or wprice <= price:
                continue
            d = (wprice - price) / price * 100
            if wall_dist_pct is None or d < wall_dist_pct:
                wall_dist_pct = d
    else:
        for w in (scan.get("bid_walls") or []):
            wprice = _to_float(w.get("price"))
            if wprice is None or wprice >= price:
                continue
            d = (price - wprice) / price * 100
            if wall_dist_pct is None or d < wall_dist_pct:
                wall_dist_pct = d

    failing: list[str] = []
    if spread_pct is None:
        failing.append("spread_unknown")
    elif spread_pct > max_spread:
        failing.append(f"spread {spread_pct:.2f}% > {max_spread}%")
    if depth_levels < min_depth:
        failing.append(f"depth {depth_levels} < {min_depth}")
    if min_wall_dist > 0 and wall_dist_pct is not None and wall_dist_pct < min_wall_dist:
        failing.append(f"wall {wall_dist_pct:.2f}% < {min_wall_dist}%")

    ok = len(failing) == 0
    return {
        "success": True,
        "ok": ok,
        "ticker": sym,
        "price": price,
        "side": side,
        "market_cap": market_cap,
        "is_small_cap": is_small_cap,
        "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        "depth_levels": depth_levels,
        "wall_distance_pct": round(wall_dist_pct, 3) if wall_dist_pct is not None else None,
        "thresholds": {
            "max_spread_pct": max_spread,
            "min_depth_levels": min_depth,
            "min_wall_distance_pct": min_wall_dist,
        },
        "reason": "ok" if ok else "; ".join(failing),
    }
