"""Outlier bars must be neutralised in place, never dropped.

Dropping rows breaks index continuity, which silently corrupts every
rolling/EWM indicator computed downstream.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.config import Settings
from trading_bot.market_data import YahooMarketDataClient


def _frame_with_spike() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0  # corrupted tick
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.001 for c in close],
            "Low": [c * 0.999 for c in close],
            "Close": close,
            "Volume": [1000.0] * 40,
        },
        index=idx,
    )


def _client() -> YahooMarketDataClient:
    return YahooMarketDataClient(Settings(_env_file=None))


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
    idx = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(40)]
    frame = pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.001 for c in close],
            "Low": [c * 0.999 for c in close],
            "Close": close,
            "Volume": [1000.0] * 40,
        },
        index=idx,
    )

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 0
    assert report.is_degraded is False
