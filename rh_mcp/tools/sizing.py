"""Position sizing helpers — embed the framework's tiered risk model."""

from rh_mcp.tools import account as account_mod
from rh_mcp.auth import default_account


# Risk tiers from ROBINHOOD.md (challenge framework)
RISK_TIERS = {
    "A+": 5.0,   # high conviction, textbook
    "A":  3.0,   # all criteria met
    "B":  1.5,   # criteria met, some noise
}
DEFAULT_MAX_POSITION_PCT = 25.0

# Small-cap risk discount — caps gap risk on names <$1B where stops slip
# and halts blow through brackets. Mirrors the small-cap thesis spec.
SMALL_CAP_THRESHOLD = 1_000_000_000
SMALL_CAP_RISK_TIERS = {
    "A+": 3.0,   # was 5.0
    "A":  2.0,   # was 3.0
    "B":  1.0,   # was 1.5
}
SMALL_CAP_MAX_POSITION_PCT = 15.0


def _fetch_market_cap(ticker: str | None) -> float | None:
    if not ticker:
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker.strip().upper()).info
        cap = info.get("marketCap")
        return float(cap) if cap is not None else None
    except Exception:
        return None


def position_size_helper(
    entry: float,
    stop: float,
    risk_pct: float | None = None,
    grade: str | None = None,
    equity: float | None = None,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    account_number: str | None = None,
    ticker: str | None = None,
    market_cap: float | None = None,
) -> dict:
    """Calculate position size from risk parameters.

    Provide EITHER risk_pct (explicit %) OR grade ('A+'/'A'/'B' from RISK_TIERS).
    If equity is omitted, fetches current portfolio equity automatically.

    Args:
        entry: Entry price.
        stop: Stop-loss price.
        risk_pct: Risk as % of equity (e.g. 3.0 = 3%). Optional — derived from grade if omitted.
        grade: Setup grade ('A+', 'A', 'B') → tiered risk per framework.
        equity: Account equity. Auto-fetched if omitted.
        max_position_pct: Max position size as % of equity (default 25%).
    """
    if entry == stop:
        return {"success": False, "error": "entry == stop"}

    if risk_pct is None and grade is None:
        return {"success": False, "error": "must provide risk_pct or grade"}

    # Determine small-cap status (look up cap if ticker given and cap not passed)
    if market_cap is None and ticker:
        market_cap = _fetch_market_cap(ticker)
    is_small_cap = market_cap is not None and market_cap < SMALL_CAP_THRESHOLD

    # Pick the tiered risk table — small-caps use the reduced one
    risk_table = SMALL_CAP_RISK_TIERS if is_small_cap else RISK_TIERS
    if is_small_cap and max_position_pct == DEFAULT_MAX_POSITION_PCT:
        max_position_pct = SMALL_CAP_MAX_POSITION_PCT

    if risk_pct is None:
        risk_pct = risk_table.get(grade.upper())
        if risk_pct is None:
            return {"success": False, "error": f"unknown grade '{grade}'; expected one of {list(risk_table)}"}

    if equity is None:
        port = account_mod.get_portfolio(account_number or default_account())
        if not port.get("success"):
            return {"success": False, "error": "could not fetch equity", "raw": port}
        equity = port.get("equity")
    if not equity or equity <= 0:
        return {"success": False, "error": f"invalid equity: {equity}"}

    risk_per_share = abs(entry - stop)
    target_risk_dollars = equity * (risk_pct / 100)
    shares_by_risk = target_risk_dollars / risk_per_share
    position_value_by_risk = shares_by_risk * entry

    max_position_value = equity * (max_position_pct / 100)
    capped = position_value_by_risk > max_position_value
    if capped:
        shares = max_position_value / entry
    else:
        shares = shares_by_risk

    shares_whole = int(shares)
    actual_risk = shares * risk_per_share
    actual_risk_whole = shares_whole * risk_per_share

    return {
        "success": True,
        "entry": entry,
        "stop": stop,
        "ticker": ticker.upper() if ticker else None,
        "market_cap": market_cap,
        "is_small_cap": is_small_cap,
        "risk_per_share": round(risk_per_share, 4),
        "equity": round(equity, 2),
        "risk_pct": risk_pct,
        "grade": grade,
        "risk_tier_table": "small_cap" if is_small_cap else "default",
        "target_risk_dollars": round(target_risk_dollars, 2),
        "shares_fractional": round(shares, 4),
        "shares_whole": shares_whole,
        "position_value_fractional": round(shares * entry, 2),
        "position_value_whole": round(shares_whole * entry, 2),
        "actual_risk_fractional": round(actual_risk, 2),
        "actual_risk_whole": round(actual_risk_whole, 2),
        "actual_risk_pct": round(actual_risk / equity * 100, 3),
        "capped_at_max_position": capped,
        "max_position_value": round(max_position_value, 2),
        "max_position_pct": max_position_pct,
    }
