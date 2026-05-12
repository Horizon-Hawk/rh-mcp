# Trade Post-Mortem Template

Copy this template into a memory file when a trade closes. Goal: capture
what worked, what didn't, what to do differently next time — before the
next trade overwrites the muscle memory.

File naming: `project_trade_postmortem_<TICKER>_<YYYY-MM-DD>.md`

---

## Identifier
- **Ticker:** _e.g. APLS_
- **Strategy:** _stock / debit_spread / iron_condor / debit_spread_short / etc._
- **Entry date:** _YYYY-MM-DD HH:MM_
- **Exit date:** _YYYY-MM-DD HH:MM_
- **Holding period:** _N days / N hours_

## Setup at entry
- **Edge thesis (one sentence):** _why did the scanner surface this? what was the stack?_
- **Signal source(s):** _scan_52w / scan_squeeze / scan_sympathy / scan_premium_sellers / etc._
- **Grade at entry:** _A+ / A / B_
- **Pre-entry IV rank:** _N%_
- **Days to earnings:** _N days_
- **Key data points:** _volume_ratio, body%, R:R, sector, etc._

## Trade structure
- **Entry price / structure:** _e.g. 1 IC @ $2.00 credit, 60/65 - 85/90 5/29_
- **Stop / max loss:** _$XXX or breach level_
- **Target:** _50% profit / measured move / etc._
- **Position size:** _N shares / N contracts_
- **Capital deployed:** _$XXX_

## Outcome
- **Exit price / fill:** _XXX_
- **P&L $:** _+/- $XXX_
- **P&L %:** _+/- X.X% on capital, +/- X.X% on equity_
- **Realized R:R:** _X.X (reward/risk actually achieved)_
- **Win/loss/breakeven:** _W / L / BE_
- **Exit reason:** _target / stop / pattern / time / manual / news / trail_

## What worked
- _Bullet — be specific. "Scanner surfaced this 2 days before the move" is useful.
  "Trade worked" is not._

## What didn't
- _Bullet — even on winning trades, what was suboptimal? Late entry? Tight stop
  that nearly clipped? Wrong contract count?_

## What to do differently
- _Concrete action items for next similar setup. e.g. "On iron condors with
  bid/ask spreads >30%, walk the limit 5 cents at a time, don't lift offer."_

## Grade
- **Setup quality:** _A+ / A / B / C / F_ (was the setup correctly identified?)
- **Execution:** _A+ / A / B / C / F_ (was the entry/exit/sizing right?)
- **Risk management:** _A+ / A / B / C / F_ (did we stay within framework rules?)
- **Composite:** _final letter grade_

## Lessons promoted to memory
- _If a lesson is general (applies to future trades of this type), promote it
  to a `feedback_*.md` memory and link here. Don't bury insights in trade-specific
  files._
