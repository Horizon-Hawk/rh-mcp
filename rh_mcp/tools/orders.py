"""Order placement, cancellation, and management tools."""

import json as _json
import os
import subprocess
import time
from pathlib import Path

from rh_mcp.lib.rh_client import client
from rh_mcp.auth import default_account


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pre-flight order book check
# ---------------------------------------------------------------------------

_ORDER_BOOK_SCRIPT = Path(
    os.environ.get("RH_ORDER_BOOK_SCRIPT")
    or (Path.home() / "order_book.py")
)


def _preflight_order_book(ticker: str, price: float, side: str = "buy") -> dict:
    """Run order_book.py and return (allowed, reason, book_snapshot).

    Enforces ROBINHOOD.md entry rules at the API layer — prevents accidentally
    placing stock entries without first checking top-of-book pressure, walls,
    and spread. Refuses when:
      - OBI top-10 signals STRONG against trade direction
      - Spread > 1% of price
      - Wall within $0.50 of entry on the entry side

    Pass force=True at the caller to bypass after manual review.
    """
    if not _ORDER_BOOK_SCRIPT.exists():
        return {
            "ok": True,
            "reason": f"order_book.py not found at {_ORDER_BOOK_SCRIPT} — skipping preflight (set RH_ORDER_BOOK_SCRIPT env)",
            "book": None,
        }
    try:
        result = subprocess.run(
            ["python", str(_ORDER_BOOK_SCRIPT), ticker.upper(), f"{price:.2f}"],
            capture_output=True, text=True, timeout=45,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "order_book.py timed out (>45s)", "book": None}
    except Exception as e:
        return {"ok": False, "reason": f"order_book.py failed: {e}", "book": None}

    book = None
    for line in result.stdout.splitlines():
        if line.startswith("JSON: "):
            try:
                book = _json.loads(line[6:])
                break
            except Exception:
                pass
    if book is None:
        return {
            "ok": False,
            "reason": "order_book.py produced no JSON output",
            "stderr": result.stderr[-500:] if result.stderr else "",
            "book": None,
        }

    side = side.lower()
    obi10 = (book.get("obi_10") or {}).get("signal", "BALANCED")
    obi10_ratio = (book.get("obi_10") or {}).get("ratio")
    spread = book.get("spread") or 0.0
    walls_key = "ask_walls" if side == "buy" else "bid_walls"
    walls = book.get(walls_key) or []

    # Refuse: STRONG signal against trade direction
    if side == "buy" and "STRONG SELL" in obi10:
        return {
            "ok": False,
            "reason": (
                f"Pre-flight FAIL: OBI top-10 = STRONG SELL PRESSURE "
                f"(ratio {obi10_ratio}). Framework: skip or downgrade. "
                f"Pass force=True to override after review."
            ),
            "book": book,
        }
    if side == "sell" and "STRONG BUY" in obi10:
        return {
            "ok": False,
            "reason": (
                f"Pre-flight FAIL: OBI top-10 = STRONG BUY PRESSURE "
                f"(ratio {obi10_ratio}) — short into buying. "
                f"Pass force=True to override after review."
            ),
            "book": book,
        }

    # Refuse: spread > 1% of price
    spread_pct = spread / price if price > 0 else 0
    if spread_pct > 0.01:
        return {
            "ok": False,
            "reason": (
                f"Pre-flight FAIL: spread ${spread:.2f} = "
                f"{spread_pct * 100:.2f}% > 1% threshold. Adjust limit_price "
                f"for slippage or pass force=True."
            ),
            "book": book,
        }

    # Refuse: wall within $0.50 of entry on the entry side
    for wall in walls:
        wp = wall.get("price")
        wsz = wall.get("size")
        if wp is None:
            continue
        if side == "buy" and 0 < wp - price <= 0.50:
            return {
                "ok": False,
                "reason": (
                    f"Pre-flight FAIL: ask wall {wsz} sh at ${wp:.2f} within "
                    f"$0.50 of entry ${price:.2f}. Framework: skip or wait. "
                    f"Pass force=True to override."
                ),
                "book": book,
            }
        if side == "sell" and 0 < price - wp <= 0.50:
            return {
                "ok": False,
                "reason": (
                    f"Pre-flight FAIL: bid wall {wsz} sh at ${wp:.2f} within "
                    f"$0.50 of entry ${price:.2f}. Framework: skip or wait. "
                    f"Pass force=True to override."
                ),
                "book": book,
            }

    return {
        "ok": True,
        "reason": f"preflight passed (OBI10 {obi10}, spread ${spread:.2f}, no near walls)",
        "book": book,
    }


