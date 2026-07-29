"""Outlier bars must be neutralised in place, never dropped.

Dropping rows breaks index continuity, which silently corrupts every
rolling/EWM indicator computed downstream.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trading_bot.config import Settings
from trading_bot.market_data import YahooMarketDataClient
from trading_bot.models import DataQuality

OHLC = ["Open", "High", "Low", "Close"]


def _frame_from_closes(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.001 for c in closes],
            "Low": [c * 0.999 for c in closes],
            "Close": closes,
            "Volume": [1000.0] * len(closes),
        },
        index=idx,
    )


def _frame_with_spike() -> pd.DataFrame:
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0  # corrupted tick
    return _frame_from_closes(close)


def _client(event_logger: Any | None = None) -> YahooMarketDataClient:
    return YahooMarketDataClient(Settings(_env_file=None), event_logger=event_logger)


class _RecordingEventLogger:
    """Mirrors trading_bot.json_log.JsonEventLogger.log's signature.

    The production call site swallows every exception, so a signature
    mismatch here would show up as "no events recorded" rather than an error.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, data: Any, *, cycle_id: str | None = None) -> None:
        self.events.append((event, data))


def test_outlier_bar_is_not_dropped_index_stays_contiguous():
    client = _client()
    frame = _frame_with_spike()
    original_index = frame.index.copy()

    client._apply_sanity_checks(frame, "TEST")

    assert len(frame) == 40
    assert frame.index.equals(original_index)


def test_outlier_close_is_replaced_with_interpolated_value():
    client = _client()
    frame = _frame_with_spike()

    client._apply_sanity_checks(frame, "TEST")

    spiked = float(frame["Close"].iloc[20])
    assert spiked < 200.0
    assert 101.0 < spiked < 105.0


def test_quality_report_counts_imputed_bars():
    client = _client()
    frame = _frame_with_spike()

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_total == 40
    assert report.bars_imputed == 1
    assert report.is_degraded is True
    assert report.max_gap_pct is not None and report.max_gap_pct > 100.0


def test_clean_frame_reports_no_degradation():
    client = _client()
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 0
    assert report.is_degraded is False
    assert report.note is None


# --------------------------------------------------------------------------
# C1: the >20% NaN guard must not be defeated by the imputation pass.
# --------------------------------------------------------------------------


def _frame_with_nan_rows(nan_rows: range | tuple, spike_at: int | None = None):
    close = [100.0 + i * 0.1 for i in range(40)]
    if spike_at is not None:
        close[spike_at] = 900.0
    frame = _frame_from_closes(close)
    frame.iloc[list(nan_rows), [frame.columns.get_loc(c) for c in OHLC]] = np.nan
    return frame


def test_nan_guard_raises_even_when_an_outlier_is_present():
    """Regression: the guard used to run AFTER imputation.

    Imputation fills the whole OHLC block, so it erased every pre-existing
    NaN -- but only on the code path where an outlier was found. A frame with
    37.5% NaN rows therefore passed silently (reporting bars_imputed=1, a ~15x
    understatement) whenever it also happened to contain one outlier.
    """
    with pytest.raises(ValueError, match=r">20% NaN rows after fetch"):
        _client()._apply_sanity_checks(
            _frame_with_nan_rows(range(15), spike_at=25), "TEST"
        )


def test_nan_guard_raises_without_an_outlier():
    """The control case for the test above: same frame, no outlier."""
    with pytest.raises(ValueError, match=r">20% NaN rows after fetch"):
        _client()._apply_sanity_checks(_frame_with_nan_rows(range(15)), "TEST")


def test_zero_volume_still_raises_and_takes_precedence():
    """The zero-volume check must survive the guard reordering, and still be
    raised in preference to the NaN guard when both conditions hold."""
    frame = _frame_with_nan_rows(range(15))
    frame["Volume"] = 0.0
    with pytest.raises(ValueError, match=r"all rows have zero volume"):
        _client()._apply_sanity_checks(frame, "TEST")


def test_forex_exemption_from_zero_volume_still_applies():
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])
    frame["Volume"] = 0.0

    report = _client()._apply_sanity_checks(frame, "JPY=X")

    assert report.bars_total == 40
    assert report.is_degraded is False


