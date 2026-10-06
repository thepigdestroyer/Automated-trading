# Task 1 report — Mark-don't-drop outlier bars

Branch: `feat/reliability-and-evidence-loop`
Commit: `a00fda4` (see §6)
Status: **DONE_WITH_CONCERNS** — implemented and green, but I had to deviate
from the brief in three places because the brief was internally inconsistent
and one of its instructions would have silently disabled a live supervisor
safety rule. All three deviations are documented below with evidence.

---

## 1. What changed

| File | Change |
|---|---|
| `trading_bot/models.py` | Added `DataQuality(StrictModel)` (`bars_total`, `bars_imputed`, `max_gap_pct`, `is_degraded`, `note`) above `SymbolContext`; added `data_quality: DataQuality | None = None` to `SymbolContext`. |
| `trading_bot/market_data.py` | `_apply_sanity_checks` now returns `DataQuality` and **imputes instead of dropping**: corrupt OHLC is blanked and time-interpolated so the index stays contiguous. Added `import numpy as np`. `__init__` gains `self._last_quality: DataQuality | None = None`; `fetch_history` assigns it; `build_symbol_context` captures it and passes `data_quality=` into `SymbolContext(...)`. |
| `tests/test_data_quality.py` | **New.** The brief's four tests, verbatim. |
| `tests/test_market_data.py` | `test_sanity_filter_drops_extreme_price_gap` → `test_sanity_filter_imputes_extreme_price_gap_without_dropping`. Deviation #2 below. |
| `docs/decisions/0006-mark-dont-drop-outlier-bars.md` | **New.** The brief's ADR plus three factual corrections (deviation #3). |

Kept as instructed: the inline `import logging` inside the function, the
energy `floor_pct = 0.30` / default `0.15` thresholds, and the `10x median`
rule. No surrounding code refactored. `trading_bot/config.py` untouched, so
no risk default moved. `ig_bridge.py` and `session_manager.py` untouched.

## 2. TDD evidence — the new tests failed first

`.venv/bin/pytest tests/test_data_quality.py -v` **before** implementation:

```
tests/test_data_quality.py::test_outlier_bar_is_not_dropped_index_stays_contiguous FAILED
tests/test_data_quality.py::test_outlier_close_is_replaced_with_interpolated_value PASSED
tests/test_data_quality.py::test_quality_report_counts_imputed_bars FAILED
tests/test_data_quality.py::test_clean_frame_reports_no_degradation FAILED
========================= 3 failed, 1 passed in 0.52s ==========================
```

Failure reasons, as predicted by the brief:

```
E       assert 38 == 40          # rows were being dropped
E       AttributeError: 'NoneType' object has no attribute 'bars_total'
E       AttributeError: 'NoneType' object has no attribute 'bars_imputed'
WARNING  trading_bot.market_data:market_data.py:63 TEST: dropping 2 outlier bar(s) with gap > 15.0%
```

Two honest notes on this baseline:

- The brief predicted `len(frame) == 39`. The real value is **38**, because a
  single corrupted tick produces *two* extreme returns (the jump in and the
  jump out). This discrepancy is what led to deviation #1.
- `test_outlier_close_is_replaced_with_interpolated_value` **passed before
  implementation** — vacuously. Dropping 2 rows shifts `iloc[20]` onto the bar
  originally at index 22 (close 102.2), which happens to satisfy
  `101.0 < x < 105.0`. That test is not an independent discriminator on its
  own; the other three carry the weight.

## 3. Deviations from the brief (all three are substantive)

### #1 — `bars_imputed` counted returns, not bars (brief self-contradiction)

The brief's test asserts `report.bars_imputed == 1`. The brief's own
implementation produces `2`:

```
E       AssertionError: assert 2 == 1
E        +  where 2 = DataQuality(bars_total=40, bars_imputed=2, max_gap_pct=783.22, is_degraded=True, note='2/40 bars imputed (5.0%)').bars_imputed
```

Cause: `bad_rows = returns[returns > threshold].index` flags *extreme returns*,
which are a property of a **pair** of bars. The fixture has exactly one corrupt
bar (index 20, close 900.0), but both index 20 (jump up, +783%) and index 21
(reversal down, −88.7%) exceed the threshold. Bar 21 is a perfectly good bar.

The brief's version would therefore have (a) reported double the true
degradation and (b) **overwritten a good bar with an interpolated value** —
destroying real data. I treated the test as the spec and fixed the
implementation: flag the corrupt bar, and when the next return is also extreme
*and of the opposite sign*, treat that as the recovery and leave it alone.
A genuine one-way level shift (rollover gap, no reversal) still counts as one
bar. Verified: one-way shift → `imputed: 1`; two separate spikes → `imputed: 2`.

### #2 — an existing test encoded the behaviour we were told to remove

`tests/test_market_data.py::test_sanity_filter_drops_extreme_price_gap`
asserted `len(result) < 200` and `"dropping" in caplog.text`. The brief's Step 6
claimed this suite would pass; it cannot — the assertions are the *inverse* of
the task's requirement. It failed:

```
E       assert 200 < 200
```

I rewrote it to assert the new contract (length preserved, index equal, spike
neutralised, `"imputing"` logged, `_last_quality.bars_imputed == 1`) and renamed
it `test_sanity_filter_imputes_extreme_price_gap_without_dropping` with a
docstring pointing at ADR 0006. Flagging explicitly: **this is me changing an
existing test.** It is justified because the test asserts the removed
behaviour, not because it was inconvenient — and the replacement is strictly
stronger (5 assertions vs 2).

### #3 — the brief's rename would have silently killed a supervisor rule

The brief renames the event field `bars_dropped` → `bars_imputed`, and its ADR
states the supervisor "keys on `last_good_ts` and is unaffected". That is wrong.
`supervisor/rules.py:506` reads:

```python
bars_dropped = int(data.get("bars_dropped", 0))
...
if bars_dropped <= 0:
    break          # streak resets -> data_feed_degraded never fires
```

With only `bars_imputed` emitted, `data_feed_degraded` would read 0, break the
streak on every event, and **never fire again**. No test would have caught it:
every case in `tests/test_supervisor_rules.py` builds synthetic payloads
containing `bars_dropped`, so the suite stays green while the live rule is dead.

Fix (minimal, additive, supervisor code untouched): emit **both** keys with the
same count. Proven end-to-end against the real emitted payload rather than
assumed — 5 consecutive fetches through the actual `_apply_sanity_checks`:

```
8. emitted payload keys: ['bars_dropped', 'bars_imputed', 'gap_pct_max', 'last_good_ts', 'symbol', 'threshold_pct']
   data_feed_degraded fires on real payload: True | {'symbol': 'NG=F', 'consecutive_dropped_cycles': 5, 'threshold': 3, 'max_age_minutes': 30.0}
```

