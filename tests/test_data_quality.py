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
    """A rollover-style gap with no reversal is a single bad bar."""
    close = [100.0 + i * 0.1 for i in range(20)] + [200.0 + i * 0.1 for i in range(20)]

    report = _client()._apply_sanity_checks(_frame_from_closes(close), "TEST")

    assert report.bars_imputed == 1


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
    """
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0
    close[21] = 900.0
    frame = _frame_from_closes(close)

    report = _client()._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 2  # bar 20 + the reversal at 22
    assert float(frame["Close"].iloc[21]) == 900.0  # still wrong


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
    assert payload["bars_dropped"] == payload["bars_imputed"] == 1
    # The exact expression the supervisor rule evaluates.
    assert int(payload.get("bars_dropped", 0)) > 0


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
    `Infinity` into events.jsonl. Normalise to None at the source."""
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 0.0
    close[21] = 0.0
    logger = _RecordingEventLogger()

    report = _client(logger)._apply_sanity_checks(_frame_from_closes(close), "TEST")

    assert report.max_gap_pct is None
    assert report.is_degraded is True
    assert logger.events[0][1]["gap_pct_max"] is None


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
