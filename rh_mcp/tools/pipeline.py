"""High-level pipeline tools — orchestrate multiple primitives in one call.

Tools here are composites that bundle 3-8 underlying operations into a single
MCP-callable workflow. Each catches per-step failures gracefully and returns a
structured report including any partial successes.

Flagship tools:
- execute_alert: alert → grade → place → protect → log → deactivate
- monitor_position: bars + position → pattern scan → hold/exit decision
- pre_market_scan: portfolio + positions + orders + movers + analysis → morning brief
- auto_grade_setup: stack + book + historicals + earnings + news → final grade
- eod_brief: realized P&L + open positions + alerts fired → daily wrap
"""

import time
from datetime import datetime, date

from rh_mcp.lib.rh_client import client
from rh_mcp.auth import default_account
from rh_mcp.tools import account as account_mod
from rh_mcp.tools import orders as orders_mod
from rh_mcp.tools import alerts as alerts_mod
from rh_mcp.tools import trade_log as trade_log_mod
from rh_mcp.tools import sizing as sizing_mod
from rh_mcp.tools import quotes as quotes_mod
from rh_mcp.tools import analysis as analysis_mod
from rh_mcp.tools import scanners as scanners_mod


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def execute_alert(
    ticker: str,
    entry: float,
    stop: float,
    target: float | None = None,
    direction: str = "long",
    grade: str = "A",
    risk_pct: float | None = None,
    trail_percent: float = 3.0,
    strategy: str = "momentum_breakout",
    thesis: str = "",
    account_number: str | None = None,
    deactivate_alert: bool = True,
    fill_timeout_sec: int = 90,
) -> dict:
    """Run the full alert→execute→protect→log→deactivate pipeline atomically.

    Steps:
      1. Compute shares via position_size_helper (uses framework tiered risk per grade).
      2. Place limit BUY (or sell-short) at entry.
      3. Wait for fill (up to fill_timeout_sec).
      4. Set native trailing stop on filled shares.
      5. Call auto_stage_trade_card to write STOP/+1R/+2R/TARGET alerts.
      6. Log open via trade_logger.
      7. Deactivate matching alert(s) in price_alerts.json.

    On any failure mid-pipeline, returns a structured error with `stage` field naming where
    it failed. Position may be left in a partial state (e.g., entry filled but trail not set);
    response will indicate what's done and what's not.

    Args:
        ticker: Stock symbol.
        entry: Entry limit price.
        stop: Stop-loss price (for sizing math AND alert staging).
        target: Optional measured-move target for alert staging.
        direction: 'long' (default — long stock breakout). 'short' uses sell-short.
        grade: 'A+' / 'A' / 'B' → tiered risk per framework.
        risk_pct: Override grade-based risk %. Omit to use grade tier.
        trail_percent: Trailing stop percentage (default 3.0).
        strategy: For trade_logger ('momentum_breakout' / 'earnings_drift' / etc).
        thesis: Free-text setup description.
        deactivate_alert: True (default) — deactivates matching alert(s) in price_alerts.json on success.
    """
    rh = client()
    sym = ticker.strip().upper()
    acct = account_number or default_account()
    direction = direction.lower()

    # --- STEP 1: SIZING --------------------------------------------------
    sizing = sizing_mod.position_size_helper(
        entry=entry, stop=stop, risk_pct=risk_pct, grade=grade,
        account_number=acct,
    )
    if not sizing.get("success"):
        return {"success": False, "stage": "sizing", "error": sizing.get("error"), "raw": sizing}
    shares = sizing["shares_whole"]
    if shares < 1:
        return {"success": False, "stage": "sizing",
                "error": f"computed shares={shares} (< 1). Equity={sizing['equity']}, "
                         f"risk_per_share=${sizing['risk_per_share']}",
                "sizing": sizing}

    # --- STEP 2: PLACE ENTRY ----------------------------------------------
    if direction == "long":
        entry_result = orders_mod.place_long(sym, shares, entry, account_number=acct)
    elif direction == "short":
        entry_result = orders_mod.place_short(sym, shares, entry, account_number=acct)
    else:
        return {"success": False, "stage": "entry", "error": f"invalid direction '{direction}'"}

    if not entry_result.get("success"):
        return {"success": False, "stage": "entry",
                "error": entry_result.get("error"), "sizing": sizing, "raw": entry_result}

    entry_order_id = entry_result["order_id"]

    # --- STEP 3: WAIT FOR FILL --------------------------------------------
    fill_state, filled_qty = None, 0.0
    for _ in range(fill_timeout_sec):
        info = rh.get_stock_order_info(entry_order_id) or {}
        fill_state = info.get("state")
        filled_qty = _to_float(info.get("cumulative_quantity")) or 0.0
        if fill_state in ("filled", "cancelled", "failed", "rejected"):
            break
        time.sleep(1)
    if fill_state != "filled" or filled_qty < shares - 0.0001:
        return {"success": False, "stage": "fill_wait",
                "entry_order_id": entry_order_id,
                "error": f"entry not fully filled (state={fill_state}, filled={filled_qty}/{shares})",
                "sizing": sizing}

    # --- STEP 4: TRAILING STOP --------------------------------------------
    # Long-only for trailing stop (RH's order_sell_trailing_stop). Shorts skip this step.
    trail_result = None
    if direction == "long":
        trail_result = orders_mod.set_trailing_stop(sym, filled_qty, trail_percent, trail_type="percentage")
        if not trail_result.get("success"):
            return {"success": False, "stage": "trail_set",
                    "entry_order_id": entry_order_id, "filled_quantity": filled_qty,
                    "error": "trail set failed — SHARES UNPROTECTED",
                    "raw": trail_result, "sizing": sizing}

    # --- STEP 5: STAGE TRADE-CARD ALERTS ---------------------------------
    card_result = alerts_mod.auto_stage_trade_card(
        ticker=sym, entry=entry, stop=stop, target=target,
        shares=filled_qty, direction=direction, grade=grade, thesis=thesis,
    )

    # --- STEP 6: LOG OPEN -------------------------------------------------
    log_result = trade_log_mod.log_open(
        account=str(acct), ticker=sym, direction=direction, strategy=strategy,
        entry=entry, shares=filled_qty,
        stop=stop, target=target, thesis=thesis,
    )

    # --- STEP 7: DEACTIVATE SOURCE ALERT(S) ------------------------------
    deactivated = None
    if deactivate_alert:
        deactivated = alerts_mod.deactivate_alert(ticker=sym)

    return {
        "success": True,
        "symbol": sym, "direction": direction,
        "entry": entry, "stop": stop, "target": target,
        "shares": filled_qty,
        "risk_dollars": sizing.get("actual_risk_whole"),
        "risk_pct": sizing.get("actual_risk_pct"),
        "entry_order_id": entry_order_id,
        "trail_order_id": (trail_result or {}).get("order_id"),
        "trail_percent": trail_percent if direction == "long" else None,
        "trade_card_alerts_staged": card_result.get("alerts_added"),
        "log_result": log_result.get("stdout", ""),
        "alerts_deactivated": (deactivated or {}).get("deactivated_count", 0),
        "sizing_detail": sizing,
    }