The ADR was corrected to describe what the code actually does.

## 4. Test commands and real output

**New tests after implementation** — `.venv/bin/pytest tests/test_data_quality.py tests/test_market_data.py -v`:

```
tests/test_data_quality.py::test_outlier_bar_is_not_dropped_index_stays_contiguous PASSED [ 14%]
tests/test_data_quality.py::test_outlier_close_is_replaced_with_interpolated_value PASSED [ 28%]
tests/test_data_quality.py::test_quality_report_counts_imputed_bars PASSED [ 42%]
tests/test_data_quality.py::test_clean_frame_reports_no_degradation PASSED [ 57%]
tests/test_market_data.py::test_sanity_filter_imputes_extreme_price_gap_without_dropping PASSED [ 71%]
tests/test_market_data.py::test_sanity_filter_rejects_all_zero_volume PASSED [ 85%]
tests/test_market_data.py::test_sanity_filter_passes_clean_data PASSED   [100%]

============================== 7 passed in 0.39s ===============================
```

**Full suite baseline, before any of my changes** — `.venv/bin/pytest -q`:

```
8 failed, 596 passed in 4.27s
```

**Full suite after** — `.venv/bin/pytest -q`:

```
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl - ass...
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation - asser...
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick
8 failed, 600 passed in 4.30s
```

**vs baseline: 8 failed → 8 failed (identical test names), 596 passed → 600
passed (+4 new).** No regressions. The 8 are the known macOS artifacts
(`/proc/self/status`, `/bin/true`) and were not touched.

## 5. Self-review findings

Beyond the suite, I probed 14 edge cases directly against `_apply_sanity_checks`
(scratch script kept outside the repo, in `$TMPDIR/opencode/`, not committed):

| # | Probe | Result |
|---|---|---|
| 1 | OHLC ordering after imputation | `High >= Close >= Low` holds on every row; zero NaN left |
| 2 | One-way level shift (rollover, no reversal) | 1 bar imputed, 40 rows kept |
| 3 | Two separate spikes | 2 imputed, correct values, `note` = `2/40 bars imputed (5.0%)` |
| 4 | Spike on the **last** bar (no right neighbour) | imputed via `ffill`, no NaN |
| 5 | Spike on the **first** bar | **blind spot — see concerns** |
| 6 | Energy floor preserved | `NG=F` +20% gap → 0 imputed; `SPY` → 1. Thresholds unchanged |
| 7 | tz-naive DatetimeIndex | `method="time"` interpolation works |
| 8 | Supervisor `data_feed_degraded` on real payload | fires, streak 5 |
| 9 | **Indicators on imputed vs clean frame** | `sma_20` **delta 0.0** — identical |
| 10 | `SymbolContext` schema | `data_quality` present |
| 13 | Flat series (median return 0) | 0 imputed, no false positive, no div-by-zero |
| 14 | Single-row frame (`returns` empty) | no crash, report returned with `bars_total=1` |

Probe 9 is the substantive proof the task's goal was achieved: with a corrupted
tick present, indicators computed on the imputed frame are now **numerically
identical** to the same frame with no corruption at all. Previously the frame
was silently short two bars and every rolling window was misaligned.

## 6. Commit

```
a00fda4 fix(data): mark-don't-drop outlier bars to preserve index continuity
a00fda45634c620360d9932504069c3cf0c27cd4
```

5 files changed, 258 insertions(+), 19 deletions(-).

