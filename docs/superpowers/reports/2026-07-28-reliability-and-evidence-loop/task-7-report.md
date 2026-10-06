# Task 7 report — Vectorised Track-A backtester

**Status:** DONE. Commits `9cc2cae` (backtester) and `09d3eaf` (reporting-honesty fixes after the adversarial review: cost scenarios, entry-week clustered CIs, stop-first bound, unknown symbols skipped, disclosures). Both were committed with explicit paths only; the user's unrelated work in the tree was not touched. The review rebuilt the simulation independently and matched every number, so `09d3eaf` changes only the reporting. Trade generation, fills, stops and R are unchanged, and `--costs max` reproduces the original output exactly.
**Tests:** `tests/test_backtest.py` has 46 passing tests (35 + 11 new). Full suite: **965 passed, 8 failed**. The 8 failures are the same pre-existing macOS watcher/memory failures as the baseline. No unit test touches the network.

## Headline (revised 2026-10-06 after adversarial review)

**Track-A has no detectable edge.**
- **Before costs, expectancy is about zero.** Gross E[R] (mid to mid, no costs):
  - daily-ATR stops: +0.020R [−0.055, +0.094]
  - live-style hourly-ATR stops: −0.053R [−0.153, +0.045]
- **After realistic costs, it is at best break-even.** Net E[R] with daily-ATR stops is −0.009R [−0.085, +0.065] under typical IG costs.
- **Live's hourly-ATR stops are materially worse.** Net E[R] is −0.128R [−0.227, −0.033] under typical costs and −0.361R [−0.461, −0.265] at the max-spread bound.

All CIs are 95% and resample whole **entry weeks**. All figures are longs only, 28 symbols, entries 2021-10-07 → 2026-10-05, repo-default settings, nothing tuned.

The previous version of this report said "the R-expectancy CI lies entirely below zero in all four runs". **That claim is withdrawn for the strategy as designed.** It held only under the `max` cost scenario. That scenario charges each instrument's `max_spread_bps`, the spread at which live *refuses* to trade, so it is an upper bound and not a typical cost. Under typical costs, the clustered CI for daily-ATR stops straddles zero in both runs. The only configuration whose clustered CI under typical costs lies entirely below zero is live's hourly-ATR stop regime (see caveat (a) under point 3).

### Longs only

| costs | stop mode | n (weeks) | gross E[R] [week CI] | **net E[R] [week CI]** | net i.i.d. CI (too narrow) | both-hit bars → target-first net E[R] | verdict (week CI) |
|---|---|---|---|---|---|---|---|
| max | daily_atr | 1028 (220) | +0.020 [−0.055, +0.094] | **−0.087 [−0.162, −0.012]** | [−0.135, −0.036] | 1 → −0.084 | negative_expectancy |
| typical | daily_atr | 1028 (220) | +0.020 [−0.055, +0.094] | **−0.009 [−0.085, +0.065]** | [−0.058, +0.040] | 1 → −0.006 | indeterminate |
| zero | daily_atr | 1028 (220) | +0.020 [−0.055, +0.094] | **+0.020 [−0.055, +0.094]** | [−0.028, +0.070] | 1 → +0.023 | indeterminate |
| max | live_hourly_equiv | 1334 (221) | −0.053 [−0.153, +0.045] | **−0.361 [−0.461, −0.265]** | [−0.435, −0.282] | 58 → −0.230 | negative_expectancy |
| typical | live_hourly_equiv | 1334 (221) | −0.053 [−0.153, +0.045] | **−0.128 [−0.227, −0.033]** | [−0.203, −0.050] | 58 → +0.003 | negative_expectancy |
| zero | live_hourly_equiv | 1334 (221) | −0.053 [−0.153, +0.045] | **−0.053 [−0.153, +0.045]** | [−0.127, +0.026] | 58 → +0.078 | indeterminate |

### With shorts (`--shorts`)