# ---------------------------------------------------------------------------
# Stock orders
# ---------------------------------------------------------------------------

def place_long(
    ticker: str,
    quantity: float,
    limit_price: float,
    account_number: str | None = None,
    time_in_force: str = "gfd",
    extended_hours: bool = False,
) -> dict:
    """Place a buy limit order to go long.

    Args:
        ticker: Stock symbol.
        quantity: Number of shares (supports fractional for fractional endpoints).
        limit_price: Limit price (required — no market orders).
        time_in_force: 'gfd' (good for day) or 'gtc' (good till cancel).
        extended_hours: True to allow pre/post-market fills.
    """
    rh = client()
    acct = account_number or default_account()
    sym = ticker.strip().upper()
    result = rh.order_buy_limit(
        symbol=sym,
        quantity=quantity,
        limitPrice=limit_price,
        account_number=acct,
        timeInForce=time_in_force,
        extendedHours=extended_hours,
    )
    if not result or "id" not in result:
        return {"success": False, "error": result.get("detail") if isinstance(result, dict) else "order failed", "raw": result}
    return {
        "success": True,
        "order_id": result["id"],
        "symbol": sym,
        "side": "buy",
        "quantity": quantity,
        "limit_price": limit_price,
        "state": result.get("state"),
    }


def place_short(
    ticker: str,
    quantity: float,
    limit_price: float,
    account_number: str | None = None,
    time_in_force: str = "gfd",
) -> dict:
    """Place a sell-short limit order. Check shortable + borrow fee first."""
    rh = client()
    acct = account_number or default_account()
    sym = ticker.strip().upper()
    result = rh.order_sell_limit(
        symbol=sym,
        quantity=quantity,
        limitPrice=limit_price,
        account_number=acct,
        timeInForce=time_in_force,
    )
    if not result or "id" not in result:
        return {"success": False, "error": result.get("detail") if isinstance(result, dict) else "order failed", "raw": result}
    return {
        "success": True,
        "order_id": result["id"],
        "symbol": sym,
        "side": "sell",
        "quantity": quantity,
        "limit_price": limit_price,
        "state": result.get("state"),
    }


def close_position(
    ticker: str,
    quantity: float | None = None,
    limit_price: float | None = None,
    account_number: str | None = None,
) -> dict:
    """Close a long position. If quantity omitted, closes the full position.

    Args:
        ticker: Stock symbol.
        quantity: Shares to sell. Omit for full position.
        limit_price: Limit price. Omit to use current bid - $0.05 for immediate fill.
    """
    rh = client()
    acct = account_number or default_account()
    sym = ticker.strip().upper()

    # build_holdings() ignores account_number; use the positions endpoint scoped to acct
    try:
        raw_positions = rh.helper.request_get(
            f"https://api.robinhood.com/positions/?account_number={acct}&nonzero=true",
            "pagination",
        ) or []
    except Exception as e:
        return {"success": False, "error": f"positions fetch failed: {e}"}

    held_qty = 0.0
    for p in raw_positions:
        instrument_url = p.get("instrument")
        try:
            psym = rh.stocks.get_symbol_by_url(instrument_url) if instrument_url else None
        except Exception:
            psym = None
        if psym == sym:
            held_qty = float(p.get("quantity") or 0)
            break

    if held_qty == 0:
        return {"success": False, "error": f"no open position in {sym}"}
    qty = quantity if quantity is not None else held_qty
    if qty > held_qty + 0.0001:
        return {"success": False, "error": f"requested {qty} > held {held_qty}"}

    if limit_price is None:
        # Aggressive fill: just below current bid
        quotes = rh.stocks.get_quotes([sym])
        if not quotes or not quotes[0]:
            return {"success": False, "error": "no quote available for pricing"}
        bid = _to_float(quotes[0].get("bid_price")) or _to_float(quotes[0].get("last_trade_price"))
        if bid is None:
            return {"success": False, "error": "no bid available"}
        limit_price = round(bid - 0.05, 2)

    result = rh.order_sell_limit(
        symbol=sym,
        quantity=qty,
        limitPrice=limit_price,
        account_number=acct,
        timeInForce="gfd",
    )
    if not result or "id" not in result:
        return {"success": False, "error": "close order failed", "raw": result}
    return {
        "success": True,
        "order_id": result["id"],
        "symbol": sym,
        "quantity_closed": qty,
        "limit_price": limit_price,
        "state": result.get("state"),
    }


