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
back out. Only the corrupted bar is imputed — when the following return is
also extreme and of the opposite sign, that bar is genuine and is left
untouched. Imputing it as well would discard good data and double the
reported degradation.

## Consequences

- Rolling/EWM windows are time-aligned again.
- Degradation becomes visible to the executor via the Task 2 degraded-inputs gate.
- `data_quality` events now carry `bars_imputed`. They also keep emitting
  `bars_dropped` (same count) because `supervisor.rules.data_feed_degraded`
  keys on `bars_dropped` and breaks its streak when the key is absent —
  renaming it outright would have silently disabled that rule. The
  `data_feed_stale` rule keys on `last_good_ts` and is unaffected.
- `is_degraded` is a boolean, not a severity: it is `True` for 1 imputed bar
  out of 1000 just as for 900. The ratio is carried in `note`; any consumer
  needing a tolerance threshold should read `bars_imputed / bars_total`.
- Known limitation, unchanged from the drop-based version: a `pct_change`
  detector cannot validate the *first* bar, which has no predecessor. If bar
  0 is the corrupt one it survives, and bar 1 is imputed instead — measured
  as bar 0 = 900.0 (left corrupt), bar 1 interpolated to 500.1 between it and
  bar 2. The old code dropped bar 1 and left the same 900.0 in place, so this
  is not a new failure, but neither version handles it. Fixing it needs a
  different detector (e.g. compare each bar against a rolling median rather
  than its immediate predecessor) and is deliberately out of scope here.
  `is_degraded` does fire in this case, so the condition is at least visible.