| costs | stop mode | n (weeks) | gross E[R] [week CI] | **net E[R] [week CI]** | net i.i.d. CI (too narrow) | both-hit bars → target-first net E[R] | verdict (week CI) |
|---|---|---|---|---|---|---|---|
| max | daily_atr | 1322 (246) | +0.024 [−0.033, +0.087] | **−0.074 [−0.133, −0.011]** | [−0.115, −0.033] | 1 → −0.072 | negative_expectancy |
| typical | daily_atr | 1322 (246) | +0.024 [−0.033, +0.087] | **−0.004 [−0.061, +0.059]** | [−0.045, +0.038] | 1 → −0.001 | indeterminate |
| zero | daily_atr | 1322 (246) | +0.024 [−0.033, +0.087] | **+0.024 [−0.033, +0.087]** | [−0.017, +0.066] | 1 → +0.027 | indeterminate |
| max | live_hourly_equiv | 1745 (247) | −0.041 [−0.128, +0.046] | **−0.325 [−0.413, −0.239]** | [−0.392, −0.258] | 78 → −0.191 | negative_expectancy |
| typical | live_hourly_equiv | 1745 (247) | −0.041 [−0.128, +0.046] | **−0.112 [−0.198, −0.026]** | [−0.177, −0.045] | 78 → +0.022 | negative_expectancy |
| zero | live_hourly_equiv | 1745 (247) | −0.041 [−0.128, +0.046] | **−0.041 [−0.128, +0.046]** | [−0.107, +0.026] | 78 → +0.093 | indeterminate |

**Cost scenarios** (`--costs`; slippage is per leg; swap is 200 bps/yr flat in max and typical):

| scenario | spread | slippage | swap |
|---|---|---|---|
| max | `max_spread_bps`, live's refuse-to-trade cap | 5 bps | 200 bps/yr |
| typical | IG published minimum / core-hours spread, from the reviewer's table (for example SPX 0.9, EURUSD 0.6, GOLD 1.3, WTI 4.0 bps; shares 3–4 bps including CFD commission) | 1 bp | 200 bps/yr |
| zero | none | none | none |

**Both-hit bars** are bars whose range touched both the stop and the target. The backtest books them as stops. The last column re-scores them as targets, so the truth lies between the two numbers.

**What the numbers say**

1. **No edge before costs.** Gross E[R] is within ±0.06R of zero in all four runs, and every gross clustered CI straddles zero. The daily-ATR gross win rate is 51.4%, which is a coin flip with average win and average loss both about 0.61R.
2. **At best break-even after realistic costs, and no better than that.** Under typical costs, daily-ATR stops give:
   - longs only: −0.009R [−0.085, +0.065]
   - with shorts: −0.004R [−0.061, +0.059]

   These numbers cannot be told apart from zero, and **this backtest does not show the designed strategy losing money under typical costs**. It is significantly negative only at the `max` bound (week CI [−0.162, −0.012]). Two biases that are not modelled both *flatter* the result (see point 6), so the honest reading is "break-even at best". It is not "a small positive edge hidden by noise".
3. **Live's hourly-ATR stops are materially worse.**
   - Its average stop is 0.88% against 2.71%, and 57% of its trades are stopped out (759 of 1334).
   - Costs weigh about 2.6–2.9x more per trade in R on these tight stops: 0.075R against 0.029R under typical costs, and 0.308R against 0.107R under max.
   - Under typical costs, live-style stops lose −0.128R per trade, with a clustered CI entirely below zero. The daily-ATR CI straddles zero.

   Two caveats bound this claim:
   - **(a) The stop-first rule.** 58 of the 1334 live-style trades exit on a daily bar that touched both stop and target; only 1 daily-ATR trade does. Booking all 58 at the target instead lifts live-style typical-cost E[R] from −0.128 to +0.003, level with daily-ATR (−0.006).
     - The stop sits half as far from entry as the target, so the stop is the likelier first touch, and the truth is probably nearer −0.128 than +0.003.
     - Under typical costs, though, the gap between the regimes partly rests on this rule.
     - Under max costs, live-style stops are worse in both scorings: −0.361 / −0.230 against −0.087 / −0.084.
     - An hourly replay of the 58 bars would settle it. Yahoo's 60m history covers only the last ~2 of the 5 years.
   - **(b) The approximation.** live_hourly_equiv approximates live as daily_ATR14 / r_sym; it is not an hourly replay (see the approximation check below). The 0/11 live record (all stop-outs) fits this regime, but those 11 were LLM-track trades, not Track-A signals.