def close_all(
    ticker: str,
    account_number: str | None = None,
) -> dict:
    """Close the FULL position on a ticker, including any fractional residual.

    RH rejects fractional shares on limit orders, so this splits the close
    into two orders:
      1. Whole shares  -> limit sell at the current bid
      2. Fractional    -> market sell

    Returns both order IDs in one response. Either side is skipped if zero.
    """
    rh = client()
    acct = account_number or default_account()
    sym = ticker.strip().upper()

    try:
        raw_positions = rh.helper.request_get(
            f"https://api.robinhood.com/positions/?account_number={acct}&nonzero=true",
            "pagination",
        ) or []
    except Exception as e:
        return {"success": False, "error": f"positions fetch failed: {e}"}

    held_qty = 0.0
    for p in raw_positions:
        instrument_url = p.get("instrument")
        try:
            psym = rh.stocks.get_symbol_by_url(instrument_url) if instrument_url else None
        except Exception:
            psym = None
        if psym == sym:
            held_qty = float(p.get("quantity") or 0)
            break

    if held_qty == 0:
        return {"success": False, "error": f"no open position in {sym}"}

    whole_qty = int(held_qty)
    fractional_qty = round(held_qty - whole_qty, 8)

    quotes = rh.stocks.get_quotes([sym])
    if not quotes or not quotes[0]:
        return {"success": False, "error": "no quote available for pricing"}
    bid = _to_float(quotes[0].get("bid_price")) or _to_float(quotes[0].get("last_trade_price"))
    if bid is None:
        return {"success": False, "error": "no bid available"}

    result = {
        "success": True,
        "symbol": sym,
        "total_quantity": held_qty,
        "whole_quantity": whole_qty,
        "fractional_quantity": fractional_qty,
    }

    if whole_qty > 0:
        whole = rh.order_sell_limit(
            symbol=sym, quantity=whole_qty, limitPrice=round(bid, 2),
            account_number=acct, timeInForce="gfd",
        )
        if not whole or "id" not in whole:
            return {"success": False, "stage": "whole_shares", "error": "whole-share sell failed", "raw": whole, **result}
        result["whole_share_order_id"] = whole["id"]
        result["whole_share_limit_price"] = round(bid, 2)

    if fractional_qty > 0.0001:
        frac = rh.order_sell_market(
            symbol=sym, quantity=fractional_qty,
            account_number=acct, timeInForce="gfd",
        )
        if not frac or "id" not in frac:
            return {"success": False, "stage": "fractional", "error": "fractional sell failed", "raw": frac, **result}
        result["fractional_order_id"] = frac["id"]

    return result


# ---------------------------------------------------------------------------
# Native trailing stop — the big win over Trayd
# ---------------------------------------------------------------------------

def set_trailing_stop(
    ticker: str,
    quantity: float,
    trail_amount: float,
    trail_type: str = "percentage",
    time_in_force: str = "gtc",
) -> dict:
    """Place a native Robinhood trailing stop SELL order on an existing long position.

    Robinhood enforces this server-side — survives chain failures, gap-downs, etc.

    Args:
        ticker: Stock symbol.
        quantity: Shares to protect.
        trail_amount: Trail size. For percentage: 3.0 = 3%. For price: dollar amount.
        trail_type: 'percentage' (default) or 'price'.
        time_in_force: 'gtc' (default, 90-day on RH) or 'gfd'.
    """
    rh = client()
    sym = ticker.strip().upper()
    result = rh.order_sell_trailing_stop(
        symbol=sym,
        quantity=quantity,
        trailAmount=trail_amount,
        trailType=trail_type,
        timeInForce=time_in_force,
    )
    if not result or "id" not in result:
        return {"success": False, "error": "trailing stop order failed", "raw": result}
    return {
        "success": True,
        "order_id": result["id"],
        "symbol": sym,
        "quantity": quantity,
        "trail_amount": trail_amount,
        "trail_type": trail_type,
        "time_in_force": time_in_force,
        "state": result.get("state"),
    }


def set_stop_loss(
    ticker: str,
    quantity: float,
    stop_price: float,
    time_in_force: str = "gtc",
    account_number: str | None = None,
) -> dict:
    """Place a native stop-loss SELL order at a fixed price (not trailing)."""
    rh = client()
    sym = ticker.strip().upper()
    kwargs = {
        "symbol": sym,
        "quantity": quantity,
        "stopPrice": stop_price,
        "timeInForce": time_in_force,
    }
    if account_number:
        kwargs["account_number"] = account_number
    result = rh.order_sell_stop_loss(**kwargs)
    if not result or "id" not in result:
        return {"success": False, "error": "stop-loss order failed", "raw": result}
    return {
        "success": True,
        "order_id": result["id"],
        "symbol": sym,
        "quantity": quantity,
        "stop_price": stop_price,
        "state": result.get("state"),
    }


