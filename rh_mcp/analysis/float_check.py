"""Float + short-interest discriminator — ported from float_check.py CLI script.

Pure yfinance, no robin_stocks login needed. Returns the same shape the legacy
script emitted on its `JSON:` line so existing callers and the MCP tool wrapper
get identical output without a subprocess hop.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError as e:
    raise ImportError(
        "float_check requires yfinance — pip install yfinance"
    ) from e


def grade_float_bucket(float_shares: int | None) -> str:
    if float_shares is None:
        return "UNKNOWN"
    if float_shares < 10_000_000:
        return "MICRO"
    if float_shares < 50_000_000:
        return "LOW"
    if float_shares < 500_000_000:
        return "MID"
    return "HIGH"


def grade_squeeze(
    float_shares: int | None,
    short_pct: float | None,
    days_to_cover: float | None,
    short_increasing: bool,
) -> str:
    """SQUEEZE / ELEVATED / NORMAL / LOW_RISK_SHORT / UNKNOWN."""
    if float_shares is None or short_pct is None or days_to_cover is None:
        return "UNKNOWN"
    if short_pct < 0.05:
        return "LOW_RISK_SHORT"
    if (
        float_shares < 100_000_000
        and short_pct >= 0.15
        and days_to_cover >= 3
        and short_increasing
    ):
        return "SQUEEZE"
    if short_pct >= 0.10 and days_to_cover >= 2:
        return "ELEVATED"
    return "NORMAL"


def _fetch_one(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

    float_shares = info.get("floatShares")
    shares_outstanding = info.get("sharesOutstanding")
    shares_short = info.get("sharesShort")
    shares_short_prior = info.get("sharesShortPriorMonth")
    short_ratio = info.get("shortRatio")
    short_pct = info.get("shortPercentOfFloat")
    insider_pct = info.get("heldPercentInsiders")
    inst_pct = info.get("heldPercentInstitutions")
    market_cap = info.get("marketCap")
    avg_vol = info.get("averageVolume")
    avg_vol_10d = info.get("averageVolume10days")
    date_short_int = info.get("dateShortInterest")

    short_increasing = (
        shares_short is not None
        and shares_short_prior is not None
        and shares_short > shares_short_prior
    )

    effective_float = None
    if float_shares is not None and insider_pct is not None:
        effective_float = int(float_shares * (1 - insider_pct))

    short_int_date = None
    if date_short_int:
        try:
            short_int_date = datetime.fromtimestamp(date_short_int, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            short_int_date = None

    return {
        "ticker": ticker,
        "float_shares": float_shares,
        "shares_outstanding": shares_outstanding,
        "effective_float": effective_float,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_pct_of_float": short_pct,
        "days_to_cover": short_ratio,
        "short_increasing": short_increasing,
        "insider_pct": insider_pct,
        "institutional_pct": inst_pct,
        "market_cap": market_cap,
        "avg_volume": avg_vol,
        "avg_volume_10d": avg_vol_10d,
        "short_interest_date": short_int_date,
        "float_bucket": grade_float_bucket(float_shares),
        "squeeze_grade": grade_squeeze(float_shares, short_pct, short_ratio, short_increasing),
    }


def analyze(tickers: list[str] | str) -> list[dict]:
    """Grade float + short interest for one or more tickers."""
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    return [_fetch_one(t) for t in cleaned]