4. **The 72-hour time stop dominates the designed strategy.** 80% of daily_atr exits (822 of 1028) are time stops, and only 2.3% (24) reach the 2R target. In practice the strategy is "hold 3 days after a breakout".
5. **No year is reliably positive.** Longs only, typical costs, net E[R]:

   | year | daily_atr | live_hourly_equiv |
   |---|---|---|
   | 2021 (Oct–Dec only, n=72) | +0.167 | +0.163 |
   | 2022 | −0.068 | −0.180 |
   | 2023 | −0.020 | −0.097 |
   | 2024 | +0.009 | −0.024 |
   | 2025 | −0.101 | −0.304 |
   | 2026 YTD | +0.065 | −0.195 |

   daily_atr scatters around zero. live_hourly_equiv is negative in every full calendar year.
6. **The unmodelled biases mostly flatter the strategy.** The CLI now prints these disclosures.
   - **Swap.** Swap is a flat 2%/yr, charged on both longs and shorts. IG long funding is roughly benchmark + ~3%, and the benchmark was ~0–5% over 2021–26.
     - The reviewer re-priced the same trades with year-by-year IG funding (scratch script `bt_review/costs.py`, not a backtest.py output). Typical-cost daily-ATR longs fall from −0.009 to −0.031R [−0.102, +0.040].
     - Shorts are the exception: there the flat 2% is conservative.
   - **Survivorship.** The universe is today's instrument list, picked with hindsight (NVDA, META, AMD, GOLD and so on).
   - **Biases the other way.** The `max` spread is now bracketed by the `typical` and `zero` scenarios. The stop-first rule is bracketed by the target-first bound.
7. **Shorts do not rescue it.** Under typical costs, daily_atr sells make +0.017R (n=294, gross +0.038) and live sells make −0.059R (n=411).

**Why week clustering matters.** SPX, SPY, US30, QQQ and NAS100 frequently enter on the same day, so the 1028 trades fall into only 220 distinct entry weeks. Resampling whole weeks widens the CIs by 30–50% against the i.i.d. trade bootstrap (daily-ATR max: [−0.162, −0.012] against [−0.135, −0.036]). In this data the wider CIs flip no verdict, but they matter when a CI sits near zero.

No parameter was tuned. All settings are repo defaults (below). The zero-cost run is a diagnostic only, not a candidate configuration.

## Mutation testing (each mutant applied, suite run, source restored; md5 verified unchanged)

| mutant | result | killing tests |
|---|---|---|
| (a1) fill on signal bar t (entry index = t) | **KILLED** | 14, incl. `test_signal_on_bar_t_fills_at_open_of_bar_t_plus_1` |
| (a2) fill at signal bar t's close | **KILLED** | `test_no_lookahead_entry_uses_next_bar_open`, `test_signal_on_bar_t_fills_at_open_of_bar_t_plus_1` |
| (b) target wins a both-touched bar | **KILLED** | `test_bar_touching_both_stop_and_target_records_the_stop`, short mirror |
| (c) ignore gaps (fill at the stop/target level) | **KILLED** | 3 gap tests (long stop, long target, short stop) |
| (c2) gap check removed entirely | **KILLED** | same 3 |
| (d) zero costs (no adverse fill, no swap) | **KILLED** | `test_costs_reduce_expectancy`, `test_costs_mirror_paper_adverse_fill…`, `test_default_swap…` |
| (e) stop_mode ignored (ratio not applied) | **KILLED** | `test_live_hourly_equiv_sizes_stop…`, `…honours_pct_floor`, `test_run_backtest_stop_mode_changes_the_result` |
| extra: forming bar kept | KILLED | `test_simulation_never_uses_a_still_forming_bar` |
| extra: cooldown ignored | KILLED | `test_one_position_at_a_time_and_cooldown_between_entries` |
| extra: overlapping positions | KILLED | same |
| extra: shorts always on | KILLED | `test_shorts_off_by_default_and_on_when_allowed` |
| extra: time stop ignored | KILLED | `test_time_stop…` + 2 |

Note: the brief's verbatim `test_no_lookahead_entry_uses_next_bar_open` **does not catch mutant (a1)** on its own. Its fixture has Open == Close and a signal on almost every bar, so filling on bar t still satisfies "entry == Open[entry_idx]". The single-signal scenario tests are what kill it.

## Deviation from the brief (must read)