# ---------------------------------------------------------------------------
# Atomic operations — the differentiators
# ---------------------------------------------------------------------------

def place_with_stop(
    ticker: str,
    quantity: float,
    limit_price: float,
    stop_price: float,
    account_number: str | None = None,
    extended_hours: bool = False,
    fill_timeout_sec: int = 60,
    force: bool = False,
) -> dict:
    """Atomic: order_book preflight → limit BUY → wait for fill → fixed stop-loss.

    PRE-FLIGHT CHECK (new 2026-05-15): Before submitting the entry, runs
    `order_book.py TICKER LIMIT_PRICE` and refuses to place if:
      - OBI top-10 = STRONG SELL PRESSURE  (per feedback_obi_thresholds)
      - Spread > 1% of price (per ROBINHOOD.md)
      - Ask wall within $0.50 of entry  (per ROBINHOOD.md)
    Pass force=True to bypass after manual review.

    For trailing stops use `place_with_trailing_stop` instead.

    Robinhood does not support true OCO brackets via API — combining stop + take-profit
    creates two independent orders that don't unlink when one fills. We don't pretend
    otherwise. Use partial exits via order_book signals (per ROBINHOOD.md +2R rule) for
    the take-profit side; this tool handles only the entry + downside protection.

    Args:
        ticker: Stock symbol.
        quantity: Shares.
        limit_price: Entry limit price.
        stop_price: Native stop-loss level.
        fill_timeout_sec: Seconds to wait for entry fill before bailing.
        force: Bypass the order-book pre-flight check after manual review.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    # Pre-flight: order book check — refuse on STRONG opposing signal / wide spread / near wall
    if not force:
        preflight = _preflight_order_book(sym, limit_price, side="buy")
        if not preflight["ok"]:
            return {
                "success": False,
                "stage": "preflight_order_book",
                "error": preflight["reason"],
                "order_book": preflight.get("book"),
                "hint": "Set force=True to override after reviewing the book.",
            }
        # Bubble up the snapshot so caller sees what the book looked like at entry
        preflight_snapshot = preflight.get("book")
    else:
        preflight_snapshot = {"forced": True}

    entry = place_long(sym, quantity, limit_price, account_number=acct, extended_hours=extended_hours)
    if not entry.get("success"):
        return {"success": False, "stage": "entry", "error": entry.get("error"), "raw": entry}

    order_id = entry["order_id"]
    fill_state, filled_qty = None, 0.0
    for _ in range(fill_timeout_sec):
        info = rh.get_stock_order_info(order_id) or {}
        fill_state = info.get("state")
        filled_qty = _to_float(info.get("cumulative_quantity")) or 0.0
        if fill_state in ("filled", "cancelled", "failed", "rejected"):
            break
        time.sleep(1)
    if fill_state != "filled" or filled_qty < quantity - 0.0001:
        return {
            "success": False, "stage": "fill_wait",
            "error": f"entry not fully filled (state={fill_state}, filled={filled_qty}/{quantity})",
            "entry_order_id": order_id,
        }

    stop_result = set_stop_loss(sym, filled_qty, stop_price, time_in_force="gtc")
    if not stop_result.get("success"):
        return {
            "success": False, "stage": "stop_loss",
            "entry_order_id": order_id, "filled_quantity": filled_qty,
            "error": stop_result.get("error"), "raw": stop_result,
        }

    return {
        "success": True,
        "entry_order_id": order_id,
        "stop_order_id": stop_result["order_id"],
        "symbol": sym,
        "filled_quantity": filled_qty,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "preflight_order_book": preflight_snapshot,
    }


def _cancel_orders_for_ticker(sym: str, account_number: str | None, side: str = "sell") -> list:
    """Cancel all open orders for a ticker on the given side ('sell' or 'buy').
    Returns list of cancelled order summaries."""
    rh = client()
    open_orders = rh.get_all_open_stock_orders(account_number=account_number) or []
    cancelled = []
    for o in open_orders:
        if not o.get("instrument"):
            continue
        inst = rh.get_instrument_by_url(o["instrument"])
        if not inst or inst.get("symbol", "").upper() != sym:
            continue
        if o.get("side") != side:
            continue
        try:
            rh.cancel_stock_order(o["id"])
            cancelled.append({
                "id": o["id"],
                "type": o.get("type"),
                "trigger": o.get("trigger"),
                "stop_price": _to_float(o.get("stop_price")),
                "price": _to_float(o.get("price")),
            })
        except Exception as e:
            cancelled.append({"id": o["id"], "error": str(e)})
    return cancelled


def _cancel_sell_side_orders_for_ticker(sym: str, account_number: str | None) -> list:
    """Backwards-compatible alias."""
    return _cancel_orders_for_ticker(sym, account_number, side="sell")


def cover_short(
    ticker: str,
    quantity: float,
    limit_price: float | None = None,
    cancel_existing: bool = True,
    account_number: str | None = None,
) -> dict:
    """Cover a short position. Short-side equivalent of flatten_position.

    Cancels any open buy-side orders for the ticker (existing stops/covers), then places
    a BUY order to close the short. Defaults to market BUY (matches RH app's "Close Position").

    Args:
        ticker: Stock symbol.
        quantity: Number of shares to cover (required — RH's short tracking is awkward
                  via API, so we require explicit qty rather than auto-detecting).
        limit_price: Optional limit. Omit for market BUY.
        cancel_existing: Default True — cancels existing buy-side orders first.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    cancelled = []
    if cancel_existing:
        cancelled = _cancel_orders_for_ticker(sym, acct, side="buy")
        if cancelled:
            time.sleep(1.5)

    if limit_price is None:
        result = rh.order_buy_market(
            symbol=sym, quantity=quantity,
            account_number=acct, timeInForce="gfd",
        )
        order_type = "market"
    else:
        result = rh.order_buy_limit(
            symbol=sym, quantity=quantity, limitPrice=limit_price,
            account_number=acct, timeInForce="gfd",
        )
        order_type = "limit"

    if not result or "id" not in result:
        return {"success": False, "stage": "cover_order", "error": "cover order failed",
                "cancelled_orders": cancelled, "raw": result}

    return {
        "success": True, "symbol": sym,
        "cancelled_orders": cancelled,
        "cancelled_count": len(cancelled),
        "exit_order_id": result["id"],
        "order_type": order_type,
        "quantity_covered": quantity,
        "limit_price": limit_price,
    }


def adjust_trailing_stop(
    ticker: str,
    new_trail_percent: float,
    account_number: str | None = None,
) -> dict:
    """Modify the trailing stop on an existing long position.

    Cancels current trail (and any other sell-side orders for the ticker), then sets
    a new trailing stop on remaining shares at the new percentage. Use to tighten
    or loosen the trail without changing position size.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    cancelled = _cancel_orders_for_ticker(sym, acct, side="sell")
    if cancelled:
        time.sleep(1.5)

    holdings = rh.build_holdings() or {}
    if sym not in holdings:
        return {"success": False, "stage": "position",
                "error": f"no long position in {sym}", "cancelled_orders": cancelled,
                "note": "stop cancelled — re-set manually if intended"}
    qty = float(holdings[sym].get("quantity") or 0)
    if qty < 0.0001:
        return {"success": False, "stage": "position",
                "error": f"qty={qty} too small", "cancelled_orders": cancelled}

    trail = set_trailing_stop(sym, qty, new_trail_percent, trail_type="percentage")
    if not trail.get("success"):
        return {"success": False, "stage": "trail_set",
                "cancelled_orders": cancelled, "error": "new trail failed",
                "note": "SHARES UNPROTECTED — manually re-attach a stop", "raw": trail}

    return {
        "success": True, "symbol": sym,
        "cancelled_orders": cancelled,
        "cancelled_count": len(cancelled),
        "new_trail_order_id": trail["order_id"],
        "trail_percent": new_trail_percent,
        "quantity": qty,
    }


def flatten_position(
    ticker: str,
    limit_price: float | None = None,
    account_number: str | None = None,
) -> dict:
    """Fully exit a position the same way the RH app's "Close Position" button does:
    cancel all open sell-side orders for the ticker, then MARKET sell remaining shares.

    Handles the RH constraint where shares attached to a stop can't have a second sell
    order placed on them — cancels first, exits second.

    Args:
        ticker: Stock symbol.
        limit_price: Optional sell limit. Omit for market sell (matches the app's button).
                     Provide a price when slippage matters (thin liquidity, AH, etc.).
        account_number: Robinhood account number. Omit for default.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    cancelled = _cancel_sell_side_orders_for_ticker(sym, acct)
    if cancelled:
        time.sleep(1.5)

    holdings = rh.build_holdings() or {}
    if sym not in holdings:
        return {
            "success": True, "symbol": sym, "cancelled_orders": cancelled,
            "exit_order_id": None, "note": "no shares held; orders cancelled only",
        }
    qty = float(holdings[sym].get("quantity") or 0)
    if qty < 0.0001:
        return {
            "success": True, "symbol": sym, "cancelled_orders": cancelled,
            "exit_order_id": None, "note": f"qty={qty} too small to sell",
        }

    if limit_price is None:
        # Match the app's behavior — market sell
        result = rh.order_sell_market(
            symbol=sym, quantity=qty,
            account_number=acct, timeInForce="gfd",
        )
        order_type = "market"
    else:
        result = rh.order_sell_limit(
            symbol=sym, quantity=qty, limitPrice=limit_price,
            account_number=acct, timeInForce="gfd",
        )
        order_type = "limit"

    if not result or "id" not in result:
        return {"success": False, "stage": "exit_order", "error": "exit order failed",
                "cancelled_orders": cancelled, "raw": result}

    return {
        "success": True, "symbol": sym,
        "cancelled_orders": cancelled,
        "cancelled_count": len(cancelled),
        "exit_order_id": result["id"],
        "order_type": order_type,
        "quantity_exited": qty,
        "limit_price": limit_price,
    }


def partial_with_trail_rearm(
    ticker: str,
    partial_quantity: float,
    exit_limit_price: float | None = None,
    trail_percent: float = 3.0,
    account_number: str | None = None,
    fill_timeout_sec: int = 60,
) -> dict:
    """Atomic +2R-style partial exit: cancel existing trail, sell partial, re-set trail on remainder.

    Required because RH locks shares to the first sell-side order. Single tool handles the
    3-step dance: cancel-existing-stop → partial-sell → re-arm-trail-on-remaining.

    If partial fails to fill: ABORTS without re-setting trail (shares unprotected, user must
    manually re-attach a stop). Caller should check the return value's `stage` field.

    Args:
        ticker: Stock symbol.
        partial_quantity: Shares to sell (must be < total held).
        exit_limit_price: Sell limit. Omit for bid - $0.05.
        trail_percent: New trailing stop percentage on remaining shares (default 3.0).
        fill_timeout_sec: Seconds to wait for partial fill.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    cancelled = _cancel_sell_side_orders_for_ticker(sym, acct)
    if cancelled:
        time.sleep(1.5)

    holdings = rh.build_holdings() or {}
    if sym not in holdings:
        return {"success": False, "stage": "position",
                "error": f"no position in {sym}", "cancelled_orders": cancelled,
                "note": "stop already cancelled — re-set protection manually if needed"}
    held_qty = float(holdings[sym].get("quantity") or 0)
    if partial_quantity > held_qty + 0.0001:
        return {"success": False, "stage": "position",
                "error": f"requested {partial_quantity} > held {held_qty}",
                "cancelled_orders": cancelled,
                "note": "stop already cancelled — re-set protection manually"}

    if exit_limit_price is None:
        # Market sell — match the app's exit behavior
        partial = rh.order_sell_market(
            symbol=sym, quantity=partial_quantity,
            account_number=acct, timeInForce="gfd",
        )
    else:
        partial = rh.order_sell_limit(
            symbol=sym, quantity=partial_quantity, limitPrice=exit_limit_price,
            account_number=acct, timeInForce="gfd",
        )
    if not partial or "id" not in partial:
        return {"success": False, "stage": "partial_sell", "error": "partial sell order rejected",
                "cancelled_orders": cancelled, "raw": partial,
                "note": "stop already cancelled — re-set protection manually"}

    partial_id = partial["id"]
    fill_state, partial_filled = None, 0.0
    for _ in range(fill_timeout_sec):
        info = rh.get_stock_order_info(partial_id) or {}
        fill_state = info.get("state")
        partial_filled = _to_float(info.get("cumulative_quantity")) or 0.0
        if fill_state in ("filled", "cancelled", "failed", "rejected"):
            break
        time.sleep(1)

    if fill_state != "filled":
        return {
            "success": False, "stage": "partial_fill_wait",
            "cancelled_orders": cancelled, "partial_order_id": partial_id,
            "partial_filled": partial_filled,
            "error": f"partial did not fill (state={fill_state})",
            "note": "remaining shares unprotected — original stop cancelled, trail not yet re-armed. Set protection manually.",
        }

    remaining = held_qty - partial_filled
    if remaining < 0.0001:
        return {
            "success": True, "symbol": sym,
            "cancelled_orders": cancelled, "partial_order_id": partial_id,
            "partial_filled": partial_filled, "remaining": 0,
            "new_trail_order_id": None,
            "note": "partial closed the full position; no trail to re-arm",
        }

    trail = set_trailing_stop(sym, remaining, trail_percent, trail_type="percentage")
    if not trail.get("success"):
        return {
            "success": False, "stage": "trail_rearm",
            "cancelled_orders": cancelled, "partial_order_id": partial_id,
            "partial_filled": partial_filled, "remaining": remaining,
            "error": "trail re-arm failed — REMAINING SHARES UNPROTECTED",
            "raw": trail,
        }

    return {
        "success": True, "symbol": sym,
        "cancelled_orders": cancelled,
        "partial_order_id": partial_id,
        "partial_filled": partial_filled,
        "exit_limit_price": exit_limit_price,
        "remaining": remaining,
        "new_trail_order_id": trail["order_id"],
        "trail_percent": trail_percent,
    }


def place_with_trailing_stop(
    ticker: str,
    quantity: float,
    limit_price: float,
    trail_percent: float = 3.0,
    account_number: str | None = None,
    extended_hours: bool = False,
    fill_timeout_sec: int = 60,
) -> dict:
    """Atomic: place a limit BUY, wait for fill, then immediately set a native trailing stop.

    This eliminates the gap between fill and stop placement where positions sit unprotected.

    Args:
        ticker: Stock symbol.
        quantity: Shares.
        limit_price: Entry limit price.
        trail_percent: Trailing stop percentage (default 3.0%).
        fill_timeout_sec: Seconds to wait for entry fill before bailing.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()

    entry = place_long(sym, quantity, limit_price, account_number=acct, extended_hours=extended_hours)
    if not entry.get("success"):
        return {"success": False, "stage": "entry", "error": entry.get("error"), "raw": entry}

    order_id = entry["order_id"]
    fill_state = None
    filled_qty = 0.0
    for _ in range(fill_timeout_sec):
        info = rh.get_stock_order_info(order_id) or {}
        fill_state = info.get("state")
        filled_qty = _to_float(info.get("cumulative_quantity")) or 0.0
        if fill_state in ("filled", "cancelled", "failed", "rejected"):
            break
        time.sleep(1)

    if fill_state != "filled" or filled_qty < quantity - 0.0001:
        return {
            "success": False,
            "stage": "fill_wait",
            "error": f"entry not fully filled (state={fill_state}, filled={filled_qty}/{quantity})",
            "entry_order_id": order_id,
        }

    trail = set_trailing_stop(sym, filled_qty, trail_percent, trail_type="percentage")
    if not trail.get("success"):
        return {
            "success": False,
            "stage": "trailing_stop",
            "entry_order_id": order_id,
            "filled_quantity": filled_qty,
            "error": trail.get("error"),
            "raw": trail,
        }

    return {
        "success": True,
        "entry_order_id": order_id,
        "trailing_stop_order_id": trail["order_id"],
        "symbol": sym,
        "filled_quantity": filled_qty,
        "limit_price": limit_price,
        "trail_percent": trail_percent,
    }


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def cancel_order(order_id: str) -> dict:
    """Cancel a single open order by ID. Auto-detects whether the ID is a
    stock or option order: tries stock cancel first, falls back to option
    cancel on not-found. Explicit callers can use `cancel_option_order`
    directly to skip the fallback path.
    """
    rh = client()
    # Stock first — by far the more common case
    try:
        result = rh.cancel_stock_order(order_id)
    except Exception:
        result = None
    if result is not None:
        return {"success": True, "order_id": order_id, "kind": "stock", "raw": result}
    # Fall back to option
    try:
        result = rh.orders.cancel_option_order(order_id)
    except Exception:
        result = None
    if result is not None:
        return {"success": True, "order_id": order_id, "kind": "option", "raw": result}
    return {"success": False, "error": "cancel failed — order not found as stock or option"}


def cancel_option_order(order_id: str) -> dict:
    """Cancel a single open OPTION order by ID. Explicit alternative to
    cancel_order's auto-dispatch — use when you already know the order is
    an option order and want to skip the stock-first probe."""
    rh = client()
    try:
        result = rh.orders.cancel_option_order(order_id)
    except Exception as e:
        return {"success": False, "error": f"option cancel failed: {e}"}
    if result is None:
        return {"success": False, "error": "option cancel failed — order may not exist"}
    return {"success": True, "order_id": order_id, "raw": result}


def cancel_all_orders(account_number: str | None = None) -> dict:
    """Cancel ALL open orders — both stock and option. Use with caution."""
    rh = client()
    stock_result = rh.cancel_all_stock_orders()
    option_result = None
    try:
        option_result = rh.orders.cancel_all_option_orders()
    except Exception as e:
        option_result = {"error": str(e)}
    return {"success": True, "stock_cancelled": stock_result, "option_cancelled": option_result}


def _option_order_summary(info: dict) -> dict:
    """Common-shape summary for an option order — used by both
    get_option_order_status and the auto-dispatch fallback in get_order_status."""
    legs = []
    for leg in (info.get("legs") or []):
        opt_url = leg.get("option") or ""
        opt_id = opt_url.rstrip("/").rsplit("/", 1)[-1] or None if opt_url else None
        executions = leg.get("executions") or []
        filled_qty = sum(_to_float(ex.get("quantity")) or 0 for ex in executions)
        legs.append({
            "option_id": opt_id,
            "side": leg.get("side"),
            "position_effect": leg.get("position_effect"),
            "ratio_quantity": leg.get("ratio_quantity"),
            "filled_quantity": filled_qty or None,
        })
    return {
        "success": True,
        "kind": "option",
        "order_id": info.get("id"),
        "state": info.get("state"),
        "type": info.get("type"),
        "direction": info.get("direction"),
        "ticker": info.get("chain_symbol"),
        "quantity": _to_float(info.get("quantity")),
        "processed_quantity": _to_float(info.get("processed_quantity")),
        "price": _to_float(info.get("price")),
        "premium": _to_float(info.get("premium")),
        "processed_premium": _to_float(info.get("processed_premium")),
        "time_in_force": info.get("time_in_force"),
        "trigger": info.get("trigger"),
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
        "legs": legs,
    }


def get_option_order_status(order_id: str) -> dict:
    """Get status of a specific OPTION order by ID."""
    rh = client()
    try:
        info = rh.orders.get_option_order_info(order_id)
    except Exception as e:
        return {"success": False, "error": f"option order lookup failed: {e}"}
    if not info:
        return {"success": False, "error": "option order not found"}
    return _option_order_summary(info)


def get_order_status(order_id: str) -> dict:
    """Get status of a specific order. Auto-detects stock vs option by ID:
    tries the stock endpoint first, falls back to option on not-found.
    Use `get_option_order_status` directly if you already know it's an
    option order and want to skip the probe.
    """
    rh = client()
    info = rh.get_stock_order_info(order_id)
    if info:
        return {
            "success": True,
            "kind": "stock",
            "order_id": info.get("id"),
            "state": info.get("state"),
            "side": info.get("side"),
            "type": info.get("type"),
            "trigger": info.get("trigger"),
            "quantity": _to_float(info.get("quantity")),
            "cumulative_quantity": _to_float(info.get("cumulative_quantity")),
            "price": _to_float(info.get("price")),
            "stop_price": _to_float(info.get("stop_price")),
            "average_price": _to_float(info.get("average_price")),
            "created_at": info.get("created_at"),
            "updated_at": info.get("updated_at"),
        }
    # Fall back to option lookup
    try:
        opt_info = rh.orders.get_option_order_info(order_id)
    except Exception:
        opt_info = None
    if opt_info:
        return _option_order_summary(opt_info)
    return {"success": False, "error": "order not found as stock or option"}

def get_orders_status_batch(order_ids: list[str]) -> dict:
    """Get status for multiple orders in one call (any mix of stock/option IDs).

    Returns dict keyed by order_id with each value being the same shape as
    get_order_status. Failed lookups return {"success": False, "error": ...}
    for that ID — the overall call succeeds.
    """
    results = {}
    for oid in order_ids or []:
        try:
            results[oid] = get_order_status(oid)
        except Exception as e:
            results[oid] = {"success": False, "error": str(e)}
    return {
        "success": True,
        "count": len(results),
        "orders": results,
    }


def stage_stop_safe(
    ticker: str,
    quantity: float,
    stop_price: float,
    time_in_force: str = "gtc",
    account_number: str | None = None,
) -> dict:
    """Place a stop-loss with graceful handling of settlement-lockup rejections.

    Tries set_stop_loss. If the broker rejects with PDT/settlement language
    (typical when the underlying shares were bought with unsettled funds
    earlier the same day), returns a structured "deferred" result so the
    caller can retry after T+1 settlement clears, instead of treating the
    rejection as a hard error.

    Returns one of:
      success=True  → stop placed (order_id included)
      success=False, deferred=True → settlement lockup; retry next session
      success=False, deferred=False → real failure
    """
    result = set_stop_loss(ticker, quantity, stop_price, time_in_force, account_number)
    if result.get("success"):
        return result

    raw = result.get("raw") or {}
    detail = ""
    if isinstance(raw, dict):
        detail = str(raw.get("detail", ""))
    is_settlement_block = (
        "PDT" in detail
        or "Pattern Day" in detail
        or "good faith" in detail.lower()
        or "settlement" in detail.lower()
    )

    if is_settlement_block:
        return {
            "success": False,
            "deferred": True,
            "reason": "settlement_lockup",
            "detail": detail or "broker rejected — likely settlement lockup",
            "ticker": ticker.upper(),
            "quantity": quantity,
            "stop_price": stop_price,
            "retry_when": "next trading session after T+1 settlement",
        }
    return {**result, "deferred": False}