# ---------------------------------------------------------------------------
# Position monitor — 2-min pattern scanner per ROBINHOOD.md
# ---------------------------------------------------------------------------

def _candle_body_pct(o: float, c: float, h: float, l: float) -> float:
    """Body size as a percentage of the candle's high-low range."""
    rng = h - l
    if rng <= 0:
        return 0.0
    return abs(c - o) / rng * 100


def monitor_position(
    ticker: str,
    direction: str = "long",
    entry_price: float | None = None,
    interval: str = "5minute",
    account_number: str | None = None,
) -> dict:
    """Pattern-based exit-signal scan for an open position. Returns hold/exit decision.

    Patterns detected (only relevant when trade is in profit):
    - Double top (long) / Double bottom (short): two extremes within 0.5% of each other
      after price moved toward target.
    - Two consecutive 60%+ body counter-candles: momentum has flipped against the trade.
    - Strong reversal candle (70%+ body) at the trade's best level: decisive rejection.

    Args:
        ticker: Stock symbol.
        direction: 'long' or 'short' — the position direction.
        entry_price: Optional entry price for P&L calc. If omitted, fetched from holdings (long only).
        interval: Bar interval to scan (default '5minute').
    """
    sym = ticker.strip().upper()
    direction = direction.lower()

    bars_result = quotes_mod.get_bars(sym, interval=interval, span="day")
    if not bars_result.get("success") or not bars_result.get("bars"):
        return {"success": False, "stage": "bars", "error": "no bars returned"}
    bars = bars_result["bars"]
    if len(bars) < 6:
        return {"success": False, "stage": "bars", "error": f"only {len(bars)} bars — need ≥6 for pattern scan"}

    quote_result = quotes_mod.get_quote(sym)
    last_price = None
    if quote_result.get("success") and quote_result.get("quotes"):
        last_price = quote_result["quotes"][0].get("last_trade_price")
    if last_price is None:
        last_price = bars[-1].get("close")

    if entry_price is None and direction == "long":
        rh = client()
        holdings = rh.build_holdings() or {}
        if sym in holdings:
            try:
                entry_price = float(holdings[sym].get("average_buy_price") or 0)
            except (TypeError, ValueError):
                entry_price = None

    profit_pct = None
    in_profit = None
    if entry_price and last_price:
        if direction == "long":
            profit_pct = (last_price - entry_price) / entry_price * 100
        else:
            profit_pct = (entry_price - last_price) / entry_price * 100
        in_profit = profit_pct > 0

    # Pattern scan over last 6 completed bars
    recent = bars[-6:]
    pattern = None
    pattern_detail = None

    # 1. Double top (long) / double bottom (short) — last 6 bars
    extremes = []
    for b in recent:
        if direction == "long":
            extremes.append(b.get("high") or 0)
        else:
            extremes.append(b.get("low") or 0)
    if extremes:
        # Find two values within 0.5% of each other with at least one bar between them
        for i in range(len(extremes)):
            for j in range(i + 2, len(extremes)):
                if abs(extremes[i] - extremes[j]) / max(extremes[i], 0.0001) < 0.005:
                    pattern = "double_top" if direction == "long" else "double_bottom"
                    pattern_detail = {
                        "bar1_idx": i, "bar1_value": extremes[i],
                        "bar2_idx": j, "bar2_value": extremes[j],
                        "spread_pct": abs(extremes[i] - extremes[j]) / max(extremes[i], 0.0001) * 100,
                    }
                    break
            if pattern:
                break

    # 2. Two consecutive 60%+ body counter-candles
    if not pattern and len(recent) >= 2:
        last_two = recent[-2:]
        bodies = []
        for b in last_two:
            o = b.get("open") or 0
            c = b.get("close") or 0
            h = b.get("high") or 0
            l = b.get("low") or 0
            counter = (direction == "long" and c < o) or (direction == "short" and c > o)
            body_pct = _candle_body_pct(o, c, h, l)
            bodies.append({"counter": counter, "body_pct": body_pct})
        if all(b["counter"] and b["body_pct"] >= 60 for b in bodies):
            pattern = "two_counter_candles"
            pattern_detail = {"bodies": bodies}

    # 3. Strong reversal at the trade's best level
    if not pattern and len(recent) >= 2:
        # "best level" = highest high (long) or lowest low (short) across recent bars
        if direction == "long":
            best = max((b.get("high") or 0) for b in recent)
            best_bar_idx = max(range(len(recent)), key=lambda i: recent[i].get("high") or 0)
        else:
            best = min((b.get("low") or 0) for b in recent)
            best_bar_idx = min(range(len(recent)), key=lambda i: recent[i].get("low") or 0)
        # Look for a strong counter-candle near or at that best
        for i in range(max(0, best_bar_idx - 1), len(recent)):
            b = recent[i]
            o, c, h, l = (b.get("open") or 0, b.get("close") or 0,
                          b.get("high") or 0, b.get("low") or 0)
            counter = (direction == "long" and c < o) or (direction == "short" and c > o)
            body_pct = _candle_body_pct(o, c, h, l)
            touches_best = (direction == "long" and h >= best * 0.998) or \
                            (direction == "short" and l <= best * 1.002)
            if counter and body_pct >= 70 and touches_best:
                pattern = "strong_reversal_at_best"
                pattern_detail = {"bar_idx": i, "body_pct": body_pct, "best_level": best,
                                  "bar": {"o": o, "h": h, "l": l, "c": c}}
                break

    # Decision
    action = "hold"
    if pattern and in_profit:
        action = "close"
    elif pattern and not in_profit:
        action = "hold_pattern_unprofitable"  # framework only acts on patterns in profit

    return {
        "success": True,
        "symbol": sym, "direction": direction,
        "entry_price": entry_price,
        "last_price": last_price,
        "profit_pct": profit_pct,
        "in_profit": in_profit,
        "pattern_detected": pattern,
        "pattern_detail": pattern_detail,
        "recommended_action": action,
        "bars_scanned": len(recent),
        "interval": interval,
        "recent_bars": recent,
    }


