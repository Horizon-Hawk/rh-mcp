"""Daily-bar momentum-breakout setup grader.

Ported from historicals.py CLI. Pulls ticker + SPY daily bars in one call,
computes volume ratio, body %, 20d SMA position, SPY trend, ATR(14), prior-range
base height, measured-move target, suggested stop, R:R, and an A/B/SKIP grade.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

VOLUME_THRESHOLD = 1.5
BODY_THRESHOLD = 50.0
MA_PERIOD = 20
ATR_PERIOD = 14


def _today_eastern() -> str:
    """Today's date in YYYY-MM-DD using US Eastern wall clock (NYSE timezone)."""
    # Approximate: UTC - 4 (EDT) or UTC - 5 (EST). Use -4 — close enough for a
    # same-day comparison; off-by-one only matters between midnight and 1 AM ET.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _build_synthetic_today_bar(ticker: str) -> dict | None:
    """Fetch today's 5-minute regular-session bars and aggregate into a daily-shape bar.

    Used as a fallback when RH hasn't yet published today's daily bar (typical
    mid-session and for an hour or two after close). Returns None on any error
    or if there's no intraday data for today.
    """
    try:
        bars = rh.stocks.get_stock_historicals(
            ticker, interval="5minute", span="day", bounds="regular",
        ) or []
    except Exception:
        return None
    today = _today_eastern()
    todays = [b for b in bars if (b.get("begins_at") or "").startswith(today)]
    if not todays:
        return None
    try:
        opens = float(todays[0]["open_price"])
        closes = float(todays[-1]["close_price"])
        highs = max(float(b["high_price"]) for b in todays)
        lows = min(float(b["low_price"]) for b in todays)
        vols = sum(int(b["volume"]) for b in todays)
    except (KeyError, ValueError, TypeError):
        return None
    return {
        "date": today,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
        "synthetic": True,
    }


