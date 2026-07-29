# 0006 — Mark, don't drop, outlier bars

Date: 2026-07-28
Status: accepted

## Context

`YahooMarketDataClient._apply_sanity_checks` dropped bars whose absolute
return exceeded max(10x median, 15%/30% floor). Roughly 2.4 bars per cycle
on energy symbols (693 in a 15-day window) were removed. Dropping a row
removes its timestamp from the index, so every subsequent `rolling()` and
`ewm()` computed over a discontinuous series while still being labelled as
an N-period window. Indicators were therefore subtly wrong on exactly the
symbols the bot traded most.

## Decision

Keep every bar. Blank the corrupt OHLC values and time-interpolate them,
preserving index continuity. Emit a `DataQuality` report per fetch
(`bars_total`, `bars_imputed`, `max_gap_pct`, `is_degraded`) and attach it
to `SymbolContext`.

Imputation is not "correct" — a genuine rollover gap is real price movement
and interpolating it understates the move. But the filter only fires on
ticks already classified as corrupt, and a contiguous index with one
interpolated bar is strictly less wrong than a silently gapped index that
misaligns every window.

An extreme return is a property of a *pair* of bars, not of one bar, so a
single corrupted tick registers twice: the jump into it and the reversal
back out. Only the corrupted bar is imputed — the following bar is left
untouched when it **reverts to the pre-spike level**, which is the signature
of a one-bar glitch. Imputing it as well would discard good data and double
the reported degradation.

Reverting-to-level, not a sign flip, is the test. An earlier version skipped
the next bar whenever its extreme return merely changed sign; two genuinely
corrupt consecutive bars do exactly that, so `900.0, 10.0` left the 10x-low
price sitting in the frame. Requiring a reversion means such a pair is fully
imputed, at the cost of also imputing the (good) bar after it — because the
reference for that comparison is the immediate predecessor, which is itself
corrupt. Over-imputing one good bar is strictly cheaper than leaving a price
that is wrong by an order of magnitude.

The guards (`all rows have zero volume`, `>20% NaN rows after fetch`) run
**before** imputation. They must: the interpolation fills every NaN in the
OHLC block, not only the outlier rows, so evaluating the NaN guard afterwards
let a mostly-empty frame through whenever an outlier happened to be present —
the imputation had already erased the evidence. Bars that were already NaN on
arrival are fabricated by that same pass, so they count towards
`bars_imputed` too.

## Consequences

- Index contiguity is preserved *through outlier handling*: no bar is removed
  because its values looked wrong, so the sanity filter no longer silently
  misaligns rolling/EWM windows. This is not an absolute guarantee of a
  gap-free index. `fetch_history` still calls `dropna(how="all")`, which
  removes wholly-empty bars — Yahoo emits these for halted or missing
  intraday sessions, and a bar with no OHLC and no volume carries nothing to
  interpolate from. Those rows are dropped but no longer invisible: the count
  is reported in `DataQuality.note` as `N wholly-empty bar(s) dropped before
  checks`. Note that `bars_total` is measured *after* that drop.
- Degradation becomes visible to the executor via the Task 2 degraded-inputs gate.
- `data_quality` events now carry `bars_imputed`. They also keep emitting
  `bars_dropped` (same count) because `supervisor.rules.data_feed_degraded`
  keys on `bars_dropped` and breaks its streak when the key is absent —
  renaming it outright would have silently disabled that rule. The
  `data_feed_stale` rule keys on `last_good_ts` and is unaffected. The
  duplicate key is pinned by
  `tests/test_data_quality.py::test_data_quality_event_emits_both_imputed_and_dropped_keys`
  and by a second test that feeds a payload built by the *real*
  `_apply_sanity_checks` into `data_feed_degraded`; do not delete it as
  redundant without first changing that rule.
- `max_gap_pct` is `None` rather than `inf` when the previous close was zero.
  `inf` is not representable in JSON: pydantic renders it as `null` anyway and
  `json.dumps` would write the non-standard literal `Infinity` into
  `events.jsonl`.
- `build_fallback_symbol_context` reports
  `DataQuality(bars_total=0, is_degraded=True)`. That context has no price
  history at all, so `None` ("quality unknown") would have read as
  not-degraded to a risk gate.
- `fetch_history` clears `_last_quality` on entry, so a raising fetch cannot
  leave the previous symbol's report visible to a caller.
- `is_degraded` is a boolean, not a severity: it is `True` for 1 imputed bar
  out of 1000 just as for 900. The ratio is carried in `note`; any consumer
  needing a tolerance threshold should read `bars_imputed / bars_total`.
- Known limitation, unchanged from the drop-based version: a `pct_change`
  detector cannot validate the *first* bar, which has no predecessor. If bar
  0 is the corrupt one it survives, and bar 1 is imputed instead — measured
  as bar 0 = 900.0 (left corrupt), bar 1 interpolated to 500.1 between it and
  bar 2. The old code dropped bar 1 and left the same 900.0 in place, so this
  is not a new failure, but neither version handles it. `is_degraded` does
  fire in this case, so the condition is at least visible. Note the change in
  failure mode: where the old code *deleted* the neighbouring bar, the new
  code *fabricates* a value for it — bar 1 becomes a midpoint between a
  corrupt and a good price, and a blanked leading bar is fabricated by
  back-fill from the first valid bar rather than removed. Fabricated-but-
  present is the behaviour this ADR chose; it is not the same as correct.
  Pinned by `test_leading_corrupt_bar_documented_expectation`.
- Known limitation, not addressed by either version: a **same-direction
  burst**. Two consecutive corrupt bars at the same wrong level (`900.0,
  900.0`) produce only one extreme return — the jump in — because the second
  bar looks flat relative to the first. Bar 20 is imputed and bar 21 keeps its
  900.0. Detecting this requires a reference that does not depend on the
  immediately preceding bar (e.g. a rolling median), which is a separate
  design decision and deliberately out of scope. Pinned by
  `test_same_direction_burst_is_not_detected_known_limitation` so the gap is
  explicit rather than assumed.
- Both limitations share one root cause: the detector's reference price is the
  previous bar, which may itself be corrupt. A rolling-median reference would
  fix the leading bar, same-direction bursts, and the over-imputation of the
  bar following a consecutive-corruption pair in one change.