def test_pre_existing_nan_bars_are_counted_as_imputed():
    """Below the 20% ceiling the frame is accepted, but the NaN bars are
    fabricated by the same interpolation pass as the outlier, so they must be
    counted -- otherwise `bars_imputed` understates the fabrication."""
    client = _client()
    frame = _frame_with_nan_rows(range(5, 9), spike_at=25)  # 4 NaN (10%) + 1 spike

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 5
    assert report.is_degraded is True
    assert "4 already NaN on arrival" in (report.note or "")
    # And they really were filled in place -- no row lost, no NaN left.
    assert len(frame) == 40
    assert not frame[OHLC].isna().any().any()


# --------------------------------------------------------------------------
# I4: bar-counting behaviour.
# --------------------------------------------------------------------------


def test_one_way_level_shift_counts_one_bar():
    """A rollover-style gap with no reversal is a single bad bar.

    Also pins the VALUE, which documents ADR 0006's "imputation understates
    genuine gaps" consequence with a number: bar 20's true close is 200.0 (a
    real 96.27% rollover gap), and the imputer replaces it with the midpoint
    151.0 between bar 19 (101.9) and bar 21 (200.1) -- flattening roughly half
    the move. The bar count alone would pass even if the fabricated price were
    wildly wrong.
    """
    close = [100.0 + i * 0.1 for i in range(20)] + [200.0 + i * 0.1 for i in range(20)]
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 1
    assert report.max_gap_pct == pytest.approx(96.27, abs=0.01)
    assert float(frame["Close"].iloc[20]) == pytest.approx(151.0, abs=1e-6)
    # Neighbours untouched: only the flagged bar is fabricated.
    assert float(frame["Close"].iloc[19]) == pytest.approx(101.9, abs=1e-6)
    assert float(frame["Close"].iloc[21]) == pytest.approx(200.1, abs=1e-6)


def test_two_separate_spikes_count_two_bars():
    close = [100.0 + i * 0.1 for i in range(40)]
    close[10] = 900.0
    close[25] = 900.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 2
    # Both spikes neutralised, both recovery bars left near their true values.
    assert float(frame["Close"].iloc[10]) < 200.0
    assert float(frame["Close"].iloc[25]) < 200.0
    assert 100.0 < float(frame["Close"].iloc[11]) < 105.0
    assert 100.0 < float(frame["Close"].iloc[26]) < 105.0


def test_two_consecutive_corrupt_bars_are_both_imputed():
    """Regression for the opposite-sign skip test.

    The old rule skipped the next bar whenever the next extreme return merely
    flipped sign. Two genuinely corrupt consecutive bars do exactly that, so
    only [20, 22] were flagged and the 10x-low price at 21 stayed in the frame
    (`101.9, 55.95, 10.0, 56.15`). The rule now requires the follow-up bar to
    REVERT to the pre-spike level before it is treated as an innocent
    recovery. Bar 22 is over-imputed as a result; that is the cheaper error.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0
    close[21] = 10.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 3  # 20 and 21 corrupt, 22 over-imputed
    window = [float(x) for x in frame["Close"].iloc[19:23]]
    # No grossly-wrong price survives anywhere in the frame.
    assert all(90.0 < c < 115.0 for c in window), window
    assert float(frame["Close"].max()) < 115.0
    assert float(frame["Close"].min()) > 90.0


def test_leading_corrupt_bar_documented_expectation():
    """A pct_change detector cannot validate bar 0 -- it has no predecessor.

    Documented in ADR 0006 as a known limitation. Bar 0 survives corrupt and
    bar 1 is fabricated by interpolating ACROSS the corrupt value, so it lands
    roughly midway. This test pins the documented behaviour so a future change
    to the detector shows up here rather than silently.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[0] = 900.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 1
    assert report.is_degraded is True  # at least visible
    assert float(frame["Close"].iloc[0]) == 900.0  # undetected
    assert 400.0 < float(frame["Close"].iloc[1]) < 600.0  # fabricated midpoint


