"""52-week high breakout scanner.

Finds tickers at-or-near 52-week highs with confirming volume, filtered by the
ROBINHOOD.md momentum-breakout framework (SPY uptrend, no earnings within 5d,
not chased, min price). Returns ranked candidates with everything an entry
pipeline needs to grade them.

Inputs:
    - Explicit `tickers` list, OR
    - `universe_file` path (one or more whitespace-separated tickers per line;
      lines may have `# comments` stripped)

Output (matches the spec):
    {
        "scanned_at": ISO ts,
        "spy_uptrend": bool,
        "total_scanned": int,
        "passed_proximity": int,
        "passed_volume": int,
        "passed_earnings": int,
        "candidates": [{ticker, price, high_52w, pct_to_52w, volume_ratio,
                        pct_change_today, sector, market_cap, earnings_days_away,
                        earnings_signal, grade, auto_alert_eligible}, ...]
    }

Grade A = passes all four framework criteria cleanly. Grade B = price already
extended past 52w by more than 5% (chasing). Skip otherwise.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from statistics import mean

import robin_stocks.robinhood as rh

from rh_mcp.analysis import earnings as _earnings
from rh_mcp.auth import login

# Defaults follow ROBINHOOD.md framework constants.
MIN_PRICE = 5.00
MIN_VOLUME_RATIO = 1.5
MAX_GAIN_TODAY_PCT = 5.0           # framework "chased" rule
PROXIMITY_PCT = 0.0                # 0 = strict at-or-above 52w high
REQUIRE_SPY_UPTREND = True
SKIP_EARNINGS_WITHIN_DAYS = 5
DEFAULT_TOP_N = 20

_BATCH_SIZE = 75
_BATCH_SLEEP_SECS = 0.1
_SESSION_MINUTES = 390             # regular session length


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_universe_from_file(path: Path) -> list[str]:
    """Parse a whitespace-delimited ticker file; ignore `# comments`."""
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    # de-dupe while preserving order
    return list(dict.fromkeys(tickers))


def _spy_uptrend() -> bool:
    """SPY close above its 20d SMA — one cheap call to gate the scan."""
    try:
        bars = rh.stocks.get_stock_historicals(
            "SPY", interval="day", span="month", bounds="regular",
        ) or []
        closes = [float(b["close_price"]) for b in bars if b.get("close_price")]
        if len(closes) < 20:
            return True  # not enough data → don't block
        sma20 = mean(closes[-20:])
        return closes[-1] > sma20
    except Exception:
        return True  # if RH can't answer, don't block — caller can re-check


def _elapsed_session_minutes(now: datetime) -> int:
    """Minutes since 9:30 AM ET (assume PT clock for our user; clamp to 1..390)."""
    # 6:30 AM PT = 9:30 AM ET
    open_minute = 6 * 60 + 30
    cur_minute = now.hour * 60 + now.minute
    elapsed = cur_minute - open_minute
    if elapsed <= 0:
        return 1  # pre-market → raw volume not pace-adjusted
    return min(_SESSION_MINUTES, elapsed)


def _pace_adjusted_ratio(volume: int, avg_volume: float, now: datetime) -> float:
    """Project today's volume to a full-day equivalent for ratio comparison."""
    if not avg_volume:
        return 0.0
    elapsed = _elapsed_session_minutes(now)
    if elapsed >= _SESSION_MINUTES:
        projected = volume
    else:
        projected = volume * (_SESSION_MINUTES / max(elapsed, 1))
    return round(projected / avg_volume, 2)


