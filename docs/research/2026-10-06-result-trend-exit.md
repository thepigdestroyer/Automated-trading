**FAIL.** Both pre-registered conditions are missed under `typical` costs: (1) the full-period week-clustered 95% CI lower bound of net E[R] is **−0.118R** (needed > 0); (2) net E[R] on the most recent 2 years of entries is **−0.147R** (needed > 0).

# Result: canonical trend-following exit on the Track-A entry

Pre-registration: `docs/research/2026-10-06-preregistration-trend-exit.md` (commit `3120e9f`, pushed before
any implementation). Implementation: commit `6cef185` (`exit_mode="channel_trail"`, CLI `--exit-mode`).
Run once on 2026-10-06, exactly as registered. No parameter was changed or tried.

## Verdict against the pre-registered criterion

| Condition (`--costs typical`) | Result | Threshold | Met |
|---|---|---|---|
| 1. Full period: lower bound of the week-clustered 95% CI of net E[R] | **−0.118R**. E[R] +0.057R, CI [−0.118, +0.234], n = 518 trades in 201 entry weeks | > 0 | No |
| 2. Most recent 2 years of entries (entry date ≥ 2026-10-05 − 730 days = 2024-10-05): net E[R] point estimate | **−0.147R**. n = 225, week CI [−0.404, +0.150] | > 0 | No |

**H1 fails.** The full-period point estimate is positive, but its CI straddles zero, and the most
recent two years are negative under every cost scenario. Under the commitments, no other parameter
values will be tried on this data.

## Headline numbers (28 symbols, longs and shorts, daily-ATR stop, 5 years)

Trade generation is the same in all three scenarios: the same 518 trades with the same exits and
gross R (checked row by row). Only costs differ.

| Costs | n (weeks) | Win rate [Wilson 95%] | **Net E[R] [week CI]** | Net E[R] i.i.d. CI (too narrow) | Recent 2y net E[R] [week CI] | Net % / trade [week CI] | Profit factor (R) | Avg win / loss R | Worst R |
|---|---|---|---|---|---|---|---|---|---|
| **typical** (primary) | 518 (201) | 32.8% [28.9, 37.0] | **+0.057 [−0.118, +0.234]** | [−0.091, +0.222] | −0.147 [−0.404, +0.150] | +0.317% [−0.582, +1.306] | 1.10 | +1.94 / −0.86 | −3.68 |
| max (upper bound) | 518 (201) | 31.9% [28.0, 36.0] | **+0.006 [−0.168, +0.185]** | [−0.141, +0.173] | −0.200 [−0.456, +0.098] | +0.137% [−0.762, +1.126] | 1.01 | +1.95 / −0.90 | −3.72 |
| zero (gross) | 518 (201) | 34.7% [30.8, 39.0] | **+0.128 [−0.050, +0.309]** | [−0.024, +0.297] | −0.083 [−0.343, +0.218] | +0.529% [−0.370, +1.533] | 1.23 | +1.95 / −0.84 | −3.60 |

Every scenario's verdict on the week-clustered CI is `indeterminate`. Even with zero costs, the
CI includes zero.

## Exits and holding period (identical across cost scenarios)

| Exit reason | Count | Share | Mean net R (typical) | Avg hold (calendar days) |
|---|---|---|---|---|
| channel (10-bar opposite breakout, next open) | 295 | 57.0% | +0.90 | 42.5 |
| stop (initial 2x ATR stop) | 217 | 41.9% | −1.09 | 11.3 |
| ↳ of which gap-through at the open | 41 | 7.9% | | |
| end_of_data (still open on 2026-10-05, marked at the last close) | 6 | 1.2% | +0.26 | 14.3 |
| target / time | 0 / 0 | — | — | — |

- Holding period: mean **29.1 days**, median 21, max 178. Task 7's exit was 80% time stops at about 3 days.
- Average initial stop distance: 4.36% of the entry price.
- The stop-and-target count is 0 by construction. The channel exit is decided on a bar's close and
  filled at the next open, so it can never share a bar with an intrabar stop. A bar that touches the
  stop is booked as a stop, even if its close also breaks the channel.

## By entry year (net E[R]; n)