def _fetch_bars(tickers: list[str], span: str) -> dict[str, list[dict]]:
    raw = rh.stocks.get_stock_historicals(tickers, interval="day", span=span, bounds="regular")
    if not raw:
        return {t: [] for t in tickers}
    result = {t: [] for t in tickers}
    for b in raw:
        sym = b.get("symbol", "").upper()
        if sym not in result:
            continue
        try:
            result[sym].append({
                "date": b["begins_at"][:10],
                "open": float(b["open_price"]),
                "high": float(b["high_price"]),
                "low": float(b["low_price"]),
                "close": float(b["close_price"]),
                "volume": int(b["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            pass
    for sym in result:
        result[sym].sort(key=lambda x: x["date"])
    return result


def _sma(bars, period, field="close"):
    if len(bars) < period:
        return None
    return round(mean(b[field] for b in bars[-period:]), 4)


def _avg_volume(bars, period):
    history = bars[-(period + 1):-1]
    if not history:
        return 0
    return int(mean(b["volume"] for b in history))


def _atr(bars, period):
    trs = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]["close"]
        tr = max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - prev),
            abs(bars[i]["low"] - prev),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return round(mean(window), 4)


def _body_pct(bar):
    rng = bar["high"] - bar["low"]
    if rng == 0:
        return 0.0
    return round(abs(bar["close"] - bar["open"]) / rng * 100, 1)


def _grade(ticker: str, bars: list[dict], spy_bars: list[dict]) -> dict:
    if len(bars) < MA_PERIOD + 2:
        return {"ticker": ticker, "error": f"not enough history ({len(bars)} bars, need {MA_PERIOD + 2})"}

    last = bars[-1]
    avg_vol = _avg_volume(bars, MA_PERIOD)
    vol_ratio = round(last["volume"] / avg_vol, 2) if avg_vol else 0
    vol_pass = vol_ratio >= VOLUME_THRESHOLD

    bp = _body_pct(last)
    body_pass = bp >= BODY_THRESHOLD
    bullish_candle = last["close"] >= last["open"]

    sma20 = _sma(bars, MA_PERIOD)
    above_sma = (last["close"] > sma20) if sma20 else None
    pct_vs_sma = round((last["close"] / sma20 - 1) * 100, 2) if sma20 else None

    spy_sma20 = _sma(spy_bars, MA_PERIOD) if spy_bars else None
    spy_close = spy_bars[-1]["close"] if spy_bars else None
    spy_bull = (spy_close > spy_sma20) if (spy_sma20 and spy_close) else None

    atr14 = _atr(bars, ATR_PERIOD)

    prior = bars[-(MA_PERIOD + 1):-1]
    prior_high = round(max(b["high"] for b in prior), 4) if prior else None
    prior_low = round(min(b["low"] for b in prior), 4) if prior else None
    base_height = round(prior_high - prior_low, 4) if (prior_high and prior_low) else None
    measured_move = round(prior_high + base_height, 2) if (prior_high and base_height) else None

    already_broken_out = prior_high and last["close"] > prior_high
    if already_broken_out:
        suggested_stop = round(last["low"] * 0.98, 2)
        stop_basis = "breakout candle low - 2%"
    elif prior_low and atr14:
        suggested_stop = round(prior_low - 0.5 * atr14, 2)
        stop_basis = "prior low - 0.5 ATR"
    else:
        suggested_stop = None
        stop_basis = ""

    rr = None
    if measured_move and suggested_stop and last["close"] > suggested_stop:
        reward = measured_move - last["close"]
        risk = last["close"] - suggested_stop
        rr = round(reward / risk, 2) if risk > 0 else None

    failures = []
    if not vol_pass:
        failures.append(f"volume {vol_ratio}x < {VOLUME_THRESHOLD}x")
    if not body_pass:
        failures.append(f"body {bp}% < {BODY_THRESHOLD}%")
    if above_sma is False:
        failures.append("price below 20d SMA")
    if spy_bull is False:
        failures.append("SPY below 20d SMA - market downtrend")

    passed = 4 - len(failures)
    grade = "A" if passed == 4 else "B" if passed == 3 else "SKIP"

    return {
        "ticker": ticker,
        "date": last["date"],
        "synthetic_today": bool(last.get("synthetic")),
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "close": last["close"],
        "volume": last["volume"],
        "avg_volume_20d": avg_vol,
        "volume_ratio": vol_ratio,
        "volume_pass": vol_pass,
        "body_pct": bp,
        "body_pass": body_pass,
        "bullish_candle": bullish_candle,
        "sma_20d": sma20,
        "above_sma": above_sma,
        "pct_vs_sma": pct_vs_sma,
        "spy_close": spy_close,
        "spy_sma_20d": spy_sma20,
        "spy_bullish": spy_bull,
        "atr_14d": atr14,
        "prior_high": prior_high,
        "prior_low": prior_low,
        "base_height": base_height,
        "measured_move": measured_move,
        "suggested_stop": suggested_stop,
        "stop_basis": stop_basis,
        "rr_preview": rr,
        "setup_grade": grade,
        "criteria_passed": passed,
        "failures": failures,
    }


def analyze(ticker: str, span: str = "3month", allow_synthetic_today: bool = True) -> dict:
    """Daily-bar setup grade for one ticker. Pulls SPY in the same API call.

    If RH hasn't yet published today's daily bar (common mid-session and during
    the first hour after close), append a synthetic bar built from today's
    aggregated 5-minute intraday data so the grading runs on the current
    breakout candle, not yesterday's. Disable with allow_synthetic_today=False.
    """
    ticker = ticker.upper()
    login()
    all_bars = _fetch_bars([ticker, "SPY"], span)
    bars = all_bars.get(ticker, [])
    spy_bars = all_bars.get("SPY", [])
    if not bars:
        return {"ticker": ticker, "error": "no data returned"}

    if allow_synthetic_today:
        today = _today_eastern()
        if bars[-1]["date"] < today:
            synthetic = _build_synthetic_today_bar(ticker)
            if synthetic and synthetic["date"] > bars[-1]["date"]:
                bars = bars + [synthetic]

    return _grade(ticker, bars, spy_bars)
