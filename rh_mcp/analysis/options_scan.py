"""Options chain scan: ATM IV, max pain, OI walls, spread ideas.

Ported from options_scan.py CLI.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

DEFAULT_RANGE_PCT = 10.0
MIN_EXPIRY_DAYS = 21
OI_WALL_PERCENTILE = 0.80


def _select_expiry(ticker: str, requested: str | None) -> str | None:
    chain = rh.options.get_chains(ticker)
    dates = chain.get("expiration_dates", [])
    if not dates:
        return None
    if requested:
        return requested if requested in dates else None
    today = date.today()
    cutoff = today + timedelta(days=MIN_EXPIRY_DAYS)
    for d in sorted(dates):
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff:
            return d
    return None


def _parse_row(row: dict) -> dict | None:
    try:
        return {
            "strike": float(row["strike_price"]),
            "bid": float(row.get("bid_price") or 0),
            "ask": float(row.get("ask_price") or 0),
            "iv": float(row.get("implied_volatility") or 0),
            "oi": int(float(row.get("open_interest") or 0)),
            "volume": int(float(row.get("volume") or 0)),
            "delta": float(row.get("delta") or 0),
        }
    except (TypeError, ValueError, KeyError):
        return None


def _calc_max_pain(calls, puts) -> float:
    all_strikes = sorted({r["strike"] for r in calls + puts})
    if not all_strikes:
        return 0.0
    min_pain = float("inf")
    max_pain_strike = all_strikes[0]
    for test_price in all_strikes:
        call_loss = sum(max(0, test_price - r["strike"]) * r["oi"] * 100 for r in calls)
        put_loss = sum(max(0, r["strike"] - test_price) * r["oi"] * 100 for r in puts)
        total = call_loss + put_loss
        if total < min_pain:
            min_pain = total
            max_pain_strike = test_price
    return max_pain_strike


def _find_walls(calls, puts, current_price: float, range_pct: float) -> dict:
    low = current_price * (1 - range_pct / 100)
    high = current_price * (1 + range_pct / 100)
    calls_in = [r for r in calls if low <= r["strike"] <= high and r["oi"] > 0]
    puts_in = [r for r in puts if low <= r["strike"] <= high and r["oi"] > 0]

    def walls(rows):
        if not rows:
            return []
        threshold = sorted(r["oi"] for r in rows)[int(len(rows) * OI_WALL_PERCENTILE)]
        return sorted(
            [r for r in rows if r["oi"] >= threshold],
            key=lambda x: x["oi"], reverse=True,
        )

    return {
        "call_walls": walls(calls_in),
        "put_walls": walls(puts_in),
        "calls_in_range": calls_in,
        "puts_in_range": puts_in,
    }


def _find_atm(rows, current_price: float):
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r["strike"] - current_price))


def _suggest_spreads(calls, puts, current_price: float) -> list[dict]:
    spreads = []
    strikes = sorted({r["strike"] for r in calls})
    atm_call = _find_atm(calls, current_price)
    if atm_call:
        atm_idx = strikes.index(atm_call["strike"])
        for offset in (1, 2):
            if atm_idx + offset < len(strikes):
                short_strike = strikes[atm_idx + offset]
                short_leg = next((r for r in calls if r["strike"] == short_strike), None)
                if short_leg:
                    cost = round(atm_call["ask"] - short_leg["bid"], 2)
                    width = short_strike - atm_call["strike"]
                    max_profit = round(width - cost, 2)
                    if cost > 0:
                        spreads.append({
                            "type": "bull_call_spread",
                            "buy_strike": atm_call["strike"],
                            "sell_strike": short_strike,
                            "cost": cost,
                            "max_profit": max_profit,
                            "breakeven": round(atm_call["strike"] + cost, 2),
                            "rr": round(max_profit / cost, 2) if cost > 0 else 0,
                            "width": width,
                        })

    put_strikes = sorted({r["strike"] for r in puts})
    atm_put = _find_atm(puts, current_price)
    if atm_put:
        atm_idx = put_strikes.index(atm_put["strike"])
        for offset in (1, 2):
            if atm_idx - offset >= 0:
                short_strike = put_strikes[atm_idx - offset]
                short_leg = next((r for r in puts if r["strike"] == short_strike), None)
                if short_leg:
                    cost = round(atm_put["ask"] - short_leg["bid"], 2)
                    width = atm_put["strike"] - short_strike
                    max_profit = round(width - cost, 2)
                    if cost > 0:
                        spreads.append({
                            "type": "bear_put_spread",
                            "buy_strike": atm_put["strike"],
                            "sell_strike": short_strike,
                            "cost": cost,
                            "max_profit": max_profit,
                            "breakeven": round(atm_put["strike"] - cost, 2),
                            "rr": round(max_profit / cost, 2) if cost > 0 else 0,
                            "width": width,
                        })
    return spreads


def analyze(ticker: str, expiry: str | None = None, range_pct: float | None = None) -> dict:
    """Options chain scan: ATM IV, max pain, OI walls, Phase 2 spread ideas."""
    ticker = ticker.upper()
    range_pct = DEFAULT_RANGE_PCT if range_pct is None else range_pct

    login()
    prices = rh.get_latest_price(ticker)
    if not prices or not prices[0]:
        return {"ticker": ticker, "error": "no price"}
    current_price = float(prices[0])

    selected_expiry = _select_expiry(ticker, expiry)
    if not selected_expiry:
        return {"ticker": ticker, "error": "no valid expiry available"}
    days_out = (datetime.strptime(selected_expiry, "%Y-%m-%d").date() - date.today()).days

    raw_calls = rh.options.find_options_by_expiration(ticker, selected_expiry, optionType="call") or []
    raw_puts = rh.options.find_options_by_expiration(ticker, selected_expiry, optionType="put") or []
    calls = [r for r in (_parse_row(x) for x in raw_calls) if r]
    puts = [r for r in (_parse_row(x) for x in raw_puts) if r]
    if not calls and not puts:
        return {"ticker": ticker, "error": "no options data returned"}

    walls = _find_walls(calls, puts, current_price, range_pct)
    max_pain = _calc_max_pain(calls, puts)
    spreads = _suggest_spreads(calls, puts, current_price)
    atm_call = _find_atm(walls["calls_in_range"], current_price)
    atm_put = _find_atm(walls["puts_in_range"], current_price)
    total_call_oi = sum(r["oi"] for r in walls["calls_in_range"])
    total_put_oi = sum(r["oi"] for r in walls["puts_in_range"])
    pc_ratio = round(total_put_oi / total_call_oi, 2) if total_call_oi else 0

    return {
        "ticker": ticker,
        "current_price": current_price,
        "expiry": selected_expiry,
        "days_to_expiry": days_out,
        "atm_iv_call": atm_call["iv"] if atm_call else 0,
        "atm_iv_put": atm_put["iv"] if atm_put else 0,
        "pc_ratio": pc_ratio,
        "max_pain": max_pain,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "call_walls": walls["call_walls"],
        "put_walls": walls["put_walls"],
        "spreads": spreads,
    }