def _fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """Batched quotes for current price, prev close, % change.

    Note: RH's quotes endpoint does NOT return intraday volume — we pull it
    separately via _fetch_volumes() below.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_quotes(chunk, info=None) or []
        except Exception:
            results = []
        for q in results:
            if not q:
                continue
            sym = (q.get("symbol") or "").upper()
            if not sym:
                continue
            price = _to_float(q.get("last_trade_price"))
            prev = _to_float(q.get("adjusted_previous_close")) or _to_float(q.get("previous_close"))
            if not price or not prev:
                continue
            out[sym] = {
                "price": price,
                "prev_close": prev,
                "pct_change": round((price - prev) / prev * 100, 2),
                "halted": bool(q.get("trading_halted", False)),
            }
        time.sleep(_BATCH_SLEEP_SECS)
    return out


def _fetch_volumes(tickers: list[str]) -> dict[str, int]:
    """Batched today's volume via the most recent daily bar.

    RH's get_quotes endpoint doesn't expose intraday volume (only ask/bid/last),
    so we pull it from daily historicals. One call returns ~5 bars per ticker;
    we keep only the most recent (today's, or yesterday's if pre-market).
    """
    out: dict[str, int] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            bars = rh.stocks.get_stock_historicals(
                chunk, interval="day", span="week", bounds="regular",
            ) or []
        except Exception:
            bars = []
        # Bars come back interleaved by symbol+date; pick the latest per ticker.
        latest_by_sym: dict[str, tuple[str, int]] = {}
        for b in bars:
            sym = (b.get("symbol") or "").upper()
            ts = b.get("begins_at") or ""
            vol = _to_float(b.get("volume"))
            if not sym or not ts or vol is None:
                continue
            cur = latest_by_sym.get(sym)
            if cur is None or ts > cur[0]:
                latest_by_sym[sym] = (ts, int(vol))
        for sym, (_, vol) in latest_by_sym.items():
            out[sym] = vol
        time.sleep(_BATCH_SLEEP_SECS)
    return out


def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """Batched fundamentals for 52w high, avg volume, sector, market cap."""
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        try:
            results = rh.stocks.get_fundamentals(chunk, info=None) or []
        except Exception:
            results = []
        for sym, f in zip(chunk, results):
            if not f:
                continue
            out[sym.upper()] = {
                "high_52w": _to_float(f.get("high_52_weeks")),
                "low_52w": _to_float(f.get("low_52_weeks")),
                "avg_volume": _to_float(f.get("average_volume")),
                "sector": f.get("sector"),
                "industry": f.get("industry"),
                "market_cap": _to_float(f.get("market_cap")),
            }
        time.sleep(_BATCH_SLEEP_SECS)
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    proximity_pct: float = PROXIMITY_PCT,
    min_price: float = MIN_PRICE,
    min_volume_ratio: float = MIN_VOLUME_RATIO,
    max_gain_today_pct: float = MAX_GAIN_TODAY_PCT,
    require_spy_uptrend: bool = REQUIRE_SPY_UPTREND,
    skip_earnings_within_days: int = SKIP_EARNINGS_WITHIN_DAYS,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Scan a universe for stocks at-or-near 52-week highs with confirming volume.

    Caller provides the universe via `tickers` (list) or `universe_file` (path).
    Defaults to reading the legacy `stock_universe.txt` from the cwd if neither
    is provided — matches the existing premarket_scan.py convention.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {
            "success": False,
            "error": "empty universe — provide tickers list or universe_file",
        }

    login()
    now = datetime.now()
    scanned_at = now.isoformat(timespec="seconds")

    spy_up = _spy_uptrend()
    if require_spy_uptrend and not spy_up:
        return {
            "success": True,
            "scanned_at": scanned_at,
            "spy_uptrend": False,
            "total_scanned": len(tickers),
            "passed_proximity": 0,
            "passed_volume": 0,
            "passed_earnings": 0,
            "candidates": [],
            "note": "SPY below 20d SMA — long momentum scan gated off",
        }

    quotes = _fetch_quotes(tickers)
    funds = _fetch_fundamentals(tickers)
    volumes = _fetch_volumes(tickers)

    passed_proximity = 0
    passed_volume = 0
    candidates_stage1: list[dict] = []
    threshold_factor = 1 - (proximity_pct / 100.0)

    for ticker in tickers:
        q = quotes.get(ticker)
        f = funds.get(ticker)
        if not q or not f:
            continue
        price = q["price"]
        high_52w = f.get("high_52w")
        avg_vol = f.get("avg_volume")
        if not high_52w or not avg_vol or price < min_price:
            continue
        if q["halted"]:
            continue

        # Proximity gate: at or above (52w * threshold_factor).
        # When proximity_pct=0, this is "price >= 52w high" exactly.
        if price < high_52w * threshold_factor:
            continue
        passed_proximity += 1

        today_vol = volumes.get(ticker, 0)
        vol_ratio = _pace_adjusted_ratio(today_vol, avg_vol, now)
        if vol_ratio < min_volume_ratio:
            continue
        passed_volume += 1

        # Skip already-chased (today's move already large)
        if q["pct_change"] is not None and q["pct_change"] > max_gain_today_pct:
            # Still record but downgrade to B
            grade_floor = "B"
        else:
            grade_floor = "A"

        candidates_stage1.append({
            "ticker": ticker,
            "price": price,
            "high_52w": high_52w,
            "pct_to_52w": round((price - high_52w) / high_52w * 100, 2),
            "volume_ratio": vol_ratio,
            "pct_change_today": q["pct_change"],
            "sector": f.get("sector"),
            "market_cap": f.get("market_cap"),
            "grade_floor": grade_floor,
        })

    # Earnings filter on stage-1 candidates only (one batched call per ticker
    # via rh_mcp.analysis.earnings; cheap because they're already prefiltered).
    candidates: list[dict] = []
    for c in candidates_stage1:
        try:
            er = _earnings.analyze(c["ticker"])
            e0 = er[0] if er else {}
        except Exception:
            e0 = {}
        days_away = e0.get("days_away")
        signal = e0.get("signal", "UNKNOWN")
        if days_away is not None and days_away < skip_earnings_within_days:
            continue  # framework hard rule
        grade = c.pop("grade_floor")
        c["earnings_days_away"] = days_away
        c["earnings_signal"] = signal
        c["grade"] = grade
        c["auto_alert_eligible"] = grade == "A"
        candidates.append(c)

    candidates.sort(key=lambda x: x["volume_ratio"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    return {
        "success": True,
        "scanned_at": scanned_at,
        "spy_uptrend": spy_up,
        "total_scanned": len(tickers),
        "passed_proximity": passed_proximity,
        "passed_volume": passed_volume,
        "passed_earnings": len(candidates),
        "candidates": candidates,
    }