Command used (brief Step 9, plus `tests/test_market_data.py` per deviation #2):

```bash
git add trading_bot/models.py trading_bot/market_data.py tests/test_data_quality.py \
        tests/test_market_data.py docs/decisions/0006-mark-dont-drop-outlier-bars.md
git commit -m "fix(data): mark-don't-drop outlier bars to preserve index continuity"
```

No `.env` or secret staged; verified with `git diff --name-only --cached`.

## 7. Concerns for the next task

1. **Leading-corrupt-bar blind spot (pre-existing, not fixed).** A `pct_change`
   detector cannot validate bar 0 — it has no predecessor. Measured: bar 0
   stays at 900.0 (corrupt, undetected) and bar 1 is imputed to 500.1 by
   interpolating between the corrupt 900.0 and 100.2. The old drop-based code
   was equally broken here (it dropped bar 1 and left 900.0 → 100.2 adjacent),
   so this is not a new failure, but it is not fixed either. A real fix needs a
   rolling-median detector — a separate design decision, deliberately out of
   scope. `is_degraded` does fire, so it is at least visible. Documented in
   ADR 0006.
2. **`is_degraded` is a boolean, not a severity.** It is `True` for 1 imputed
   bar in 1000 exactly as for 900 in 1000. Task 2's degraded-inputs gate should
   read `bars_imputed / bars_total` (carried in `note`, and derivable from the
   two int fields) rather than gate purely on the flag, or a single stale tick
   will veto trading as hard as a wholly corrupt feed.
3. **Imputation understates genuine gaps.** For a real rollover gap the
   interpolated bar replaces true price movement with a midpoint (probe 2:
   96.27% gap flattened). ADR 0006 accepts this consciously — the detector only
   fires on ticks already classified as corrupt — but any future volatility or
   ATR-based sizing that consumes these bars will read slightly low on affected
   symbols.
4. **`_last_quality` is mutable per-client state.** `build_symbol_context` reads
   it immediately after `fetch_history`, and a raising `fetch_history` aborts
   the caller, so no stale cross-symbol attribution is reachable today. It is
   still a latent footgun if anything later calls `fetch_history` and reads
   `_last_quality` out of order. Left as the brief specified; not reset at
   function entry.
5. **`bars_dropped` is now emitted redundantly** purely to keep
   `supervisor.rules.data_feed_degraded` alive. Whoever owns the supervisor
   should switch that rule to read `bars_imputed` (or accept either key) and
   then drop the duplicate, ideally with a test that asserts the rule fires on
   a payload produced by the real `_apply_sanity_checks` rather than a synthetic
   dict — that gap is why the break was invisible.

---

# Fix pass (review findings)

Branch: `feat/reliability-and-evidence-loop`
Base commit reviewed: `a00fda4`
Status: **DONE** — all findings (C1, I2–I7, all four Minors) fixed. No finding
declined. Files touched: `trading_bot/market_data.py`,
`tests/test_data_quality.py`, `docs/decisions/0006-mark-dont-drop-outlier-bars.md`.
`ig_bridge.py`, `session_manager.py`, `supervisor/**` and `config.py` untouched.

## 0. Every defect reproduced first, against `a00fda4`

Before changing any production code I ran a scratch probe
(`$TMPDIR/opencode/repro_before.py`, not committed) directly against the
shipped implementation. Real output:

```
C1: >20% NaN guard defeated when an outlier is present
  frame: 40 bars, 15 NaN OHLC rows (37.5%), 1 outlier at bar 25
    -> NO RAISE. report = DataQuality(bars_total=40, bars_imputed=1, max_gap_pct=778.91, is_degraded=True, note='1/40 bars imputed (2.5%)')
  frame: 40 bars, 15 NaN OHLC rows, NO outlier
    -> raised: TEST: >20% NaN rows after fetch

I2: consecutive corruption -> 10x-wrong price left in frame
  report      = DataQuality(bars_total=40, bars_imputed=2, max_gap_pct=922.0, ...)
  close 19:23 = [101.9, 55.95, 10.0, 56.15]

MINOR: max_gap_pct = inf -> model_dump_json emits null
  report.max_gap_pct = inf
  model_dump_json    = {...,"max_gap_pct":null,...}

I5: _last_quality survives a raising fetch
  after AAA ok    : DataQuality(bars_total=40, bars_imputed=1, max_gap_pct=783.22, ...)
  BBB raised      : no market data returned for BBB
  after BBB raise : DataQuality(bars_total=40, bars_imputed=1, max_gap_pct=783.22, ...)  <-- still AAA's

I7: fallback context reports quality unknown
  data_quality = None
```

Every number matches the review's description exactly, including
`101.9, 55.95, 10.0, 56.15`. The same probe re-run after the fixes is quoted
per-finding below.

## 1. C1 (CRITICAL) — NaN guard defeated by the imputation pass

**Changed** (`market_data.py:84-102`): both guards now run *before* any
imputation. The zero-volume check and the `>20% NaN` check were lifted above
the outlier-detection block, in that same relative order, so which error wins
when both conditions hold is unchanged. Both raise strings are byte-identical
(`f"{data_symbol}: all rows have zero volume"`,
`f"{data_symbol}: >20% NaN rows after fetch"`) — other tests assert on them.
The `=X` forex exemption is carried across verbatim.

Pre-existing NaN bars are now counted honestly. `pre_nan_mask` is captured
before imputation; `bars_imputed = bars_outlier + bars_nan_prefilled`, computed
from disjoint masks (`pre_nan_mask & ~outlier_mask`) so a row that is both NaN
and an outlier is not double-counted. The interpolation pass now runs whenever
*anything* needs filling, not only when an outlier is present — otherwise
`bars_imputed` would still have been path-dependent. `note` gains
`"N already NaN on arrival"`.

**Evidence** — same probe, after:

```
frame: 40 bars, 15 NaN OHLC rows (37.5%), 1 outlier at bar 25
  -> raised: TEST: >20% NaN rows after fetch
frame: 40 bars, 15 NaN OHLC rows, NO outlier
  -> raised: TEST: >20% NaN rows after fetch
```

Guard boundary is unchanged (`<`, i.e. exactly 20% NaN still passes):

```
7/40 NaN (17.5%) -> ok, bars_imputed=8, note='8/40 bars imputed (20.0%); 7 already NaN on arrival'
8/40 NaN (20.0%) -> ok, bars_imputed=9, note='9/40 bars imputed (22.5%); 8 already NaN on arrival'
9/40 NaN (22.5%) -> raised: T: >20% NaN rows after fetch
```

Tests: `test_nan_guard_raises_even_when_an_outlier_is_present`,
`test_nan_guard_raises_without_an_outlier`,
`test_pre_existing_nan_bars_are_counted_as_imputed` (asserts `bars_imputed == 5`
for 4 NaN + 1 outlier), `test_zero_volume_still_raises_and_takes_precedence`,
`test_forex_exemption_from_zero_volume_still_applies`.

## 2. I2 (IMPORTANT) — opposite-sign skip mishandled consecutive corruption

**Changed** (`market_data.py:118-146`): the skip test no longer keys on a sign
flip. The follow-up bar is skipped only when it *reverts to the pre-spike
level* — `abs(closes[nxt] / closes[pos-1] - 1.0) <= extreme_threshold`, guarded
by `np.isfinite(ref) and ref != 0`. `np.sign` and the `signs` array are gone.

**Evidence** — `close[20]=900.0, close[21]=10.0`:

| | `Close[19:23]` | `bars_imputed` |
|---|---|---|
| before | `[101.9, 55.95, 10.0, 56.15]` | 2 |
| after | `[101.9, 102.0, 102.1, 102.2]` | 3 |

The 10x-low price is gone. Bar 22 is over-imputed, because the reference for
its comparison is bar 21, which is itself corrupt — the review's accepted
trade-off ("over-imputing a good bar" beats "leaving a 10x-wrong price").
`test_two_consecutive_corrupt_bars_are_both_imputed` asserts `bars_imputed == 3`
and that `frame["Close"].max() < 115.0` / `.min() > 90.0`, i.e. no grossly-wrong
price anywhere in the frame.

The known-remaining same-direction-burst limitation is documented in ADR 0006
and pinned by `test_same_direction_burst_is_not_detected_known_limitation`
(bar 21 keeps its 900.0). Pinning it means the gap is explicit rather than
assumed, and the test flips to a failure the day someone adds a rolling-median
reference.

## 3. I3 (IMPORTANT) — dual-key event emission now tested

**Added** two tests, plus a comment at the emission site naming the test:

- `test_data_quality_event_emits_both_imputed_and_dropped_keys` — recording
  event-logger stub (`_RecordingEventLogger`, signature-compatible with
  `JsonEventLogger.log`, since the production call site swallows all
  exceptions and a mismatch would otherwise show up as "no events"). Asserts
  both keys present, `payload["bars_dropped"] == payload["bars_imputed"] == 1`,
  and `int(payload.get("bars_dropped", 0)) > 0` — the supervisor's literal
  expression.
- `test_supervisor_data_feed_degraded_fires_on_a_real_emitted_payload` — feeds
  5 payloads built by the *real* `_apply_sanity_checks` into
  `supervisor.rules.data_feed_degraded` and asserts it fires with
  `consecutive_dropped_cycles == 5`. This closes the gap that made the original
  break invisible: every pre-existing supervisor test hand-writes
  `bars_dropped`. Read-only import; `supervisor/` was not modified.

Both pass. Emitted payload, captured from a real `JsonEventLogger` write:

```
{"bars_dropped": 2, "bars_imputed": 2, "bars_nan_prefilled": 0,
 "gap_pct_max": null, "last_good_ts": "2026-01-02T15:00:00+00:00",
 "symbol": "TEST", "threshold_pct": 15.0}
```

## 4. I4 (IMPORTANT) — bar-counting coverage

Added to `tests/test_data_quality.py`:

| Test | Asserts |
|---|---|
| `test_one_way_level_shift_counts_one_bar` | rollover-style shift, no reversal → `bars_imputed == 1` |
| `test_two_separate_spikes_count_two_bars` | → `bars_imputed == 2`, both spikes neutralised, both recovery bars left in `100..105` |
| `test_two_consecutive_corrupt_bars_are_both_imputed` | the I2 case → 3 imputed, no 10x price left |
| `test_leading_corrupt_bar_documented_expectation` | bar 0 stays `900.0` (undetected), bar 1 fabricated to `400..600`, `is_degraded is True` |

The leading-bar test pins the *documented* expectation rather than a desired
one, and its docstring says so: a `pct_change` detector cannot validate bar 0.

## 5. I5 (IMPORTANT) — `_last_quality` reset

**Changed**: `self._last_quality = None` is the first statement of
`fetch_history` (`market_data.py:39-42`).

**Evidence** — after: `after BBB raise : None` (was AAA's report).
Test: `test_last_quality_is_reset_when_a_later_fetch_raises`.

## 6. I6 (IMPORTANT) — `dropna(how="all")` and the ADR's overstated claim

**Changed**: kept the drop — a bar with no OHLC *and* no volume has nothing to
interpolate from — but stopped hiding it. `fetch_history` measures the row count
either side of the drop and passes `bars_dropped_empty=` into
`_apply_sanity_checks` (keyword-only, defaults to 0, so direct calls are
unaffected). The count surfaces in `DataQuality.note` as
`"N wholly-empty bar(s) dropped before checks"`.

**ADR corrected.** The old line was:

> - Rolling/EWM windows are time-aligned again.

It now states the real, narrower guarantee: contiguity is preserved *through
outlier handling* — no bar is removed because its values looked wrong — while
explicitly conceding this is not an absolute gap-free-index guarantee, that
`dropna(how="all")` still removes wholly-empty bars, and that `bars_total` is
measured after that drop. No overstatement.

Test: `test_wholly_empty_bars_dropped_are_reported_in_the_note` (41-row frame,
row 10 all-NaN → 40 rows returned, `bars_total == 40`, note contains the count).

## 7. I7 (IMPORTANT) — fallback context reported "quality unknown"

**Changed**: `build_fallback_symbol_context` now passes
`data_quality=DataQuality(bars_total=0, is_degraded=True, note=note)`.

**Evidence** — before `None`; after
`DataQuality(bars_total=0, bars_imputed=0, max_gap_pct=None, is_degraded=True, note='no price history')`.
Test: `test_fallback_context_reports_degraded_not_unknown`.

No live behaviour change today — nothing currently reads `data_quality` (grep:
only `models.py` and `market_data.py`); the Task 2 gate is the first consumer.
This makes the field honest before that gate exists.

## 8. Minors

1. **`max_gap_pct` can be `inf`.** Guarded with `math.isfinite`, falling back
   to `None` (`market_data.py:147-156`); `import math` added. Before:
   `max_gap_pct = inf`. After: `None`. Verified end-to-end against a real
   `JsonEventLogger` writing to a temp `events.jsonl` — strict `json.loads`
   parses it and the file contains no `Infinity` literal. Test:
   `test_infinite_gap_is_reported_as_none_not_inf` (asserts both the model
   field and the event payload's `gap_pct_max`).
2. **Stale "dropped" comment** at the top of `YahooMarketDataClient` → now
   "outlier bars are imputed -- neutralised in place, never dropped, see
   ADR 0006".
3. **Unused `import pytest`** — now used, by the four `pytest.raises` tests.
   Also added `numpy` and `unittest.mock.patch` imports, all used.
4. **ADR 0006** now documents (a) the same-direction-burst limitation, with the
   root cause it shares with the leading-bar blind spot (the reference price is
   the previous bar, which may itself be corrupt) and the rolling-median fix
   that would resolve both, and (b) the changed failure mode on a leading
   corrupt bar: the neighbouring bar is now *fabricated* (interpolated midpoint,
   or back-filled if the leading bar itself is blanked) rather than deleted —
   with the explicit note that fabricated-but-present is what this ADR chose,
   and is not the same as correct.

## 9. Test commands and real output

**Targeted** — `.venv/bin/pytest tests/test_data_quality.py tests/test_market_data.py -v`:

```
tests/test_data_quality.py::test_outlier_bar_is_not_dropped_index_stays_contiguous PASSED [  4%]
tests/test_data_quality.py::test_outlier_close_is_replaced_with_interpolated_value PASSED [  8%]
tests/test_data_quality.py::test_quality_report_counts_imputed_bars PASSED [ 13%]
tests/test_data_quality.py::test_clean_frame_reports_no_degradation PASSED [ 17%]
tests/test_data_quality.py::test_nan_guard_raises_even_when_an_outlier_is_present PASSED [ 21%]
tests/test_data_quality.py::test_nan_guard_raises_without_an_outlier PASSED [ 26%]
tests/test_data_quality.py::test_zero_volume_still_raises_and_takes_precedence PASSED [ 30%]
tests/test_data_quality.py::test_forex_exemption_from_zero_volume_still_applies PASSED [ 34%]
tests/test_data_quality.py::test_pre_existing_nan_bars_are_counted_as_imputed PASSED [ 39%]
tests/test_data_quality.py::test_one_way_level_shift_counts_one_bar PASSED [ 43%]
tests/test_data_quality.py::test_two_separate_spikes_count_two_bars PASSED [ 47%]
tests/test_data_quality.py::test_two_consecutive_corrupt_bars_are_both_imputed PASSED [ 52%]
tests/test_data_quality.py::test_leading_corrupt_bar_documented_expectation PASSED [ 56%]
tests/test_data_quality.py::test_same_direction_burst_is_not_detected_known_limitation PASSED [ 60%]
tests/test_data_quality.py::test_data_quality_event_emits_both_imputed_and_dropped_keys PASSED [ 65%]
tests/test_data_quality.py::test_supervisor_data_feed_degraded_fires_on_a_real_emitted_payload PASSED [ 69%]
tests/test_data_quality.py::test_infinite_gap_is_reported_as_none_not_inf PASSED [ 73%]
tests/test_data_quality.py::test_last_quality_is_reset_when_a_later_fetch_raises PASSED [ 78%]
tests/test_data_quality.py::test_wholly_empty_bars_dropped_are_reported_in_the_note PASSED [ 82%]
tests/test_data_quality.py::test_fallback_context_reports_degraded_not_unknown PASSED [ 86%]
tests/test_market_data.py::test_sanity_filter_imputes_extreme_price_gap_without_dropping PASSED [ 91%]
tests/test_market_data.py::test_sanity_filter_rejects_all_zero_volume PASSED [ 95%]
tests/test_market_data.py::test_sanity_filter_passes_clean_data PASSED   [100%]

============================== 23 passed in 0.45s ==============================
```

**Full suite** — `.venv/bin/pytest -q`:

```
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl - ass...
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation - asser...
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick
8 failed, 616 passed in 3.94s
```

**8 failed → 8 failed (identical test names), 600 passed → 616 passed (+16).**
No regressions. The 8 are the known macOS platform artifacts
(`/proc/self/status`, `/bin/true` vs `/usr/bin/true`) and were not touched, per
instruction.

## 10. The new tests are real regression guards, not tautologies

I stashed only `trading_bot/market_data.py` (keeping the new tests) and re-ran
`tests/test_data_quality.py` against the shipped `a00fda4` implementation:

```
FAILED tests/test_data_quality.py::test_nan_guard_raises_even_when_an_outlier_is_present
FAILED tests/test_data_quality.py::test_pre_existing_nan_bars_are_counted_as_imputed
FAILED tests/test_data_quality.py::test_two_consecutive_corrupt_bars_are_both_imputed
FAILED tests/test_data_quality.py::test_infinite_gap_is_reported_as_none_not_inf
FAILED tests/test_data_quality.py::test_last_quality_is_reset_when_a_later_fetch_raises
FAILED tests/test_data_quality.py::test_wholly_empty_bars_dropped_are_reported_in_the_note
FAILED tests/test_data_quality.py::test_fallback_context_reports_degraded_not_unknown
7 failed, 13 passed in 0.58s
```

One failure per behavioural finding — C1 (both aspects), I2, the `inf` minor,
I5, I6, I7. Stash restored afterwards.

The remaining new tests pass against `a00fda4` **by design**, and I am flagging
that rather than implying they caught something:
`test_data_quality_event_emits_both_imputed_and_dropped_keys` and
`test_supervisor_data_feed_degraded_fires_on_a_real_emitted_payload` (I3) guard
correct existing behaviour against a future cleanup — that was the whole point
of the finding; and the I4 counting tests plus
`test_same_direction_burst_is_not_detected_known_limitation` document behaviour
that was already correct (or already a known limitation) but untested.

## 11. Behaviour I deliberately confirmed unchanged

| Probe | Result |
|---|---|
| Energy floor (`0.30`) vs default (`0.15`) | `NG=F` +20% gap → 0 imputed; `SPY` same gap → 1 imputed. Thresholds untouched |
| NaN-guard boundary | exactly 20% NaN still passes, 22.5% raises — the `<` comparison is unchanged |
| Raise strings | byte-identical; relative order (zero-volume before NaN) preserved |
| Indicators, imputed vs clean frame | `sma_20` delta `0.000000000`, `ema_26` delta `0.000000000` — the task's original goal still holds |
| OHLC integrity after imputation | 0 NaN left, `High >= Close`, `Low <= Close`, 40 rows, index monotonic |
| `events.jsonl` round-trip | strict `json.loads` succeeds; no `Infinity`/`NaN` literals |

## 12. Concerns still open (unchanged from the original report, not in scope)

1. **`is_degraded` is a boolean, not a severity.** Task 2's gate should read
   `bars_imputed / bars_total`, not the flag, or one stale tick vetoes trading
   as hard as a wholly corrupt feed. Note that `bars_imputed` is now *larger*
   than before on frames with pre-existing NaN (correctly so) — Task 2 should
   size its tolerance against the honest number, not the old understated one.
2. **Imputation understates genuine gaps**, so any future ATR/volatility sizing
   consuming these bars reads slightly low. Accepted in ADR 0006.
3. **`bars_dropped` remains a deliberate duplicate.** Now safe to remove — but
   only after `supervisor/rules.py::data_feed_degraded` accepts `bars_imputed`,
   and the two I3 tests will fail loudly if it is removed before that.
4. **Rolling-median detector is the real fix** for the leading-corrupt-bar blind
   spot, same-direction bursts, and the over-imputation of the bar after a
   consecutive-corruption pair. All three are pinned by tests and documented in
   ADR 0006; all three remain out of scope here.
5. **`bars_dropped_empty` is reported in `note` only** — not in `is_degraded`
   and not as a typed field. A wholly-empty bar carries no data, so treating it
   as degradation would risk false vetoes; but it does break index contiguity,
   so if Task 2 wants to gate on it, it needs a typed field on `DataQuality`
   rather than string-parsing the note. Called out rather than silently decided.

---

# Fix pass 2 (review round 2)

Branch: `feat/reliability-and-evidence-loop`
Base commit reviewed: `1db0ed4`
Status: **DONE** — F1–F9 all fixed, none declined. Files touched:
`trading_bot/models.py`, `trading_bot/market_data.py`,
`tests/test_data_quality.py`,
`docs/decisions/0006-mark-dont-drop-outlier-bars.md`.
`ig_bridge.py`, `session_manager.py`, `supervisor/**` and `config.py`
untouched (`git diff --stat` lists exactly those four files).

## 0. Every finding reproduced first, against `1db0ed4`

Scratch probe (`$TMPDIR/opencode/repro2_before.py`, not committed) run against
the shipped code before any edit. Real output, trimmed of log lines:

```
F1: 50-bar frame, rows 10-19 wholly empty
  rows returned      : 40
  index step values  : ['0 days 01:00:00', '0 days 11:00:00']   <-- 10-bar hole
  report             : DataQuality(bars_total=40, bars_imputed=0, max_gap_pct=None,
                                   is_degraded=False,
                                   note='10 wholly-empty bar(s) dropped before checks')
  fields available   : ['bars_imputed','bars_total','is_degraded','max_gap_pct','note']

F2: raised: ZeroDivisionError: division by zero
    has imputed_ratio  : False

F3: 3 NaN-OHLC rows, NO outlier -> events: 1
    payload bars_dropped=3 bars_imputed=3 bars_nan_prefilled=3

F4a: Close[19:24] = [101.9, 50.95, 0.0, 51.15, 102.3]
F4b: Close[19:24] = [101.9, 500.95, 900.0, 501.15, 102.3]
     clean truth  = [101.9, 102.0, 102.1, 102.2, 102.3]

F6: report.max_gap_pct = None  (a real 791.97% spike lost)

F8: report = DataQuality(bars_total=40, bars_imputed=0, max_gap_pct=None,
                         is_degraded=False, note=None)
    Volume NaN left in frame : 3

F9: Close[18:23] = [101.8, 101.9, 151.0, 200.1, 200.2]   (true bar 20 = 200.0)

F7: occurrences of 'signed_returns': 2
```

Every number matches the brief's measurements exactly, including
`[101.9, 50.95, 0.0, 51.15, 102.3]` and `[101.9, 500.95, 900.0, 501.15, 102.3]`.
The same probe re-run after the fixes is quoted per-finding below.

## F1 — wholly-empty drops are now a typed field, folded into `is_degraded`

**Changed.** `DataQuality.bars_dropped_empty: int = 0` (additive + defaulted, so
no existing construction site breaks). `_apply_sanity_checks` passes the count
it already received through to the model, and `is_degraded` became
`bars_imputed > 0 or bars_dropped_empty > 0 or bars_volume_filled > 0`. The
human-readable `note` copy is kept; the `DataQuality` docstring now states that
`note` is prose and carries no compatibility guarantee.

**Evidence** — same 50-bar / rows-10-19-empty probe, after:

```
report          : DataQuality(bars_total=40, bars_imputed=0, bars_dropped_empty=10,
                              bars_volume_filled=0, max_gap_pct=None, is_degraded=True,
                              note='10 wholly-empty bar(s) dropped before checks')
fields available: ['bars_dropped_empty','bars_imputed','bars_total',
                   'bars_volume_filled','is_degraded','max_gap_pct','note']
```

`bars_imputed` stays `0` — nothing was fabricated, and that remains true.

Tests: `test_wholly_empty_bar_drops_are_a_typed_field_and_flag_degradation`
(asserts the typed count, `is_degraded is True`, `bars_imputed == 0`, **and**
that the gap is real — `pd.Timedelta(hours=11)` is in the set of index steps)
plus `test_clean_fetch_reports_no_empty_drops` as the control so the field
cannot fire spuriously.

## F2 — `imputed_ratio`, so the documented consumer cannot divide by zero

**Changed.** Added the property to `DataQuality` verbatim as specified. ADR 0006
no longer tells consumers to compute `bars_imputed / bars_total`; two places
that said so now say `imputed_ratio`, and the reason (`bars_total=0` from
`build_fallback_symbol_context`) is recorded.

**Evidence:**

```
bars_total=0 -> 1.0
40/3         -> 0.075
fallback context .imputed_ratio == 1.0      (was ZeroDivisionError)
```

Tests: `test_imputed_ratio_is_one_when_there_are_no_bars` (bare model **and**
the real fallback context) and `test_imputed_ratio_is_the_ratio_when_there_are_bars`
(0.025, 0.25, 0.0, plus agreement with a report from the real code path).

Flagging one design detail rather than leaving it implicit: `imputed_ratio` is a
plain `@property`, as specified, so it does **not** appear in
`model_dump_json()` output — measured
`{"bars_total":40,"bars_imputed":3,"bars_dropped_empty":2,"bars_volume_filled":1,...}`.
It is fully derivable from two serialised ints, so nothing is lost on the wire;
if a future consumer needs it in the JSON it must become a
`pydantic.computed_field`.

## F3 — legacy `bars_dropped` restored to "outliers only"

**Changed.** The event now emits `"bars_dropped": bars_outlier`, with
`bars_imputed`, `bars_nan_prefilled`, `bars_volume_filled` and
`bars_dropped_empty` as the richer signals. A 13-line comment at the emission
site records the rule that depends on it, the measured widening, and
"Do NOT simplify the two to one value", naming the pinning test.
Nothing under `supervisor/` was touched.

**Evidence** — 3 NaN-OHLC rows, no outlier:

| | payload `bars_dropped` | `bars_imputed` |
|---|---|---|
| before | 3 | 3 |
| after | **0** | 3 |

And end-to-end through the real `supervisor.rules.data_feed_degraded`, 5
consecutive cycles of payloads built by the real `_apply_sanity_checks`:

```
outlier   -> fires=True  {'symbol': 'NG=F', 'consecutive_dropped_cycles': 5,
                          'threshold': 5, 'max_age_minutes': 30.0}
nan-only  -> fires=False
```

That is the finding proven in both directions: the rule still fires on the
condition it was written for, and no longer fires on the one it was not.

Tests: `test_event_bars_dropped_counts_outliers_only` (NaN-only → `bars_dropped
== 0` and the rule's literal `int(data.get("bars_dropped", 0)) <= 0`; plus the
converse, 4 NaN + 1 outlier → `bars_imputed == 5` but `bars_dropped == 1`).
`test_data_quality_event_emits_both_imputed_and_dropped_keys` still passes
unchanged — its fixture has one outlier and no NaN, so the two counts coincide;
a comment now says so rather than leaving the equality looking definitional.

## F4 — the two half-truth tests now assert the actual values

Both were passing on behaviour that leaves grossly wrong prices in the frame.
Neither is a new regression; the point is that they would have kept passing if
the fabrication got worse.

**`test_infinite_gap_is_reported_as_none_not_inf`** — its old fixture
(`close[20]=close[21]=0.0`) stopped exercising the inf path once F6 landed,
because that fixture also contains a finite −100% gap which is now correctly
reported (`max_gap_pct=100.0`). Split in two:

- The name and the inf→`None` guarantee moved to a fixture where the *only*
  extreme return is infinite (`close[0] = 0.0`) → `max_gap_pct is None`,
  `gap_pct_max` in the payload `None`. Still a real guard.
- New `test_zero_close_pair_leaves_a_literal_zero_price_in_the_frame` keeps the
  old fixture and asserts the measured output exactly:
  `Close[19:24] == approx([101.9, 50.95, 0.0, 51.15, 102.3])`, plus
  `Close.iloc[21] == 0.0` and `Close.min() == 0.0` — the surviving zero price
  pinned explicitly, not implied.

**`test_same_direction_burst_is_not_detected_known_limitation`** — now asserts
`Close[19:24] == approx([101.9, 500.95, 900.0, 501.15, 102.3])` against the
uncorrupted truth `[101.9, 102.0, 102.1, 102.2, 102.3]`, and adds
`Close.iloc[22] != approx(102.2)` to pin that a **good bar was destroyed**. Its
docstring spells out which of the three wrong bars is which.

## F5 — ADR 0006 no longer understates the burst damage

The ADR's Decision section previously ended on "a contiguous index with one
interpolated bar is strictly less wrong than a silently gapped index". That
paragraph now says the premise is **conditional** and points at a new
"Where the premise fails" section, which quotes the measured numbers:

```
Close[19:24] actual : [101.9, 500.95, 900.0, 501.15, 102.3]
Close[19:24] truth  : [101.9, 102.0,  102.1, 102.2,  102.3]
```

and states plainly that 3 of 5 bars are wrong afterwards while only 1 of the 3
was wrong before — bar 20 "repaired" to garbage (500.95, ~5x true), bar 21
surviving corrupt at 900.0, and bar 22, **a good bar with true close 102.2,
overwritten with 501.15**. The zero-close pair is documented alongside it. The
conclusion is not spun: *"for a multi-bar burst the imputer both fails to remove
the corruption and adds new corruption of its own, including overwriting data
that was correct… 'less wrong' is only true for isolated single-tick
corruption. For bursts, both designs are wrong and this one is wrong in more
places."* The decision is still recorded as standing, with the reason (a gapped
index corrupts every indicator on every symbol silently; burst damage is
localised and flagged) — but as a trade of one wrong answer for another, not a
fix. It also records that `imputed_ratio` **understates** burst damage: 2/40
reported for a window with 3 wrong bars.

The old text asserting "Bar 20 is imputed and bar 21 keeps its 900.0" — the
one-bad-bar implication — is gone.

## F6 — finite gaps survive a co-occurring `inf`

**Changed** to exactly the specified form: filter to finite first, then max.
`import math` became unused and was removed.

**Evidence** — `close[10]=900.0` (real spike) with `close[20]=close[21]=0.0`:

| | `max_gap_pct` |
|---|---|
| before | `None` |
| after | **`791.97`** |

The inf→`None` guarantee is intact: when *every* flagged gap is non-finite the
field is still `None` (`test_infinite_gap_is_reported_as_none_not_inf`), and a
real `JsonEventLogger` round-trip through a temp `events.jsonl` still parses
with strict `json.loads` and contains no `Infinity`/`NaN` literal. New test:
`test_finite_gap_survives_a_co_occurring_infinite_gap`.

## F7 — `signed_returns` inlined

`signed_returns = close.pct_change(); returns = signed_returns.abs()` →
`returns = close.pct_change().abs()`. Probe: occurrences of `signed_returns`
went `2` → `0`.

## F8 — decision: fill `Volume`, count it in its own field

Both options were on the table. I checked what actually consumes `Volume`
before choosing (`indicators._volume_ratio`, `indicators._obv_slope`,
`signals._vwap_deviation_bps`) and measured the blast radius:

```
volume.rolling(20).mean() NaN count: 3 NaN volumes -> 27 of 60 NaN positions
_volume_ratio / _obv_slope with NaN at bars 5-7, 45, 57-59, 59: all still return
  a number -- both call .dropna() defensively
```

So the honest position, which the ADR now records: the *current* consumers
degrade gracefully rather than producing garbage — `_volume_ratio` silently
falls back to an older bar's ratio and labels it current, `_obv_slope` fits over
fewer points. The material defect was **invisibility**
(`is_degraded=False, note=None`), plus the exposure of any future volume
indicator.

**Chose to fill**, because the alternative — reporting it in `note` only — would
have put the sole machine-readable signal for volume NaN into a prose string in
the very same commit where F1 removes exactly that anti-pattern. Counted in a
new `bars_volume_filled` field rather than in `bars_imputed`, so `bars_imputed`
and `imputed_ratio` keep the meaning the F2 docstring promises ("fabricated
OHLC"). Folded into `is_degraded` and `note`. Only NaN is filled — no real
volume is overwritten, and outlier blanking still touches OHLC only, so a
corrupt-price bar keeps its true traded volume.

**Evidence** — 3 NaN-Volume rows:

```
before: DataQuality(bars_total=40, bars_imputed=0, max_gap_pct=None,
                    is_degraded=False, note=None)          Volume NaN left: 3
after : DataQuality(bars_total=40, bars_imputed=0, bars_dropped_empty=0,
                    bars_volume_filled=3, max_gap_pct=None, is_degraded=True,
                    note='3 NaN volume bar(s) interpolated') Volume NaN left: 0
```

Tests: `test_nan_volume_is_filled_and_counted` (typed count, `is_degraded`,
`bars_imputed == 0`, `imputed_ratio == 0.0`, no NaN left, OHLC untouched),
`test_nan_volume_is_reported_in_the_event_payload` (payload key present and
`bars_dropped == 0`, so this cannot leak into the supervisor streak), and
`test_zero_volume_frames_are_untouched_by_the_volume_fill` (forex all-zero
volume is not NaN → nothing filled, report clean).

Guards verified un-defeated by the new fill: `>20% NaN Volume` still raises
(`SPY: >20% NaN rows after fetch`), and the zero-volume raise still precedes it.

## F9 — value assertion on the rollover bar

`test_one_way_level_shift_counts_one_bar` now asserts
`Close.iloc[20] == approx(151.0)` (true value 200.0) and
`max_gap_pct == approx(96.27)`, plus that both neighbours are untouched
(101.9 / 200.1). The docstring names this as the concrete number behind ADR
0006's "imputation understates genuine gaps" consequence — roughly half the real
move flattened.

## Test commands and real output

`.venv/bin/pytest tests/test_data_quality.py tests/test_market_data.py -v`:

```
tests/test_data_quality.py::test_outlier_bar_is_not_dropped_index_stays_contiguous PASSED [  3%]
tests/test_data_quality.py::test_outlier_close_is_replaced_with_interpolated_value PASSED [  6%]
tests/test_data_quality.py::test_quality_report_counts_imputed_bars PASSED [  9%]
tests/test_data_quality.py::test_clean_frame_reports_no_degradation PASSED [ 12%]
tests/test_data_quality.py::test_nan_guard_raises_even_when_an_outlier_is_present PASSED [ 15%]
tests/test_data_quality.py::test_nan_guard_raises_without_an_outlier PASSED [ 18%]
tests/test_data_quality.py::test_zero_volume_still_raises_and_takes_precedence PASSED [ 21%]
tests/test_data_quality.py::test_forex_exemption_from_zero_volume_still_applies PASSED [ 24%]
tests/test_data_quality.py::test_pre_existing_nan_bars_are_counted_as_imputed PASSED [ 27%]
tests/test_data_quality.py::test_one_way_level_shift_counts_one_bar PASSED [ 30%]
tests/test_data_quality.py::test_two_separate_spikes_count_two_bars PASSED [ 33%]
tests/test_data_quality.py::test_two_consecutive_corrupt_bars_are_both_imputed PASSED [ 36%]
tests/test_data_quality.py::test_leading_corrupt_bar_documented_expectation PASSED [ 39%]
tests/test_data_quality.py::test_same_direction_burst_is_not_detected_known_limitation PASSED [ 42%]
tests/test_data_quality.py::test_data_quality_event_emits_both_imputed_and_dropped_keys PASSED [ 45%]
tests/test_data_quality.py::test_event_bars_dropped_counts_outliers_only PASSED [ 48%]
tests/test_data_quality.py::test_supervisor_data_feed_degraded_fires_on_a_real_emitted_payload PASSED [ 51%]
tests/test_data_quality.py::test_infinite_gap_is_reported_as_none_not_inf PASSED [ 54%]
tests/test_data_quality.py::test_finite_gap_survives_a_co_occurring_infinite_gap PASSED [ 57%]
tests/test_data_quality.py::test_zero_close_pair_leaves_a_literal_zero_price_in_the_frame PASSED [ 60%]
tests/test_data_quality.py::test_last_quality_is_reset_when_a_later_fetch_raises PASSED [ 63%]
tests/test_data_quality.py::test_wholly_empty_bars_dropped_are_reported_in_the_note PASSED [ 66%]
tests/test_data_quality.py::test_wholly_empty_bar_drops_are_a_typed_field_and_flag_degradation PASSED [ 69%]
tests/test_data_quality.py::test_clean_fetch_reports_no_empty_drops PASSED [ 72%]
tests/test_data_quality.py::test_imputed_ratio_is_one_when_there_are_no_bars PASSED [ 75%]
tests/test_data_quality.py::test_imputed_ratio_is_the_ratio_when_there_are_bars PASSED [ 78%]
tests/test_data_quality.py::test_nan_volume_is_filled_and_counted PASSED [ 81%]
tests/test_data_quality.py::test_nan_volume_is_reported_in_the_event_payload PASSED [ 84%]
tests/test_data_quality.py::test_zero_volume_frames_are_untouched_by_the_volume_fill PASSED [ 87%]
tests/test_data_quality.py::test_fallback_context_reports_degraded_not_unknown PASSED [ 90%]
tests/test_market_data.py::test_sanity_filter_imputes_extreme_price_gap_without_dropping PASSED [ 93%]
tests/test_market_data.py::test_sanity_filter_rejects_all_zero_volume PASSED [ 96%]
tests/test_market_data.py::test_sanity_filter_passes_clean_data PASSED   [100%]

============================== 33 passed in 0.75s ==============================
```

`.venv/bin/pytest -q`:

```
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl - ass...
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation - asser...
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick
8 failed, 626 passed in 4.15s
```

**8 failed → 8 failed (identical test names), 616 passed → 626 passed (+10).**
No regressions. The 8 are the known macOS platform artifacts
(`/proc/self/status`, `/bin/true`) and were not touched, per instruction.

## The new tests are real guards

Stashed **only** `trading_bot/market_data.py` and `trading_bot/models.py`
(keeping the new tests) and re-ran against `1db0ed4`:

```
FAILED tests/test_data_quality.py::test_event_bars_dropped_counts_outliers_only
FAILED tests/test_data_quality.py::test_finite_gap_survives_a_co_occurring_infinite_gap
FAILED tests/test_data_quality.py::test_wholly_empty_bar_drops_are_a_typed_field_and_flag_degradation
FAILED tests/test_data_quality.py::test_clean_fetch_reports_no_empty_drops
FAILED tests/test_data_quality.py::test_imputed_ratio_is_one_when_there_are_no_bars
FAILED tests/test_data_quality.py::test_imputed_ratio_is_the_ratio_when_there_are_bars
FAILED tests/test_data_quality.py::test_nan_volume_is_filled_and_counted
FAILED tests/test_data_quality.py::test_nan_volume_is_reported_in_the_event_payload
FAILED tests/test_data_quality.py::test_zero_volume_frames_are_untouched_by_the_volume_fill
9 failed, 24 passed in 0.64s
```

One or more failures per behavioural finding — F1 (+ its control), F2, F3, F6,
F8. Stash restored afterwards and re-verified green.

**Stated plainly rather than implied:** the F4, F5 and F9 assertions pass
against `1db0ed4` **by design**. The brief said so and it is true — the
behaviour they pin is byte-identical pre- and post-fix. They caught nothing;
their job is to stop the fabrication silently getting worse, and to make the
ADR's honesty checkable in code.

## Behaviour deliberately re-confirmed unchanged

| Probe | Result |
|---|---|
| All four `ValueError` strings | byte-identical: `SPY: all rows have zero volume`, `SPY: >20% NaN rows after fetch`, `SPY: missing columns ['Volume']`, `no market data returned for SPY` |
| Guard order | zero-volume still raised in preference to the NaN guard |
| NaN-guard boundary | 17.5% ok, exactly 20.0% ok, 22.5% raises — the `<` comparison unchanged |
| Energy floor 0.30 vs default 0.15 | `NG=F` +20% gap → 0 imputed; `SPY` same gap → 1 |
| Indicators, imputed vs clean frame | `sma_20`/`ema_26`/`rsi_14`/`atr_14` delta `0.000000000000` — the task's original goal still holds |
| OHLC + Volume integrity after imputation | 60 rows, 0 NaN, `High >= Close`, `Low <= Close`, index monotonic |
| `events.jsonl` round-trip (real `JsonEventLogger`) | strict `json.loads` parses; no `Infinity`/`NaN` literal |
| Edge cases | single-row frame and flat series: no crash, no div-by-zero |
| Forex + partial NaN volume | no zero-volume raise, volume filled, `bars_volume_filled=2` |
| Scope | `git diff --stat` = 4 files; `supervisor/`, `config.py`, `ig_bridge.py`, `session_manager.py` untouched; no risk default moved; no secret staged |

## Concerns still open (not in scope)

1. **`imputed_ratio` understates burst damage.** It counts flagged bars, and the
   burst cases prove more bars are wrong than are flagged (2/40 reported for a
   window with 3 wrong bars). A Task 2 gate keyed on it is directionally right
   but not a bound. Recorded in ADR 0006.
2. **`imputed_ratio` is not serialised** (plain `@property`, as specified). Both
   inputs are, so nothing is lost on the wire — but a consumer reading a
   persisted `DataQuality` JSON has to divide, and must remember the
   `bars_total == 0` case. Promote to `computed_field` if that becomes a real
   consumer.
3. **`bars_volume_filled` fabricates volume.** Current consumers `dropna()`, so
   this replaces silent staleness with a plausible-looking invented number. It
   is counted and `is_degraded` fires, which is the improvement; it is not
   "correct". A volume-surge signal built on these bars would read suppressed,
   not spiked.
4. **`is_degraded` is now broader.** It fires for empty-bar drops and NaN volume
   in addition to imputed OHLC. That is deliberate — all three break something
   downstream — but it means Task 2 must gate on `imputed_ratio` and the
   individual counts, not on the flag, or a single dropped halted-session bar
   will veto trading as hard as a wholly corrupt feed.
5. **Rolling-median detector remains the real fix** for the leading-corrupt-bar
   blind spot, same-direction bursts, the zero-close pair, and the
   over-imputation after a consecutive-corruption pair. All four are pinned by
   value-asserting tests and documented in ADR 0006; all four remain out of
   scope. Those tests will fail loudly when it lands — the intended signal to
   revisit the ADR.
