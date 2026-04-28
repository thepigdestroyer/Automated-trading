from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.indicators import build_indicator_snapshot, compute_higher_tf_fields


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