| Year | n | typical | max | zero |
|---|---|---|---|---|
| 2021 (Oct–Dec) | 30 | +0.377 | +0.319 | +0.471 |
| 2022 | 85 | +0.163 | +0.124 | +0.226 |
| 2023 | 93 | +0.268 | +0.217 | +0.347 |
| 2024 | 119 | +0.002 | −0.053 | +0.072 |
| 2025 | 127 | −0.086 | −0.139 | −0.018 |
| 2026 (to 5 Oct) | 64 | −0.152 | −0.203 | −0.086 |

Expectancy is positive in 2021–2023, about zero in 2024, and negative after that. 2025 and 2026 to
date are negative in every scenario, even gross of costs.

## By asset class (net E[R]; n)

| Class | n | typical | max | zero | Sum of net R (typical) |
|---|---|---|---|---|---|
| equity (single shares) | 157 | +0.316 | +0.280 | +0.354 | +49.7 |
| metal | 74 | +0.106 | +0.047 | +0.178 | +7.9 |
| index | 103 | +0.079 | +0.023 | +0.165 | +8.1 |
| etf | 65 | +0.069 | +0.003 | +0.138 | +4.5 |
| forex | 73 | −0.329 | −0.386 | −0.180 | −24.0 |
| energy | 46 | −0.357 | −0.406 | −0.328 | −16.4 |

By side (typical): longs n = 373, +0.226R; shorts n = 145, −0.378R.

These splits are **descriptive and post hoc**. They were not pre-registered and are not hypotheses.
Choosing "longs only" or "equities only" on the strength of this table would be exactly the
data-mined variant that the pre-registration rules out.

## Is the result driven by one or two trades? Yes, the full-period point estimate is.

Under typical costs the 518 trades sum to **+29.6R**. The five largest:

| Symbol | Side | Entry → exit | Exit | Net R |
|---|---|---|---|---|
| PLATINUM | buy | 2025-05-12 → 2025-07-31 | channel | +13.46 |
| NVDA | buy | 2024-01-09 → 2024-04-05 | channel | +13.20 |
| USDJPY | buy | 2022-03-14 → 2022-08-03 | channel | +9.39 |
| AMD | buy | 2026-04-16 → 2026-07-29 | channel | +9.09 |
| PLATINUM | buy | 2025-12-10 → 2026-02-02 | channel | +8.63 |

- The single best trade (PLATINUM, +13.46R) is **45%** of the total net R. Under `max` costs it is 4.1x
  the total, because the other 517 trades sum to a loss.
- Net E[R] (typical) drops to +0.031 without the top trade, to **+0.006 without the top two**, and to
  −0.047 without the top five.
- The 14 trades above +5R sum to +110.1R. The other 504 sum to −80.5R. The median trade is −0.62R.

Positive skew is how trend-following is supposed to earn its return, so this is not a defect in
itself. It does mean that the positive full-period point estimate rests on a handful of outsized
winners, mostly in hindsight-selected names (NVDA, AMD; see the survivorship caveat), and that none
of it shows up in the most recent two years.

## Context against Task 7 (same entries, Track-A exit)

Task 7 with shorts (daily-ATR stop, typical costs) gave −0.004R [−0.061, +0.059] over 1322 trades;
gross was +0.024R [−0.033, +0.087]. Letting winners run raises the gross point estimate to +0.128R,
but it also cuts the number of trades by 61% and fattens the tails, so the CI widens to
[−0.050, +0.309]. **R is not like-for-like:** 1R here is a 2.0x ATR stop, against 1.5x ATR in Task 7.

## Verification

- **Tests:** `tests/test_backtest.py` has 66 passing tests (20 new). The full suite gives 1012 passed and 8
  failed. The 8 failures are the pre-existing macOS ones (events rotation, memory pressure, watcher
  heartbeat), unchanged from the 992/8 baseline. Unit tests make no network calls.
- **Mutation testing** (each mutant applied to `backtest.py`, then `tests/test_backtest.py` run):
  - (a) the channel includes bar t: **KILLED**, 4 tests failed;
  - (b) the exit fills at bar t's close: **KILLED**, 4 tests failed;
  - (c) the 72h time cap is left active: **KILLED**, 9 tests failed;
  - (d) a 1.5 stop multiple: **KILLED**, 7 tests failed.
  - Two extra checks on the default mode were also KILLED: the default mode given a 2.0 multiple
    (19 tests failed), and the default time stop disabled (10 tests failed).
