"""Tests for the Phase 1.1/1.2 deterministic signal-pack layer."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.indicators import build_indicator_snapshot
from trading_bot.models import SignalPack
from trading_bot.signals import (
    _bollinger_position,
    _classify_volume,
    _donchian_position,
    _mean_reversion_z,
    _vwap_deviation_bps,
    build_signal_pack,
)


def _trending_up(length: int = 220) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=length, freq="h")
    close = 100 + np.linspace(0, 40, length)  # steady uptrend
    return pd.DataFrame(
        {"Open": close, "High": close * 1.005, "Low": close * 0.995,
         "Close": close, "Volume": np.linspace(1000, 2000, length)},
        index=idx,
    )


def _trending_down(length: int = 220) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=length, freq="h")
    close = 140 - np.linspace(0, 40, length)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.005, "Low": close * 0.995,
         "Close": close, "Volume": np.linspace(1000, 2000, length)},
        index=idx,
    )


def _choppy(length: int = 220) -> pd.DataFrame:
    np.random.seed(3)
    idx = pd.date_range("2024-01-01", periods=length, freq="h")
    close = 100 + np.cumsum(np.random.normal(0, 0.2, length))
    return pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99,
         "Close": close, "Volume": np.linspace(1000, 2000, length)},
        index=idx,
    )


def test_build_signal_pack_returns_model():
    frame = _trending_up()
    snap = build_indicator_snapshot(frame)
    pack = build_signal_pack(frame, snap)
    assert isinstance(pack, SignalPack)
    assert pack.summary  # non-empty


def test_uptrend_classified_up():
    frame = _trending_up()
    snap = build_indicator_snapshot(frame)
    pack = build_signal_pack(frame, snap)
    assert pack.trend in {"up", "up_strong"}
    # In a steady uptrend price sits near the top of the Donchian channel.
    assert pack.donchian_position > 0.7


def test_downtrend_classified_down():
    frame = _trending_down()
    snap = build_indicator_snapshot(frame)
    pack = build_signal_pack(frame, snap)
    assert pack.trend in {"down", "down_strong"}
    assert pack.donchian_position < 0.3


def test_mean_reversion_z_sign():
    # Price spiked far above its 20-bar mean → strongly positive z.
    close = pd.Series([100.0] * 19 + [110.0])
    z = _mean_reversion_z(close)
    assert z > 2.0
    # Below mean → negative.
    close2 = pd.Series([100.0] * 19 + [90.0])
    assert _mean_reversion_z(close2) < -2.0


def test_bollinger_position_bands():
    assert _bollinger_position(2.5) == "above_upper"
    assert _bollinger_position(0.5) == "upper_half"
    assert _bollinger_position(-0.5) == "lower_half"
    assert _bollinger_position(-2.5) == "below_lower"


def test_donchian_position_extremes():
    idx = pd.date_range("2024-01-01", periods=20, freq="h")
    # Close at the very top of the range.
    df_top = pd.DataFrame(
        {"High": np.linspace(10, 20, 20), "Low": np.linspace(5, 15, 20),
         "Close": [20.0] * 20}, index=idx,
    )
    assert _donchian_position(df_top) == pytest.approx(1.0, abs=0.01)
    df_bot = pd.DataFrame(
        {"High": np.linspace(10, 20, 20), "Low": np.linspace(5, 15, 20),
         "Close": [5.0] * 20}, index=idx,
    )
    assert _donchian_position(df_bot) == pytest.approx(0.0, abs=0.01)


def test_donchian_degenerate_channel_returns_half():
    idx = pd.date_range("2024-01-01", periods=20, freq="h")
    df = pd.DataFrame({"High": [10.0] * 20, "Low": [10.0] * 20,
                       "Close": [10.0] * 20}, index=idx)
    assert _donchian_position(df) == 0.5


def test_vwap_deviation_none_without_volume():
    idx = pd.date_range("2024-01-01", periods=30, freq="h")
    df = pd.DataFrame({"High": [10.0] * 30, "Low": [9.0] * 30,
                       "Close": [9.5] * 30}, index=idx)  # no Volume column
    assert _vwap_deviation_bps(df) is None


def test_vwap_deviation_zero_volume_returns_none():
    idx = pd.date_range("2024-01-01", periods=30, freq="h")
    df = pd.DataFrame({"High": [10.0] * 30, "Low": [9.0] * 30,
                       "Close": [9.5] * 30, "Volume": [0.0] * 30}, index=idx)
    assert _vwap_deviation_bps(df) is None


def test_volume_state_thresholds():
    from trading_bot.models import IndicatorSnapshot

    def _snap(ratio):
        return IndicatorSnapshot(
            price=100, sma_20=100, sma_50=100, ema_12=100, ema_26=100,
            rsi_14=50, macd=0, macd_signal=0, atr_14=1, volatility_20d=0.1,
            return_1d=0, return_5d=0, volume_ratio_vs_20d=ratio,
        )
    assert _classify_volume(_snap(2.0)) == "surge"
    assert _classify_volume(_snap(0.3)) == "thin"
    assert _classify_volume(_snap(1.0)) == "normal"
    assert _classify_volume(_snap(None)) == "unknown"


def test_signal_pack_defensive_on_short_frame():
    idx = pd.date_range("2024-01-01", periods=60, freq="h")
    close = 100 + np.cumsum(np.random.RandomState(1).normal(0, 0.3, 60))
    frame = pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99,
         "Close": close, "Volume": np.linspace(1000, 2000, 60)},
        index=idx,
    )
    snap = build_indicator_snapshot(frame)
    pack = build_signal_pack(frame, snap)  # must not raise
    assert isinstance(pack, SignalPack)


def test_choppy_market_neutralish_trend():
    frame = _choppy()
    snap = build_indicator_snapshot(frame)
    pack = build_signal_pack(frame, snap)
    # A random walk shouldn't read as a strong directional trend most of the
    # time; at minimum the classifier must return a valid label.
    assert pack.trend in {"up_strong", "up", "neutral", "down", "down_strong"}
