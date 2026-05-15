"""Futures RSI(2) mean reversion scanner.

Per-instrument edge re-validated 2026-05-14 on 5y daily bars:

  Root  Action       Long-PF  Short-PF  Expected $/yr
  ----  -----------  -------  --------  -------------
  MNQ   BOTH         2.48     1.50      +$2,418
  NQ    LONG_ONLY    2.28     skip      +$18,215
  MES   BOTH         1.63     1.50      +$994
  RTY   BOTH         2.47     3.20      +$9,401
  M2K   BOTH         2.68     1.63      +$809
  YM    LONG_ONLY    1.44     skip      +$3,659
  MYM   SHORT_ONLY   skip     1.56      +$177
  GC    LONG_ONLY    3.60     LOSING    +$4,217
  MGC   LONG_ONLY    1.46     LOSING    +$669
  SI    LONG_ONLY    33.91*   LOSING    +$14,338    *n=11 exploratory only
  CL    BOTH         1.79     1.51      +$7,057

  SKIP: ES (PF 1.16 long, marginal), NG (negative), MCL (no data)

Scanner finds:
  - LONG signals: RSI(2) < oversold AND price > 200-SMA AND action allows long
  - SHORT signals: RSI(2) > overbought AND price < 200-SMA AND action allows short
  - Multi-fire flag when correlated instruments fire simultaneously
"""
from __future__ import annotations

from datetime import datetime
from statistics import mean

from rh_mcp.analysis import futures_history as fh

# Per-instrument config — drives both default basket and per-side gating.
# 'tier' is informational: A = PF>=2.0 & N>=20, B = PF>=1.5 & N>=15,
# EXPLORATORY = good PF but small N (treat as observational, not actionable).
INSTRUMENT_CONFIG: dict[str, dict] = {
    # Equity index — CME
    "MNQ": {"action": "BOTH",       "point_value": 2.0,    "tier": "A",
            "label": "Micro Nasdaq-100", "expected_yr_pnl": 2418},
    "NQ":  {"action": "LONG_ONLY",  "point_value": 20.0,   "tier": "A",
            "label": "E-mini Nasdaq-100", "expected_yr_pnl": 18215},
    "MES": {"action": "BOTH",       "point_value": 5.0,    "tier": "B",
            "label": "Micro E-mini S&P", "expected_yr_pnl": 994},
    "RTY": {"action": "BOTH",       "point_value": 50.0,   "tier": "A",
            "label": "E-mini Russell 2000", "expected_yr_pnl": 9401},
    "M2K": {"action": "BOTH",       "point_value": 5.0,    "tier": "A",
            "label": "Micro Russell 2000", "expected_yr_pnl": 809},
    # CBOT (Dow)
    "YM":  {"action": "LONG_ONLY",  "point_value": 5.0,    "tier": "B",
            "label": "E-mini Dow", "expected_yr_pnl": 3659},
    "MYM": {"action": "SHORT_ONLY", "point_value": 0.5,    "tier": "B",
            "label": "Micro Dow", "expected_yr_pnl": 177},
    # Metals — COMEX
    "GC":  {"action": "LONG_ONLY",  "point_value": 100.0,  "tier": "A",
            "label": "Gold (full)", "expected_yr_pnl": 4217},
    "MGC": {"action": "LONG_ONLY",  "point_value": 10.0,   "tier": "B",
            "label": "Micro Gold", "expected_yr_pnl": 669},
    "SI":  {"action": "LONG_ONLY",  "point_value": 5000.0, "tier": "EXPLORATORY",
            "label": "Silver (n=11)", "expected_yr_pnl": 14338,
            "note": "PF 33.91 on only 11 trades — small sample, treat as observational"},
    # Energy — NYMEX
    "CL":  {"action": "BOTH",       "point_value": 1000.0, "tier": "B",
            "label": "Crude Oil", "expected_yr_pnl": 7057},
}

# Default basket = every instrument with documented edge
DEFAULT_TICKERS = list(INSTRUMENT_CONFIG.keys())

# Edge-correlated groups for multi-fire signal detection
CORRELATED_GROUPS = {
    "equity_indices": {"MNQ", "NQ", "MES", "RTY", "M2K", "YM", "MYM"},
    "precious_metals": {"GC", "MGC", "SI"},
    "energy": {"CL"},
}

OVERSOLD_THRESHOLD = 5.0
OVERBOUGHT_THRESHOLD = 95.0
SMA_PERIOD = 200
EXTREME_OVERSOLD = 2.0
EXTREME_OVERBOUGHT = 98.0


