# Task 2 report — `degraded_inputs` gate

**Branch:** `feat/reliability-and-evidence-loop`
**Commit:** `aa21eb0bd807ef4e95d1f4f74e47118f79fdd491` — `feat(risk): degraded-inputs confidence gate for new entries`
**Status:** DONE_WITH_CONCERNS (one deliberate, tested deviation from the brief's insertion point — see §5)

---

## 1. What changed

| File | Change |
|---|---|
| `trading_bot/config.py` | Added `degraded_inputs_min_confidence: float = 0.85` directly below `min_model_confidence: float = 0.72`, with the brief's comment verbatim. No existing default touched. |
| `trading_bot/risk.py` | Added `degraded_input_reasons(quality_by_symbol)` below `_round_trip_cost_price_units` (brief's code verbatim). Added `from typing import Mapping` (matches `track_a.py:29`) and `DataQuality` to the existing runtime `trading_bot.models` import. Added the gate to `build_execution_plans`. |
| `trading_bot/bot.py` | `_build_guardrail_context` now publishes `guardrail_context["degraded_inputs"]: list[str]` via `degraded_input_reasons`. Import extended to `from trading_bot.risk import RiskManager, degraded_input_reasons`. |
| `tests/test_degraded_inputs.py` | New, 21 tests. Reuses `tests/test_risk_gates.py` helpers (`_ctx`, `_decision`, `_acct`, `_base_guardrail`) and `tests/test_overnight_pnl_credit.py::_make_bot` rather than new scaffolding. |

### The gate

Evaluated at the brief's insertion point — `risk.py:190-211`, immediately after the
`if price <= 0: ... continue` check, so `context` is in scope and it runs before any
sizing work. Enforced at `risk.py:258-271`, immediately before `_entry_block_reason`
and still before all sizing (`base_cash_risk`, `confidence_multiplier`, quantity).
Rationale for the split in §5.

Enforcement follows the file's existing conventions exactly: `LOGGER.info(...)` +
`continue`, with the `if reversing_position: "Converted %s %s into CLOSE because %s"`
idiom copied from the adjacent `entry_block_reason` handler. No `rejected` list, no
`_rejection` helper.

Gate logic: only `action in {"BUY", "SELL"}`; only when `context.data_quality is not
None and quality.is_degraded`; rejects when `trade.confidence < degraded_inputs_min_confidence`.
Reason token `DEGRADED_INPUTS_BLOCK`. Gates on `is_degraded` only — no `bars_imputed /
bars_total` division anywhere in the new code.

---

## 2. Test commands and real output

### Baseline before any change

```
$ .venv/bin/pytest -q
8 failed, 626 passed in 4.47s
```

Failing: `test_events_rotation.py` (×2), `test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status`,
`test_watcher_heartbeat_journal.py` (×5). Pre-existing macOS platform artifacts. Not touched.

### Step 2 — new tests fail before implementation (confirmed)

```
$ .venv/bin/pytest tests/test_degraded_inputs.py -v
E   AttributeError: 'Settings' object has no attribute 'degraded_inputs_min_confidence'
E   ImportError: cannot import name 'degraded_input_reasons' from 'trading_bot.risk'
4 failed in 0.22s
```

Exactly the two failure modes the brief predicted.

### Step 5 — helper + setting pass

```
$ .venv/bin/pytest tests/test_degraded_inputs.py -v
4 passed in 0.18s
```

### Step 7 — gate tests fail before the gate exists (confirmed)

```
$ .venv/bin/pytest tests/test_degraded_inputs.py -v
E   AssertionError: assert [ExecutionPla...ing_hours=72)] == []
E     + ExecutionPlan(symbol='WTI', action='BUY', confidence=0.75, reason='t', quantity=0.0316,
E       target_price=95.0, stop_distance=150.0, take_profit_distance=300.0, ...)
FAILED tests/test_degraded_inputs.py::test_degraded_context_rejects_buy_below_degraded_floor
FAILED tests/test_degraded_inputs.py::test_degraded_context_rejects_sell_below_degraded_floor
2 failed, 14 passed in 0.27s
```

Correct reason: with no gate, the degraded BUY/SELL is traded at full size. The 14 controls
passed *before* the implementation too — that is what makes them controls; they must not move.

### Reversal-exit regression tests fail before the fix (confirmed)

```
$ .venv/bin/pytest tests/test_degraded_inputs.py -q -k "reversal or re_enter"
E   AssertionError: assert [] == ['CLOSE']
E     Right contains one more item: 'CLOSE'
INFO  trading_bot.risk:risk.py:201 Rejected SELL WTI: DEGRADED_INPUTS_BLOCK confidence 0.75 below degraded floor 0.85 (None)
FAILED tests/test_degraded_inputs.py::test_degraded_context_still_exits_when_a_reversal_carries_the_exit
FAILED tests/test_degraded_inputs.py::test_degraded_reversal_exits_but_does_not_re_enter
2 failed, 19 deselected in 0.98s
```

### After implementation

```
$ .venv/bin/pytest tests/test_degraded_inputs.py -q
21 passed in 0.78s

$ .venv/bin/pytest tests/test_degraded_inputs.py tests/test_risk_gates.py tests/test_risk_sizing.py \
    tests/test_cost_edge_gate.py tests/test_drawdown_close_only.py tests/test_overnight_pnl_credit.py \
    tests/test_data_quality.py tests/test_market_data.py -q
98 passed in 1.26s
```

### Step 8 — full suite (post-commit)

```
$ .venv/bin/pytest -q
8 failed, 647 passed in 4.05s
```

**626 → 647 passed (+21 new). 8 failed → 8 failed, identical test names.** No regression.

---

## 3. Constraint compliance

- **No risk default changed.** Verified by probe (`Settings(_env_file=None)`):
  `risk_per_trade_pct=0.025`, `risk_per_trade_pct_base=0.025`, `min_model_confidence=0.72`,
  `risk_confidence_scaling_max=1.0`, `atr_stop_multiplier=1.5`, `stop_loss_pct_floor=0.005`,
  `risk_reward_ratio=2.0`, `dry_run=True`. New: `degraded_inputs_min_confidence=0.85`.
- **Reject-only.** `test_degraded_gate_cannot_upsize_or_loosen_anything` asserts a degraded
  high-conviction entry has byte-identical `quantity`, `stop_distance` and
  `take_profit_distance` to the same entry on a clean feed. The gate's only effect is `continue`.
- **CLOSE never gated.** `test_degraded_context_never_blocks_close` (with `bars_imputed=99`,
  confidence `0.10`) and `test_degraded_context_never_blocks_close_on_empty_fallback_context`
  (`bars_total=0`). Structurally guaranteed too: the `action == "CLOSE"` branch returns at
  `risk.py:154` before the gate.
- **HOLD never gated.** `test_degraded_context_does_not_gate_hold` asserts no plan *and* that
  `DEGRADED_INPUTS_BLOCK` was not logged, so an incident log never misattributes a HOLD.
- **`data_quality=None` is not degraded.** `test_missing_quality_is_not_treated_as_degraded`
  (unit) and `test_unmeasured_quality_allows_the_same_buy` (integration: the trade still executes).
- **Threshold, not a blanket ban.** `test_degraded_context_allows_buy_above_degraded_floor`
  (0.90 passes on a degraded feed) plus `test_degraded_context_allows_buy_exactly_at_degraded_floor`
  pinning the boundary as inclusive.
- **No `bars_imputed / bars_total`.** `test_reasons_handles_empty_fallback_context_without_zero_division`
  covers the `bars_total=0` fallback context.
- **Untouched:** `ig_bridge.py`, `session_manager.py`, `supervisor/`. Only `.env.example` is
  tracked in git; no secret committed. Staged exactly 4 files.

---

## 4. Test inventory (21)

Unit — `degraded_input_reasons`: default floor above normal floor; degraded symbol flagged;
clean flags nothing; `None` flags nothing; multi-symbol sorted + skips healthy; `bars_total=0`
without ZeroDivisionError.

Gate — rejects BUY below floor; healthy context allows the same BUY (control); unmeasured
quality allows the same BUY; allows BUY above floor; allows BUY exactly at floor; rejects SELL
below floor; never blocks CLOSE; never blocks CLOSE on empty fallback context; reversal still
exits; reversal exits but does not re-enter; does not gate HOLD; cannot upsize or loosen.

Guardrail context — publishes `degraded_inputs`; empty on healthy feeds; does not set
`block_new_entries`.

All gate tests hold the other gates constant at the `WTI @ 95.0, atr=1.0` setup that
`test_risk_gates.py::test_open_session_allows_entry` already proves is tradeable, and each
blocking test additionally asserts the `DEGRADED_INPUTS_BLOCK` log token via
`caplog.at_level(logging.INFO, logger="trading_bot.risk")` — the convention in
`tests/test_cost_edge_gate.py:105-112`. So a rejection cannot be silently attributed to a
different guard.

---

## 5. Self-review findings

**Finding 1 (fixed, and a deliberate deviation from the brief) — enforcing the gate at the
brief's insertion point blocked a live exit path.**

The brief specifies inserting *and enforcing* the gate immediately after the price check.
That point is **before** the reversal branch (`risk.py:222-256`). A `BUY`/`SELL` against an
opposite-direction position carries an exit: the branch appends a `CLOSE` leg, and with the
default `allow_shorts=False` a `SELL`-against-a-long is converted to a pure `CLOSE`. Enforcing
at the insertion point `continue`d before that leg was ever appended. Measured, with the brief's
literal code:

```
DEGRADED feed: SELL vs open LONG (allow_shorts=False) => []                    <-- stranded
clean feed:    SELL vs open LONG (allow_shorts=False) => [('CLOSE', 'DI001')]  <-- exits
```

A degraded feed silently removed an exit that works fine on a clean feed — a direct violation
of "we must always be able to exit a position, especially on a degraded feed", in the default
configuration.

Resolution: keep **evaluation** at the brief's insertion point (both stated reasons preserved —
`context` in scope, before any sizing work), move **enforcement** ~47 lines down to sit beside
the other entry-only blocks, and reuse the file's existing `if reversing_position: "Converted X
into CLOSE"` idiom. This is the same treatment `_entry_block_reason` and `_trend_alignment_block`
already get; the degraded gate was the only entry-only gate not following it. After the fix both
feeds produce `[('CLOSE', 'DI001')]`, and `test_degraded_reversal_exits_but_does_not_re_enter`
proves the re-entry leg is still refused (so the gate is not weakened — it exits without
re-entering). Sizing still happens strictly after enforcement.

**Finding 2 (no action) — `quality.note` can render as `(None)` in the log.** The brief's format
string interpolates `quality.note`, which is `Optional`. In production this cannot happen:
`market_data.py:271-283` sets `is_degraded=True` only when `bars_imputed`, `bars_dropped_empty`
or `bars_volume_filled` is non-zero, and each of those appends a `note_part`, so `note` is always
populated when `is_degraded` is `True`. `(None)` appears only for synthetic test objects
constructed with `is_degraded=True` and no counts. Left as the brief specifies.

**Finding 3 (noted, not changed) — no validator ties the two floors together.** If someone sets
`DEGRADED_INPUTS_MIN_CONFIDENCE` below `min_model_confidence`, the gate becomes a silent no-op,
because the `min_model_confidence` check at `risk.py:150` already rejects everything below it.
Only reachable by explicitly lowering the value; the default is asserted correct by
`test_default_degraded_confidence_floor_is_above_normal_floor`. A cross-field validator would be
the fix if you want it enforced — out of scope here.

**Finding 4 (noted) — the LLM is told `min_model_confidence` but not the degraded floor.**
`openai_decision.py:672` puts `min_model_confidence` in the prompt. The model now sees
`guardrail_context["degraded_inputs"]` (so it knows *which* symbols are degraded) but not the
0.85 number it must clear. It will therefore keep emitting ~0.75 entries on degraded symbols
that the executor silently discards. Functionally safe — fail-closed, and rejections are logged
— but wasteful. Candidate follow-up: surface `degraded_inputs_min_confidence` in the prompt
contract.

**Finding 5 (noted) — `guardrail_context["degraded_inputs"]` is intentionally not a
`block_reason`.** Adding it there would set `block_new_entries=True` and halt *all* symbols
because one feed degraded. `test_guardrail_context_degraded_inputs_does_not_block_new_entries`
pins this: `degraded_inputs` is populated while `block_new_entries is False` and
`block_reasons == []`.

**Finding 6 (noted) — the gate is per-symbol, evaluated from that symbol's own
`context.data_quality`.** A degraded WTI feed does not raise the bar for a GOLD entry. That
matches the brief's design and `degraded_input_reasons`' one-entry-per-symbol shape.

---

## 6. Interfaces produced (for downstream tasks)

- `Settings.degraded_inputs_min_confidence: float = 0.85`
- `trading_bot.risk.degraded_input_reasons(Mapping[str, DataQuality | None]) -> list[str]` —
  returns `["SYM:{bars_imputed}/{bars_total} bars imputed", ...]`, sorted by symbol, skipping
  `None` and non-degraded.
- Rejection reason token `DEGRADED_INPUTS_BLOCK`, logged at INFO on `trading_bot.risk`.
  On a **non-reversal** entry it is always `Rejected <action> <symbol>:
  DEGRADED_INPUTS_BLOCK ...`. That form is unconditional and safe to depend on.

  **Corrected 2026-07-29 (review finding F4).** This section previously claimed
  the reversal form `Converted <action> <symbol> into CLOSE because
  DEGRADED_INPUTS_BLOCK ...` is *the* reversal contract. It is not — it is
  config-dependent, because three earlier `continue`s in the reversal branch can
  claim the conversion first and discard the already-computed block string. The
  outcome is correct in every case (the exit leg is appended at `risk.py:239`
  before any of them, and the entry is never re-added); **only the log token
  differs.** Measured, degraded SELL/BUY @0.75 against an opposite position
  (defaults are `allow_shorts=False`, `allow_position_reversals=True`):

  | Case | Config | Logged token |
  |---|---|---|
  | BUY vs open SHORT | pure code defaults | `... because DEGRADED_INPUTS_BLOCK` |
  | SELL vs open LONG | `allow_shorts=True`, `allow_position_reversals=True` | `... because DEGRADED_INPUTS_BLOCK` |
  | SELL vs open LONG | default `allow_shorts=False` | `... because shorting is disabled` (`risk.py:241-246`) |
  | either | `allow_position_reversals=False` | `... because reversals are disabled` (`risk.py:248-254`) |

  Note this is also narrower than the review's own phrasing, which said the token
  "only appears when BOTH `allow_shorts` and `allow_position_reversals` are
  enabled". True for the SELL-vs-long path only: `allow_shorts` gates the SELL
  side exclusively, so **BUY vs an open short reaches the `DEGRADED_INPUTS_BLOCK`
  reversal line under pure code defaults** (row 1). All four rows produce
  `['CLOSE']`.

  Consumers must therefore key on the substring `DEGRADED_INPUTS_BLOCK`, not on
  the `Converted ...` prefix, and must not treat the absence of the token on a
  reversal as evidence the gate did not fire.
- `guardrail_context["degraded_inputs"]: list[str]`.

---

## Fix pass (review findings)

Four findings from review of `aa21eb0` (two Important, two documentation).
Files touched: `tests/test_degraded_inputs.py`, `trading_bot/config.py`,
`.env.example`, this report. **`trading_bot/risk.py` was deliberately NOT
changed** — the gate's behaviour was already correct; what was wrong was the
test that claimed to prove it and the config that could disable it.

### F1 — the `reversing_position` branch was covered only by a tautology

**Confirmed the review's diagnosis by measurement.** On a reversal the position
being closed is still in `open_positions_by_symbol`, so
`CorrelationCap.would_breach('WTI', ['WTI'])` self-correlates at 1.0 > cap 0.7
and converts to CLOSE at `risk.py:401-404` regardless of data quality. All four
combinations are identical, so the old
`assert [p.action for p in plans] == ["CLOSE"]` discriminated nothing:

```
degraded SELL 0.75 -> ['CLOSE']    clean SELL 0.75 -> ['CLOSE']
degraded SELL 0.90 -> ['CLOSE']    clean SELL 0.90 -> ['CLOSE']
```

The old `assert _blocked(caplog)` did not save it either: it only greps for the
substring `DEGRADED_INPUTS_BLOCK`, which the surviving unconditional
`LOGGER.info("Rejected %s %s: %s", ...)` still emits. Both assertions survive
the mutation.

**Change:** in `test_degraded_reversal_exits_but_does_not_re_enter`, replaced
the substring check with an exact-line assertion on the reversal log path:

```python
assert any(
    "Converted SELL WTI into CLOSE because DEGRADED_INPUTS_BLOCK" in r.getMessage()
    for r in caplog.records
)
```

Rewrote the docstring. It previously claimed "the new short is refused by the
degraded floor" — factually wrong, since the correlation cap co-enforces the
same outcome. It now states that the CLOSE-only *outcome* proves nothing about
this gate, and that what the test actually pins is *which* branch claims the
conversion and what it logs (the degraded gate at `risk.py:261` sits well before
the correlation cap, so it must claim it).

**Mutation evidence — deleting the whole `if reversing_position:` branch at
`risk.py:262-269`, leaving only the unconditional "Rejected" log:**

| | Result |
|---|---|
| Mutation applied, **old** test | `21 passed in 0.83s` — tautology confirmed, mutation survives |
| Mutation applied, **new** test | `1 failed, 20 passed in 0.77s` — `FAILED ...::test_degraded_reversal_exits_but_does_not_re_enter`, captured log shows the wrong form: `Rejected SELL WTI: DEGRADED_INPUTS_BLOCK ...` |
| Branch restored, **new** test | `21 passed in 0.78s` |

`risk.py` was byte-for-byte restored after the mutation (`git diff --stat
trading_bot/risk.py` empty).

### F2 — the gate could be silently disabled by config

**Reproduced before the fix:** `Settings(_env_file=None,
degraded_inputs_min_confidence=0.10)` was accepted, and a degraded BUY @0.75
then produced `['BUY']` — the gate a total no-op. Because
`min_model_confidence` (0.72) already rejects everything beneath itself
(`risk.py:150`), *any* value below it makes the task inert with no error, no
warning, no log. This closes Finding 3 of §5, which had noted the hole but left
it open.

**Change:** added `_degraded_floor_not_below_normal_floor` (`mode="after"`
cross-field validator) to `trading_bot/config.py`. **Placement deviation:** the
review suggested "after the field" (line ~235); I put it with the other
validators after `validate_llm_config` (`config.py:583-595`) because every
validator in this file is grouped there and interleaving a method into the
field-declaration block breaks that convention. Behaviour is identical.

**Verified after the fix:** the same construction now raises
`pydantic_core._pydantic_core.ValidationError: ... Value error,
DEGRADED_INPUTS_MIN_CONFIDENCE (0.1) must be >= MIN_MODEL_CONFIDENCE (0.72); a
lower value silently disables the degraded-inputs gate.`

**Four tests added** (the review asked for three; the fourth pins that the
validator is genuinely cross-field rather than a constant check):

- `test_degraded_floor_below_normal_floor_is_rejected` — 0.10 raises
  `ValidationError`, message names both settings.
- `test_degraded_floor_equal_to_normal_floor_is_accepted` — 0.72/0.72 accepted.
- `test_degraded_floor_above_normal_floor_is_accepted` — 0.95 accepted.
- `test_raising_normal_floor_past_the_degraded_floor_is_rejected` —
  `min_model_confidence=0.90` against the default 0.85 floor also raises.

**Audit for fixtures the validator would now break: none found.** Searched
`tests/`, `trading_bot/`, `supervisor/` for `min_model_confidence=` /
`MIN_MODEL_CONFIDENCE` / `degraded_inputs_min_confidence=` /
`DEGRADED_INPUTS_MIN_CONFIDENCE`; the only hits outside `config.py` are the new
tests. No existing test sets either field, so no fixture needed weakening and
the validator was not weakened. No live `.env` exists in the repo (only
`.env.example`, which ships 0.72/0.85 and satisfies the invariant).

### F3 — `.env.example` did not document the new setting

Added `DEGRADED_INPUTS_MIN_CONFIDENCE=0.85` as a sibling of
`MIN_MODEL_CONFIDENCE=0.72` under `# Risk`, with a comment stating it is the
entry floor when the price feed is degraded, that CLOSE/HOLD are never gated,
and that it must be `>= MIN_MODEL_CONFIDENCE` or startup fails. This matters
because that file is a live deployment template whose values override code
defaults (it ships `ALLOW_SHORTS=true` vs code `False`, and
`RISK_CONFIDENCE_SCALING_MAX=2.5` vs code `1.0`), so a safety floor absent from
it is a real discoverability gap.

### F4 — corrected the log-contract claim in §6

§6 claimed the `Converted ... into CLOSE because DEGRADED_INPUTS_BLOCK` line is
*the* reversal contract. Amended in place with a measured four-row table.

**The review's own phrasing was also slightly wrong, and the amendment records
the narrower truth.** The review said the token appears "only when BOTH
`allow_shorts` and `allow_position_reversals` are enabled". Measured defaults
are `allow_shorts=False`, `allow_position_reversals=True`, and `allow_shorts`
gates the SELL side only — so **a BUY against an open short reaches the
`DEGRADED_INPUTS_BLOCK` reversal line under pure code defaults**:

```
BUY vs open SHORT,  all defaults                     -> ['CLOSE']  'Converted BUY WTI into CLOSE because DEGRADED_INPUTS_BLOCK ...'
SELL vs open LONG,  allow_shorts=True, reversals=True -> ['CLOSE']  'Converted SELL WTI into CLOSE because DEGRADED_INPUTS_BLOCK ...'
SELL vs open LONG,  default allow_shorts=False        -> ['CLOSE']  'Converted SELL WTI into CLOSE because shorting is disabled'
SELL vs open LONG,  allow_position_reversals=False    -> ['CLOSE']  'Converted SELL WTI into CLOSE because reversals are disabled'
```

The review's substantive point stands and is preserved: on the SELL-vs-long
path under the code default the block string is computed then discarded at
`risk.py:241-246` and never logged. The outcome is correct in all four cases
(`['CLOSE']`, exit kept, entry never re-added); only the log token differs by
config. §6 now tells consumers to key on the `DEGRADED_INPUTS_BLOCK` substring
rather than the `Converted ...` prefix.

### Not fixed (out of scope, per instruction)

`openai_decision.py:672` still tells the LLM `min_model_confidence` but not the
0.85 degraded floor (§5 Finding 4). Explicitly excluded: it is a prompt-contract
change outside this task's file set with its own test surface. Still a live
follow-up — the model will keep emitting ~0.75 entries on degraded symbols that
the executor discards. Fail-closed and logged, so wasteful rather than unsafe.

### Constraint compliance

- No risk default changed. Re-verified by probe after all edits:
  `risk_per_trade_pct=0.025`, `min_model_confidence=0.72`,
  `risk_confidence_scaling_max=1.0`, `atr_stop_multiplier=1.5`,
  `stop_loss_pct_floor=0.005`, `risk_reward_ratio=2.0`, `dry_run=True`,
  `degraded_inputs_min_confidence=0.85`.
- Gate remains reject-only; CLOSE/HOLD remain ungated (`risk.py` untouched, and
  `test_degraded_gate_cannot_upsize_or_loosen_anything` plus the two
  never-blocks-CLOSE tests still pass).
- `ig_bridge.py`, `session_manager.py`, `supervisor/` untouched. No secrets.

### Test output

```
$ .venv/bin/pytest tests/test_degraded_inputs.py tests/test_risk_gates.py -v
31 passed in 0.76s
```

```
$ .venv/bin/pytest -q
8 failed, 651 passed in 4.18s
```

**647 -> 651 passed (+4 new tests). 8 failed -> 8 failed, identical test names**
(`test_events_rotation` x2, `test_memory_pressure::test_get_rss_bytes_reads_proc_self_status`,
`test_watcher_heartbeat_journal` x5) — the known macOS platform artifacts
(Linux-only `/proc/self/status`, `/bin/true`). Not touched. No regression.

**Note:** `.superpowers/` is gitignored (`.gitignore:24`), so this report is not
part of the commit; the commit contains only `.env.example`,
`trading_bot/config.py`, `tests/test_degraded_inputs.py`.
