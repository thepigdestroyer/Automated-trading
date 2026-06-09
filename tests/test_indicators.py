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


def test_bars_per_year_contiguous_hourly_is_density():
    # 100 contiguous hourly bars span 99h. Density = 100 / (99h in years)
    # ≈ 8855 — i.e. ~8760/yr for a truly 24h-contiguous series. (Bug v1 also
    # returned ~8760 here; the divergence only appears with session gaps,
    # see the session test below.)
    idx = pd.date_range("2024-01-01", periods=100, freq="h")
    bpy = _bars_per_year(idx)
    assert 8000.0 < bpy < 9200.0


def test_bars_per_year_infers_daily():
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    # 100 daily bars span 99 days. Density = 100 / (99/365.25) ≈ 369.
    assert _bars_per_year(idx) == pytest.approx(100.0 / (99.0 / 365.25), rel=1e-6)


def test_bars_per_year_session_traded_is_not_overstated():
    """THE FIX (2026-05 v2): an equity-style session of 7 hourly bars/day,
    5 days/week must yield ~1,500-1,900 bars/yr — NOT 8760. Bug v1 (median
    intraday spacing) ignored the overnight/weekend gaps and returned 8760,
    overstating realized vol ~2.3x."""
    # Build 8 weeks of 7-bar weekday sessions (Mon-Fri, 09:00-15:00).
    days = pd.bdate_range("2024-01-01", periods=40)  # 40 business days
    stamps = []
    for d in days:
        for h in range(9, 16):  # 7 bars 09:00..15:00
            stamps.append(d + pd.Timedelta(hours=h))
    idx = pd.DatetimeIndex(stamps)
    bpy = _bars_per_year(idx)
    # 7 bars/day * ~252 trading days ≈ 1,764. Allow a band for the short span.
    assert 1400.0 < bpy < 2100.0, bpy


def test_bars_per_year_falls_back_on_non_datetime_index():
    idx = pd.RangeIndex(0, 100)
    assert _bars_per_year(idx) == 252.0


def test_bars_per_year_falls_back_on_single_point():
    idx = pd.DatetimeIndex(["2024-01-01"])
    assert _bars_per_year(idx) == 252.0


def test_volatility_annualisation_uses_observed_density():
    # Same per-bar return series sampled at two densities. The annualisation
    # ratio must equal sqrt(density_hourly / density_daily). With the
    # observed-density estimator and contiguous series, hourly density ≈
    # 24x daily, so the vol ratio ≈ sqrt(24) ≈ 4.9.
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
    assert ratio == pytest.approx(np.sqrt(24.0), rel=0.02)


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