**The brief's test fixture could never trade under the real rule.** `_trending_up()` set `High = Close × 1.01` on a ramp rising about 0.25% per bar. A Donchian up-breakout needs `close ≥ 0.999 × prior-20-bar high` = `1.009 × prev close`, so it never fires. I verified this: 0 breakouts and 0 signals in 400 bars. As written, `test_uptrend_generates_long_trades` could only pass if the entry rule were broken. I kept all four test bodies verbatim and changed only the fixture to `High = Close × 1.001` (351 signals). A comment in the test file records why.

Other small additions beyond the brief, all backwards-compatible:
- `BacktestTrade` gains three fields with defaults: `hold_hours`, `gap` (exit filled at an open beyond the level) and `stop_pct`.
- The report also returns `expectancy_r` / `expectancy_r_ci`, which compare directly with forward_eval's `expectancy_r`. As the brief asks, `expectancy_ci` is the CI of `expectancy_pct`. It also returns `verdict` (forward_eval thresholds), `exit_reasons`, `gap_exits`, `avg_stop_pct`, `by_side`, `by_year`, `skipped` and `symbol_concentration`.
- New public helpers: `entry_signals`, `drop_unsettled_bar`, `measure_hourly_ratio`, `format_comparison`.

## How it works

- **Signals:** `entry_signals(compute_trend_frame(daily))` reproduces `decide_track_a`. Trend strength is rounded to 0.1 before the `>= min_trend_strength` gate, because that is the value live compares (`build_daily_trend` rounds it). `test_entry_signals_match_decide_track_a_on_every_prefix` checks every bar of two 420-bar random walks against `decide_track_a(build_daily_trend(daily[:t+1]))`, covering 45 long and 16 short signals.
- **Shorts:** on when `allow_shorts=True` (CLI `--shorts`), otherwise `getattr(settings, "paper_allow_shorts", False)`.
- **Stops:** `max(ATR14[t] × atr_stop_multiplier, entry_mid × stop_loss_pct_floor, 0.01)`. ATR is the daily ATR in `daily_atr` mode and `ATR14 / r_sym` in `live_hourly_equiv` mode, computed with `indicators._atr`, the same function live uses. Levels sit around the mid entry (the open of bar t+1). TP = stop × risk_reward_ratio.
- **Exit walk:**
  - The entry bar's own range is checked.
  - On each later bar's open: a gap through the stop fills at the open (`stop`). Otherwise a gap through the target fills at the open (`tp`). Otherwise, once the time limit is reached, the position closes at that open (`time`).
  - Then the bar's range is checked, and stop wins if both levels were touched.
  - If the data ends first, the position closes at the last close (`end_of_data`).
- **Costs:** `paper_pnl._adverse_fill` is reused on both legs. Financing is `fill_entry × swap/1e4 × hold/8766`, and R = pnl_units / stop distance, both exactly as `paper_pnl._close` computes them.
- **Flat and cooldown:** a signal on bar t counts only if the book is flat at bar t's close, and only if the fill time (t+1) is at least `track_a_cooldown_hours` after the previous entry.
- **Forming bar:** `drop_unsettled_bar` drops any bar dated today or later in the bar's own timezone (UTC if the index is naive), before any other processing. In the real run it dropped today's (6 Oct) bar for futures, FX and EU indices. US cash markets had not opened yet, so they had no such bar.
- **r_sym:** `measure_hourly_ratio` samples every hourly bar (each one is a moment when live could size a stop). For each, it takes the daily ATR14 of the most recent settled daily bar dated strictly before that hour, divides by that hour's hourly ATR14, and returns the median.
- **CLI:**
  - Downloads `years` of daily bars plus 400 days of warm-up (live also downloads 400 days of daily bars, so sma200 exists on the first evaluated bar). Only signals inside the window are traded.
  - Downloads 730 days of 60m bars to measure r_sym.
  - Runs both regimes on the same symbols (those with an r_sym).
  - Prints JSON to stdout and the side-by-side table to stderr. Optional `--cache` and `--trades-out` flags.

## Real run

```
LLM_PROVIDER=openrouter OPENROUTER_API_KEY=unused-backtest \
  .venv/bin/python -m trading_bot.backtest --years 5 [--shorts] --cache <tmp>/bt_cache --trades-out <csv>
```

(`Settings()` refuses to build without an LLM key, and the repo has no `.env`, so I passed a placeholder. The backtest makes no LLM calls.)

