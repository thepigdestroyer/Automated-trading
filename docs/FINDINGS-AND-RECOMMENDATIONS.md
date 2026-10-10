# Findings and recommendations (October 2026)

A consolidated write-up of the July–October 2026 reliability and evidence work: what was found,
what was fixed, what the evidence says about the strategy, and what to do next. Detailed records
live in the documents indexed at the end.

## Bottom line

1. **The bot's trading strategy has no detectable edge.** A 5-year, 28-instrument backtest of the
   mechanical strategy breaks even before costs and is break-even-to-losing after them. A second,
   pre-registered test of a standard trend-following design also failed.
2. **The live configuration was worse than the strategy as designed**, because stops were sized
   from hourly volatility on a daily signal. This fits the live record of 11 losses out of 11, all
   stopped out, and −30% on the demo account.
3. **The engineering is now considerably more robust.** Six data-integrity, safety and reliability
   defects were fixed, news coverage was broadened, and the test suite grew from 596 to 1,012
   passing tests.
4. **Recommendation:** keep the bot in paper mode and do not fund this strategy. Spend future effort
   on a genuinely different trading hypothesis, tested the same disciplined way.

## 1. Evidence about the strategy

### 1.1 Backtest of the mechanical strategy (Track-A)

Daily Donchian breakout in the direction of a strong daily trend, 5 years x 28 symbols, longs,
expectancy per trade in R (multiples of initial risk), 95% confidence intervals clustered by entry
week (same-day entries on correlated indices are not independent):

| Stop regime | Before costs | Realistic IG costs | Worst-case costs |
|---|---|---|---|
| Daily-ATR stops (as designed) | +0.020R [−0.055, +0.094] | −0.009R [−0.085, +0.065] | −0.087R [−0.162, −0.012] |
| Hourly-ATR stops (as live runs) | −0.053R [−0.153, +0.045] | −0.128R [−0.227, −0.033] | −0.361R [−0.461, −0.265] |

- Gross expectancy is about zero. No year and no asset class has a gross confidence interval above
  zero.
- The real-world picture is probably slightly worse than shown: today's instrument list includes
  hindsight winners (survivorship bias), and the flat 2%/year funding assumption understates IG's
  long funding cost.
- **The result is trustworthy.** An independent reviewer rewrote the backtester as plain loops with
  no shared code and reproduced all four headline numbers exactly, matched 8 symbols trade by trade,
  and hand-checked individual trades from raw prices.

### 1.2 Pre-registered test of a canonical trend-following exit

The design, data and pass/fail rule were committed and pushed **before** any code was written
(`docs/research/2026-10-06-preregistration-trend-exit.md`). The test kept the same entry and
replaced the exit with the published Turtle System 1 rules: initial stop at 2x daily ATR, exit on
a 10-day opposite channel breakout, no profit target, no time cap.

**Result: FAIL** on both pre-registered conditions under realistic costs.

- Full period: +0.057R, but the CI [−0.118, +0.234] includes zero (518 trades).
- Most recent 2 years: −0.147R.
- The positive full-period average comes almost entirely from a few trades (PLATINUM +13.5R and
  NVDA +13.2R of a +29.6R total). NVDA is a hindsight winner of exactly the kind survivorship bias
  favours.
- Performance decays steadily: positive 2021–2023, about zero in 2024, negative in 2025 and 2026.

As committed in the pre-registration, no further parameter variants were tried on this data:
searching many settings on the same history would find one that looks good by chance.

## 2. Root-cause observations

### 2.1 Stops were sized on the wrong timeframe

The entry signal is a *daily* breakout, but `atr_14` is computed from the *hourly* price frame
(`price_interval="60m"`), and both the live risk manager and Track-A size stops from it. Measured on
live data:

| Instrument | Daily ATR ÷ hourly ATR | Live stop distance | Daily-ATR stop distance |
|---|---|---|---|
| WTI crude | 8.3x | 0.97% | 7.97% |
| Brent crude | 7.9x | 0.90% | 7.09% |
| Gold | 5.7x | 0.52% | 2.96% |
| S&P 500 | 3.3x | 0.50% | 1.41% |

WTI moves about 5% on an ordinary day, so a 0.97% stop on a daily signal is hit by routine
intraday noise. Widening stops is not a free fix: it shrinks position size ~8x and pushes a 2:1
target out of reach.

### 2.2 The exit structure removes trend-following's source of return

With daily-ATR stops, 80% of exits were the 72-hour time stop and only 2.3% reached the fixed 2R
target. In practice the strategy was "hold for 3 days after a breakout". Trend-following earns from
a few large, long-held winners, and a 72h cap plus a fixed 2R target removes exactly that tail.
Section 1.2 tested the obvious remedy, and it did not create an edge.

### 2.3 Costs decide marginal strategies

The gap between gross and net results is mostly slippage (5 bps per leg) and spread, not funding.
The backtest's worst-case scenario uses each instrument's `max_spread_bps`, which is the level at
which the live bot *refuses* to trade, so it is an upper bound. The repo has no logs of actual IG
spreads or fills, so real costs are estimated from IG's published minimums.

### 2.4 Live signals use a still-forming daily bar

`yf.download(interval="1d")` returns today's unfinished bar, so the live trend verdict and breakout
rest on a partial close: an intraday spike can register as a breakout and then reverse by the
close. The backtest uses settled closes and enters at the next open. Any future daily strategy
should act only on completed bars.

## 3. Engineering fixes made

