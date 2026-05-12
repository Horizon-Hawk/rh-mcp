"""IV rank + percentile + IV-vs-HV premium for one or more tickers.

Ported from iv_rank.py CLI. Back-calculates IV from RH option price history
via Black-Scholes + Brent root-finding.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from statistics import stdev

import robin_stocks.robinhood as rh

try:
    from scipy.optimize import brentq
    from scipy.stats import norm
except ImportError as e:
    raise ImportError("iv_rank requires scipy — pip install scipy") from e

from rh_mcp.auth import login

RISK_FREE_RATE = 0.05
MIN_EXPIRY_DAYS = 45
HV_WINDOW = 30
IV_HIGH_THRESHOLD = 70
IV_LOW_THRESHOLD = 30


def _bs_call(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def _bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _implied_vol(option_price, S, K, T, r, option_type="call"):
    pricer = _bs_call if option_type == "call" else _bs_put
    intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
    time_value = option_price - intrinsic
    if time_value < 0.02 or T <= 0:
        return None
    try:
        iv = brentq(
            lambda sigma: pricer(S, K, T, r, sigma) - option_price,
            0.01, 20.0, xtol=1e-5, maxiter=200,
        )
        return iv if 0.05 <= iv <= 5.0 else None
    except (ValueError, RuntimeError):
        return None


def _historical_vol(closes, window=30):
    if len(closes) < window + 1:
        return None
    recent = closes[-(window + 1):]
    returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    if len(returns) < 2:
        return None
    return round(stdev(returns) * math.sqrt(252), 4)


def _select_expiry(ticker, min_days=MIN_EXPIRY_DAYS):
    chain = rh.options.get_chains(ticker)
    dates = chain.get("expiration_dates", [])
    today = date.today()
    cutoff = today + timedelta(days=min_days)
    for d in sorted(dates):
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff:
            return d
    return dates[-1] if dates else None


def _fetch_atm_option(ticker, expiry, current_price, option_type="call"):
    opts = rh.options.find_options_by_expiration(ticker, expiry, optionType=option_type) or []
    if not opts:
        return None
    return min(opts, key=lambda x: abs(float(x.get("strike_price", 0)) - current_price))


def _fetch_iv_history(ticker, expiry, strike, option_type="call"):
    raw = rh.options.get_option_historicals(
        ticker, expiry, str(int(float(strike))), option_type,
        interval="day", span="year",
    )
    return raw if isinstance(raw, list) else []


def _fetch_stock_closes(ticker, span="year"):
    raw = rh.stocks.get_stock_historicals(ticker, interval="day", span=span, bounds="regular")
    bars = {}
    for b in (raw or []):
        if b.get("symbol", "").upper() == ticker.upper():
            try:
                bars[b["begins_at"][:10]] = float(b["close_price"])
            except (KeyError, ValueError):
                pass
    return bars


def _analyze_one(ticker: str) -> dict:
    prices = rh.get_latest_price(ticker)
    if not prices or not prices[0]:
        return {"ticker": ticker, "error": "no price"}
    current_price = float(prices[0])

    expiry = _select_expiry(ticker)
    if not expiry:
        return {"ticker": ticker, "error": "no expiry found"}

    atm = _fetch_atm_option(ticker, expiry, current_price, "call")
    if not atm:
        return {"ticker": ticker, "error": "no ATM option found"}

    strike = float(atm["strike_price"])
    current_iv = float(atm.get("implied_volatility") or 0)
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()

    opt_bars = _fetch_iv_history(ticker, expiry, strike, "call")
    stock_closes = _fetch_stock_closes(ticker)

    iv_series = []
    for bar in opt_bars:
        date_str = bar.get("begins_at", "")[:10]
        S = stock_closes.get(date_str)
        if not S:
            continue
        try:
            opt_price = float(bar["close_price"])
        except (KeyError, ValueError):
            continue
        bar_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        T = (expiry_date - bar_date).days / 365.0
        iv = _implied_vol(opt_price, S, strike, T, RISK_FREE_RATE, "call")
        if iv:
            iv_series.append((date_str, iv))

    if len(iv_series) < 20:
        return {"ticker": ticker, "error": f"insufficient IV history ({len(iv_series)} points)"}

    iv_values = [v for _, v in iv_series]
    iv_52w_high = round(max(iv_values), 4)
    iv_52w_low = round(min(iv_values), 4)

    iv_rank = round(
        (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100, 1
    ) if iv_52w_high > iv_52w_low else 50.0

    iv_pct = round(
        sum(1 for v in iv_values if v < current_iv) / len(iv_values) * 100, 1
    )

    sorted_closes = [v for _, v in sorted(stock_closes.items())]
    hv30 = _historical_vol(sorted_closes, HV_WINDOW)
    iv_premium = round(current_iv - hv30, 4) if hv30 else None

    if iv_rank >= IV_HIGH_THRESHOLD:
        signal, note = "HIGH", "options expensive - avoid debit spreads or size down"
    elif iv_rank <= IV_LOW_THRESHOLD:
        signal, note = "LOW", "options cheap - favorable debit spread entry"
    else:
        signal, note = "MID", "neutral"

    return {
        "ticker": ticker,
        "current_price": current_price,
        "expiry": expiry,
        "strike": strike,
        "current_iv": round(current_iv, 4),
        "iv_52w_high": iv_52w_high,
        "iv_52w_low": iv_52w_low,
        "iv_rank": iv_rank,
        "iv_percentile": iv_pct,
        "hv30": round(hv30, 4) if hv30 else None,
        "iv_premium": iv_premium,
        "iv_history_points": len(iv_series),
        "signal": signal,
        "signal_note": note,
    }


def analyze(tickers: list[str] | str) -> list[dict]:
    """IV rank for one or more tickers."""
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    login()
    return [_analyze_one(t) for t in cleaned]
