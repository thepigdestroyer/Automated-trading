# Task 4b report — Cash-risk-at-stop tradeability model

Branch: `feat/reliability-and-evidence-loop`
Commit: **`ae91764`** — `feat(universe): cash-risk-at-stop tradeability filter on built contexts`

## Headline

Shipped. **15/15 mutants killed** after fixing **2 real survivors** found by self-mutation.
Full suite `8 failed, 802 passed` — exactly the 8-failure macOS baseline (`730 passed` before),
**+72 new tests, zero regressions**.

**The purpose was achieved.** BRENT at £6,976 equity is now filtered whenever
`atr_14 > 1.4533` price-units (USD ≈ 1.46% of the $99.30 price). The old Task 4 formula never
filtered it at any realistic ATR. At the historically observed BRENT volatility
(task-4-report.md line 116: the production ATR stop was "roughly 12× the 0.5% floor" ⇒ ~596 IG
points ⇒ implied `atr_14 ≈ 3.97`) the forced cash risk is **£596 vs a £218 ceiling — a 2.7×
breach, comfortably filtered**. A genuinely quiet tape (`atr_14 ≤ 1.45`) still trades, which is
the point: BRENT took 4 of the 11 historical fills.

---

## What changed

Two files only (`git diff --stat`): `trading_bot/bot.py` (+165) and
`tests/test_tradeability_filter.py` (+820). Nothing else touched.

### 1. `trading_bot/bot.py` — `structural_cash_risk_breach(...)` (module level, beside `is_structurally_tradeable`)

Implemented verbatim from the brief's Step 3, plus two clarifying comment blocks (see
"Deliberate permissive bias" below). Returns `(is_breach, diagnostics)`.

```
stop_price_units = max(atr_14 * 1.5, price * 0.005, 0.01)     # == risk.py:354
stop_ig_points   = stop_price_units * instrument.ig_price_scale
risk_per_unit    = stop_ig_points * point_value_per_unit
target_cash_risk = equity * 0.025
budget_ceiling   = target_cash_risk * 1.25
raw_size         = target_cash_risk / risk_per_unit
forced_size      = max(raw_size, min_deal_size)
breach           = forced_size * risk_per_unit > budget_ceiling
```

Guard: any of `price`/`atr_14`/`equity`/`min_deal_size` that is `None`, non-finite or `<= 0`
returns `(False, {"reason": "insufficient_data"})`.

The `max(raw_size, min_deal_size)` gives the "no inflation ⇒ no breach" property for free, as
the brief required — when `raw_size` already exceeds the broker minimum, `forced_cash_risk`
equals `target_cash_risk` exactly, which is inside the ceiling by construction. No special case.

### 2. `trading_bot/bot.py` — `TradingBot._drop_unfillable_contexts(symbol_contexts, *, account_snapshot)`

Called at `bot.py:725`, immediately after `_collect_symbol_contexts` and before
`_build_guardrail_context` / `_invoke_news_synthesiser` / `_build_symbol_research` / `decide` —
so a dropped symbol costs **zero** LLM tokens in all four calls.

Behaviour, in order:
1. `tradeability_filter_enabled == False` → return input unchanged, no broker call.
2. `getattr(self.broker, "get_market_constraints", None)` not callable (paper broker) → return
   input unchanged.
3. Per symbol, inside `try/except Exception: keep`:
   - symbol in the open-position set (built with `.strip().upper()`) → keep, **without even
     consulting the broker**;
   - `constraints` falsy (`None`/`{}`) → keep;
   - otherwise evaluate `structural_cash_risk_breach` with `context.indicators.price` /
     `.atr_14`, `total_funds`, and `constraints["min_deal_size"]`;
   - breach → `LOGGER.info("Dropping %s before the LLM — STRUCTURAL_CASH_RISK: ...")` with the
     full diagnostics dict, and drop.
4. Rebuilds the dict in iteration order with the same objects — it can only ever REMOVE.

No extra IG API load: `ig_bridge.get_market_constraints` is cached on a 60-minute TTL and
`_select_active_symbols` already looked up the same symbols earlier in the same cycle, so every
call here is a cache hit. Open-position symbols are bypassed before the lookup in both places.

Task 4's `is_structurally_tradeable` and its `_select_active_symbols` wiring are **untouched**
and still run, as cheap defence-in-depth where no ATR exists yet.