- **Generated:** 2026-10-06T11:36Z. **Window:** 2021-10-06 → last settled bar 2026-10-05.
- **Settings (repo defaults):** min_trend_strength 20, atr_stop_multiplier 1.5, stop_loss_pct_floor 0.005, risk_reward_ratio 2.0, max hold 72h, cooldown 24h, slippage 5 bps per leg, swap 200 bps/yr. Spread = each instrument's `max_spread_bps`.
- **Universe:** `list_supported_instruments(allowed_asset_classes)`, 28 symbols. **Download failures: none.** Every symbol got a daily series (1530–1585 bars from 2020-09-01) and an hourly series (5k bars for US cash, about 13.5k for futures, about 17k for FX).
- **Raw outputs:** `.superpowers/sdd/task-7-backtest-5y-{longs,shorts}.json` and `task-7-trades-{longs,shorts}.csv`.
- **Re-run after review (`09d3eaf`, 2026-10-06T12:00Z, same cached data, `--costs max|typical|zero`):** `task-7-backtest-5y-{longs,shorts}-{max,typical,zero}.{json,txt}` (the `.txt` holds the stderr table and disclosures) and `task-7-trades-{longs,shorts}-{max,typical,zero}.csv`. The `max` runs reproduce the tables below to the last digit.
  - Each trade row now carries `entry_mid`, `exit_mid`, `gross_r`, `both_hit`, `r_target_first` and its `spread_bps` / `slippage_bps` / `swap_bps`.
  - Recomputing `entry`, `r_multiple` and `gross_r` from those columns matches to within 1e-11.

The two tables below are the ORIGINAL output (`max` costs, with the i.i.d. CI and the verdict taken from it). The headline tables above supersede them.

### Longs only (`--years 5`)

```
metric                                         daily_atr             live_hourly_equiv
--------------------------------------------------------------------------------------
trades                                              1028                          1334
win rate                                           43.9%                         33.7%
win rate 95% CI                           [40.9%, 46.9%]                [31.2%, 36.3%]
expectancy R                                      -0.087                        -0.361
expectancy R 95% CI                     [-0.135, -0.036]              [-0.435, -0.282]
expectancy % / trade                             -0.237%                       -0.284%
expectancy % 95% CI                   [-0.433%, -0.044%]            [-0.360%, -0.202%]
verdict (R, n>=60)                   negative_expectancy           negative_expectancy
avg stop distance                                  2.71%                         0.88%
avg win / loss R                           +0.60 / -0.62                 +1.41 / -1.26
profit factor (R)                                   0.75                          0.57
worst trade R                                      -3.29                         -7.10
exits stop/tp/time/eod                      181/24/822/1                 759/284/290/1
gap-through exits                                     46                           116
avg hold hours                                      76.2                          36.4

symbol     r_sym  daily n   win%    E[R]  stop%   live n   win%    E[R]  stop%
------------------------------------------------------------------------------
AAPL        3.10       47    45%  -0.160  2.86%       63    37%  -0.336  0.92%
AMD         3.19       37    43%  +0.076  6.21%       53    30%  -0.085  1.90%
AMZN        3.15       29    24%  -0.350  3.31%       36    22%  -0.859  1.05%
AUDUSD      6.44       16    50%  -0.048  1.51%       16    50%  -0.056  1.46%
BRENT       4.82       15    60%  +0.087  4.83%       22     9%  -1.240  0.96%
COPPER      3.93       29    45%  -0.204  2.67%       42    29%  -0.744  0.69%
EURUSD      6.90       14    29%  -0.265  1.18%       14    29%  -0.373  0.89%
GBPUSD      6.93       12    33%  -0.173  1.28%       12    33%  -0.285  0.77%
GER40       3.49       48    35%  -0.122  1.54%       59    37%  -0.263  0.52%
GOLD        5.36       54    48%  -0.073  2.29%       79    28%  -0.679  0.52%
GOOGL       3.14       37    41%  -0.184  3.46%       43    23%  -0.591  1.11%
IWM         3.05       25    32%  -0.299  2.30%       31    35%  -0.427  0.75%
META        3.11       38    53%  -0.085  3.95%       52    25%  -0.540  1.26%
MSFT        3.06       43    42%  -0.284  2.59%       52    31%  -0.622  0.84%
NAS100      3.16       55    60%  +0.132  1.91%       83    39%  -0.220  0.60%
NATGAS      6.06       15    40%  -0.362  8.54%       28    32%  -0.413  1.49%
NVDA        3.09       56    45%  +0.006  5.14%       79    37%  -0.109  1.63%
PLATINUM    2.92       27    52%  +0.500  3.01%       33    45%  +0.313  1.00%
QQQ         3.01       56    57%  +0.070  1.94%       80    38%  -0.280  0.65%
SILVER      3.82       36    39%  -0.126  3.72%       53    42%  -0.109  1.01%
SPX         3.08       73    30%  -0.194  1.42%       83    31%  -0.431  0.53%
SPY         2.95       73    33%  -0.214  1.47%       82    29%  -0.535  0.54%
TSLA        3.21       24    50%  +0.061  5.68%       31    35%  +0.027  1.76%
UK100       3.42       39    49%  -0.024  1.32%       52    38%  -0.220  0.51%
US30        3.07       50    42%  -0.161  1.39%       58    36%  -0.337  0.52%
USDCHF      5.90       16    38%  -0.144  1.19%       16    38%  -0.119  1.10%
USDJPY      6.34       48    58%  +0.059  1.16%       57    53%  +0.099  0.50%
WTI         5.68       16    56%  -0.099  5.46%       25    16%  -1.024  0.90%

daily_atr by entry year:  2021: n=72 win=54.2% E[R]=+0.085  2022: n=54 win=46.3% E[R]=-0.137  2023: n=221 win=43.0% E[R]=-0.094  2024: n=278 win=48.2% E[R]=-0.072  2025: n=254 win=35.8% E[R]=-0.179  2026: n=149 win=45.0% E[R]=-0.009

live_hourly_equiv by entry year:  2021: n=101 win=43.6% E[R]=-0.074  2022: n=73 win=34.2% E[R]=-0.432  2023: n=291 win=34.0% E[R]=-0.325  2024: n=350 win=38.0% E[R]=-0.260  2025: n=331 win=26.9% E[R]=-0.544  2026: n=188 win=31.9% E[R]=-0.407
```

