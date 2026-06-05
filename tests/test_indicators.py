from __future__ import annotations
import pytest

import numpy as np
import pandas as pd

from trading_bot.indicators import (
    _bars_per_year,
    build_indicator_snapshot,
    compute_higher_tf_fields,
)


def _ohlcv(length: int = 220) -> pd.DataFrame:
    np.random.seed(1)
    idx = pd.date_range("2024-01-01", periods=length, freq="h")
    close = 100 + np.cumsum(np.random.normal(0, 0.3, length))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.linspace(1000, 2000, length),
        },
        index=idx,
    )


def test_indicator_snapshot_includes_volume_metrics():
    snap = build_indicator_snapshot(_ohlcv())
    assert snap.volume_ratio_vs_20d is not None
    assert snap.volume_ratio_vs_20d > 0
    assert snap.obv_slope_5d is not None


def test_bars_per_year_infers_hourly():
    idx = pd.date_range("2024-01-01", periods=100, freq="h")
    # 365*24 = 8760 hourly bars per year
    assert _bars_per_year(idx) == pytest.approx(8760.0, rel=1e-6)


def test_bars_per_year_infers_daily():
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    assert _bars_per_year(idx) == pytest.approx(365.0, rel=1e-6)


def test_bars_per_year_falls_back_on_non_datetime_index():
    idx = pd.RangeIndex(0, 100)
    assert _bars_per_year(idx) == 252.0


def test_bars_per_year_falls_back_on_single_point():
    idx = pd.DatetimeIndex(["2024-01-01"])
    assert _bars_per_year(idx) == 252.0


def test_volatility_annualisation_is_interval_aware():
    # Same return series, two intervals. Hourly annualisation factor is
    # sqrt(8760) vs daily sqrt(365) => hourly vol should be ~sqrt(24)=4.9x
    # the daily figure for an identical per-bar std. This guards against
    # the pre-2026-05 hard-coded sqrt(252) hourly-vol understatement bug.
    np.random.seed(7)
    rets = np.random.normal(0, 0.01, 220)
    close = 100 * np.cumprod(1 + rets)

    def _frame(freq: str) -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=len(close), freq=freq)
        return pd.DataFrame(
            {"Open": close, "High": close * 1.01, "Low": close * 0.99,
             "Close": close, "Volume": np.linspace(1000, 2000, len(close))},
            index=idx,
        )

    hourly = build_indicator_snapshot(_frame("h")).volatility_20d
    daily = build_indicator_snapshot(_frame("D")).volatility_20d
    ratio = hourly / daily
    assert ratio == pytest.approx(np.sqrt(8760.0 / 365.0), rel=1e-3)


def test_compute_higher_tf_fields_matches_manual():
    np.random.seed(2)
    daily_idx = pd.date_range("2023-01-01", periods=250, freq="D")
    daily_close = 100 + np.cumsum(np.random.normal(0, 1, 250))
    daily = pd.DataFrame({"Close": daily_close}, index=daily_idx)
    weekly = daily.resample("W").last()
    fields = compute_higher_tf_fields(daily=daily, weekly=weekly)
    assert fields["daily_sma_200"] is not None
    assert fields["daily_sma_200_distance_bps"] is not None
    assert fields["weekly_return_5w"] is not None