# ---------------------------------------------------------------------------
# Pre-market scan — morning brief composite
# ---------------------------------------------------------------------------

def pre_market_scan(account_number: str | None = None) -> dict:
    """One-call morning brief: portfolio + positions + open orders + movers + active alerts.

    Aggregates account state and broad-market signals into a single structured report
    you can read before the open. Errors in any sub-component don't kill the report —
    they're surfaced as `null` fields with `_errors` listing what failed.
    """
    errors = []

    # Account state
    try:
        portfolio = account_mod.get_portfolio(account_number)
    except Exception as e:
        portfolio = None
        errors.append(f"portfolio: {e}")

    try:
        positions = account_mod.get_positions(account_number)
    except Exception as e:
        positions = None
        errors.append(f"positions: {e}")

    try:
        open_orders = account_mod.get_open_orders(account_number)
    except Exception as e:
        open_orders = None
        errors.append(f"open_orders: {e}")

    # Active alerts
    try:
        active_alerts = alerts_mod.list_alerts(active_only=True)
    except Exception as e:
        active_alerts = None
        errors.append(f"alerts: {e}")

    # Top movers (gain + loss)
    try:
        movers_up = scanners_mod.top_movers(direction="up", scope="sp500")
    except Exception as e:
        movers_up = None
        errors.append(f"movers_up: {e}")
    try:
        movers_down = scanners_mod.top_movers(direction="down", scope="sp500")
    except Exception as e:
        movers_down = None
        errors.append(f"movers_down: {e}")

    # Market hours
    try:
        hours = quotes_mod.get_market_hours()
    except Exception as e:
        hours = None
        errors.append(f"market_hours: {e}")

    return {
        "success": True,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "market_hours": hours,
        "portfolio": portfolio,
        "positions": positions,
        "open_orders": open_orders,
        "active_alerts": active_alerts,
        "top_movers_up": movers_up,
        "top_movers_down": movers_down,
        "_errors": errors,
    }