| Area | Problem | Fix |
|---|---|---|
| Price data | Outlier bars were **deleted**, breaking the time index and silently corrupting every rolling indicator | Corrupt ticks are repaired in place. Real moves that hold (e.g. earnings gaps) are left intact. A `DataQuality` report is attached to every symbol. |
| Degraded inputs | A symbol with repaired or missing data was traded at full confidence | New entries need 0.85 confidence (vs 0.72) when recent data is degraded. Exits are never gated. A config validator stops the threshold being set below the normal floor. |
| Memory | Market-calendar data used ~94 MB, driving regular memory restarts on a 400 MB-capped VM | Bounded to a rolling window: **93.7 MB → 2.6 MB** peak (measured). Lookups fail open. |
| Unfillable trades | Instruments whose broker minimum size breached the risk budget (e.g. BRENT) were only rejected at submit time, after costing LLM tokens | A pre-LLM filter mirrors the broker's own cash-risk-at-stop check. It is dynamic per cycle, so BRENT is dropped only when that cycle's volatility makes it unfillable. |
| LLM failure | A failed or unparseable `decide()` produced **no plans at all**, including no exits | An LLM-free exit-only fallback still closes positions past their max holding time or trailing exit, never opens anything, and isolates each close from the others' errors. |
| LLM output | JSON is scraped from the model's reasoning channel | Optional NIM guided-JSON decoding behind a flag that defaults **off**, plus a fair probe script to decide whether to enable it. |
| News | One source (Google News RSS), 15–30 minutes stale, a single point of failure | Provider abstraction with per-feed failure isolation. Added **FinancialJuice** (real-time macro wire; newest items minutes old) and **Federal Reserve press releases**. Real-time items win on deduplication, and no single feed can take all prompt slots. |
| Security | A news feed body shaped like `file://...` was **read from local disk** by the RSS parser | Feed bodies are now passed as bytes, so URL-shaped content is never fetched. |
| Evaluation | The LLM-vs-mechanical comparison used different symbol sets in paper mode | Restricted to the symbols both tracks could trade, with both figures reported. |

Every change went through implementation, independent review, and **mutation testing** (deliberately
breaking the code to confirm a test fails). That process caught six defects in the original plan
itself before they shipped, including one that would have stranded open positions on a degraded
data feed, and a filter formula ~350x too loose to ever catch its motivating case.

## 4. Recommendations

### 4.1 Strategy (highest priority)

1. **Do not fund this strategy.** Keep `DRY_RUN=true`. Neither the strategy as designed nor the
   canonical trend-following variant shows an edge.
2. **Do not search parameters on this history.** With enough variants, one will look profitable by
   chance and then fail live.
3. **Test a genuinely different hypothesis, the same way:** write the design and pass/fail rule
   first, then judge it on recent data it was not built from. The backtester
   (`trading_bot/backtest.py`) supports cost scenarios, week-clustered CIs and both stop regimes.
4. **Keep any future strategy internally consistent:** size stops and holding periods on the same
   timeframe as the entry signal, and act only on completed bars.
5. **Measure real costs.** Log actual IG spreads and fill slippage in paper mode. For a marginal
   strategy, real costs decide the verdict.
6. **Treat the LLM's value as unproven.** The forward-walk comparison of the LLM track against the
   mechanical baseline is now like-for-like; let it accumulate before relying on the LLM.

### 4.2 Before the next server deploy

1. On the server, run `grep -E 'MIN_MODEL_CONFIDENCE|DEGRADED_INPUTS' .env`. A new validator stops
   the bot starting if `MIN_MODEL_CONFIDENCE` exceeds `DEGRADED_INPUTS_MIN_CONFIDENCE` (0.85), and
   systemd would then restart it in a loop.
2. Confirm the server can reach `financialjuice.com` and `federalreserve.gov` (both enabled by
   default; each fails gracefully if unreachable).
3. Do not run `pytest` inside the live bot directory: the test setup can read the live `.env` and
   write to live log paths.

### 4.3 Security

1. **Rotate every API key** shared during the original session (NVIDIA, Finnhub, FRED, EIA,
   OpenRouter, IG). They were visible on a public session-share page. None is in this repository's
   history; that was checked across all commits.
2. Restrict SSH on the server to known IP addresses and keep key-only authentication.

### 4.4 Open follow-ups (not blocking)

- Failures *early* in a cycle (VIX, economic calendar, account snapshot) still skip exit
  management for that cycle. Fix this before any live trading.
- The degraded-data confidence floor is not shown to the LLM, so it proposes entries the executor
  then discards. Safe, but wastes tokens.
- `FX` is not a recognised exchange-calendar name, so FX sessions always count as open (unchanged
  from before).
- Federal Reserve items are usually older than the 12-hour prompt window, so that feed contributes
  little except right after a release (a `news_provider_stale` event makes this visible).
- FinancialJuice returned HTTP 429 on rapid re-fetches during testing. At a 15-minute poll this is
  unlikely; a short cache would remove the risk.

## 5. Where everything is

| Document | Contents |
|---|---|
| `docs/superpowers/plans/2026-07-28-reliability-and-evidence-loop.md` | The implementation plan, with amendments recorded as work progressed |
| `docs/superpowers/reports/2026-07-28-reliability-and-evidence-loop/` | Task-by-task progress ledger and implementer/review reports, including the full backtest tables (`task-7-report.md`) |
| `docs/research/2026-10-06-preregistration-trend-exit.md` | Pre-registration, committed before implementation |
| `docs/research/2026-10-06-result-trend-exit.md` | Result of the pre-registered test |
| `docs/decisions/0006`, `0007` | Design decisions: repairing rather than deleting bad price bars; one trend implementation shared by live and backtest |
| `trading_bot/backtest.py` | The backtester. It makes no LLM calls, but settings validation needs a placeholder key: `LLM_PROVIDER=openrouter OPENROUTER_API_KEY=unused python -m trading_bot.backtest --help` |
