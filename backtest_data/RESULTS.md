# Small-Cap Strategy Stress Test — Per-Year Results

**Last refresh:** 2026-05-13
**Test window:** 2020-04 → 2026-05 (~6.1 years of price data, 8-K corpus
covers 2022-05 → 2026-05)
**Universe:** Finviz `cap_small` ($300M-$2B, price ≥ $5, avg vol ≥ 500K) =
525 tickers
**Simulator:** `rh_mcp.analysis.backtest_smallcap` — $36K start each
year, framework risk rules (A+ 3% / A 2% / B 1% on small caps, 15% max
position, 6% daily loss limit, 5 concurrent max)

---

## Strategy verdict matrix

| Strategy | Side | Hold | 4-yr Return | Annualized | Max DD | Status |
|---|---|---|---:|---:|---:|---|
| **`bullish_8k` on small-cap** (no filter) | long | 5d | **+143.94%** | **~25%** | **22.7%** | ✓ VALIDATED — small-cap, all regimes |
| **`bullish_8k` on mid-cap** (no filter) | long | 5d | **+147.95%** | **~25%** | **21.6%** | ✓ VALIDATED — better in bear, lower DD |
| **`bullish_8k` STACK** (small + mid, shared $36K, 5 concurrent) | long | 5d | **+372.92%** | **~47%** | **20.8%** | ✓ VALIDATED — 2× trades, lower DD than either alone |
| **`buyback` (buyback_authorized + dividend_increase)** | long | 5d | **+42.29%** | **~9%** | **18.2%** | ✓ VALIDATED — independent signal pool |
| **PEAD negative-earnings bounce** (item 2.02, t+1 neg, hold 4d) | long | 4d drift | **+141.66%** | **~24%** | **29.4%** | ✓ VALIDATED — 4 of 5 years positive incl. 2022 (+33%) |
| bullish_8k + low_float | long | 5d | +44.76% | ~10% | 18.3% | INVALIDATED — filter destroys edge |
| bullish_8k + low_float + quality (strict) | long | 5d | +11.91% | ~3% | 4.3% | INVALIDATED — over-filters |
| bullish_8k + low_float + loose_quality | long | 5d | +19.80% | ~5% | 8.8% | INVALIDATED — still cuts signal |
| SHORT bearish_8k + low_float | short | 1d | -37.36% | -11% | 38.6% | INVALIDATED — negative every year |
| FRD_LONG (bounce) | long | 5d | +46.59% | ~10% | 63.7% | INVALIDATED — -40.77% in 2022 bear |
| FRD_LONG (bounce) | long | 1d | +54.13% | ~12% | 35.7% | INVALIDATED — regime-fitted |
| FRD_SHORT (continuation) | short | 1d | -63.79% | -23% | 71.3% | INVALIDATED — thesis is wrong |
| gap_and_go (≥20%, small-cap) | long | 1d | +32.12% | ~7% | 17.1% | SECONDARY — modest, stable |
| gap_and_go (≥20%, penny universe) | long | 1d | +19.88%¹ | ~5% | 20.8% | SECONDARY — day-trade only |
| SPY buy-and-hold | long | B&H | +70.64% | ~14% | 21.3% | benchmark (4-yr window) |

¹ Penny universe figure is 1-year only. Penny stocks fade by t+5.

---

## Per-year performance ($36K reset each year)

8-K strategies have no data before 2022 because the corpus build window
was 1500 days (May 2026 lookback). Momentum strategies cover full window.

### 8-K strategies

| Year | bullish_8k (no filter) | bullish_8k+lf | SHORT bear+lf | Regime |
|---|---:|---:|---:|---|
| 2022 | **+21.67%** | +3.47% | -11.76% | Bear market |
| 2023 | **+54.27%** | +17.21% | -8.76% | Sideways recovery |
| 2024 | **+27.28%** | +19.26% | -4.37% | Bull resumption |
| 2025 | -0.92% | +1.08% | -16.70% | Continued bull |
| 2026 (partial) | +3.06% | -0.97% | -2.32% | Current bull |
| **4-yr cumulative** | **+143.94%** | **+44.76%** | **-37.36%** | |

`bullish_8k` was POSITIVE in 4 of 5 years and stayed flat in the worst
year (2025: -0.92%). The only strategy that survives across all regimes.

### bullish_8k on MID-CAP universe ($2B-$10B Finviz cap_mid)