### With shorts (`--years 5 --shorts`)

```
metric                                         daily_atr             live_hourly_equiv
--------------------------------------------------------------------------------------
trades                                              1322                          1745
win rate                                           44.9%                         33.8%
win rate 95% CI                           [42.3%, 47.6%]                [31.6%, 36.1%]
expectancy R                                      -0.074                        -0.325
expectancy R 95% CI                     [-0.115, -0.033]              [-0.392, -0.258]
expectancy % / trade                             -0.163%                       -0.274%
expectancy % 95% CI                   [-0.347%, +0.012%]            [-0.355%, -0.190%]
verdict (R, n>=60)                   negative_expectancy           negative_expectancy
avg stop distance                                  3.23%                         1.04%
avg win / loss R                           +0.60 / -0.62                 +1.45 / -1.23
profit factor (R)                                   0.78                          0.60
worst trade R                                      -3.29                         -7.10
exits stop/tp/time/eod                     223/32/1066/1                 978/384/382/1
gap-through exits                                     62                           148
avg hold hours                                      76.9                          36.1

symbol     r_sym  daily n   win%    E[R]  stop%   live n   win%    E[R]  stop%
------------------------------------------------------------------------------
AAPL        3.10       52    42%  -0.198  3.08%       69    35%  -0.364  1.00%
AMD         3.19       52    44%  +0.077  6.64%       75    31%  -0.093  2.06%
AMZN        3.15       40    32%  -0.214  4.49%       50    24%  -0.698  1.42%
AUDUSD      6.44       28    50%  -0.030  1.78%       28    50%  -0.028  1.48%
BRENT       4.82       25    60%  +0.228  4.74%       39    21%  -0.819  1.00%
COPPER      3.93       38    45%  -0.197  2.78%       57    32%  -0.611  0.75%
EURUSD      6.90       34    21%  -0.292  1.22%       34    21%  -0.387  0.91%
GBPUSD      6.93       22    41%  -0.002  1.50%       24    46%  +0.068  0.80%
GER40       3.49       60    33%  -0.161  1.89%       76    34%  -0.320  0.61%
GOLD        5.36       60    45%  -0.101  2.27%       88    27%  -0.682  0.52%
GOOGL       3.14       50    48%  -0.130  3.89%       60    27%  -0.490  1.26%
IWM         3.05       33    48%  -0.115  2.67%       43    35%  -0.399  0.92%
META        3.11       61    54%  -0.032  5.37%       81    32%  -0.304  1.66%
MSFT        3.06       59    42%  -0.274  3.06%       74    30%  -0.553  1.02%
NAS100      3.16       63    60%  +0.128  2.25%       97    37%  -0.220  0.73%
NATGAS      6.06       33    45%  -0.189 10.96%       61    31%  -0.395  1.91%
NVDA        3.09       66    50%  +0.053  5.74%       92    38%  -0.078  1.82%
PLATINUM    2.92       40    45%  +0.267  2.89%       53    49%  +0.300  0.96%
QQQ         3.01       64    58%  +0.073  2.28%       95    36%  -0.285  0.79%
SILVER      3.82       50    36%  -0.127  3.54%       73    40%  -0.134  0.95%
SPX         3.08       78    33%  -0.180  1.54%       91    31%  -0.424  0.58%
SPY         2.95       78    36%  -0.202  1.60%       91    29%  -0.543  0.61%
TSLA        3.21       36    47%  -0.016  7.03%       47    36%  +0.064  2.20%
UK100       3.42       42    52%  +0.002  1.36%       57    39%  -0.210  0.51%
US30        3.07       55    44%  -0.141  1.56%       65    38%  -0.231  0.58%
USDCHF      5.90       27    41%  -0.136  1.24%       27    41%  -0.125  1.14%
USDJPY      6.34       50    58%  +0.054  1.22%       59    53%  +0.077  0.50%
WTI         5.68       26    50%  -0.070  5.45%       39    13%  -1.052  0.94%

daily_atr by entry year:  2021: n=84 win=51.2% E[R]=+0.052  2022: n=224 win=54.0% E[R]=+0.010  2023: n=257 win=44.4% E[R]=-0.080  2024: n=300 win=46.7% E[R]=-0.090  2025: n=287 win=34.8% E[R]=-0.185  2026: n=170 win=44.7% E[R]=-0.026
daily_atr by side:  buy: n=1028 win=43.9% E[R]=-0.087  sell: n=294 win=48.6% E[R]=-0.031

live_hourly_equiv by entry year:  2021: n=114 win=43.9% E[R]=-0.068  2022: n=320 win=34.7% E[R]=-0.201  2023: n=343 win=33.8% E[R]=-0.350  2024: n=380 win=38.4% E[R]=-0.232  2025: n=371 win=26.1% E[R]=-0.565  2026: n=217 win=32.3% E[R]=-0.360
live_hourly_equiv by side:  buy: n=1334 win=33.7% E[R]=-0.361  sell: n=411 win=34.1% E[R]=-0.212
```