def test_same_direction_burst_is_not_detected_known_limitation():
    """Two same-direction corrupt bars are NOT caught by either version.

    Only the jump in is extreme; the second 900.0 looks like a flat bar. Fixing
    this needs a rolling-median reference -- out of scope, documented in ADR
    0006. Pinned so the limitation is explicit rather than assumed.

    Assert the FULL measured window, not just the surviving 900.0. The imputer
    makes this case actively worse, and a test that only checked bar 21 would
    keep passing while the fabrication degraded:

        [101.9, 500.95, 900.0, 501.15, 102.3]  <- actual
        [101.9, 102.0,  102.1, 102.2,  102.3]  <- uncorrupted truth

    Bar 20 is fabricated to a midpoint between a good and a corrupt price
    (500.95, garbage); bar 21 keeps its 900.0; and bar 22 -- a GOOD bar, true
    value 102.2 -- is destroyed, overwritten with 501.15 because the reversal
    off the corrupt 900.0 reads as extreme. 3 of 5 bars wrong afterwards. This
    is the counter-example to ADR 0006's "one interpolated bar" framing; see
    the ADR's Consequences section.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0
    close[21] = 900.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 2  # bar 20 + the reversal at 22
    window = [float(x) for x in frame["Close"].iloc[19:24]]
    assert window == pytest.approx([101.9, 500.95, 900.0, 501.15, 102.3], abs=1e-6)
    assert float(frame["Close"].iloc[21]) == 900.0  # still wrong
    # A good bar was overwritten. If a future detector stops doing this, this
    # assertion fails and the ADR needs updating -- which is the point.
    assert float(frame["Close"].iloc[22]) != pytest.approx(102.2, abs=1e-6)


# --------------------------------------------------------------------------
# I3: the dual-key event payload keeps supervisor.rules.data_feed_degraded alive.
# --------------------------------------------------------------------------


def test_data_quality_event_emits_both_imputed_and_dropped_keys():
    """DO NOT "clean up" the duplicate `bars_dropped` key.

    supervisor/rules.py::data_feed_degraded reads
    `int(data.get("bars_dropped", 0))` and breaks its streak on `<= 0`.
    Emitting only `bars_imputed` would make that rule read 0 on every event
    and never fire again -- silently disabling a live feed monitor.
    """
    logger = _RecordingEventLogger()

    _client(logger)._apply_sanity_checks(_frame_with_spike(), "TEST")

    assert [name for name, _ in logger.events] == ["data_quality"]
    payload = logger.events[0][1]
    assert "bars_imputed" in payload
    assert "bars_dropped" in payload, "supervisor data_feed_degraded needs this key"
    # One outlier, no pre-existing NaN, so the two counts coincide here. They
    # are NOT the same quantity -- see test_event_bars_dropped_counts_outliers_only.
    assert payload["bars_dropped"] == payload["bars_imputed"] == 1
    # The exact expression the supervisor rule evaluates.
    assert int(payload.get("bars_dropped", 0)) > 0


def test_event_bars_dropped_counts_outliers_only():
    """`bars_dropped` is the LEGACY key and must keep its ORIGINAL meaning.

    supervisor/rules.py::data_feed_degraded reads `bars_dropped` and breaks its
    streak on `<= 0`. Its docstring calls it the "bars-dropped" pattern: the
    count of OUTLIER bars. Feeding it `bars_imputed` (outliers PLUS bars that
    arrived NaN) silently widened a live alerting rule -- measured: a frame with
    3 NaN-OHLC bars and no outlier at all emitted nothing before the mark-don't
    -drop change and afterwards emitted `bars_dropped: 3`, i.e. it started
    counting towards a 5-cycle alert streak for a condition the rule was never
    written to catch.

    So: `bars_dropped` = outliers only. `bars_imputed` / `bars_nan_prefilled`
    are the richer signals for new consumers. Do not collapse them.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    frame = _frame_from_closes(close)
    frame.iloc[[5, 6, 7], [frame.columns.get_loc(c) for c in OHLC]] = np.nan
    logger = _RecordingEventLogger()

    report = _client(logger)._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 3  # the frame IS degraded ...
    payload = logger.events[0][1]
    assert payload["bars_imputed"] == 3
    assert payload["bars_nan_prefilled"] == 3
    # ... but zero bars were dropped as outliers, so the supervisor's
    # outlier-streak rule must see 0 and break its streak.
    assert payload["bars_dropped"] == 0
    assert int(payload.get("bars_dropped", 0)) <= 0

    # And the converse: a real outlier alongside NaN bars reports only the
    # outlier in `bars_dropped`.
    frame2 = _frame_with_nan_rows(range(5, 9), spike_at=25)  # 4 NaN + 1 outlier
    logger2 = _RecordingEventLogger()
    report2 = _client(logger2)._apply_sanity_checks(frame2, "TEST")
    assert report2.bars_imputed == 5
    assert logger2.events[0][1]["bars_dropped"] == 1


