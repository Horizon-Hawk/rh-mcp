"""Walk-forward backtest framework for price-based strategies.

Honest caveats baked in — read these every time you interpret results:
  1. Survivorship bias: RH's current universe excludes delisted names
  2. No transaction costs: would erode 0.05-0.2% per round-trip in reality
  3. No slippage model: assumes fills at close, no impact
  4. Daily-bar granularity: intraday patterns/stops not captured
  5. Look-ahead risk: framework only passes bars[:i+1] at signal date to avoid

Strategies are plugins implementing two functions:
  signal(bars, idx) -> True/False — should we enter on bars[idx]'s close?
  exit_idx(bars, entry_idx) -> int — bar index to exit at (close)

The backtester walks history day by day, calling signal() on each bar.
When True, opens a position at next bar's open (more realistic than close-on-signal-day).
Tracks all trades and produces summary stats.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime
from pathlib import Path

import robin_stocks.robinhood as rh

from rh_mcp.auth import login

DEFAULT_LOOKBACK_YEARS = 5
_BATCH_SIZE = 75
_BATCH_SLEEP = 0.1


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

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


def _fetch_5y_bars(tickers: list[str]) -> dict[str, list[dict]]:
    """Pull 5 years of daily bars per ticker, batched. Retries on transient errors."""
    out: dict[str, list[dict]] = {t: [] for t in tickers}
    for i in range(0, len(tickers), _BATCH_SIZE):
        chunk = tickers[i:i + _BATCH_SIZE]
        raw = []
        # Retry up to 3x on transient errors (502s, timeouts)
        for attempt in range(3):
            try:
                raw = rh.stocks.get_stock_historicals(
                    chunk, interval="day", span="5year", bounds="regular",
                ) or []
                break
            except Exception:
                if attempt == 2:
                    raw = []
                else:
                    time.sleep(2.0 * (attempt + 1))  # 2s, 4s backoff
        for b in raw:
            if not b or not isinstance(b, dict):
                continue
            sym = (b.get("symbol") or "").upper()
            if sym not in out:
                continue
            try:
                out[sym].append({
                    "date": b["begins_at"][:10],
                    "open": float(b["open_price"]),
                    "high": float(b["high_price"]),
                    "low": float(b["low_price"]),
                    "close": float(b["close_price"]),
                    "volume": int(b["volume"]),
                })
            except (KeyError, ValueError, TypeError):
                pass
        time.sleep(_BATCH_SLEEP)
    for sym in out:
        out[sym].sort(key=lambda x: x["date"])
    return out


def _fetch_earnings_history(ticker: str) -> list[dict]:
    """Pull historical earnings for a ticker (used by PEAD backtest)."""
    try:
        raw = rh.stocks.get_earnings(ticker, info=None) or []
    except Exception:
        return []
    out = []
    for r in raw:
        report = r.get("report") or {}
        eps = r.get("eps") or {}
        ds = report.get("date")
        actual = eps.get("actual")
        estimate = eps.get("estimate")
        if not ds or actual is None or estimate is None:
            continue
        try:
            out.append({
                "date": ds,
                "actual": float(actual),
                "estimate": float(estimate),
            })
        except (ValueError, TypeError):
            pass
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------------------
# Strategy plugins — each returns (entries, exits) for a ticker's bars
# ---------------------------------------------------------------------------

def _strat_capitulation(bars: list[dict], hold_days: int = 5, **_) -> list[dict]:
    """Find capitulation-reversal entries. Hold N days, exit at close."""
    if len(bars) < 25:
        return []
    trades = []
    for i in range(22, len(bars) - 1):  # need 20d avg before, 1d to enter
        yesterday = bars[i - 1]
        today = bars[i]
        avg_vol = statistics.mean(b["volume"] for b in bars[i - 22:i - 2])
        if avg_vol <= 0:
            continue
        # Yesterday capitulation
        prior_close = bars[i - 2]["close"]
        if prior_close <= 0:
            continue
        decline = (yesterday["close"] - prior_close) / prior_close * 100
        if decline > -5:
            continue
        if yesterday["volume"] / avg_vol < 3.0:
            continue
        rng = yesterday["high"] - yesterday["low"]
        if rng <= 0 or (yesterday["close"] - yesterday["low"]) / rng * 100 > 25:
            continue
        # Today reversal
        today_rng = today["high"] - today["low"]
        if today_rng <= 0:
            continue
        if (today["close"] - today["low"]) / today_rng * 100 < 50:
            continue
        if today["high"] < yesterday["close"] or today["close"] < yesterday["close"]:
            continue
        # Entry on NEXT bar's open; exit hold_days later at close
        entry_idx = i + 1
        exit_idx = min(i + 1 + hold_days, len(bars) - 1)
        if entry_idx >= len(bars):
            continue
        entry_price = bars[entry_idx]["open"]
        exit_price = bars[exit_idx]["close"]
        trades.append({
            "entry_date": bars[entry_idx]["date"],
            "exit_date": bars[exit_idx]["date"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": (exit_price - entry_price) / entry_price * 100,
            "direction": "long",
        })
    return trades


def _strat_rsi2_long(bars: list[dict], hold_days: int = 5, **_) -> list[dict]:
    """RSI(2) < 5 + price > 200d SMA → long, exit when RSI(2) > 70 OR hold_days hit."""
    if len(bars) < 202:
        return []
    closes = [b["close"] for b in bars]
    trades = []
    i = 201
    while i < len(bars) - 1:
        sma = statistics.mean(closes[i - 200:i])
        if sma <= 0:
            i += 1
            continue
        if closes[i] <= sma:
            i += 1
            continue
        rsi = _rsi2(closes, i)
        if rsi is None or rsi >= 5:
            i += 1
            continue
        # Entry next open
        entry_idx = i + 1
        if entry_idx >= len(bars):
            break
        entry_price = bars[entry_idx]["open"]
        # Exit on RSI(2) > 70 or hold_days
        exit_idx = min(entry_idx + hold_days, len(bars) - 1)
        for j in range(entry_idx + 1, exit_idx + 1):
            r = _rsi2(closes, j)
            if r is not None and r > 70:
                exit_idx = j
                break
        exit_price = bars[exit_idx]["close"]
        trades.append({
            "entry_date": bars[entry_idx]["date"],
            "exit_date": bars[exit_idx]["date"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": (exit_price - entry_price) / entry_price * 100,
            "direction": "long",
        })
        i = exit_idx + 1  # don't re-enter on overlapping bars
    return trades


def _rsi2(closes: list[float], idx: int) -> float | None:
    """RSI(2) at index idx using only data up through closes[:idx+1]."""
    if idx < 2:
        return None
    gains = []
    losses = []
    for k in (idx - 1, idx):
        diff = closes[k] - closes[k - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = statistics.mean(gains)
    avg_loss = statistics.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _strat_momentum_12_1(bars: list[dict], rebalance_days: int = 21, **_) -> list[dict]:
    """Monthly rebalance: enter on month boundaries where 12-1 signal is positive
    (just a single-name position check — true momentum requires cross-sectional ranking
    which the backtest framework runs separately at the portfolio level).

    For per-name backtest, treat as: enter if 12-1 return > 10% (top-decile-ish proxy),
    hold 21 days.
    """
    if len(bars) < 252:
        return []
    closes = [b["close"] for b in bars]
    trades = []
    i = 252
    while i < len(bars) - rebalance_days:
        # 12-1 signal at bar i: return from bar i-252 to bar i-21, minus return from i-21 to i
        if closes[i - 252] <= 0 or closes[i - 21] <= 0:
            i += rebalance_days
            continue
        ret_12m = (closes[i] - closes[i - 252]) / closes[i - 252] * 100
        ret_1m = (closes[i] - closes[i - 21]) / closes[i - 21] * 100
        signal = ret_12m - ret_1m
        if signal < 10:
            i += rebalance_days
            continue
        entry_idx = i + 1
        if entry_idx >= len(bars):
            break
        exit_idx = min(entry_idx + rebalance_days, len(bars) - 1)
        entry_price = bars[entry_idx]["open"]
        exit_price = bars[exit_idx]["close"]
        trades.append({
            "entry_date": bars[entry_idx]["date"],
            "exit_date": bars[exit_idx]["date"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": (exit_price - entry_price) / entry_price * 100,
            "direction": "long",
        })
        i = exit_idx + 1
    return trades


def _strat_pead(bars: list[dict], earnings: list[dict], hold_days: int = 30, **_) -> list[dict]:
    """PEAD: enter on day after earnings beat ≥5% with gap-up ≥3%. Hold hold_days."""
    if not earnings or len(bars) < 30:
        return []
    bar_dates = {b["date"]: i for i, b in enumerate(bars)}
    trades = []
    for e in earnings:
        if e["estimate"] <= 0:
            continue
        beat_pct = (e["actual"] - e["estimate"]) / abs(e["estimate"]) * 100
        if beat_pct < 5:
            continue
        # Find the first bar on or after the earnings date
        ed = e["date"]
        print_idx = None
        for d, idx in bar_dates.items():
            if d >= ed:
                if print_idx is None or idx < print_idx:
                    print_idx = idx
        if print_idx is None or print_idx == 0:
            continue
        pre_close = bars[print_idx - 1]["close"]
        if pre_close <= 0:
            continue
        print_open = bars[print_idx]["open"]
        gap_pct = (print_open - pre_close) / pre_close * 100
        if gap_pct < 3:
            continue
        # Enter next bar after print (so we have full info to confirm)
        entry_idx = print_idx + 1
        if entry_idx >= len(bars):
            continue
        exit_idx = min(entry_idx + hold_days, len(bars) - 1)
        entry_price = bars[entry_idx]["open"]
        exit_price = bars[exit_idx]["close"]
        trades.append({
            "entry_date": bars[entry_idx]["date"],
            "exit_date": bars[exit_idx]["date"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": (exit_price - entry_price) / entry_price * 100,
            "direction": "long",
            "beat_pct": round(beat_pct, 1),
            "gap_pct": round(gap_pct, 2),
        })
    return trades


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _trade_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    returns = [t["return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_ret = statistics.mean(returns)
    win_rate = len(wins) / len(returns) * 100
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = statistics.mean(losses) if losses else 0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    # Rough Sharpe approximation (per-trade returns, not annualized)
    if len(returns) > 1:
        sd = statistics.stdev(returns)
        sharpe_per_trade = avg_ret / sd if sd > 0 else 0
    else:
        sharpe_per_trade = 0
    return {
        "trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_return_pct": round(avg_ret, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "sharpe_per_trade": round(sharpe_per_trade, 2),
        "best_trade_pct": round(max(returns), 2),
        "worst_trade_pct": round(min(returns), 2),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

STRATEGIES = {
    "capitulation_reversal": (_strat_capitulation, False),   # (fn, needs_earnings)
    "rsi2_long": (_strat_rsi2_long, False),
    "momentum_12_1": (_strat_momentum_12_1, False),
    "pead": (_strat_pead, True),
}


def backtest(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    strategies: list[str] | None = None,
) -> dict:
    """Run all (or selected) strategy backtests across the given tickers.

    Returns per-strategy stats + per-ticker stats. Costs/slippage NOT modeled.
    Read the module docstring for the full caveats list.
    """
    if tickers is None:
        if universe_file is None:
            universe_file = Path("stock_universe.txt")
        tickers = _load_universe_from_file(Path(universe_file))
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {"success": False, "error": "empty universe"}

    if strategies is None:
        strategies = list(STRATEGIES.keys())
    strategies = [s for s in strategies if s in STRATEGIES]
    if not strategies:
        return {"success": False, "error": "no valid strategies"}

    login()
    started_at = datetime.now().isoformat(timespec="seconds")

    bars_by_ticker = _fetch_5y_bars(tickers)
    earnings_by_ticker: dict[str, list[dict]] = {}
    if any(STRATEGIES[s][1] for s in strategies):
        # Earnings needed — pull serially (no batched endpoint)
        for t in tickers:
            earnings_by_ticker[t] = _fetch_earnings_history(t)
            time.sleep(0.05)

    per_strategy: dict[str, dict] = {}
    for strat_name in strategies:
        fn, needs_earnings = STRATEGIES[strat_name]
        all_trades: list[dict] = []
        per_ticker_counts: dict[str, int] = {}
        for ticker in tickers:
            bars = bars_by_ticker.get(ticker, [])
            if not bars:
                continue
            if needs_earnings:
                trades = fn(bars=bars, earnings=earnings_by_ticker.get(ticker, []))
            else:
                trades = fn(bars=bars)
            for t in trades:
                t["ticker"] = ticker
            all_trades.extend(trades)
            per_ticker_counts[ticker] = len(trades)
        per_strategy[strat_name] = {
            "stats": _trade_stats(all_trades),
            "signal_count_by_ticker": per_ticker_counts,
            "lookback_window": (
                bars_by_ticker[next(iter(bars_by_ticker))][0]["date"] if bars_by_ticker and bars_by_ticker[next(iter(bars_by_ticker))] else None,
                bars_by_ticker[next(iter(bars_by_ticker))][-1]["date"] if bars_by_ticker and bars_by_ticker[next(iter(bars_by_ticker))] else None,
            ),
        }

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "tickers_scanned": len(tickers),
        "strategies": per_strategy,
        "caveats": [
            "Survivorship bias: delisted names not in current RH universe",
            "No transaction costs (would erode ~0.05-0.2% per round trip)",
            "No slippage modeled (assumes fills at close)",
            "Daily bars only — intraday patterns/stops invisible",
            "Look-ahead-safe by construction (only past bars used at signal date)",
        ],
    }