---

## TDD evidence — the new tests failed first

Predicate tests were written and run before any implementation existed:

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -k cash_risk -v
tests/test_tradeability_filter.py::test_cash_risk_breach_mirrors_ig_bridge_formula FAILED
>       from trading_bot.bot import structural_cash_risk_breach
E       ImportError: cannot import name 'structural_cash_risk_breach' from 'trading_bot.bot'
====================== 1 failed, 132 deselected in 1.46s =======================
```

Whole new suite before implementation — **66 failed, 67 passed** (the 67 are Task 4's
pre-existing tests). Every failure reason verified:

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -q 2>&1 | grep -E "^E " | sort | uniq -c | sort -rn
  37 E  ImportError: cannot import name 'structural_cash_risk_breach' from 'trading_bot.bot'
  28 E  AttributeError: 'TradingBot' object has no attribute '_drop_unfillable_contexts'
   1 E  AssertionError: decide() saw a structurally unfillable symbol
```

That last one is the run_cycle wiring test failing for exactly the right reason: before this
change, an unfillable BRENT context really was handed to the LLM.

After the predicate (Step 4, brief's command):

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -k "cash_risk or inflate or non_finite" -v
====================== 28 passed, 105 deselected in 1.17s ======================
```

After the filter:

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -q
137 passed in 2.07s
```

### Two tests that passed pre-implementation (disclosed)

`test_run_cycle_reaches_the_llm_for_a_fillable_context` (the positive control) and
`test_run_cycle_keeps_a_held_unfillable_symbol_in_the_llm_payload` both assert that something is
*kept*, so they necessarily pass when no filter exists. They are not tautological: mutant M5
(open-position bypass removed) kills the second, and the first is what makes
`decide.assert_not_called()` in the negative test meaningful rather than vacuous.

---

## Mutation testing — 15 mutants, 15 killed, 2 survivors found and fixed

Method: mutate `trading_bot/bot.py`, run the **full** suite, count failures above the
8-failure baseline, restore from a checksum-verified copy. Every "killed" number below is
tradeability-file failures only.

| # | Mutation | Result | Killed by (count) |
|---|---|---|---|
| M1 | `forced_cash_risk > ceiling` → `<=` (invert) | **KILLED** | 17 tests |
| M1b | `>` → `>=` (boundary off-by-one) | **KILLED** | `test_breach_boundary_is_exact_and_inclusive` |
| M2 | drop `* instrument.ig_price_scale` | **KILLED** | 13 tests |
| M3 | `max(raw_size, min_deal_size)` → `min_deal_size` | **KILLED** | `test_diagnostics_report_the_budget_correct_size_when_it_dominates` |
| M4 | remove the non-finite / non-positive guard | **KILLED** | 31 tests |
| M5 | remove the open-position bypass | **KILLED** | 5 tests |
| M5b | `.strip().upper()` → `.upper()` in the bypass set | **KILLED** | 3 tests |
| M6 | delete the `_drop_unfillable_contexts` call from `run_cycle` | **KILLED** | `test_run_cycle_drops_the_unfillable_context_before_the_llm` |
| M7 | drop `* point_value` | **SURVIVED → fixed → KILLED** | `test_point_value_per_unit_is_applied` |
| M8 | drop both stop floors from `max()` | **SURVIVED → fixed → KILLED** | 3 tests |
| M8b | drop only `price * stop_loss_pct_floor` | **KILLED** | 2 tests |
| M8c | drop only the `0.01` absolute floor | **KILLED** | 2 tests |
| M9 | drop `* atr_stop_multiplier` | **KILLED** | 9 tests |
| M10 | remove `budget_ceiling` tolerance (`×1.25` → `×1`) | **KILLED** | 8 tests |
| M11 | remove the `tradeability_filter_enabled` check | **KILLED** | `test_filter_disabled_drops_nothing_and_makes_no_broker_call` |
| M12 | `except Exception:` → `raise` (fail-open removed) | **KILLED** | 3 tests |
| M13 | missing-accessor short-circuit removed | **KILLED** | `test_broker_without_accessor_drops_nothing` |
| M14 | `None` constraints treated as `min_deal_size=1.0` | **KILLED** | `test_accessor_returning_none_drops_nothing` |
| M15 | `total_funds` → `free_funds` | **KILLED** | `test_filter_budgets_from_total_funds_not_free_funds` |

### The two survivors, and the tautologies behind them

**M7 — `point_value` was completely untested.** `point_value_per_unit` defaults to `1.0`, so
deleting the multiplication changed nothing anywhere in the suite: `8 failed, 798 passed`,
identical to clean. This is the same class of bug the brief warned about for `ig_price_scale`,
one factor over. Fixed by adding `test_point_value_per_unit_is_applied`, which holds every
input constant and moves only the point value (1.0 → 2.0): the forced cash risk goes
180 → 360 and the verdict flips `False → True`. Also asserts the default is still `1.0`.

**M8 — the stop floors were untested.** In every test case `atr_14 * 1.5` dominated, so
reducing the sizer's three-way `max()` to just the ATR term survived: `8 failed, 799 passed`.
Fixed with two realistic floor-dominated cases:
- `test_percentage_stop_floor_is_applied` — GOLD $4,700 with a quiet ATR of 14.0:
  `14.0*1.5 = 21.0` but `4700*0.005 = 23.5`, so at `ig_price_scale=10` the stop is
  **235 IG pts, not 210** — the difference between breaching the 218 ceiling and not.
  The verdict, not just a diagnostic, flips.
- `test_absolute_stop_floor_is_applied` — EURUSD on a very quiet tape (ATR 0.005): the ATR term
  is 0.0075 and the percentage floor 0.005415, so the `0.01` absolute floor wins →
  **100 pips, not 75**.
Both were also added as rows to `test_predicate_agrees_with_the_ig_bridge_submit_time_guard`,
giving a second, independent kill.

### Prediction that held

I predicted before running M3 that the brief's `test_no_breach_when_min_deal_size_does_not_inflate`
could not kill it — because the breach *boolean* is provably identical under
`max(raw_size, min_deal_size)` and bare `min_deal_size` in all cases (when `raw_size` wins the
forced risk equals the target, always inside the ceiling; when `min_deal_size` wins the
expressions are the same). Only the diagnostics differ, and they are what gets logged. I added
`test_diagnostics_report_the_budget_correct_size_when_it_dominates` for exactly that, and M3
was killed by that test alone — confirming the brief's test would have shipped a survivor.

Likewise, `test_non_finite_or_nonpositive_inputs_never_breach[nan]` is tautological for M4:
with the guard deleted every `nan` path still falls out as "no breach" because all `nan`
comparisons are `False`. Only the `inf`/`0.0`/`-1.0` parametrisations fail. I added
`test_unusable_inputs_are_reported_as_insufficient_data` (24 cases, asserting
`diag["reason"] == "insufficient_data"`) so the guard itself is pinned, and the `nan` cases do
now kill M4 — visible in the M4 run: `..._never_breach[nan]` did **not** fail, but all six
`...insufficient_data[*-nan]` cases did.

---

## The BRENT number (the number that says whether this worked)

BRENT, £6,976 equity, `min_deal_size=1.0`, price 99.3, all defaults:

```
  atr_14  stop_pts  raw_size  cash_risk  ceiling  filtered
     1.0    150.00    1.1627     174.40   218.00  False   <- min does not inflate at all
     1.2    180.00    0.9689     180.00   218.00  False
     1.4    210.00    0.8305     210.00   218.00  False
    1.45    217.50    0.8018     217.50   218.00  False
  1.4533    217.99    0.8000     217.99   218.00  False   <- threshold
    1.46    219.00    0.7963     219.00   218.00  True
     1.5    225.00    0.7751     225.00   218.00  True
     2.0    300.00    0.5813     300.00   218.00  True
     2.5    375.00    0.4651     375.00   218.00  True
     3.0    450.00    0.3876     450.00   218.00  True
```

**Breach threshold: `atr_14 > 1.45333` price-units (USD), i.e. > 1.4636% of price.**
Closed form: `equity * 0.025 * 1.25 / (atr_stop_multiplier * ig_price_scale) = 218 / 150`.

Answering the question as asked — is BRENT filtered at a realistic ATR in the 1.0–3.0 range?
**Yes, over most of that range.** Filtered from 1.46 upward; kept below. Brent daily ATR(14) at
~$99 spot normally runs $1.50–$2.50, and the ATR actually observed in the production incident
implies ≈ $3.97 (596 IG pts vs a 218 ceiling = 2.7× over). So the motivating case is now caught,
with margin — whereas the Task 4 formula returns "tradeable" for it at *every* ATR (verified
directly: `is_structurally_tradeable(equity=6976, min_deal_size=1.0, price=9930) is True`).

Honest caveats:
- This is **not** a blanket BRENT ban and must not be read as one. On a quiet cycle
  (`atr_14 ≤ 1.45`) BRENT still reaches the LLM. That is deliberate: BRENT took 4 of the 11
  historical fills. `test_low_atr_cycle_keeps_the_same_symbol_a_high_atr_cycle_drops` pins that
  the decision is recomputed every cycle with no carried state.
- I could not find a recorded live `atr_14` for BRENT anywhere in the repo (no decision logs or
  event journals are checked in), so the ≈3.97 figure is **derived** from task-4-report.md's
  "roughly 12× the 0.5% floor", not read from a production record. The threshold itself
  (1.45333) is exact and machine-verified.
- The threshold scales with equity: at £20,000 the same £/pt maths keeps BRENT at ATR 3.0
  (`raw_size` 1.11 > min 1.0, no inflation). `test_higher_equity_rescues_the_same_atr` pins it.

---

## Deliberate permissive bias (documented in code, pinned by test)

The sizer budgets from `max(free_funds, 0) * risk_per_trade_pct_base * confidence_multiplier`
(`risk.py:330`, `risk.py:347`), not `total_funds * risk_per_trade_pct`. With current defaults
`risk_per_trade_pct == risk_per_trade_pct_base == 0.025` and
`risk_confidence_scaling_max == 1.0`, so the only live difference is `free_funds` vs
`total_funds`, and `free_funds <= total_funds`.

Using total equity therefore makes `target_cash_risk` and the ceiling as **large** as they can
legitimately be, i.e. the filter drops **fewer** symbols and is a strict subset of what the
broker layer would reject at submit time. That is the correct direction to err: a false drop
silently removes a real trade with no trace, whereas a missed drop is still caught by
`ig_bridge`. I kept the brief's `equity`/`risk_per_trade_pct` interface rather than unilaterally
tightening the filter, and pinned the choice with
`test_filter_budgets_from_total_funds_not_free_funds` (M15) plus a comment block at the
`target_cash_risk` line so it cannot drift silently.

---

## Test inventory (+72 tests, 65 → 137 in this file)

Predicate (37): brief's 3 verbatim (`mirrors_ig_bridge_formula`, `no_breach_when_min_deal_size_does_not_inflate`,
`non_finite_or_nonpositive_inputs_never_breach`) plus `unusable_inputs_are_reported_as_insufficient_data`
(24 cases), `diagnostics_report_the_budget_correct_size_when_it_dominates`,
`breach_boundary_is_exact_and_inclusive`, `ig_price_scale_is_applied_to_the_stop`,
`fx_scale_of_10000_is_applied`, `percentage_stop_floor_is_applied`,
`absolute_stop_floor_is_applied`, `point_value_per_unit_is_applied`,
`brent_breach_threshold_at_current_equity`, `higher_equity_rescues_the_same_atr`,
`predicate_agrees_with_the_ig_bridge_submit_time_guard` (8 instrument rows).

