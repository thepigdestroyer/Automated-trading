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

**That premise is conditional, and the condition matters.** It holds for
*isolated single-tick* corruption. It does **not** hold for a multi-bar burst,
where the imputer makes the frame actively worse than the corruption it was
handed — including destroying good bars. Measured, not argued: see
"Where the premise fails" under Consequences. The decision stands anyway,
because a gapped index corrupts *every* indicator on *every* symbol silently,
whereas burst damage is localised and `is_degraded` fires — but this is a
trade of one wrong answer for a different wrong answer, not a fix.

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
  interpolate from. Those rows are dropped, and the count is a **typed field**,
  `DataQuality.bars_dropped_empty`, folded into `is_degraded`. It was
  originally reported only in `note`; that was wrong — `note` is prose for
  humans and string-parsing it is not a contract, and a gapped index is the
  exact failure this ADR exists to eliminate, so it cannot be the one thing
  only reported in English. Measured before the fix: a 50-bar frame with rows
  10-19 empty returned 40 rows with an 11-hour step in a 1-hour index, and
  reported `bars_imputed=0, is_degraded=False`. `bars_total` is measured
  *after* the drop.
- **Consumers must not parse `note`.** Every count is a typed field:
  `bars_total`, `bars_imputed`, `bars_dropped_empty`, `bars_volume_filled`,
  `max_gap_pct`. `note` is a human-readable summary with no compatibility
  guarantee.
- **Use `DataQuality.imputed_ratio`, not `bars_imputed / bars_total`.** An
  earlier version of this ADR told consumers to do the division directly. That
  advice was unsafe: `build_fallback_symbol_context` reports `bars_total=0`, so
  the documented recipe raised `ZeroDivisionError` on the very context it was
  meant to judge. `imputed_ratio` returns `1.0` when `bars_total <= 0`, so a
  missing history reads as fully degraded. Pinned by
  `test_imputed_ratio_is_one_when_there_are_no_bars` and
  `test_imputed_ratio_is_the_ratio_when_there_are_bars`.
- Degradation becomes visible to the executor via the Task 2 degraded-inputs gate.
- `data_quality` events carry `bars_imputed`, `bars_nan_prefilled`,
  `bars_volume_filled` and `bars_dropped_empty` as the rich signals, plus the
  legacy `bars_dropped` key — which carries the **outlier count only**, not
  `bars_imputed`. `supervisor.rules.data_feed_degraded` reads `bars_dropped`
  and breaks its streak on `<= 0`, and its docstring defines it as the
  "bars-dropped" (outlier) pattern. Removing the key would silently disable
  that rule; setting it to `bars_imputed` silently *widened* it, because
  `bars_imputed` also counts bars that arrived NaN. Measured: a frame with 3
  NaN-OHLC bars and no outlier at all emitted no event before the
  mark-don't-drop change, and afterwards emitted `bars_dropped: 3` — feeding a
  5-cycle alert streak for a condition the rule was never written to catch.
  The narrow meaning is pinned by
  `test_event_bars_dropped_counts_outliers_only`, and the key's continued
  presence by `test_data_quality_event_emits_both_imputed_and_dropped_keys`
  plus a test that feeds a payload built by the *real* `_apply_sanity_checks`
  into `data_feed_degraded`. Do not collapse or delete either key without
  changing that rule first. `data_feed_stale` keys on `last_good_ts` and is
  unaffected.
- `max_gap_pct` is `None` rather than `inf` when the previous close was zero.
  `inf` is not representable in JSON: pydantic renders it as `null` anyway and
  `json.dumps` would write the non-standard literal `Infinity` into
  `events.jsonl`. The non-finite gaps are filtered out *before* the max is
  taken, not checked after: `np.nanmax([1.0, inf])` is `inf`, so the earlier
  `isfinite(max)` version blanked the whole field whenever any flagged bar had
  a zero predecessor, discarding a co-occurring real spike (measured: a genuine
  791.97% gap reported as `None`). If every flagged gap is non-finite the field
  is `None`; otherwise it is the largest finite gap.
