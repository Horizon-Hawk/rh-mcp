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
| **`bullish_8k`** (no float/quality filter) | long | 5d | **+143.94%** | **~25%** | **22.7%** | ✓ VALIDATED — only stress-test survivor |
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