Filter (32): `unfillable_context_is_dropped`, `fillable_contexts_are_untouched`
(order + object identity), `open_position_context_is_never_dropped`,
`whitespace_padded_open_position_context_is_never_dropped` (3),
`broker_without_accessor_drops_nothing`, `accessor_returning_none_drops_nothing`,
`accessor_raising_drops_nothing`, `malformed_constraints_drop_nothing`,
`non_finite_min_deal_size_drops_nothing` (5), `corrupt_equity_drops_nothing` (4),
`corrupt_atr_drops_nothing` (4), `malformed_context_is_kept_and_does_not_abort_the_cycle`,
`filter_disabled_drops_nothing_and_makes_no_broker_call`,
`drop_is_logged_at_info_with_diagnostics`, `higher_equity_rescues_a_context`,
`filter_budgets_from_total_funds_not_free_funds`,
`low_atr_cycle_keeps_the_same_symbol_a_high_atr_cycle_drops`.

`run_cycle` wiring (3): `reaches_the_llm_for_a_fillable_context` (positive control),
`drops_the_unfillable_context_before_the_llm`,
`keeps_a_held_unfillable_symbol_in_the_llm_payload`.

All five hard safety properties are covered: (1) garbage never shrinks the universe — 33 cases
across predicate and filter; (2) open positions never dropped — 5 cases incl. through the real
`run_cycle`; (3) no accessor / `None` → nothing dropped; (4) flag off → nothing dropped, no
broker call; (5) malformed context kept, cycle survives.