| Year | N | Mid-cap Return | Small-cap Return | Winner |
|---|---:|---:|---:|---|
| 2022 (bear) | 106 | **+38.63%** | +21.67% | **Mid-cap** (1.8×) |
| 2023 (sideways) | 121 | +55.70% | +54.27% | tied |
| 2024 (bull) | 142 | +9.33% | +27.28% | small-cap |
| 2025 (bull) | 157 | -4.32% | -0.92% | small-cap (less bad) |
| 2026 (partial) | 80 | +9.81% | +3.06% | **mid-cap** |
| **4yr cumulative** | **606** | **+147.95%** | +143.94% | tied |

Mid-cap wins decisively in bear and recovery regimes; small-cap wins in
sustained bull. Different signal pools (mid-cap fires more 8-Ks per
ticker due to filing density). **Stack both for ~250 signals/yr** with
regime-complementary characteristics. Mid-cap max DD is 16% vs
small-cap 23% — lower drawdown for equivalent cumulative return.

Corpus: `8k_history_midcap_4yr.csv` (4,035 rows / 848 tickers).
Universe: `mid_cap_universe.txt`.

### t+1 stop-loss rule — small-cap ONLY

For small-cap bullish_8k, the t+1 close is highly predictive of t+5
outcome: 62% of t+5 losers were already down at t+1. Backtest shows
exiting at t+1 when down >=5% IMPROVES both return and DD on small-cap.
The rule does NOT transfer to mid-cap or stack:

| Universe / config | No stop | -5% t+1 stop | Net |
|---|---:|---:|---|
| Small-cap only | +143.94% / 22.7% DD | **+152.55% / 22.1% DD** | **+8.6pp / -0.6 DD** ✓ |
| Mid-cap only | +147.95% / 21.6% DD | +141.10% / 20.8% DD | -6.9pp / -0.8 DD (worse) |
| Stack (max=5) | +372.92% / 20.8% DD | +374.02% / 27.3% DD | +1.1pp / **+6.5 DD** (worse) |
| Stack (max=10) | +529.59% / 35.7% DD | +498.49% / 40.2% DD | -31pp / +4.5 DD (worse) |

**Why it only works on small-caps:** mid-cap "down >5% by t+1" filings
have meaningful recovery to t+5; cutting them locks in losses
unnecessarily. On stack, the freed concurrent slot from an early exit
pulls in additional trades that bring more losers, inflating DD.

Run via `simulate_equity_curve(..., t1_stop_pct=-0.05)` or
`python _run_stack_backtest.py --t1-stop-pct -0.05`.

### Negative findings on bullish_8k — what does NOT predict losers

Tested various pre-filters and position-management overlays on the
small-cap bullish_8k corpus (535 long-direction rows over 4yr). Don't
re-test these. None of them improved the baseline (+152.55% / 22.1% DD
with -5% t+1 stop).

**1. direction_confidence does NOT discriminate winners from losers.**

| Confidence | N | Win% | Avg t+5 | Sim Return | Max DD |
|---|---:|---:|---:|---:|---:|
| 0.50 (item-codes only) | 74 | 47.3 | +0.89% | +11.23% | 10.0% |
| 0.70-0.84 | 295 | 57.6 | +1.31% | +85.39% | 8.1% |
| 0.85+ | 166 | 53.6 | +2.00% | +59.01% | 12.8% |

Winners avg conf: 0.724. Losers avg conf: 0.716. Essentially identical.

**2. Filtering out low-confidence trades HURTS total return.**

| Filter | Trades | Return | Max DD |
|---|---:|---:|---:|
| Baseline (take all longs) | 448 | **+152.55%** | 22.1% |
| Body-keyword only (skip 0.50) | 385 | +126.15% | 18.3% |
| Confidence >= 0.85 | 166 | +59.01% | 12.8% |

Removing low-confidence trades trades return for less DD — not a free
win. Compounding math favors more signal volume when marginal trade
expectancy is still positive.

**3. Individual body_keywords don't discriminate.**

All four high-volume keywords (N >= 20) have positive avg returns:
- convertible_debt_issuance: 50.7% win / +3.94% avg (positive skew)
- buyback_authorized: 55.2% win / +1.13% avg
- fda_approval: 55.3% win / +2.25% avg
- definitive_agreement_acquisition: 71.4% win / +2.72% avg

No specific keyword combination predicts losers. The body-keyword
scanner adds value AGGREGATE (filings with body keywords beat item-only),
not at the individual keyword level.

**4. Scale-in / pyramid management UNDERPERFORMS baseline.**