def test_supervisor_data_feed_degraded_fires_on_a_real_emitted_payload():
    """End-to-end guard: build the payload with the real code path, not a
    synthetic dict. Every existing supervisor test hand-writes `bars_dropped`,
    which is exactly why the rename would have gone unnoticed."""
    from datetime import datetime, timezone

    from supervisor.rules import data_feed_degraded

    logger = _RecordingEventLogger()
    client = _client(logger)
    for _ in range(5):
        client._apply_sanity_checks(_frame_with_spike(), "NG=F")

    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"event": "data_quality", "timestamp": now, "data": data}
        for _, data in logger.events
    ]
    result = data_feed_degraded(recent_events=events, symbol="NG=F")

    assert result is not None
    assert result.detail["consecutive_dropped_cycles"] == 5


# --------------------------------------------------------------------------
# Minor: max_gap_pct must stay JSON-representable.
# --------------------------------------------------------------------------


def test_infinite_gap_is_reported_as_none_not_inf():
    """pct_change() off a zero close is `inf`. model_dump_json() renders that
    as `null` anyway, and json.dumps writes the non-standard literal
    `Infinity` into events.jsonl. Normalise to None at the source.

    Fixture: bar 0 is the zero, so the ONLY extreme return is the `inf` at bar
    1 and there is no finite gap to fall back on. (The previous fixture used a
    zero *pair* mid-frame, which also produces a finite -100% gap; after the
    `finite.max()` fix that gap is correctly reported, so it no longer
    exercises the inf path. It now lives in
    `test_zero_close_pair_leaves_a_literal_zero_price_in_the_frame`.)
    """
    close = [0.0] + [100.0 + i * 0.1 for i in range(39)]
    logger = _RecordingEventLogger()

    report = _client(logger)._apply_sanity_checks(_frame_from_closes(close), "TEST")

    assert report.max_gap_pct is None
    assert report.is_degraded is True
    assert logger.events[0][1]["gap_pct_max"] is None


def test_finite_gap_survives_a_co_occurring_infinite_gap():
    """F6: the inf guard must not discard finite gaps.

    `np.nanmax([1.0, inf])` is `inf`, so the old `isfinite(max)` check blanked
    the WHOLE field whenever any bad bar had a zero predecessor -- throwing
    away a real, large, reportable spike. Filter to finite values first, then
    take the max.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[10] = 900.0  # genuine finite spike: 900/100.9 - 1 = 791.97%
    close[20] = 0.0  # produces an inf return at bar 22
    close[21] = 0.0

    report = _client()._apply_sanity_checks(_frame_from_closes(close), "TEST")

    # Before the fix this was None and the 791.97% spike was invisible.
    assert report.max_gap_pct == pytest.approx(791.97, abs=0.01)


def test_zero_close_pair_leaves_a_literal_zero_price_in_the_frame():
    """Honest pin on the imputer's actual output, not just the benign half.

    A zero-close PAIR is only partly repaired. Bar 21's return is `nan`
    (`(0-0)/0`), so it is never flagged, and a literal 0.0 price survives into
    the frame -- while bars 20 and 22, whose returns ARE extreme, get
    fabricated by interpolating ACROSS that zero, landing at ~half their true
    value. Measured `Close[19:24]`, asserted exactly so that any change in the
    fabrication (better or worse) shows up here:

        [101.9, 50.95, 0.0, 51.15, 102.3]   <- actual
        [101.9, 102.0, 102.1, 102.2, 102.3] <- uncorrupted truth

    3 of these 5 bars are wrong afterwards. See ADR 0006, same-direction /
    consecutive-corruption limitation: the detector's reference is the previous
    bar, which is itself zero here.
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 0.0
    close[21] = 0.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 2
    assert report.is_degraded is True
    window = [float(x) for x in frame["Close"].iloc[19:24]]
    assert window == pytest.approx([101.9, 50.95, 0.0, 51.15, 102.3], abs=1e-6)
    # The zero itself is NOT repaired -- pinned explicitly, not implied.
    assert float(frame["Close"].iloc[21]) == 0.0
    assert float(frame["Close"].min()) == 0.0