---

## Full suite vs baseline

```
BEFORE (clean tree at 666d2c5): 8 failed, 730 passed in 6.21s
AFTER  (ae91764):               8 failed, 802 passed in 7.03s
```

Identical 8 failures, same names, all pre-existing macOS platform artifacts in
watcher/memory-pressure code (Linux-only `/proc/self/status`, `/bin/true`):
`test_events_rotation.py::test_watcher_rotates_events_jsonl`,
`::test_watcher_throttles_rotation`,
`test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status`,
`test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat`,
`::test_state_records_last_heartbeat_at`,
`::test_subsequent_tick_within_interval_does_not_emit`,
`::test_heartbeat_emits_again_after_interval`,
`::test_short_interval_emits_on_every_tick`.
Not touched, not fixed. **8 failures = success; a 9th would be a regression.**

Risk defaults re-verified unchanged after the edit: `risk_per_trade_pct=0.025`,
`min_model_confidence=0.72`, `degraded_inputs_min_confidence=0.85`,
`risk_confidence_scaling_max=1.0`, `atr_stop_multiplier=1.5`, `stop_loss_pct_floor=0.005`,
`risk_reward_ratio=2.0`, `risk_size_vs_budget_tolerance=0.25`, `dry_run=True`,
`point_value_per_unit=1.0`.

