"""The vectorised trend frame must agree with build_daily_trend bar-for-bar.

The backtester consumes the frame; the live path consumes build_daily_trend.
If they diverge, backtest results describe a strategy we do not run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.daily_trend import build_daily_trend, compute_trend_frame
from trading_bot.models import DailyTrend


def _daily(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def test_frame_has_a_row_per_bar():
    daily = _daily()
    frame = compute_trend_frame(daily)
    assert len(frame) == len(daily)
    assert frame.index.equals(daily.index)


def test_last_row_matches_build_daily_trend():
    daily = _daily()
    frame = compute_trend_frame(daily)
    trend = build_daily_trend(daily)

    last = frame.iloc[-1]
    assert last["direction"] == trend.direction
    assert last["donchian_breakout"] == trend.donchian_breakout
    assert last["sma_50_200_cross"] == trend.sma_50_200_cross
    assert round(float(last["trend_strength"]), 1) == trend.trend_strength


# --- every-bar parity ----------------------------------------------------
#
# `_reference_daily_trend` is a FROZEN copy of the scalar build_daily_trend
# (and its _adx_proxy) as it stood before the frame existed (commit 707d177).
# build_daily_trend now delegates to the frame, so comparing the frame only to
# build_daily_trend would catch lookahead but not a rule change applied to
# both; the frozen copy pins the rule itself. Do not "fix" it to track
# daily_trend.py — a deliberate rule change must update it consciously.


def _reference_adx_proxy(frame: pd.DataFrame, period: int = 14) -> float:
    if len(frame) < period + 2:
        return 0.0
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=frame.index).rolling(period).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=frame.index).rolling(period).mean() / atr.replace(0.0, np.nan)
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx = dx.rolling(period).mean()
    val = adx.dropna()
    if val.empty:
        return 0.0
    return float(max(0.0, min(100.0, val.iloc[-1])))


def _reference_daily_trend(daily: pd.DataFrame) -> DailyTrend:
    neutral = DailyTrend(
        direction="sideways", donchian_breakout="none", sma_50_200_cross="none",
        trend_strength=0.0, close_vs_sma50_bps=None,
        summary="insufficient daily data",
    )
    if daily is None or "Close" not in getattr(daily, "columns", []):
        return neutral
    close = daily["Close"].astype(float).dropna()
    if len(close) < 30:
        return neutral

    last = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    sma_cross = "none"
    if sma50 is not None and sma200 is not None:
        if sma50 > sma200:
            sma_cross = "golden"
        elif sma50 < sma200:
            sma_cross = "death"

    breakout = "none"
    if len(close) >= 20 + 1:
        high = daily["High"].astype(float)
        low = daily["Low"].astype(float)
        prior_high = float(high.iloc[-(20 + 1):-1].max())
        prior_low = float(low.iloc[-(20 + 1):-1].min())
        if last >= prior_high * (1.0 - 0.001):
            breakout = "up"
        elif last <= prior_low * (1.0 + 0.001):
            breakout = "down"

    strength = _reference_adx_proxy(daily)

    direction = "sideways"
    if sma50 is not None:
        slope_ref = (
            float(close.rolling(50).mean().iloc[-6])
            if len(close) >= 55 else sma50
        )
        sma50_rising = sma50 >= slope_ref
        if last > sma50 and sma50_rising and sma_cross != "death":
            direction = "up"
        elif last < sma50 and not sma50_rising and sma_cross != "golden":
            direction = "down"

    close_vs_sma50_bps = None
    if sma50 is not None and sma50 > 0:
        close_vs_sma50_bps = round((last - sma50) / sma50 * 10_000.0, 1)

    summary = (
        f"daily {direction} trend (strength {strength:.0f}/100), "
        f"Donchian breakout {breakout}, MA cross {sma_cross}"
    )
    return DailyTrend(
        direction=direction,
        donchian_breakout=breakout,
        sma_50_200_cross=sma_cross,
        trend_strength=round(strength, 1),
        close_vs_sma50_bps=close_vs_sma50_bps,
        summary=summary,
    )


def _ohlc(close: np.ndarray, rng: np.random.Generator) -> pd.DataFrame:
    """OHLC with a varying intrabar range around a close path."""
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    top = np.maximum(open_, close)
    bottom = np.minimum(open_, close)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": open_,
            "High": top * (1.0 + np.abs(rng.normal(0.0, 0.003, n))),
            "Low": bottom * (1.0 - np.abs(rng.normal(0.0, 0.003, n))),
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _trending() -> pd.DataFrame:
    """A rally then a sell-off: golden AND death crosses, both breakouts."""
    n = 460
    rng = np.random.default_rng(11)
    drift = np.where(np.arange(n) < n // 2, 0.004, -0.004)
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0.0, 0.008, n)))
    return _ohlc(close, rng)


def _random_walk() -> pd.DataFrame:
    """Driftless chop: the direction flips repeatedly, sma50 slope wobbles."""
    n = 320
    rng = np.random.default_rng(3)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.012, n)))
    return _ohlc(close, rng)


def _malformed() -> pd.DataFrame:
    """Real-world dirt: NaN closes (incl. the first and last bars), NaN
    highs/lows, a dead-flat stretch (zero ATR -> NaN ADX that the prefix
    evaluation carries forward), and inverted High<Low bars after NaN closes
    (negative ATR -> an out-of-range ADX that must be clipped)."""
    n = 300
    rng = np.random.default_rng(5)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    df = _ohlc(close, rng)

    flat = slice(200, 225)
    df.iloc[flat, df.columns.get_indexer(["Open", "High", "Low", "Close"])] = 150.0

    inverted = np.arange(120, 160)
    ramp = 120.0 + 0.1 * np.arange(len(inverted))
    df.iloc[inverted, df.columns.get_loc("High")] = ramp - 1.0
    df.iloc[inverted, df.columns.get_loc("Low")] = ramp + 1.0
    df.iloc[inverted, df.columns.get_loc("Close")] = ramp
    df.iloc[inverted[::2], df.columns.get_loc("Close")] = np.nan

    close_col = df.columns.get_loc("Close")
    for pos in (0, 1, 2, 37, 38, 90, 175, 260, 298, 299):
        df.iloc[pos, close_col] = np.nan
    for pos in (45, 46, 230, 270):
        df.iloc[pos, df.columns.get_loc("High")] = np.nan
    for pos in (60, 231, 275):
        df.iloc[pos, df.columns.get_loc("Low")] = np.nan
    return df


_SERIES = {
    "brief_fixture": _daily,
    "trending": _trending,
    "random_walk": _random_walk,
    "malformed": _malformed,
}


def _assert_row_matches(row: pd.Series, expected: DailyTrend, t: int) -> None:
    assert row["direction"] == expected.direction, f"t={t}"
    assert row["donchian_breakout"] == expected.donchian_breakout, f"t={t}"
    assert row["sma_50_200_cross"] == expected.sma_50_200_cross, f"t={t}"
    assert round(float(row["trend_strength"]), 1) == expected.trend_strength, f"t={t}"
    bps = row["close_vs_sma50_bps"]
    if expected.close_vs_sma50_bps is None:
        assert pd.isna(bps), f"t={t}"
    else:
        assert round(float(bps), 1) == expected.close_vs_sma50_bps, f"t={t}"


@pytest.mark.parametrize("name", sorted(_SERIES))
def test_matches_build_daily_trend_at_earlier_cutoffs(name):
    """Bar t in the frame must equal build_daily_trend(daily[:t+1]) — this is
    the no-lookahead guarantee the backtest depends on. Checked at EVERY bar,
    and against the frozen pre-refactor rule so the refactor can't drift."""
    daily = _SERIES[name]()
    frame = compute_trend_frame(daily)
    assert frame.index.equals(daily.index)

    for t in range(len(daily)):
        prefix = daily.iloc[: t + 1]
        reference = _reference_daily_trend(prefix)
        expected = build_daily_trend(prefix)
        assert expected == reference, f"{name} t={t}"
        _assert_row_matches(frame.iloc[t], expected, t)