# --------------------------------------------------------------------------
# I5 / I6: fetch_history bookkeeping.
# --------------------------------------------------------------------------


def test_last_quality_is_reset_when_a_later_fetch_raises():
    """Otherwise `_last_quality` still holds the PREVIOUS symbol's report --
    a cross-symbol mis-attribution in a field the risk gate consumes."""
    client = _client()
    with patch("trading_bot.market_data.yf.download", return_value=_frame_with_spike()):
        client.fetch_history("AAA")
    assert client._last_quality is not None

    with patch("trading_bot.market_data.yf.download", return_value=pd.DataFrame()):
        with pytest.raises(ValueError, match="no market data returned"):
            client.fetch_history("BBB")

    assert client._last_quality is None


def test_wholly_empty_bars_dropped_are_reported_in_the_note():
    """`fetch_history` still calls dropna(how="all"), which DOES break index
    contiguity. Keep the drop (an empty bar carries no data) but stop hiding
    it: `bars_total` is measured after the drop, so the count is otherwise
    invisible. See ADR 0006."""
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(41)])
    frame.iloc[10, :] = np.nan  # a halted / missing intraday bar
    client = _client()

    with patch("trading_bot.market_data.yf.download", return_value=frame):
        result = client.fetch_history("SPY")

    assert len(result) == 40
    quality = client._last_quality
    assert quality is not None
    assert quality.bars_total == 40
    assert "1 wholly-empty bar(s) dropped before checks" in (quality.note or "")


def test_wholly_empty_bar_drops_are_a_typed_field_and_flag_degradation():
    """F1: `note` is prose, not a contract.

    A machine consumer had no way to learn the index had been gapped: measured
    on a 50-bar frame with rows 10-19 wholly empty, `fetch_history` returned 40
    rows with an 11-hour hole in a 1-hour index, yet reported
    `bars_imputed=0, is_degraded=False`, with the only trace being English in
    `note`. Dropping rows removes timestamps -- the exact bug class ADR 0006
    exists to eliminate -- so it must be visible without string-parsing.
    """
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(50)])
    frame.iloc[10:20, :] = np.nan
    client = _client()

    with patch("trading_bot.market_data.yf.download", return_value=frame):
        result = client.fetch_history("SPY")

    # The gap is real: a 1-hour index with an 11-hour step across the hole.
    assert len(result) == 40
    steps = set(result.index.to_series().diff().dropna().unique())
    assert pd.Timedelta(hours=11) in steps

    quality = client._last_quality
    assert quality is not None
    assert quality.bars_dropped_empty == 10  # typed, not parsed out of note
    assert quality.is_degraded is True  # was False
    assert quality.bars_imputed == 0  # nothing was fabricated -- still true
    # Human-readable copy retained alongside the typed field.
    assert "10 wholly-empty bar(s) dropped before checks" in (quality.note or "")


def test_clean_fetch_reports_no_empty_drops():
    """Control for the test above: the field must not fire spuriously."""
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])
    client = _client()

    with patch("trading_bot.market_data.yf.download", return_value=frame):
        client.fetch_history("SPY")

    quality = client._last_quality
    assert quality is not None
    assert quality.bars_dropped_empty == 0
    assert quality.is_degraded is False
    assert quality.note is None


# --------------------------------------------------------------------------
# F2: imputed_ratio -- the documented consumer must not divide by zero.
# --------------------------------------------------------------------------