### Diagnostic: same signals, zero costs (NOT a candidate configuration)

(Original i.i.d. CIs. Superseded by the `zero` rows in the headline tables, which use week-clustered CIs: daily_atr longs [−0.055, +0.094], live longs [−0.153, +0.045].)

| run | mode | n | win (CI) | gross E[R] (CI) | net E[R] |
|---|---|---|---|---|---|
| longs | daily_atr | 1028 | 51.4% [48.3, 54.4] | +0.020 [−0.028, +0.070] | −0.087 |
| longs | live_hourly_equiv | 1334 | 36.6% [34.0, 39.2] | −0.053 [−0.126, +0.028] | −0.361 |
| shorts | daily_atr | 1322 | 51.6% [48.9, 54.3] | +0.024 [−0.017, +0.068] | −0.074 |
| shorts | live_hourly_equiv | 1745 | 36.7% [34.4, 39.0] | −0.041 [−0.100, +0.026] | −0.325 |

At the `max` scenario, costs alone are worth about 0.1R per trade with daily-ATR stops and about 0.3R with live-style stops. At `typical` they are 0.029R and 0.075R. For example, WTI's round trip (30 bps spread + 2×5 bps slippage = 40 bps) is about 0.44R against a 0.9% stop.

### Checking the stop-regime approximation