---

## Self-review findings

1. **`config.py` comment is now stale — left deliberately.** The `tradeability_filter_enabled`
   docstring (`config.py:186-189`) still says the filter "does NOT yet catch symbols that only
   breach under the wider ATR-based stop — see task-4-report.md". That is no longer true. The
   task scope is explicitly "`bot.py` + tests only", so I did not edit `config.py`. **Flagging
   for a one-line follow-up.** The test-module docstring, which *is* in scope, was updated with
   a "TASK 4b supersedes that formula" section explaining why the Task 4 model is retained.
2. **`is_structurally_tradeable` is now redundant but harmless.** It cannot remove anything
   4b would keep at typical values (it is ~350× looser), so it is pure defence-in-depth at
   selection time where no ATR exists. Left in place per instruction. It does still catch the
   large-index-CFD-on-a-small-account shape earlier and cheaper.
3. **No structured event is emitted on a drop, only a log line.** Task 4's selection-time filter
   behaves the same way. If the evidence loop wants to count pre-LLM drops, a `json_log.log(...)`
   call would be the natural addition — out of scope here, not done.
4. **Near-miss during mutation testing (process note).** One mutation edit used an 8-space
   indented `oldString` that also matched a 20-space indented identical line inside
   `_select_active_symbols`, corrupting that method (`IndentationError`). Caught immediately by
   the suite (10 collection errors), restored from the checksum-verified backup, redone with a
   unique anchor, and the clean tree re-verified at `8 failed, 802 passed` before committing.
   No trace in the commit; `git diff` was reviewed line by line afterwards.
5. **Error paths audited by inspection**, all landing on "keep": non-`str` dict key
   (`AttributeError`), non-dict truthy `constraints` (`TypeError`), missing `min_deal_size`
   (`KeyError`), unparseable `min_deal_size` (`ValueError`), `None` price/atr on the context
   (`TypeError`), raising broker, raising `indicators` property. `equity` is computed once as
   `float(total_funds or 0.0)`; `nan` is truthy so it propagates to the predicate and is caught
   by the guard as `insufficient_data`.
6. **`ig_price_scale` values read, not assumed:** BRENT 100, WTI 100, NATGAS 100, USDJPY 100,
   GOLD 10, EURUSD 10000. Tests assert the scale they depend on (`== 100.0`, `== 10000.0`,
   `== 10.0`) so an instruments-table change fails loudly instead of silently weakening the
   filter.
7. **Not sticky, not cached** — verified by test: the same context is dropped and then kept
   again on a later call with a quieter ATR, and kept at higher equity.

## Files