def test_imputed_ratio_is_one_when_there_are_no_bars():
    """ADR 0006 told consumers to compute `bars_imputed / bars_total`, and
    `build_fallback_symbol_context` reports `bars_total=0` -- so the documented
    recipe raised ZeroDivisionError on the very context it was meant to judge.
    No history at all reads as FULLY degraded, not as an error."""
    quality = DataQuality(bars_total=0, is_degraded=True, note="no price history")

    assert quality.imputed_ratio == 1.0

    context = _client().build_fallback_symbol_context(
        symbol="WTI",
        fallback_price=70.0,
        headlines=[],
        note="price fetch failed; using last known price",
    )
    assert context.data_quality is not None
    assert context.data_quality.bars_total == 0
    assert context.data_quality.imputed_ratio == 1.0  # was ZeroDivisionError


def test_imputed_ratio_is_the_ratio_when_there_are_bars():
    assert DataQuality(bars_total=40, bars_imputed=1).imputed_ratio == pytest.approx(
        0.025
    )
    assert DataQuality(bars_total=200, bars_imputed=50).imputed_ratio == pytest.approx(
        0.25
    )
    assert DataQuality(bars_total=40, bars_imputed=0).imputed_ratio == 0.0
    # And it agrees with the real report from the real code path.
    frame = _frame_with_spike()
    report = _client()._apply_sanity_checks(frame, "TEST")
    assert report.imputed_ratio == pytest.approx(1 / 40)


# --------------------------------------------------------------------------
# F8: NaN Volume was neither filled nor reported.
# --------------------------------------------------------------------------


def test_nan_volume_is_filled_and_counted():
    """`pre_nan_mask` covered OHLC only, but the >20%-NaN guard covers every
    column -- so a frame with a few NaN-Volume rows passed the guard, reported
    `bars_imputed=0, is_degraded=False, note=None`, and carried its NaN volume
    straight into the volume indicators (`rolling(20).mean()` alone turned 3
    NaN into 27 NaN positions out of 60).

    Deliberate decision (ADR 0006): fill Volume by the same interpolation pass,
    and count it in its OWN field so `bars_imputed` keeps meaning "fabricated
    OHLC" -- which is what `imputed_ratio`'s docstring promises.
    """
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])
    frame.iloc[[5, 6, 7], frame.columns.get_loc("Volume")] = np.nan

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_volume_filled == 3
    assert report.is_degraded is True  # was False
    assert report.bars_imputed == 0  # no OHLC was fabricated
    assert report.imputed_ratio == 0.0
    assert "3 NaN volume bar(s) interpolated" in (report.note or "")
    # Filled in place: no NaN left, no row lost, OHLC untouched.
    assert int(frame["Volume"].isna().sum()) == 0
    assert len(frame) == 40
    assert [float(x) for x in frame["Volume"].iloc[4:9]] == pytest.approx([1000.0] * 5)
    assert float(frame["Close"].iloc[5]) == pytest.approx(100.5, abs=1e-6)


def test_nan_volume_is_reported_in_the_event_payload():
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])
    frame.iloc[[5, 6], frame.columns.get_loc("Volume")] = np.nan
    logger = _RecordingEventLogger()

    _client(logger)._apply_sanity_checks(frame, "TEST")

    payload = logger.events[0][1]
    assert payload["bars_volume_filled"] == 2
    # Volume NaN is not an outlier, so the supervisor's streak rule stays quiet.
    assert payload["bars_dropped"] == 0


def test_zero_volume_frames_are_untouched_by_the_volume_fill():
    """Forex has legitimately all-zero volume -- zero is not NaN, so nothing is
    filled and the report stays clean."""
    frame = _frame_from_closes([100.0 + i * 0.1 for i in range(40)])
    frame["Volume"] = 0.0

    report = _client()._apply_sanity_checks(frame, "JPY=X")

    assert report.bars_volume_filled == 0
    assert report.is_degraded is False
    assert float(frame["Volume"].sum()) == 0.0


def test_fallback_context_reports_degraded_not_unknown():
    """A fallback context has no price history at all. `data_quality=None`
    reads as "quality unknown", which a risk gate treats as not-degraded."""
    context = _client().build_fallback_symbol_context(
        symbol="WTI",
        fallback_price=70.0,
        headlines=[],
        note="price fetch failed; using last known price",
    )

    assert context.data_quality is not None
    assert context.data_quality.bars_total == 0
    assert context.data_quality.is_degraded is True
    assert context.data_quality.note == "price fetch failed; using last known price"
