"""Performance + TTS-qualification statistics from the trade log CSV.

Ported from trade_stats.py CLI. Reads via rh_mcp.analysis.trade_log.get_all_trades().
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from rh_mcp.analysis.trade_log import get_all_trades


def _safe_float(x, default=0.0):
    try:
        return float(x) if x else default
    except (ValueError, TypeError):
        return default


def _safe_int(x, default=0):
    try:
        return int(x) if x else default
    except (ValueError, TypeError):
        return default


def _assess_tts(annual_pace: float, day_ratio: float, avg_hold_days: float, turnover_usd: float) -> str:
    score = 0
    if annual_pace >= 720:
        score += 2
    elif annual_pace >= 200:
        score += 1
    if day_ratio >= 0.75:
        score += 2
    elif day_ratio >= 0.5:
        score += 1
    if avg_hold_days < 30:
        score += 1
    if avg_hold_days < 7:
        score += 1
    if turnover_usd >= 1_000_000:
        score += 1
    if turnover_usd >= 25_000:
        score += 1

    if score >= 6:
        return "GREEN — pattern supports TTS qualification"
    if score >= 3:
        return "YELLOW — borderline, needs more activity/continuity"
    return "RED — currently hobbyist; far from TTS threshold"


def analyze(since: str | None = None, account: str | None = None) -> dict:
    """Compute TTS qualification + performance metrics."""
    trades = get_all_trades()
    if account:
        trades = [t for t in trades if t["account"] == account]
    if since:
        trades = [t for t in trades if t["entry_date"] >= since]
    if not trades:
        return {"success": False, "error": "no trades match filter"}

    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    total_closed = len(closed)
    wins = sum(1 for t in closed if _safe_float(t["pnl_dollars"]) > 0)
    losses = sum(1 for t in closed if _safe_float(t["pnl_dollars"]) < 0)
    breakevens = total_closed - wins - losses

    total_pnl = sum(_safe_float(t["pnl_dollars"]) for t in closed)
    total_turnover_closed = sum(_safe_float(t["position_value"]) for t in closed)
    total_turnover_all = sum(_safe_float(t["position_value"]) for t in trades)

    holding_minutes = [_safe_int(t["holding_period_minutes"]) for t in closed if t["holding_period_minutes"]]
    avg_hold_minutes = sum(holding_minutes) / len(holding_minutes) if holding_minutes else 0
    avg_hold_hours = avg_hold_minutes / 60
    avg_hold_days = avg_hold_minutes / 1440

    trade_dates = {t["entry_date"] for t in trades if t["entry_date"]}
    days_traded = len(trade_dates)

    by_strategy: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for t in closed:
        s = t["strategy"] or "unknown"
        by_strategy[s]["count"] += 1
        pnl = _safe_float(t["pnl_dollars"])
        by_strategy[s]["pnl"] += pnl
        if pnl > 0:
            by_strategy[s]["wins"] += 1

    by_direction: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in closed:
        d = t["direction"] or "unknown"
        by_direction[d]["count"] += 1
        by_direction[d]["pnl"] += _safe_float(t["pnl_dollars"])

    dates = sorted(t["entry_date"] for t in trades if t["entry_date"])
    first_date = dates[0] if dates else None
    last_date = dates[-1] if dates else None
    if first_date:
        first_dt = datetime.strptime(first_date, "%Y-%m-%d")
        days_since_start = max(1, (datetime.now() - first_dt).days)
    else:
        days_since_start = 1

    annual_pace = (len(trades) / days_since_start) * 365
    day_ratio = days_traded / days_since_start
    win_rate = (wins / total_closed * 100) if total_closed else 0
    avg_pnl = (total_pnl / total_closed) if total_closed else 0
    tts_signal = _assess_tts(annual_pace, day_ratio, avg_hold_days, total_turnover_all)

    return {
        "success": True,
        "period_start": first_date,
        "period_end": last_date,
        "days_since_start": days_since_start,
        "total_trades": len(trades),
        "closed": total_closed,
        "open": len(open_trades),
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "avg_holding_hours": round(avg_hold_hours, 1),
        "avg_holding_days": round(avg_hold_days, 2),
        "days_traded": days_traded,
        "day_ratio": round(day_ratio, 3),
        "annualized_pace": round(annual_pace, 0),
        "gross_turnover_closed": round(total_turnover_closed, 2),
        "gross_turnover_all": round(total_turnover_all, 2),
        "by_strategy": dict(by_strategy),
        "by_direction": dict(by_direction),
        "tts_signal": tts_signal,
    }
