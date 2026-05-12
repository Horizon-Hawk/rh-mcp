"""Futures historical bars via yfinance.

RH's futures historicals endpoint isn't reverse-engineered yet, but yfinance
exposes the same data (continuous-front-month + dated contracts) for the same
underlying instruments. This module fetches bars via yfinance using standard
futures tickers, providing immediate functionality for:
- Backtesting on futures
- Multi-day setup analysis (no real-time but EOD-quality)
- Cross-checking RH quotes against an independent source

When RH's historicals URL is captured, futures_client.py will get its own
historicals function that hits RH directly. This module remains as a fallback /
independent source.

Standard symbology:
  NQ=F   — Nasdaq-100 continuous front-month
  MNQ=F  — Micro Nasdaq-100 continuous front-month
  ES=F   — S&P 500 E-mini continuous
  MES=F  — Micro S&P 500 continuous
  RTY=F  — Russell 2000 continuous
  CL=F   — Crude Oil continuous
  GC=F   — Gold continuous
  SI=F   — Silver continuous
  NG=F   — Natural Gas continuous
"""

from __future__ import annotations

try:
    import yfinance as yf
except ImportError as e:
    raise ImportError("futures_history requires yfinance — pip install yfinance") from e


# Map of friendly aliases to yfinance symbols
ALIAS = {
    "MNQ": "MNQ=F",
    "NQ": "NQ=F",
    "MES": "MES=F",
    "ES": "ES=F",
    "RTY": "RTY=F",
    "M2K": "M2K=F",
    "YM": "YM=F",
    "MYM": "MYM=F",
    "CL": "CL=F",
    "MCL": "MCL=F",
    "GC": "GC=F",
    "MGC": "MGC=F",
    "SI": "SI=F",
    "SIL": "SIL=F",
    "NG": "NG=F",
    "ZB": "ZB=F",
    "ZN": "ZN=F",
    "ZF": "ZF=F",
    "BTC": "BTC=F",
}


def _resolve_symbol(ticker: str) -> str:
    """Convert friendly ticker (MNQ, NQ) or dated (MNQM26) into a yfinance symbol.

    Dated contracts (MNQM26 = MNQ June 2026) don't have direct yfinance equivalents
    — yfinance only carries continuous front-month. We fall back to the continuous
    series and warn the caller via the returned data.
    """
    raw = ticker.strip().upper().lstrip("/")
    # Strip exchange suffix if present (e.g. "MNQM26:XCME" → "MNQM26")
    raw = raw.split(":")[0]
    # Direct alias hit
    if raw in ALIAS:
        return ALIAS[raw]
    # Try stripping month+year code (last 3 chars) — e.g. MNQM26 → MNQ
    if len(raw) >= 4 and raw[-1].isdigit():
        base = raw[:-3]
        if base in ALIAS:
            return ALIAS[base]
    # Already in yfinance format?
    if raw.endswith("=F"):
        return raw
    # Fallback: assume it's a regular ticker and let yfinance handle it
    return raw


def get_bars(
    ticker: str,
    period: str = "5y",
    interval: str = "1d",
) -> dict:
    """Pull historical bars for a futures contract.

    period: '1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'ytd', 'max'
    interval: '1m', '2m', '5m', '15m', '30m', '60m', '90m', '1h', '1d', '5d', '1wk', '1mo', '3mo'

    Note: short intervals (1m, 5m) only have ~7 days of history.
    """
    symbol = _resolve_symbol(ticker)
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    except Exception as e:
        return {"success": False, "error": f"yfinance fetch failed: {e}"}

    if df.empty:
        return {
            "success": False,
            "error": f"no data returned for {symbol}",
            "yfinance_symbol": symbol,
        }

    bars = []
    for ts, row in df.iterrows():
        bars.append({
            "date": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(row["Open"]) if row["Open"] == row["Open"] else None,
            "high": float(row["High"]) if row["High"] == row["High"] else None,
            "low": float(row["Low"]) if row["Low"] == row["Low"] else None,
            "close": float(row["Close"]) if row["Close"] == row["Close"] else None,
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] and row["Volume"] > 0 else 0,
        })

    return {
        "success": True,
        "ticker": ticker.upper().lstrip("/"),
        "yfinance_symbol": symbol,
        "period": period,
        "interval": interval,
        "bar_count": len(bars),
        "first_date": bars[0]["date"] if bars else None,
        "last_date": bars[-1]["date"] if bars else None,
        "bars": bars,
    }


def get_quote(ticker: str) -> dict:
    """Independent quote source for cross-checking RH (uses yfinance fast info)."""
    symbol = _resolve_symbol(ticker)
    try:
        info = yf.Ticker(symbol).fast_info
        return {
            "success": True,
            "ticker": ticker.upper().lstrip("/"),
            "yfinance_symbol": symbol,
            "last": float(info.last_price) if info.last_price else None,
            "open": float(info.open) if info.open else None,
            "day_high": float(info.day_high) if info.day_high else None,
            "day_low": float(info.day_low) if info.day_low else None,
            "previous_close": float(info.previous_close) if info.previous_close else None,
            "currency": info.currency,
        }
    except Exception as e:
        return {"success": False, "error": f"yfinance quote failed: {e}"}