def test_parity_fixtures_exercise_every_verdict():
    """Guard against a vacuous parity test: across the fixtures, every value
    of every categorical column must actually occur."""
    frames = [compute_trend_frame(make()) for make in _SERIES.values()]
    allf = pd.concat(frames)
    assert set(allf["direction"]) == {"up", "down", "sideways"}
    assert set(allf["donchian_breakout"]) == {"up", "down", "none"}
    assert set(allf["sma_50_200_cross"]) == {"golden", "death", "none"}
    assert (allf["trend_strength"] > 0).any()


def test_future_bars_cannot_change_past_rows():
    """Direct lookahead check: scrambling every bar after t must leave rows
    0..t bit-identical."""
    daily = _trending()
    cut = 300
    scrambled = daily.copy()
    rng = np.random.default_rng(99)
    for col in ("Open", "High", "Low", "Close"):
        scrambled.iloc[cut + 1:, scrambled.columns.get_loc(col)] = rng.uniform(
            1.0, 1000.0, len(daily) - cut - 1
        )
    pd.testing.assert_frame_equal(
        compute_trend_frame(daily).iloc[: cut + 1],
        compute_trend_frame(scrambled).iloc[: cut + 1],
        check_exact=True,
    )


def test_rows_with_fewer_than_30_valid_closes_are_neutral():
    daily = _malformed()  # first three closes are NaN
    frame = compute_trend_frame(daily)
    n_closes = daily["Close"].notna().cumsum()
    early = frame[n_closes < 30]
    assert len(early) > 0
    assert set(early["direction"]) == {"sideways"}
    assert set(early["donchian_breakout"]) == {"none"}
    assert set(early["sma_50_200_cross"]) == {"none"}
    assert (early["trend_strength"] == 0.0).all()
    assert early["close_vs_sma50_bps"].isna().all()
    # The first bar with 30 valid closes is judged, not neutral.
    first_judged = int(np.argmax(n_closes.to_numpy() >= 30))
    assert "insufficient" not in build_daily_trend(daily.iloc[: first_judged + 1]).summary
    assert "insufficient" in build_daily_trend(daily.iloc[:first_judged]).summary


def test_trend_strength_stays_within_0_100_on_inverted_bars():
    frame = compute_trend_frame(_malformed())
    assert frame["trend_strength"].between(0.0, 100.0).all()
    assert not frame["trend_strength"].isna().any()


def test_missing_close_column_gives_all_neutral_rows():
    df = pd.DataFrame({"Open": [1.0, 2.0, 3.0]})
    frame = compute_trend_frame(df)
    assert frame.index.equals(df.index)
    assert set(frame["direction"]) == {"sideways"}
    assert (frame["trend_strength"] == 0.0).all()