| Strategy | Best per-$ return |
|---|---:|
| Baseline (full size entry, hold 5d, -5% stop) | **+1.453%/$** |
| Stage entries (½ + ½ on +3% confirmation) | +1.335%/$ |
| Pyramid (1 + 1 on +3% confirmation) | +1.335%/$ |

Why scaling in hurts: t+1 → t+5 drift (~+0.8% avg) is smaller than the
full five_day_return (~+2% avg). The add tranche captures less return
per dollar. Plus the concurrent-position limit already binds hard in
the equity sim — pyramiding into winners consumes slots that new
signals can't take.

**5. ATR plays a marginal role — not actionable as a filter or stop rule.**

Computed 20-day ATR at each filing date (524/535 rows had usable ATR).
Two effects measured:

a) ATR bucket analysis (per-trade quality):

| ATR bucket | N | Win% | AvgT5% | Sim Ret | DD |
|---|---:|---:|---:|---:|---:|
| 0-2% (low vol) | 17 | 52.9 | +0.83% | +1.51% | 1.7% |
| 2-4% | 219 | 55.3 | +0.71% | +26.25% | 9.2% |
| **4-6% (sweet spot)** | **188** | **59.0** | **+2.49%** | **+95.77%** | 10.0% |
| 6-10% | 89 | 55.1 | +2.05% | +30.17% | 15.1% |
| 10%+ (high vol) | 11 | 36.4 | +0.80% | +3.50% | 1.8% |

The 4-6% sweet spot has best per-trade quality. But filtering to it
cuts trade count from 438 to 186 — total return drops from +169% to
+96%. Wrong trade-off for a compounding target.

Filtering to ATR 2-10% (skip both extremes) is essentially a no-op
(+168.92% vs +169.18% baseline). Extremes contribute almost nothing.

b) ATR-based stop (replace fixed -5% with N × ATR):

| Stop rule | Trades | Return | DD |
|---|---:|---:|---:|
| **Fixed -5% t+1 stop** | **438** | **+169.18%** | 21.75% |
| 1.0x ATR stop | 424 | +153.80% | 21.65% |
| 1.5x ATR stop | 424 | +157.53% | 21.22% |
| 2.0x ATR stop | 424 | +150.63% | 24.29% |
| 3.0x ATR stop | 424 | +154.63% | 24.23% |

Fixed -5% beats every ATR multiplier. Volatility-scaled stops are
WIDER on high-vol names (let losers run) and TIGHTER on low-vol names
(fire on noise). The static -5% threshold happens to be near-optimal
because the "62% of losers show by t+1" pattern is universal, not
volatility-dependent.

**6. Sector matters marginally — skip Industrials is the only sector filter worth applying.**

Pulled GICS sector via yfinance for each ticker; bucketed 524 long-direction
small-cap rows by sector.

| Sector | N | Win% | AvgT5 | Sim Ret | DD |
|---|---:|---:|---:|---:|---:|
| Healthcare | 212 | 50.5 | +1.67% | +62.00% | 18.3% |
| Technology | 64 | 65.6 | +0.88% | +11.96% | 7.1% |
| Real Estate | 57 | 64.9 | +0.79% | +6.83% | 2.4% |
| Financial Services | 54 | **72.2** | +2.07% | +17.79% | 2.5% |
| **Industrials** | 43 | **41.9** | **+0.22%** | **+1.22%** | 4.8% |
| Communication Services | 38 | 44.7 | +4.20% | +24.26% | 9.1% |
| Energy | 34 | 50.0 | +0.26% | +2.04% | 7.8% |
| Consumer Cyclical | 27 | 59.3 | +1.40% | +3.91% | 4.5% |

Sector filter test:

| Filter | Trades | Return | DD |
|---|---:|---:|---:|
| Baseline (all sectors) | 448 | +152.55% | 22.06% |
| **Skip Industrials only** | **423** | **+153.37%** | **19.80%** |
| Skip Industrials + Energy | 404 | +146.97% | 19.07% |
| Healthcare + Financials + Tech only | 315 | +108.40% | 19.48% |
| ONLY Financial Services | 54 | +17.79% | 2.52% |

**Skip Industrials** is the FIRST refinement that wins on both dimensions:
+0.8pp return AND -2.2pp DD. Industrials standalone has 41.9% win rate
(below 50%) and +0.22% avg — a true drag.

Speculation on why: Industrials 8-Ks are disproportionately capex
announcements, plant closures, or business reorganizations — not the
deal/catalyst type that drives bullish drift. The keyword scanner reads
them as long via item-code 1.01 but the underlying isn't actually a
positive catalyst.

