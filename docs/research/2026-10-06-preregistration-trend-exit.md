# Pre-registration: canonical trend-following exit on the Track-A entry

Date registered: 2026-10-06 — committed **before** any implementation or result exists.
Branch base: `feat/reliability-and-evidence-loop` @ `3c29e4d`.

## Why this test, and why only one

The Task 7 backtest (5 years x 28 symbols) found **no detectable edge** in the mechanical Track-A
strategy: gross +0.020R [-0.055, +0.094]; realistic IG costs -0.009R [-0.085, +0.065]
(week-clustered 95% CIs). Its exit structure explains why it cannot capture a trend: 80% of exits
are the 72-hour time stop and only 2.3% reach the fixed 2R target. Trend-following historically
earns its return from a small number of large, long-held winners; a 72h cap plus a fixed 2R target
removes exactly that tail.

So there is one principled, literature-grounded question left: **does letting winners run turn
this entry into a positive-expectancy strategy?** We test that once, with standard parameters,
and accept the answer. Searching many parameter combinations on the same history would almost
certainly find one that looks good by chance and fails live, so we do not do it.

## Hypothesis

**H1:** The existing Track-A entry, combined with the canonical Turtle System 1 exit rules
(initial stop at 2x daily ATR, exit on a 10-day opposite channel breakout, no profit target, no
time cap), has positive net expectancy after realistic IG costs.

## Design (fixed; no parameter will be tuned)

| Element | Rule |
|---|---|
| Entry signal | Unchanged Track-A rule via `compute_trend_frame`: long when `direction=="up"` and `donchian_breakout=="up"`; short when both are `"down"`; both require `trend_strength >= min_trend_strength` (20). |
| Entry fill | Bar `t+1` Open after the signal on bar `t`'s settled close. Still-forming bar dropped. |
| Sides | Long **and** short (`--shorts`). |
| Initial stop | `max(2.0 x daily_ATR14[t], entry_mid x stop_loss_pct_floor, 0.01)`. Fixed at entry; checked intrabar every bar; a gap through the stop fills at the Open. |
| Exit | Long: bar `t`'s settled close < the lowest Low of the prior 10 bars (excluding bar `t`) -> exit at bar `t+1` Open. Short: mirror with the highest High. |
| Profit target | None. |
| Time cap | None. |
| Position rules | One open position per symbol; `track_a_cooldown_hours` (24h) between entries. Unchanged. |
| Costs | `--costs typical` is primary. `--costs max` and `--costs zero` are reported for robustness only. Same adverse-fill + swap model as Task 7. |
| R | Net P&L / initial stop distance from the mid entry (same as Task 7). |
| Universe and period | The same 28 symbols and the same 5-year daily window as Task 7. |
| Statistics | Expectancy in R with a 95% bootstrap CI clustered by entry week (Task 7's verdict method). |

The 2.0 ATR multiple and the 10-day exit channel are the published Turtle System 1 values, used
as-is. The 20-day entry channel is the existing Track-A rule, also the Turtle System 1 value.

## Success criterion (decided now)

H1 **PASSES** only if BOTH hold under `--costs typical`:

1. Full 5-year period: the lower bound of the week-clustered 95% CI of net expectancy is **> 0**.
2. Most recent 2 years of entries: the point estimate of net expectancy is **> 0**.

Otherwise H1 **FAILS**.

## Commitments

- The result is reported whatever it is, with all numbers, including under `max` costs.
- If H1 fails, we do **not** try other parameter values and present them as this test. Any later
  variant is exploratory, labelled as such, and would need its own pre-registration plus fresh
  out-of-sample data.
- If H1 passes, that is evidence, not proof. Survivorship (today's universe includes hindsight
  winners) and understated long funding both flatter the result. Any live use would still go
  through the paper forward-walk verdict gate first.
- Known limitation, disclosed in advance: daily bars cannot resolve intrabar order, so a bar
  touching both the stop and the exit level books the stop (pessimistic), as in Task 7.