- `trading_bot/bot.py` — +165 (import, `structural_cash_risk_breach`,
  `_drop_unfillable_contexts`, one call site)
- `tests/test_tradeability_filter.py` — +820 (72 new tests, extended module docstring)

Not touched: `trading_bot/ig_bridge.py`, `trading_bot/session_manager.py`, `supervisor/**`,
`trading_bot/config.py`, `trading_bot/risk.py`.

---

# Fix pass (review findings)

Reviewer raised four Important (F1-F4) and one Minor (F5) against `ae91764`. All five fixed.
Full suite **`8 failed, 814 passed`** — the same 8 macOS platform artifacts, `802 -> 814`
(**+12 tests, zero regressions**). **8/8 mutants killed**, including two the brief did not ask for.

Three files: `trading_bot/bot.py`, `trading_bot/config.py` (comment only), and
`tests/test_tradeability_filter.py`. `ig_bridge.py`, `session_manager.py`, `supervisor/**` and
`risk.py` untouched (`git status` verified). No risk default VALUE changed — re-probed after the
edit: `risk_per_trade_pct=0.025`, `risk_per_trade_pct_base=0.025`,
`risk_confidence_scaling_max=1.0`, `atr_stop_multiplier=1.5`, `stop_loss_pct_floor=0.005`,
`risk_reward_ratio=2.0`, `risk_size_vs_budget_tolerance=0.25`, `min_model_confidence=0.72`,
`degraded_inputs_min_confidence=0.85`, `point_value_per_unit=1.0`. The `config.py` diff contains
**zero non-comment lines** (mechanically checked).

## F1 — Track-A keeps the unfiltered universe

`run_cycle` now retains `unfiltered_symbol_contexts = symbol_contexts` *before* the filter and
passes **that** to Track-A; the LLM path keeps the filtered mapping.
`_drop_unfillable_contexts` already built a fresh `kept` dict, so the original was never
destroyed — no change needed there.

The inline Track-A block was extracted to `TradingBot._run_track_a`, carrying the full WHY in its
docstring (paper book; `min_deal_size` does not exist there; narrowing biases the
mechanical-vs-LLM comparison *selectively*, since the drops land exactly on the volatile
instruments). The call site keeps a pointer to it.

**Extra defect found and fixed (not in the brief).** The `if not symbol_contexts: return []`
guard sits at `bot.py:764`, *above* the Track-A call. `ae91764` introduced a new way for that
guard to fire — every symbol dropped as unfillable — so on an all-breach cycle Track-A silently
lost a **whole cycle of signals**. That is the maximal form of the bias F1 exists to prevent, and
it lands preferentially on volatile days. Fixed by calling `_run_track_a` before that return.
Safe: `run_track_a_cycle` is a pure function of `(contexts, paper, settings)` — verified by
reading `track_a.py`, it touches no broker, no `account_snapshot`, no `runtime_state` — so it is
order-independent w.r.t. decide/execute. The other early return (`bot.py:680`, `stop_event`) is
pre-existing and untouched.

Tests: `test_track_a_gets_the_unfiltered_universe_while_the_llm_does_not` (BRENT unfillable, WTI
not: LLM sees `["WTI"]`, Track-A sees `["BRENT", "WTI"]`, and the WTI object is *identical*, not a
copy) and `test_track_a_still_runs_when_every_symbol_is_dropped` (asserts
`decide.assert_not_called()` first, so it cannot pass vacuously).

## F2 — structured drop event + self-consistent cycle record

The commit under review contained the *comment* describing this event but **not the call** — the
emit was genuinely absent. Added, inside the `if breach:` branch:

```python
self.json_log.log(
    "structural_cash_risk_drop",
    {"symbol": symbol, "min_deal_size": min_deal_size, "equity": equity, **diagnostics},
    cycle_id=getattr(self, "_current_cycle_id", None) or None,
)
```

`cycle_id` follows the defensive sibling convention used by the other helper-method emitters
(`bot.py:467`, `:603`, `:1321`, `:2200`, `:2228`) rather than the bare
`self._current_cycle_id` used by emitters inlined in `run_cycle`.

Wrapped in its own `try/except` **deliberately**: the emit sits inside the method's keep-on-error
handler, so an escaping exception would fall through to `kept[symbol] = context` and a *logging*
failure would silently **resurrect** a symbol the filter had already dropped. Mutant (e) below
proves this is load-bearing, not defensive noise.