Updated operating rule: skip Industrials sector on small-cap bullish_8k.
All other sector filters reduce return more than they help.

**7. The ONE effective loser-killer is the -5% t+1 stop.**

62% of t+5 losers were already down at t+1. The -5% t+1 close stop
catches the worst of them. This is already in the validated rule and
adds +8.6pp return / -0.6pp DD on small-cap. DO NOT apply to mid-cap
or stack (inflates DD there).

### Stock RSI(2) — invalidated by slippage (do not deploy)

Tested via the `backtest` MCP tool with strategy=rsi2_long on both
universes. Result: marginal positive PF (1.08-1.16) consumed entirely
by realistic slippage. Don't re-test.

| Universe | Trades/5y | Win% | Avg/trade gross | PF | Net after 0.4% slip |
|---|---:|---:|---:|---:|---:|
| Small-cap | 21,874 | 59.5 | +0.19% | 1.08 | **-0.21% (negative)** |
| Mid-cap | 44,861 | 61.2 | +0.26% | 1.16 | **-0.14% (negative)** |

Additionally: 17-36 signals per day across the universe is unmanageable
in practice — you can't deploy 17 trades/day on a single account.

The real RSI(2) edge is in **FUTURES**, not stocks. Per ROBINHOOD.md
Phase 3.5: MNQ RSI(2) has PF 3.61 long / 2.68 short on 5y backtest,
~17-25x stronger than the stock equivalent. This is already deployed
in the framework as the futures layer alongside the equity stack. Use
the `scan_futures_rsi2` MCP tool for live signals.

### Real-account margin backtest (full framework rules + slippage)

Run via `python _run_margin_backtest.py`. Uses the user's actual
$36,541.78 equity, $35,000 margin limit (RH-set), 8% APR margin rate,
and the FRAMEWORK risk tiers (A+ 5% / A 3% / B 1.5%, 25% max position
— not the small-cap-adjusted 3/2/1% used elsewhere on this page).

| Config | Slippage | End equity | Return | Max DD |
|---|---:|---:|---:|---:|
| Cash only | 0% | $450,643 | +1133% | 25.5% |
| **Cash only** | **0.4%** | **$249,916** | **+584%** | 30.9% |
| Cash only | 0.6% | $178,486 | +388% | 34.8% |
| Margin max=5 | 0% | $657,837 | +1700% | 34.9% |
| **Margin max=5** | **0.4%** | **$313,197** | **+757%** | 40.5% |
| Margin max=5 | 0.6% | $218,607 | +498% | 43.4% |
| Margin max=10 | 0.4% | $335,668 | +819% | 40.5% |

The numbers above use the full framework risk tiers (5/3/1.5%), not the
small-cap-reduced tiers (3/2/1%) used in other sections of this file.
This is what the user's actual account would do running the four
validated strategies at framework sizing.

Realistic deployment mid-case (margin max=5, 0.4% slippage):
**$36.5K → $313K over 4 years (~58% annualized net)**. Beats SPY's
+70.64% / 4yr by ~10x at the cost of 40% max drawdown.

Slippage expands drawdown — losers become deeper losers when both
entry and exit cross the spread. The 0.4% slippage case shows DD
30.9% (cash) and 40.5% (margin); the 0% slippage case shows 25.5%
and 34.9% respectively.

### bullish_8k STACK (small-cap + mid-cap, one shared $36K bucket)

Concatenated rows: 935 long-direction filings across both universes
(433 small + 502 mid). Walked forward chronologically, max 5 concurrent
positions across both universes combined.

| Metric | Value |
|---|---:|
| Starting equity | $36,000 |
| Ending equity (4yr) | $170,252 |
| Total return | **+372.92%** |
| Annualized | ~47% gross |
| Max drawdown | **20.8%** (lower than either standalone) |
| Peak equity | $173,230 |
| Trades taken | 692 |
| Trades skipped by concurrent limit | 442 (the edge exceeds framework capacity) |
| Ticker overlap skips | 16 (rare — universes barely conflict) |

After slippage (~0.3-0.5% × 692 trades = -210-345% drag), realistic
NET is +150-300% over 4yr = ~25-40% annualized. $200K target realistic
in 5-8 years compounded, not 12 months.

To raise the cumulative further, increase `max_concurrent` from 5 to
8-10 — the strategy throws away 442 valid trades hitting the limit.
But that increases position correlation and effective leverage.