- **Default mode unchanged:** the default `exit_mode="track_a"` was checked on the Task 7 cached data
  with the pre-change code (HEAD `3120e9f`) against the new code. All 12 runs gave byte-identical
  trade lists and report JSON, apart from the new `exit_mode` label key. The runs were longs and
  shorts, × both stop modes, × `max`/`typical`/`zero`.
  - The default-mode trades are also pinned in the test suite by digests taken from the pre-change code.
  - A CLI run on 2026-10-06 in the default mode (`--shorts --costs max`) reproduced Task 7 exactly:
    1322 trades, −0.0742R [−0.133, −0.011].
- **Same universe and window:** the run used the same Yahoo downloads as Task 7, from its
  `--cache`, fetched 2026-10-06 11:36Z.
  - All 28 symbols loaded, with no failures and no skips. The last settled bar is 2026-10-05 for every symbol.
  - This run's window start was 2021-10-06 07:34Z, against Task 7's 05:37Z. Daily bars are stamped at
    midnight, so both windows evaluate the same bars, which the exact reproduction above confirms.
- The evaluation script re-derives the full-period E[R] and week CI from the trade CSV and asserts that
  they equal the CLI's own numbers exactly.

## Where the spec was ambiguous (implemented literally; flagged, not "fixed")

1. **"Last data date"** for the 2-year window: taken as the latest settled bar in the universe,
   2026-10-05. It is the same date for all 28 symbols, so the choice does not matter here. Entries
   dated on or after 2024-10-05 are counted.
2. **Channel exit on the entry bar:** the rule applies from the entry bar's own close onward. The
   prior-10 window can include bars from before the entry. This is literal, and standard Turtle practice.
3. **Two exits at the same open:** if a pending channel exit and a gap through the stop fall on the same
   open, the trade is booked as a gap stop at that open. Both orders fill at the same price, so P&L
   is unaffected; only the label changes.
4. **A channel break on the last bar:** there is no next open, so the position is marked at the last
   close (`end_of_data`), the existing convention. 6 trades were still open at the end.
5. **"Canonical Turtle System 1":** the registration fixes elements that differ from the full Turtle
   system. This is a test of the Track-A entry plus Turtle-style exits, as the registration states,
   not of the Turtle system. The differences:
   - ATR14 here is a 14-bar simple mean, against Turtle N, a 20-day Wilder average;
   - no unit sizing or pyramiding;
   - no "skip the breakout after a winning breakout" filter;
   - the entry is Track-A's filtered breakout (trend filter, strength gate, 0.1% tolerance), not a
     raw 20-day breakout.

## Caveats that bias this result upwards

All of these flatter the result, so the FAIL holds up against them.

- **Long funding is understated.** Swap is a flat 2%/yr, and IG long funding is roughly benchmark + ~3%.
  With 29-day average holds this weighs far more than under Task 7's 3-day holds, and all of the
  positive result comes from longs.
- **Survivorship.** Today's universe includes hindsight winners: NVDA, AMD and META are among the top
  contributors.
- **Futures are Yahoo continuous front-month series.** Roll jumps are more likely inside 1–6 month
  holds. The largest trade, PLATINUM (PL=F), was not checked for roll artefacts.
- **No portfolio limits.** There are no position caps, no correlation cap and no IG dealing rules.

## Reproduce

```
LLM_PROVIDER=openrouter OPENROUTER_API_KEY=unused-backtest \
  .venv/bin/python -m trading_bot.backtest --years 5 --shorts --stop-mode daily_atr \
  --exit-mode channel_trail --costs typical --cache <tmp>/bt_cache \
  --trades-out .superpowers/sdd/trend-exit-trades-typical.csv > .superpowers/sdd/trend-exit-5y-typical.json
# repeat with --costs max and --costs zero, then:
.venv/bin/python scripts/eval_trend_exit_prereg.py .superpowers/sdd
```

The raw outputs (`trend-exit-5y-{typical,max,zero}.{json,txt}`, `trend-exit-trades-*.csv` and
`trend-exit-eval.json`) are in `.superpowers/sdd/`, which is gitignored, like Task 7's.
