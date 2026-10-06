# 0007 — One trend rule for live and backtest

Date: 2026-10-06
Status: accepted

## Context

A vectorised backtester is next (Task 7), and Track-A universe contexts
(Task 8) also need per-bar trend verdicts. The live bot classifies the daily
trend with `build_daily_trend`, which was a scalar function: one verdict for
the last bar of the frame it is handed. A backtester needs a verdict for
every bar.

A second, "equivalent" vectorised implementation written next to the scalar
one would drift. The rule has several details that are easy to get slightly
wrong and that no summary statistic would show: the Donchian channel
excludes the current bar; the sma50 slope compares against the value 5
*valid closes* back, and only once there are 55 of them; closes are
`dropna()`'d while High/Low are not; bars with fewer than 30 valid closes
are neutral; the ADX proxy reports the last non-NaN smoothed value, clipped
to [0, 100]. A backtest of a slightly different rule describes a strategy we
do not trade, and its results would look plausible anyway.

## Decision

`trading_bot/daily_trend.py::compute_trend_frame(daily)` is the only
implementation of the daily-trend rule. It returns one row per bar, indexed
identically to `daily`: `direction`, `donchian_breakout`,
`sma_50_200_cross`, `trend_strength` and `close_vs_sma50_bps` (both
unrounded), plus `sufficient_data`. `build_daily_trend` is now a thin
wrapper. It builds the `DailyTrend` from the frame's last row, rounding and
formatting the summary exactly as before. The live path and the backtester
therefore run the same code.

**No lookahead is a guaranteed, tested property.** Row `t` equals
`build_daily_trend(daily.iloc[:t+1])`. Every input is a trailing window
(`rolling`, `shift(+k)`, `ffill`), and a bar with a NaN close is judged on
the most recent valid close, just as the prefix's `dropna()` would.
`tests/test_daily_trend_frame.py::test_matches_build_daily_trend_at_earlier_cutoffs`
enforces this at **every bar** of four synthetic series: the brief's
fixture, a rally-then-sell-off, a driftless random walk, and a deliberately
malformed series with NaN closes/highs/lows, a zero-ATR flat stretch and
inverted High<Low bars. `test_future_bars_cannot_change_past_rows` checks
directly that scrambling the bars after `t` leaves rows `0..t` bit-identical.

Because `build_daily_trend` now delegates to the frame, comparing the two
alone would catch lookahead but not a rule change applied to both. The test
therefore also compares every prefix against a frozen copy of the
pre-refactor scalar implementation (commit 707d177). Mutation checks
confirmed that the suite catches each of these: dropping the channel's
`shift(1)`, a 4-bar slope lookback, skipping the 30-close neutral rule,
skipping ADX clipping, zero-filling instead of forward-filling ADX, a
default `min_periods` on the channel, and not carrying the last valid close
forward.

## Consequences

- Any change to the trend rule is made once, in `compute_trend_frame`, and
  applies to live and backtest together. A deliberate rule change must also
  update the frozen reference in the test; that edit makes the change
  visible in review.
- `build_daily_trend` now computes the whole frame to read one row. On daily
  frames of a few hundred bars this costs about 2 ms (measured at 500 bars) and is
  irrelevant at the bot's cycle cadence.
- The old `len < period + 2 -> 0.0` ADX guard was dropped. It could never
  fire: the smoothed value needs `2 * period - 1` bars, and every bar with
  fewer than 30 closes is neutral anyway. Mutating it away changed no
  result.
- Behaviour is unchanged except for one way a frame can fail. A frame with
  at least 30 closes but no `High`/`Low` column used to raise `KeyError` in
  `build_daily_trend`, and still does. `compute_trend_frame` raises the same
  error for such a frame even when only its later bars have 30 closes.
