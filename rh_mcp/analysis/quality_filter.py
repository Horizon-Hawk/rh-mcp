"""Quality overlay — gross margin / ROE / debt-equity filter for any scanner.

Pulls fundamentals from yfinance Ticker.info. yfinance fundamentals are
*current* (not point-in-time historical), so this is a slight look-ahead
risk in backtests for the prior 12 months — acceptable because:
- Margins and ROE are slow-moving for the kind of names we trade (>$300M cap)
- The alternative (paid fundamental APIs) is not justified at this stage

Use as an OVERLAY: scan_8k and scan_capitulation_reversal already filter to
their setup criteria; this just tags each candidate `is_quality` so the
caller can drop / size-down on junk small caps.
"""

from __future__ import annotations

try:
    import yfinance as yf
except ImportError as e:
    raise ImportError("quality_filter requires yfinance — pip install yfinance") from e


# Defaults tuned for the ROBINHOOD.md challenge (small/mid quality).
# Tighten for capital preservation, loosen for catalyst plays.
DEFAULT_MIN_GROSS_MARGIN = 0.30      # 30% — separates healthy biz from breakage
DEFAULT_MIN_ROE = 0.08               # 8% — modest but rules out chronic losers
DEFAULT_MAX_DEBT_EQUITY = 2.0        # 2x — flag levered names that fold in risk-off
DEFAULT_MIN_PROFIT_MARGIN = 0.02     # 2% — at least slightly profitable on net basis


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check(
    ticker: str,
    min_gross_margin: float = DEFAULT_MIN_GROSS_MARGIN,
    min_roe: float = DEFAULT_MIN_ROE,
    max_debt_equity: float = DEFAULT_MAX_DEBT_EQUITY,
    min_profit_margin: float = DEFAULT_MIN_PROFIT_MARGIN,
) -> dict:
    """Grade a single ticker's quality. Returns:
        {ticker, is_quality, gross_margin, profit_margin, roe, debt_equity,
         market_cap, failing_checks: [str], reason}.
    `is_quality=True` requires all four checks to pass. `failing_checks`
    lists which metrics fell short for any name that didn't make it through.
    """
    try:
        info = yf.Ticker(ticker.strip().upper()).info
    except Exception as e:
        return {
            "success": False, "ticker": ticker, "error": f"yfinance fetch failed: {e}",
        }
    gross_margin = _to_float(info.get("grossMargins"))
    profit_margin = _to_float(info.get("profitMargins"))
    roe = _to_float(info.get("returnOnEquity"))
    debt_equity = _to_float(info.get("debtToEquity"))
    # debtToEquity comes back as a percentage in some versions (e.g. 150 for 1.5x)
    if debt_equity is not None and debt_equity > 20:
        debt_equity = debt_equity / 100.0
    market_cap = _to_float(info.get("marketCap"))

    failing: list[str] = []
    if gross_margin is None or gross_margin < min_gross_margin:
        failing.append(f"gross_margin<{min_gross_margin}")
    if roe is None or roe < min_roe:
        failing.append(f"roe<{min_roe}")
    if debt_equity is not None and debt_equity > max_debt_equity:
        failing.append(f"debt_equity>{max_debt_equity}")
    if profit_margin is None or profit_margin < min_profit_margin:
        failing.append(f"profit_margin<{min_profit_margin}")

    is_quality = len(failing) == 0
    return {
        "success": True,
        "ticker": ticker.upper(),
        "is_quality": is_quality,
        "gross_margin": round(gross_margin, 4) if gross_margin is not None else None,
        "profit_margin": round(profit_margin, 4) if profit_margin is not None else None,
        "roe": round(roe, 4) if roe is not None else None,
        "debt_equity": round(debt_equity, 3) if debt_equity is not None else None,
        "market_cap": market_cap,
        "failing_checks": failing,
        "reason": "all checks passed" if is_quality else f"failed: {', '.join(failing)}",
    }


def analyze(tickers: list[str] | str, **kwargs) -> dict:
    """Batch quality grade. Returns {success, count, quality_count, results: [...]}."""
    if isinstance(tickers, str):
        tickers = [tickers]
    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    results = [check(t, **kwargs) for t in cleaned]
    quality_count = sum(1 for r in results if r.get("is_quality"))
    return {
        "success": True,
        "count": len(results),
        "quality_count": quality_count,
        "results": results,
    }