Cycle record made self-consistent by adding an explicit `dropped_symbols` field to
`_write_cycle_log` (kept `active_symbols` as the *selection* record, which is what reconciles the
pair). The all-dropped `LOGGER.warning` now distinguishes a data outage from a filter that ate
the universe.

Tests: `test_drop_emits_a_structured_event_with_diagnostics` (symbol + all six diagnostics +
`cycle_id`), `test_drop_event_reaches_the_real_event_journal` (end-to-end through the real
`JsonEventLogger`, proving the payload actually serialises), `test_no_event_is_emitted_when_nothing_is_dropped`
(counts drops, not cycles), `test_a_broken_event_logger_must_not_resurrect_a_dropped_symbol`,
plus the two `dropped_symbols` cycle-log tests (one asserting it is empty on a clean cycle, so it
is not a constant).

## F3 — the permissiveness invariant is now self-enforcing

```python
budget_pct = max(settings.risk_per_trade_pct, settings.risk_per_trade_pct_base)
target_cash_risk = equity * budget_pct * max(1.0, settings.risk_confidence_scaling_max)
```

Verified the safety argument from source rather than assuming it: `risk.py:330` budgets from
`risk_per_trade_pct_base` (a *different* knob from `risk_per_trade_pct`), and
`confidence_multiplier` (`risk.py:22`) returns `1.0 + t*(max_mult - 1.0)` with `t` clamped to
`[0,1]`. So its true upper bound is `max_mult` when `max_mult > 1` and `1.0` when `max_mult < 1`
— i.e. exactly `max(1.0, max_mult)`. The clamp is precisely right in **both** directions, and the
lower clamp matters as much as the upper: without it a configured `0.5` would halve the ceiling
and start dropping fillable symbols.

Measured (`equity=6976`, BRENT, `atr_14=3.0`, `min_deal_size=1.0`):

| `risk_confidence_scaling_max` | target | ceiling | forced | breach |
|---|---|---|---|---|
| 1.0 (default) | 174.40 | 218.00 | 450.00 | **True** (dropped) |
| 2.5 | 436.00 | 545.00 | 450.00 | **False** (kept) |
| 0.5 | 174.40 | 218.00 | 450.00 | True (clamped to 1.0, not 87.20) |

Tests: `test_confidence_scaling_above_one_must_not_over_remove` (the brief's case — at 2.5 the
symbol the unpatched predicate dropped is now kept),
`test_confidence_scaling_below_one_is_clamped_to_one`,
`test_budget_pct_takes_the_larger_of_the_two_risk_knobs`.

## F4 — per-symbol instrument lookup pinned

Added `test_filter_looks_up_the_instrument_for_each_symbol` using GOLD (`ig_price_scale=10`),
whose verdict flips on the lookup. Measured, exactly as the brief predicted:

```
GOLD price=4700 atr_14=10.0 min_deal_size=0.1 equity=6976
  GOLD row  (scale 10):  stop 235.0 pts  raw 0.7421  forced 0.7421  cash 174.40 <= 218  -> KEPT
  BRENT row (scale 100): stop 2350. pts  raw 0.0742  forced 0.1000  cash 235.00 >  218  -> DROPPED
```

The test asserts the counterfactual directly (both predicate calls), then goes through the filter
with BRENT riding along as a **positive control** so "GOLD was kept" cannot be vacuous, and
asserts `GOLD.ig_price_scale == 10.0` so an instruments-table change fails loudly.

## F5 — `config.py` comment rewritten

The stale "does NOT yet catch symbols that only breach under the wider ATR-based stop" is gone.
Replaced with a description of the flag as the master switch for **both** filters, contrasting
filter 1 (selection time, ATR-blind, ~350x looser, defence-in-depth) with filter 2 (built-context
time, real ATR stop, mirrors `ig_bridge` `STRUCTURAL_MIN_DEAL_SIZE_BREACH` — string verified at
`ig_bridge.py:609`).

