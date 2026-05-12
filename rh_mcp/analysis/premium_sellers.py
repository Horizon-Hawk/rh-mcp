"""Iron condor / premium-selling candidate scanner.

Finds stocks with:
- Earnings 7-30 days away (sweet spot — long enough for theta to compound,
  short enough that IV is already elevated)
- IV rank > 70 (premium is expensive)
- Sufficient options liquidity (min avg volume, min price)

Returns ranked candidates with IV rank, days to earnings, and the suggested
expected move so the caller can sketch an iron condor structure outside
the wings.

Edge: sells the elevated IV before earnings, lets theta + post-earnings
IV crush work for the position. ~70% win rate setups when sized to defined
risk (iron condor max loss).
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.analysis import earnings as _earnings
from rh_mcp.analysis import iv_rank as _iv_rank
from rh_mcp.auth import login

MIN_DAYS_TO_EARNINGS = 7
MAX_DAYS_TO_EARNINGS = 30
MIN_IV_RANK = 70.0
MIN_PRICE = 10.0          # options liquidity floor
MIN_OPTION_OI = 50        # min OI on ATM options — guards against untradeable strikes
MAX_BID_ASK_SPREAD_PCT = 30.0  # ATM strike bid/ask spread must be <= this % of mid
MAX_IV_PARALLEL = 4       # cap parallel iv_rank calls to avoid rate limits

DEFAULT_TOP_N = 15


def _liquidity_check(ticker: str, current_price: float, min_oi: int, max_spread_pct: float) -> dict:
    """Quick liquidity verdict on ATM options around current_price.

    Returns {"liquid": bool, "reason": str, "atm_call": {...}, "atm_put": {...}}.
    Pulls the nearest expiry that's beyond a week (to avoid expiring-Friday noise)
    and checks ATM call + ATM put for OI and bid/ask spread health.

    Catches the BZ case: high IV rank but zero bids on short legs = untradeable.
    """
    try:
        chain = rh.options.get_chains(ticker)
        dates = sorted(chain.get("expiration_dates", []))
    except Exception:
        return {"liquid": False, "reason": "could not pull chain"}
    if not dates:
        return {"liquid": False, "reason": "no expiry dates"}

    # Pick an expiry ~2-6 weeks out — far enough to be liquid, close enough to matter
    from datetime import date, datetime, timedelta
    today = date.today()
    target_expiry = None
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta_days = (dt - today).days
        if 14 <= delta_days <= 60:
            target_expiry = d
            break
    if not target_expiry:
        return {"liquid": False, "reason": "no expiry in 14-60 day window"}

    try:
        calls = rh.options.find_options_by_expiration(ticker, target_expiry, optionType="call") or []
        puts = rh.options.find_options_by_expiration(ticker, target_expiry, optionType="put") or []
    except Exception:
        return {"liquid": False, "reason": "chain fetch failed"}
    if not calls or not puts:
        return {"liquid": False, "reason": "no options at target expiry"}

    def _nearest(rows):
        return min(rows, key=lambda r: abs(float(r.get("strike_price", 0)) - current_price))

    atm_call = _nearest(calls)
    atm_put = _nearest(puts)

    def _check(row):
        try:
            bid = float(row.get("bid_price") or 0)
            ask = float(row.get("ask_price") or 0)
            oi = int(float(row.get("open_interest") or 0))
        except (ValueError, TypeError):
            return None, "parse error"
        mid = (bid + ask) / 2
        if oi < min_oi:
            return None, f"OI {oi} < {min_oi}"
        if mid <= 0:
            return None, "no mid (zero bid+ask)"
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 999
        if spread_pct > max_spread_pct:
            return None, f"bid-ask {spread_pct:.0f}% > {max_spread_pct}%"
        return {"strike": float(row["strike_price"]), "bid": bid, "ask": ask, "oi": oi, "spread_pct": round(spread_pct, 1)}, None

    call_info, call_err = _check(atm_call)
    put_info, put_err = _check(atm_put)

    if call_err or put_err:
        return {"liquid": False, "reason": f"call: {call_err or 'ok'} | put: {put_err or 'ok'}"}

    return {
        "liquid": True,
        "expiry_checked": target_expiry,
        "atm_call": call_info,
        "atm_put": put_info,
    }


def _load_universe_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(tickers))


def _iv_rank_one(ticker: str) -> dict:
    """Wrap iv_rank.analyze for a single ticker with safe error handling."""
    try:
        result = _iv_rank.analyze(ticker)
        if isinstance(result, list) and result:
            return result[0]
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    return {"ticker": ticker, "error": "no data"}


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    min_days_to_earnings: int = MIN_DAYS_TO_EARNINGS,
    max_days_to_earnings: int = MAX_DAYS_TO_EARNINGS,
    min_iv_rank: float = MIN_IV_RANK,
    min_price: float = MIN_PRICE,
    min_option_oi: int = MIN_OPTION_OI,
    max_bid_ask_spread_pct: float = MAX_BID_ASK_SPREAD_PCT,
    top_n: int = DEFAULT_TOP_N,
    skip_liquidity_check: bool = False,
) -> dict:
    """Find iron-condor / premium-selling candidates.

    Stage 1: filter universe by earnings_days_away in [min, max] window.
    Stage 2: run iv_rank in parallel on survivors; filter iv_rank >= min_iv_rank.
    Stage 3: liquidity check on ATM options — drops names like BZ where high
             IV rank co-exists with zero-bid short legs (untradeable as IC).
    Stage 4: sort by iv_rank descending, return top_n.

    iv_rank is the expensive call (~20s per ticker). With MAX_IV_PARALLEL=4
    threads, scanning 30 candidates takes ~150s. Stage-1 filter is cheap
    (one earnings API call per ticker) so we narrow before paying the iv cost.

    Set skip_liquidity_check=True to keep all IV-passing candidates regardless
    of liquidity (useful for research / debugging the scanner).
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))

    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe — provide tickers list or universe_file"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    # Stage 1: earnings window filter (cheap, batched per ticker)
    # Use parallel pool here too because each call hits RH separately.
    passed_earnings: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_earnings.analyze, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            try:
                er = fut.result()
                e0 = er[0] if er else {}
            except Exception:
                continue
            days = e0.get("days_away")
            if days is None:
                continue
            if min_days_to_earnings <= days <= max_days_to_earnings:
                passed_earnings.append({
                    "ticker": t,
                    "days_to_earnings": days,
                    "earnings_date": e0.get("next_date"),
                    "timing": e0.get("timing"),
                    "eps_estimate": e0.get("eps_estimate"),
                })

    # Stage 2: IV rank filter (expensive — parallel-bounded)
    candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IV_PARALLEL) as ex:
        futures = {ex.submit(_iv_rank_one, e["ticker"]): e for e in passed_earnings}
        for fut in concurrent.futures.as_completed(futures):
            e = futures[fut]
            ivr = fut.result()
            if ivr.get("error"):
                continue
            current_price = ivr.get("current_price")
            iv_rank = ivr.get("iv_rank")
            if not current_price or current_price < min_price:
                continue
            if iv_rank is None or iv_rank < min_iv_rank:
                continue
            candidates.append({
                "ticker": e["ticker"],
                "price": current_price,
                "iv_rank": iv_rank,
                "iv_percentile": ivr.get("iv_percentile"),
                "current_iv": ivr.get("current_iv"),
                "hv30": ivr.get("hv30"),
                "iv_premium": ivr.get("iv_premium"),
                "days_to_earnings": e["days_to_earnings"],
                "earnings_date": e["earnings_date"],
                "timing": e["timing"],
                "eps_estimate": e["eps_estimate"],
            })

    candidates.sort(key=lambda x: x["iv_rank"], reverse=True)

    # Stage 3: liquidity check on ATM options — drops names with zero-bid
    # short legs (untradeable as IC). Trims to top_n first so we don't burn
    # API calls on names we wouldn't ship anyway.
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    if not skip_liquidity_check and candidates:
        liquid_candidates: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IV_PARALLEL) as ex:
            futures = {
                ex.submit(_liquidity_check, c["ticker"], c["price"], min_option_oi, max_bid_ask_spread_pct): c
                for c in candidates
            }
            for fut in concurrent.futures.as_completed(futures):
                c = futures[fut]
                liq = fut.result()
                c["liquidity"] = liq
                if liq.get("liquid"):
                    liquid_candidates.append(c)
        liquid_candidates.sort(key=lambda x: x["iv_rank"], reverse=True)
        passed_liquidity = len(liquid_candidates)
        candidates = liquid_candidates
    else:
        passed_liquidity = len(candidates)

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_scanned": len(tickers),
        "passed_earnings_window": len(passed_earnings),
        "passed_iv_threshold": passed_liquidity if skip_liquidity_check else None,
        "passed_liquidity": passed_liquidity,
        "candidates": candidates,
    }
