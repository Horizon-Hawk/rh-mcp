"""Futures RSI(2) mean reversion scanner.

Validated edge from 5y daily bar backtest:
  - GC (Gold):  73.1% win, +0.47% avg, PF 2.01 — strongest
  - MNQ/NQ:     73.1% win, +0.47% avg, PF 1.92
  - ES/MES:     69.9% win, +0.28% avg, PF 1.78
  - CL (Crude): 61.5% win, +0.46% avg, PF 1.36
  - RTY (Russell): 58.3% win — weaker, small-cap trends
  - NG (Nat Gas): NEGATIVE EXPECTANCY, excluded from default basket

Scanner finds:
  - LONG signals: RSI(2) < oversold_threshold AND price > 200-SMA
  - SHORT signals: RSI(2) > overbought_threshold AND price < 200-SMA
  - Multi-fire flag: when correlated instruments fire simultaneously
    (e.g. MNQ + ES both oversold = stronger conviction than either alone)

Default tier list excludes NG (no edge) and ZB/ZN/ZF (treasuries, different
behavior). Add them via tickers param if desired.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean

from rh_mcp.analysis import futures_history as fh

# Default basket — instruments with PF > 1.3 in the backtest
DEFAULT_TICKERS = ["GC", "MNQ", "NQ", "ES", "MES", "MGC", "RTY", "M2K", "CL", "YM", "MYM", "SI"]

# Edge-correlated groups for multi-fire signal detection
CORRELATED_GROUPS = {
    "equity_indices": {"MNQ", "NQ", "ES", "MES", "RTY", "M2K", "YM", "MYM"},
    "precious_metals": {"GC", "MGC", "SI"},
    "energy": {"CL", "MCL"},
}

OVERSOLD_THRESHOLD = 5.0
OVERBOUGHT_THRESHOLD = 95.0
SMA_PERIOD = 200
EXTREME_OVERSOLD = 2.0          # below this = "deep" oversold, higher conviction
EXTREME_OVERBOUGHT = 98.0


def _rsi2(closes: list[float]) -> float | None:
    """RSI(2) on the most recent two days. Returns None if insufficient data."""
    if len(closes) < 3:
        return None
    gains = []
    losses = []
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
    """Compute current RSI(2) and SMA(200) for a futures ticker via yfinance."""
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
    """Identify which correlation group a ticker belongs to."""
    for name, members in CORRELATED_GROUPS.items():
        if ticker in members:
            return name
    return None


def analyze(
    tickers: list[str] | None = None,
    oversold_threshold: float = OVERSOLD_THRESHOLD,
    overbought_threshold: float = OVERBOUGHT_THRESHOLD,
    sma_period: int = SMA_PERIOD,
) -> dict:
    """Scan the futures basket for RSI(2) extremes filtered by 200-SMA trend.

    Returns longs + shorts separately, plus a 'multi_fire' summary highlighting
    when correlated instruments fire simultaneously (stronger conviction).
    """
    tickers = tickers or DEFAULT_TICKERS
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]

    started_at = datetime.now().isoformat(timespec="seconds")

    statuses: list[dict] = []
    for t in tickers:
        s = _analyze_one(t)
        if s:
            statuses.append(s)

    longs: list[dict] = []
    shorts: list[dict] = []
    for s in statuses:
        # Long: oversold AND in uptrend
        if s["rsi_2"] < oversold_threshold and s["price"] > s["sma_200"]:
            tag = "deep_oversold" if s["rsi_2"] < EXTREME_OVERSOLD else "oversold"
            longs.append({**s, "signal": "long_mean_revert", "tag": tag, "group": _find_group(s["ticker"])})
        # Short: overbought AND in downtrend
        elif s["rsi_2"] > overbought_threshold and s["price"] < s["sma_200"]:
            tag = "deep_overbought" if s["rsi_2"] > EXTREME_OVERBOUGHT else "overbought"
            shorts.append({**s, "signal": "short_mean_revert", "tag": tag, "group": _find_group(s["ticker"])})

    # Sort: deep extremes first, then by RSI(2) magnitude
    longs.sort(key=lambda x: x["rsi_2"])
    shorts.sort(key=lambda x: x["rsi_2"], reverse=True)

    # Multi-fire detection: correlated group has >=2 instruments firing same direction
    multi_fire: list[dict] = []
    for group_name, members in CORRELATED_GROUPS.items():
        group_longs = [x for x in longs if x["ticker"] in members]
        group_shorts = [x for x in shorts if x["ticker"] in members]
        if len(group_longs) >= 2:
            multi_fire.append({
                "group": group_name,
                "direction": "long",
                "members_firing": [x["ticker"] for x in group_longs],
                "conviction": "high",
                "note": "multiple correlated instruments oversold simultaneously",
            })
        if len(group_shorts) >= 2:
            multi_fire.append({
                "group": group_name,
                "direction": "short",
                "members_firing": [x["ticker"] for x in group_shorts],
                "conviction": "high",
                "note": "multiple correlated instruments overbought simultaneously",
            })

    return {
        "success": True,
        "scanned_at": started_at,
        "instruments_scanned": len(statuses),
        "long_count": len(longs),
        "short_count": len(shorts),
        "longs": longs,
        "shorts": shorts,
        "multi_fire": multi_fire,
        "all_statuses": statuses,  # full snapshot for context, even non-firing
    }