def _rsi2(closes: list[float]) -> float | None:
    if len(closes) < 3:
        return None
    gains, losses = [], []
    for k in (-2, -1):
        diff = closes[k] - closes[k - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _analyze_one(ticker: str) -> dict | None:
    """Compute current RSI(2) and SMA(200) for a futures ticker."""
    r = fh.get_bars(ticker, period="2y", interval="1d")
    if not r.get("success") or len(r.get("bars", [])) < SMA_PERIOD + 3:
        return None
    bars = r["bars"]
    closes = [b["close"] for b in bars if b["close"] is not None]
    if len(closes) < SMA_PERIOD + 3:
        return None
    last = closes[-1]
    sma = mean(closes[-SMA_PERIOD:])
    rsi = _rsi2(closes)
    if rsi is None or sma <= 0:
        return None
    return {
        "ticker": ticker.upper(),
        "yfinance_symbol": r.get("yfinance_symbol"),
        "price": round(last, 2),
        "rsi_2": round(rsi, 1),
        "sma_200": round(sma, 2),
        "pct_vs_sma": round((last - sma) / sma * 100, 2),
        "as_of": r["last_date"][:10] if r.get("last_date") else None,
    }


def _find_group(ticker: str) -> str | None:
    for name, members in CORRELATED_GROUPS.items():
        if ticker in members:
            return name
    return None


def _side_actionable(ticker: str, side: str) -> bool:
    """True if the instrument's validated edge supports trading this side."""
    cfg = INSTRUMENT_CONFIG.get(ticker)
    if not cfg:
        return False
    action = cfg["action"]
    if action == "BOTH":
        return True
    if action == "LONG_ONLY":
        return side == "long"
    if action == "SHORT_ONLY":
        return side == "short"
    return False


def analyze(
    tickers: list[str] | None = None,
    oversold_threshold: float = OVERSOLD_THRESHOLD,
    overbought_threshold: float = OVERBOUGHT_THRESHOLD,
    sma_period: int = SMA_PERIOD,
    include_exploratory: bool = False,
) -> dict:
    """Scan the futures basket for RSI(2) extremes filtered by 200-SMA trend
    AND per-instrument validated edge direction.

    Args:
      tickers: instruments to scan. Defaults to all validated instruments in
        INSTRUMENT_CONFIG.
      include_exploratory: if False (default), instruments tagged tier=EXPLORATORY
        (e.g. SI with n=11) are scanned and reported but NOT marked actionable.
    """
    tickers = tickers or DEFAULT_TICKERS
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]

    started_at = datetime.now().isoformat(timespec="seconds")

    statuses: list[dict] = []
    for t in tickers:
        s = _analyze_one(t)
        if s:
            cfg = INSTRUMENT_CONFIG.get(t, {})
            s["tier"] = cfg.get("tier", "UNKNOWN")
            s["action_allowed"] = cfg.get("action", "SKIP")
            s["point_value"] = cfg.get("point_value")
            s["label"] = cfg.get("label")
            statuses.append(s)

    longs: list[dict] = []
    shorts: list[dict] = []
    for s in statuses:
        t = s["ticker"]
        cfg = INSTRUMENT_CONFIG.get(t, {})
        tier = cfg.get("tier", "")
        actionable_gate = include_exploratory or tier != "EXPLORATORY"

        if s["rsi_2"] < oversold_threshold and s["price"] > s["sma_200"]:
            tag = "deep_oversold" if s["rsi_2"] < EXTREME_OVERSOLD else "oversold"
            actionable = _side_actionable(t, "long") and actionable_gate
            longs.append({
                **s, "signal": "long_mean_revert", "tag": tag,
                "group": _find_group(t), "actionable": actionable,
            })
        elif s["rsi_2"] > overbought_threshold and s["price"] < s["sma_200"]:
            tag = "deep_overbought" if s["rsi_2"] > EXTREME_OVERBOUGHT else "overbought"
            actionable = _side_actionable(t, "short") and actionable_gate
            shorts.append({
                **s, "signal": "short_mean_revert", "tag": tag,
                "group": _find_group(t), "actionable": actionable,
            })

    longs.sort(key=lambda x: x["rsi_2"])
    shorts.sort(key=lambda x: x["rsi_2"], reverse=True)

    # Multi-fire detection — only among actionable signals
    multi_fire: list[dict] = []
    for group_name, members in CORRELATED_GROUPS.items():
        group_longs = [x for x in longs if x["ticker"] in members and x["actionable"]]
        group_shorts = [x for x in shorts if x["ticker"] in members and x["actionable"]]
        if len(group_longs) >= 2:
            multi_fire.append({
                "group": group_name, "direction": "long",
                "members_firing": [x["ticker"] for x in group_longs],
                "conviction": "high",
            })
        if len(group_shorts) >= 2:
            multi_fire.append({
                "group": group_name, "direction": "short",
                "members_firing": [x["ticker"] for x in group_shorts],
                "conviction": "high",
            })

    actionable_longs = [x for x in longs if x["actionable"]]
    actionable_shorts = [x for x in shorts if x["actionable"]]

    return {
        "success": True,
        "scanned_at": started_at,
        "instruments_scanned": len(statuses),
        "long_count": len(longs),
        "short_count": len(shorts),
        "actionable_long_count": len(actionable_longs),
        "actionable_short_count": len(actionable_shorts),
        "longs": longs,
        "shorts": shorts,
        "actionable_longs": actionable_longs,
        "actionable_shorts": actionable_shorts,
        "multi_fire": multi_fire,
        "all_statuses": statuses,
    }