- **Live-mode stops match what live actually ran.** Average live_hourly_equiv stops come out close to the stops the brief measured on live: WTI 0.90% vs 0.97%, BRENT 0.96% vs 0.90%, GOLD 0.52% vs 0.52%, SPX 0.53% vs 0.50%. The approximation reproduces the right magnitude.
- **My r_sym is lower than the brief's point estimates.** It is a median over about 2 years of every hourly bar: WTI 5.68 (last 30 days: 5.34), BRENT 4.82, GOLD 5.36 (last 30 days: 6.22), SPX 3.08. The brief's 8.3x (WTI) and 7.9x (Brent) were single-moment measurements. A higher true ratio would mean even tighter live stops, so this approximation is, if anything, generous to live.
- **FX stops are mostly set by the 0.01 absolute floor in both modes.** Track-A/risk.py apply the 0.01 floor in Yahoo price units. For AUDUSD (~0.65) or USDCHF (~0.8) that alone is a 1.2–1.5% stop, which is why the FX rows barely differ between modes. This is live parity, not a backtest bug, but it is a quirk worth knowing.

## Limitations (also printed in the JSON `meta.disclosures`)

1. `live_hourly_equiv` is an approximation (daily_ATR14 / constant r_sym), not an hourly replay. Yahoo only has about 730 days of 60m history.
2. The backtest acts on the settled daily close at the next open. Live acts intraday on a still-forming daily bar, so live entries can differ (see the Task 6 note).
3. Costs are a scenario (`--costs`):
   - `max` charges `max_spread_bps`, live's refuse-to-trade cap, so it is an upper bound.
   - `typical` charges IG published spreads plus 1 bp slippage.
   - `zero` is gross.

   The verdict for daily-ATR stops depends on the scenario: negative at `max`, indeterminate at `typical`. The "no edge" conclusion does not depend on it: even gross, the CI straddles zero.
4. Daily bars hide the intrabar path. A bar that touches both levels is scored as a stop (pessimistic), and the time stop resolves at the next daily open.
   - Both-hit bars are rare with daily-ATR stops (1 trade) but not with live-style stops (58 of 1334 longs, 78 of 1745 with shorts).
   - The target-first bound is reported next to every result.
   - Under typical costs that bound erases the live regime's deficit (+0.003 against −0.128).
5. Futures are Yahoo continuous front-month series, so contract rolls look like price gaps. The NATGAS −3.29R trade (2026-01-27→29, 6.66→3.73) is very likely a roll artifact. The PLATINUM −7.10R trade (26→27 Jan 2026, a 12% overnight gap) has not been checked against a second source, so it could be real or a roll artifact. A handful of such trades cannot move ~1,000-trade means by much.
6. Symbols are simulated independently. There are no portfolio caps, no correlation cap, no IG minimum-stop or deal-size rejections and no cost-edge gate. Correlated symbols (SPX/SPY/US30, NAS100/QQQ, WTI/BRENT, mega-caps) mean the effective N is well below the raw 1028–1745: 220–247 distinct entry weeks. The verdict therefore uses the entry-week cluster bootstrap. The i.i.d. CI is shown alongside, labelled as too narrow.
7. Results depend on code-default settings. The live server's `.env` may differ, so re-run there with `python -m trading_bot.backtest --years 5 --costs typical` to confirm.
8. Swap is a flat 200 bps/yr on both sides. IG long funding is about benchmark + ~3%, so long financing is understated, which flatters the strategy. Short financing is overstated.
9. Survivorship: the universe is today's instrument list, chosen with hindsight. This flatters a trend-following backtest.

## Recommendation for the controller (not acted on)

The evidence says Track-A as specified has no detectable edge. Gross expectancy is about zero, and after realistic costs the designed strategy is break-even at best. There is no basis for trading it for profit.

Live's hourly-ATR stop regime is materially worse, mainly because costs are 2.6–2.9x heavier in R on its tight stops. Under typical costs it loses −0.128R [−0.227, −0.033], but that figure is subject to the 58 both-hit bars noted above.

Widening stops to daily ATR does not create an edge. It moves typical-cost E[R] from about −0.13R to about −0.01R. The 72h hold then turns the strategy into a three-day hold whose target is almost never reached. Any next step (a hold horizon matched to the daily signal, or a different signal) should be pre-registered and judged out-of-sample, not tuned on this same 5-year window.