**Self-correction during this pass.** My first draft of that comment claimed "Both filters narrow
the LLM path only, never Track-A's paper book". Checking the wiring showed that is **false** for
filter 1: `is_structurally_tradeable` (`bot.py:1247`) runs inside `_select_active_symbols`, whose
`active_symbols` feeds `_collect_symbol_contexts` and hence the pre-filter mapping Track-A
receives. So filter 1 narrows *both* tracks. The comment now says so explicitly, and notes this
is pre-existing and far less biasing (ATR-blind and ~350x looser, so it only removes the
permanently unfillable). **Residual, not fixed:** Track-A's universe is therefore still not
strictly every configured symbol. Fixing it would require restructuring `_select_active_symbols`,
and Task 4's selection-time filter is explicitly out of scope for this pass.

## Mutation testing — 8 applied, 8 killed

Harness asserted each anchor matched **exactly once** before mutating (the near-miss in the
previous pass came from an ambiguous anchor) and restored from a SHA-256-verified copy after
every run; final tree checksum re-confirmed as clean.

| # | Mutation | Result | Killed by |
|---|---|---|---|
| a | `get_instrument(symbol)` -> `get_instrument("BRENT")` | **KILLED** | `test_filter_looks_up_the_instrument_for_each_symbol` |
| b2 | FILTERED contexts -> Track-A (main call site) | **KILLED** | `test_track_a_gets_the_unfiltered_universe_while_the_llm_does_not` |
| b | FILTERED contexts -> Track-A (all-dropped early return) | **KILLED** | `test_track_a_still_runs_when_every_symbol_is_dropped` |
| c | delete the `json_log` event | **KILLED** | `..._structured_event_with_diagnostics`, `..._reaches_the_real_event_journal` |
| d | revert `max(1.0, risk_confidence_scaling_max)` | **KILLED** | `test_confidence_scaling_below_one_is_clamped_to_one` |
| d2 | revert the whole F3 line to the pre-F3 `equity * risk_per_trade_pct` | **KILLED** | `..._above_one_must_not_over_remove`, `..._larger_of_the_two_risk_knobs` |
| d3 | drop only the `budget_pct` `max()` | **KILLED** | `test_budget_pct_takes_the_larger_of_the_two_risk_knobs` |
| e | remove the nested `try/except` around the emit | **KILLED** | `test_a_broken_event_logger_must_not_resurrect_a_dropped_symbol` |

Two notes on rigour:

- **(d) alone is a weak mutant, so I added (d2) and (d3).** Reverting only `max(1.0, x)` cannot
  change the `2.5` case at all (`max(1.0, 2.5) == 2.5`), so the brief's headline F3 test does not
  kill it — only the `<1.0` clamp test does. (d2) is the *true* "unpatched predicate" from before
  this fix, and it is what the brief's F3 test actually kills. Reporting (d) as killed without
  (d2) would have overstated the evidence.
- **(e) matters because that test was vacuous before this pass.**
  `test_a_broken_event_logger_must_not_resurrect_a_dropped_symbol` passed while no emit existed at
  all. Under (e) the captured log shows the precise hazard —
  `Cash-risk tradeability check failed for BRENT (keeping symbol): disk full` — an unfillable
  symbol resurrected by a *logging* failure.

A regression was also caught by the existing suite during this pass, not by inspection:
`test_run_cycle_drops_the_unfillable_context_before_the_llm` began failing with
`AttributeError: 'TradingBot' object has no attribute 'track_a_pnl'` once the early return
started calling `_run_track_a`. `TradingBot.__init__` (`bot.py:347`) always sets that attribute,
so this was test-scaffolding drift (the helper builds via `__new__` and set only `paper_pnl`).
Fixed in the `_make_cycle_bot` helper rather than by adding a `getattr` fallback to production
code, which would have masked a genuinely missing attribute and silently disabled Track-A if the
field were ever renamed.

## Test commands (real output)

```
$ .venv/bin/pytest tests/test_tradeability_filter.py tests/test_track_a.py -v
============================= 163 passed in 1.26s ==============================

$ .venv/bin/pytest -q
8 failed, 814 passed in 4.75s
```

The 8 are the identical pre-existing macOS platform artifacts listed above
(`test_events_rotation.py` x2, `test_memory_pressure.py` x1,
`test_watcher_heartbeat_journal.py` x5). Not touched, not fixed.

## Scope

The financial model, the filter's placement, and Task 4's `is_structurally_tradeable` are
unchanged. The filter still only ever REMOVES from the LLM path: `_drop_unfillable_contexts`
builds `kept` purely by copying entries from its input, and `dropped_symbols` is derived as
`unfiltered - filtered`.