# ---------------------------------------------------------------------------
# Auto grade setup — composite grader
# ---------------------------------------------------------------------------

def auto_grade_setup(
    ticker: str,
    price: float | None = None,
) -> dict:
    """One-call composite grader for a potential setup.

    Bundles stack_grade + historicals + order_book + earnings + news. Returns each
    component's verdict + a final A+ / A / B / SKIP grade with reasoning.

    Args:
        ticker: Stock symbol.
        price: Price for order_book scan (default: current last price).
    """
    sym = ticker.strip().upper()
    errors = []

    # Get current price if not provided
    if price is None:
        try:
            q = quotes_mod.get_quote(sym)
            if q.get("success") and q.get("quotes"):
                price = q["quotes"][0].get("last_trade_price")
        except Exception as e:
            errors.append(f"price: {e}")

    # Stack grade
    try:
        stack = analysis_mod.stack_grade(sym)
    except Exception as e:
        stack = {"success": False, "error": str(e)}
        errors.append(f"stack_grade: {e}")

    # Historicals
    try:
        historicals = analysis_mod.historicals(sym)
    except Exception as e:
        historicals = {"success": False, "error": str(e)}
        errors.append(f"historicals: {e}")

    # Order book
    book = None
    if price:
        try:
            book = analysis_mod.order_book_scan(sym, price)
        except Exception as e:
            book = {"success": False, "error": str(e)}
            errors.append(f"order_book: {e}")

    # Earnings proximity
    earnings = None
    try:
        earnings = scanners_mod.get_earnings(sym)
    except Exception as e:
        earnings = {"success": False, "error": str(e)}
        errors.append(f"earnings: {e}")

    # News
    news = None
    try:
        news = scanners_mod.get_news(sym, count=5)
    except Exception as e:
        news = {"success": False, "error": str(e)}
        errors.append(f"news: {e}")

    # Final grade aggregation
    reasoning = []
    final_grade = "SKIP"

    stack_grade = None
    if stack.get("success") and isinstance(stack.get("data"), list) and stack["data"]:
        s = stack["data"][0]
        stack_grade = s.get("stack")
        reasoning.append(f"Stack: {stack_grade} (setup={s.get('setup')}, rv={s.get('rv')}, body={s.get('body_pct')}%)")

    hist_grade = None
    if historicals.get("success") and historicals.get("data"):
        h = historicals["data"]
        hist_grade = h.get("setup_grade")
        reasoning.append(f"Historicals: {hist_grade} (vol_pass={h.get('volume_pass')}, body_pass={h.get('body_pass')}, R:R={h.get('rr_preview')}:1)")

    book_signal = None
    if book and book.get("success") and book.get("data"):
        b = book["data"]
        obi10 = (b.get("obi_10") or {}).get("signal")
        spread = b.get("spread")
        book_signal = obi10
        reasoning.append(f"Order book: OBI={obi10}, spread=${spread}")

    earnings_signal = None
    if earnings and earnings.get("upcoming"):
        days_to_earnings = (earnings["upcoming"] or {}).get("days_away")
        if days_to_earnings is not None and days_to_earnings <= 5:
            earnings_signal = f"SKIP — earnings in {days_to_earnings}d"
            reasoning.append(f"Earnings: SKIP ({days_to_earnings} days)")
        else:
            earnings_signal = "CLEAR"

    # Final grade logic:
    # A+: stack=A+ STACK + historicals A + book BALANCED/BUY + no earnings within 5d
    # A:  stack=A STACK or A+ + historicals A + no earnings within 5d
    # B:  stack=B STACK + historicals B/A
    # SKIP: anything else
    if earnings_signal and "SKIP" in earnings_signal:
        final_grade = "SKIP"
    elif stack_grade == "A+ STACK" and hist_grade == "A" and (not book_signal or "SELL" not in book_signal):
        final_grade = "A+"
    elif stack_grade in ("A+ STACK", "A STACK") and hist_grade == "A":
        final_grade = "A"
    elif stack_grade == "B STACK" and hist_grade in ("A", "B"):
        final_grade = "B"
    else:
        final_grade = "SKIP"

    reasoning.insert(0, f"FINAL: {final_grade}")

    return {
        "success": True,
        "symbol": sym,
        "price": price,
        "final_grade": final_grade,
        "reasoning": reasoning,
        "components": {
            "stack": stack,
            "historicals": historicals,
            "order_book": book,
            "earnings": earnings,
            "news": news,
        },
        "_errors": errors,
    }