- **NaN `Volume` is filled and counted.** The outlier/NaN mask covers OHLC, but
  the `>20% NaN` guard covers every column, so a frame with a few NaN-Volume
  rows passed the guard and then reported `bars_imputed=0, is_degraded=False,
  note=None` while carrying its NaN volume into the volume indicators
  (`rolling(20).mean()` alone turns 3 NaN into 27 NaN positions out of 60).
  Decision: interpolate Volume in the same pass, and count it in its own field
  `bars_volume_filled` rather than in `bars_imputed`, so `bars_imputed` and
  `imputed_ratio` keep meaning "fabricated **OHLC**". `bars_volume_filled`
  folds into `is_degraded`. Only NaN is filled — no real volume is ever
  overwritten, and outlier blanking still touches OHLC only, so a
  corrupt-price bar keeps its true traded volume. Note this fabricates volume:
  the current consumers (`_volume_ratio`, `_obv_slope`) both `dropna()`
  defensively, so the pre-fix damage was silent staleness rather than garbage
  — but the invisibility (`is_degraded=False`) was the real defect, and future
  volume indicators should not have to defend themselves.
- `build_fallback_symbol_context` reports
  `DataQuality(bars_total=0, is_degraded=True)`. That context has no price
  history at all, so `None` ("quality unknown") would have read as
  not-degraded to a risk gate. Its `imputed_ratio` is `1.0`.
- `fetch_history` clears `_last_quality` on entry, so a raising fetch cannot
  leave the previous symbol's report visible to a caller.
- `is_degraded` is a boolean, not a severity: it is `True` for 1 imputed bar
  out of 1000 just as for 900. Any consumer needing a tolerance threshold
  should read `imputed_ratio`.
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
### Where the premise fails

An earlier revision of this ADR described the **same-direction burst** as "bar
20 is imputed and bar 21 keeps its 900.0", implying one bad bar survives. That
understated the damage by a factor of three. The measured result, on a clean
ramp `100.0 + 0.1i` with `close[20] = close[21] = 900.0`:

```
Close[19:24] actual : [101.9, 500.95, 900.0, 501.15, 102.3]
Close[19:24] truth  : [101.9, 102.0,  102.1, 102.2,  102.3]
report              : bars_total=40 bars_imputed=2 max_gap_pct=783.22 is_degraded=True
```

Three of those five bars are wrong afterwards, and only one of the three was
wrong before:

- Bar 20 was corrupt (900.0) and is "repaired" to **500.95** — the midpoint
  between a good bar and a corrupt one. Still garbage, ~5x the true price.
- Bar 21 was corrupt and **survives untouched at 900.0**, because relative to
  the corrupt bar 20 it looks flat, so its return is never extreme.
- Bar 22 was a **good bar** (true close 102.2) and is **destroyed**, overwritten
  with **501.15**, because the reversal off the corrupt 900.0 reads as extreme
  and the interpolation reference is the corrupt bar 21.

The same mechanism appears with a zero-close pair (`close[20] = close[21] =
0.0`): measured `[101.9, 50.95, 0.0, 51.15, 102.3]`, where a **literal 0.0
price survives** into the frame — bar 21's return is `(0-0)/0 = nan` and is
never flagged — and its two neighbours are fabricated at roughly half their
true values.

This is a direct counter-example to the "strictly less wrong" framing above, and
it is stated here rather than buried: for a multi-bar burst the imputer both
fails to remove the corruption and adds new corruption of its own, including
overwriting data that was correct. The old drop-based code would have deleted
bars 20 and 22 and left 900.0 adjacent to 101.9 — also wrong, and additionally
index-gapping, which is why this decision stands. But "less wrong" is only true
for isolated single-tick corruption. For bursts, both designs are wrong and this
one is wrong in more places.

Consequences of accepting this:

- Any consumer must treat `is_degraded=True` as "these prices may be
  fabricated", not "these prices have been corrected". `imputed_ratio`
  understates burst damage: 2/40 was reported for a window where 3 bars are
  wrong, and the count can never include bars the detector did not flag.
- Both cases are pinned by tests asserting the **exact measured values** —
  `test_same_direction_burst_is_not_detected_known_limitation` and
  `test_zero_close_pair_leaves_a_literal_zero_price_in_the_frame` — not merely
  the surviving-bad-bar half. A test that only checked `bars_imputed` and "the
  spike is gone" would keep passing while the fabrication got worse; both
  earlier versions of these tests did exactly that.
- Both limitations, and the leading-corrupt-bar blind spot, share one root
  cause: the detector's reference price is the immediately preceding bar, which
  may itself be corrupt. A rolling-median reference would fix the leading bar,
  same-direction bursts, the zero-close pair, and the over-imputation of the bar
  following a consecutive-corruption pair in one change. That is a separate
  design decision and deliberately out of scope here; the tests above will fail
  loudly when it lands, which is the intended signal to revisit this ADR.