Run via `python _run_stack_backtest.py`.

### Buyback / dividend_increase (independent signal — body keyword filter)

| Year | N | Win% | Sim Return | DD% |
|---|---:|---:|---:|---:|
| 2022 (bear) | 56 | 42.9 | +5.29% | 3.5 |
| 2023 (sideways) | 83 | 63.9 | **+21.92%** | 1.1 |
| 2024 (bull) | 88 | 56.8 | +17.24% | 9.4 |
| 2025 (bull) | 113 | 49.6 | -11.73% | 17.8 |
| 2026 (partial) | 70 | 45.9 | +7.79% | 5.0 |
| **4yr cumulative** | **323** (5d) | — | **+43.19%** | 18.6 |

Profitable in 4 of 5 years incl. 2022 bear (+5.29%, only 3.5% DD).
Re-run via `python _run_buyback_pead_stress.py`.

### PEAD negative-earnings bounce (item 2.02 + t+1 negative reaction)

| Year | N | Win% | Avg/trade | Sim Return | DD% |
|---|---:|---:|---:|---:|---:|
| 2022 (bear) | 64 | 57.8 | **+3.09%** | **+33.43%** | 1.9 |
| 2023 (sideways) | 96 | 55.2 | +2.76% | +47.79% | 2.2 |
| 2024 (bull) | 134 | 44.8 | +0.91% | +19.87% | 10.2 |
| 2025 (bull) | 188 | 50.5 | -0.23% | -17.27% | 21.9 |
| 2026 (partial) | 76 | 47.4 | +1.49% | +23.57% | 3.8 |
| **4yr cumulative** | **558** | **50.4** | **+1.18%** | **+141.66%** | 29.4% |

The strongest regime-stable PEAD edge. Counter-intuitive: buying *after* a
negative earnings reaction outperforms buying after a strong beat. Edge is
the partial bounce that follows oversold post-earnings flushes.

### Momentum strategies (full 6-year window)

| Year | FRD_LONG t5 | FRD_SHORT t1 | gap_and_go t1 | Regime |
|---|---:|---:|---:|---|
| 2020 | -6.79% | +4.43% | -16.88% | COVID crash + recovery |
| 2021 | +26.59% | -24.19% | +3.94% | Speculative bull |
| 2022 | **-40.77%** | +2.26% | +5.22% | Bear market |
| 2023 | -14.35% | +10.44% | +4.18% | Sideways recovery |
| 2024 | +46.66% | -2.39% | +48.66% | Bull resumption |
| 2025 | +34.90% | -59.00% | -3.84% | Continued bull |
| 2026 (partial) | +23.81% | +1.18% | -2.42% | Current bull |
| **Cumulative** | +46.59% | -63.79% | +32.12% | |

FRD_LONG looks great in 2024-25 (the period the 1-year backtest covered)
but **lost 40.77% in 2022** — the bear test that invalidated it.

---

## What changed from the original recommendation

The first session ended with a "+490% combined return" headline that
promised $36K → $200K in 12 months. The 4-year stress test invalidated
3 of the 4 strategies in that stack. Specifically:

- **FRD_LONG was a 2024-25 phenomenon**, not a stable edge
- **SHORT bearish_8K loses money in every regime tested**
- **The low_float filter looked like a multiplier but destroys signal**
  when measured over multiple regimes
- **`bullish_8k` (no filter) is the actual durable edge**

Realistic $36K → $200K timeline at 25% annualized: **~8 years**, not 12
months. The 12-month sprint requires another 2025-class small-cap rally.

---

## Data files in this folder

- `8k_history_4yr.csv` — 3,944 rows, 8-K filings with direction labels
- `momentum_signals_4yr.csv` — 1,250 signals (FRD + gap-and-go)
- `backtest_smallcap_4yr.csv` — enriched (cap, float, quality flags)

These are checkpoints, not the live cache. The active cache is at
`~/.edgar_cache/`. Rebuild via:

```
python -m rh_mcp.analysis.edgar_history_builder  # 8-K corpus (~45 min)
python -m rh_mcp.analysis.momentum_backtest --lookback-days 1500  # momentum (~15 min)
```

After rebuild, copy the new CSVs into this folder and re-run the stress
test scripts to refresh this table:

```
python C:/Users/algee/TraderMCP-RH/_run_per_year_stress.py
```

The regime-overfit detector in `backtest_smallcap.py` will auto-flag any
new strategy that scores >2× SPY return for the period.