# ---------------------------------------------------------------------------
# EOD brief — daily wrap-up
# ---------------------------------------------------------------------------

def eod_brief(account_number: str | None = None) -> dict:
    """End-of-day summary: realized P&L today, open positions, alerts fired vs not.

    Pulls closed trades from the trade log CSV (filtered to today), open positions from
    Robinhood, and alerts state from price_alerts.json. Useful as a recap before bed.
    """
    today_str = date.today().strftime("%Y-%m-%d")
    errors = []

    # Open positions
    try:
        positions = account_mod.get_positions(account_number)
    except Exception as e:
        positions = None
        errors.append(f"positions: {e}")

    # Portfolio equity
    try:
        portfolio = account_mod.get_portfolio(account_number)
    except Exception as e:
        portfolio = None
        errors.append(f"portfolio: {e}")

    # All trades — filter for today's closes
    try:
        all_trades_result = trade_log_mod.list_all_trades(limit=500)
    except Exception as e:
        all_trades_result = None
        errors.append(f"trades: {e}")

    closed_today = []
    realized_today = 0.0
    wins = 0
    losses = 0
    if all_trades_result and all_trades_result.get("success") and all_trades_result.get("stdout"):
        for line in all_trades_result["stdout"].splitlines():
            # We need to read the actual CSV for date/pnl rather than parsing list-all output
            pass
    # Direct CSV read for accurate today filter
    try:
        import csv as csv_mod
        from rh_mcp.tools.trade_log import TRADE_CSV
        if TRADE_CSV.exists():
            with TRADE_CSV.open("r", encoding="utf-8") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    if row.get("status") == "CLOSED" and row.get("exit_date") == today_str:
                        pnl = float(row.get("pnl_dollars") or 0)
                        closed_today.append({
                            "trade_id": row.get("trade_id"),
                            "ticker": row.get("ticker"),
                            "direction": row.get("direction"),
                            "entry": float(row.get("entry_price") or 0),
                            "exit": float(row.get("exit_price") or 0),
                            "shares": float(row.get("shares") or 0),
                            "pnl_dollars": pnl,
                            "pnl_pct": float(row.get("pnl_pct") or 0),
                            "exit_reason": row.get("exit_reason"),
                            "grade": row.get("grade"),
                        })
                        realized_today += pnl
                        if pnl > 0:
                            wins += 1
                        elif pnl < 0:
                            losses += 1
    except Exception as e:
        errors.append(f"csv_read: {e}")

    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else None

    # Alerts state
    try:
        alerts_data = alerts_mod._read_alerts()
        fired_today = [
            {
                "ticker": a.get("ticker"),
                "target": a.get("target"),
                "grade": a.get("grade"),
                "fired_at": a.get("fired_at"),
                "fired_price": a.get("fired_price"),
            }
            for a in alerts_data
            if a.get("fired") and (a.get("fired_at") or "").startswith(today_str)
        ]
        active_unfired = [
            {"ticker": a.get("ticker"), "target": a.get("target"), "grade": a.get("grade")}
            for a in alerts_data
            if a.get("active") and not a.get("fired")
        ]
    except Exception as e:
        fired_today = None
        active_unfired = None
        errors.append(f"alerts: {e}")

    return {
        "success": True,
        "date": today_str,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "equity": (portfolio or {}).get("equity") if portfolio else None,
        "open_positions_count": (positions or {}).get("count") if positions else None,
        "open_positions": (positions or {}).get("positions") if positions else None,
        "realized_pnl_today": round(realized_today, 2),
        "trades_closed_today": len(closed_today),
        "wins_today": wins,
        "losses_today": losses,
        "win_rate_today": round(win_rate, 1) if win_rate is not None else None,
        "closed_trades": closed_today,
        "alerts_fired_today": fired_today,
        "alerts_fired_count": len(fired_today) if fired_today is not None else 0,
        "alerts_active_unfired": active_unfired,
        "_errors": errors,
    }
