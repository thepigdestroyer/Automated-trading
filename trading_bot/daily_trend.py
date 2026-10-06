"""Daily-timeframe trend classification (Phase 2 strategy pivot, 2026-05).

The 4-expert adversarial review concluded the bot's intraday news-chasing has
no edge, and the only durable retail edge is timeframe + patience: daily
trend-following / breakout. This module derives a `DailyTrend` from daily OHLC
bars so the executor can enforce TREND ALIGNMENT — only permit longs in a
daily uptrend and shorts in a daily downtrend, and prefer Donchian breakouts.

`compute_trend_frame` is the single implementation of the rule: one verdict
per bar, each computed only from that bar and earlier ones. The live path
(`build_daily_trend`) reads its last row; the backtester reads every row. See
docs/decisions/0007-shared-trend-frame-live-and-backtest.md.

Pure + defensive: short/NaN frames degrade to a 'sideways' / 'none' verdict
rather than raising.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.models import DailyTrend

# Donchian channel length (trading days). 20 ≈ one trading month — the classic
# Turtle breakout window.
_DONCHIAN_DAYS = 20
# Tolerance (fraction) for "at the channel high/low" to count as a breakout.
_BREAKOUT_TOL = 0.001
# Bars with fewer valid closes than this get the neutral verdict.
_MIN_CLOSES = 30
_SMA_FAST = 50
_SMA_SLOW = 200
# sma50 is "rising" if it is >= its value this many valid closes ago.
_SLOPE_LOOKBACK = 5
_ADX_PERIOD = 14


def _adx_proxy_series(frame: pd.DataFrame, period: int = _ADX_PERIOD) -> pd.Series:
    """A lightweight ADX-like trend-strength proxy in [0, 100], per bar.

    Full Wilder ADX is heavy; this uses the absolute directional movement
    normalised by ATR, smoothed — same intuition (strong directional move =
    high value, chop = low) without the full DI+/DI- apparatus.

    Bar t holds the value the proxy reports for frame[:t+1]: the most recent
    non-NaN smoothed value (carried forward across NaN stretches, e.g.
    zero-ATR flat bars), clipped to [0, 100], or 0.0 if there has never been
    one. The smoothed value needs 2 * period - 1 bars, so the old
    "fewer than period + 2 bars -> 0.0" guard is implied.
    """
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
    return adx.ffill().clip(0.0, 100.0).fillna(0.0)


def _at_latest_close(values: np.ndarray, n_closes: np.ndarray) -> np.ndarray:
    """Spread per-valid-close `values` onto bars: bar t gets the value at its
    most recent valid close at or before t, or NaN before the first one.

    The NaN is load-bearing, not cosmetic: clamping those early bars onto
    index 0 would hand them the first valid close, which lies in their future.
    """
    if len(values) == 0:
        return np.full(len(n_closes), np.nan)
    pos = np.maximum(n_closes - 1, 0)
    return np.where(n_closes > 0, values[pos], np.nan)


def _neutral_frame(index: pd.Index) -> pd.DataFrame:
    n = len(index)
    return pd.DataFrame(
        {
            "direction": np.full(n, "sideways", dtype=object),
            "donchian_breakout": np.full(n, "none", dtype=object),
            "sma_50_200_cross": np.full(n, "none", dtype=object),
            "trend_strength": np.zeros(n),
            "close_vs_sma50_bps": np.full(n, np.nan),
            "sufficient_data": np.zeros(n, dtype=bool),
        },
        index=index,
    )


def compute_trend_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Per-bar daily-trend verdicts, indexed identically to `daily`.

    Columns: direction, donchian_breakout, sma_50_200_cross, trend_strength
    (unrounded), close_vs_sma50_bps (unrounded, NaN when undefined) and
    sufficient_data (False where the neutral verdict applies).

    No lookahead: row t equals build_daily_trend(daily.iloc[:t+1]). Every
    input is a trailing window, and a bar with a NaN close is judged on the
    most recent valid close, exactly as the prefix's dropna() would.
    """
    if daily is None:
        return _neutral_frame(pd.RangeIndex(0))
    frame = _neutral_frame(daily.index)
    if "Close" not in getattr(daily, "columns", []):
        return frame

    close_all = daily["Close"].astype(float)
    n_closes = np.cumsum(close_all.notna().to_numpy())
    sufficient = n_closes >= _MIN_CLOSES
    if not sufficient.any():
        return frame

    # Close-based series live on the dropna()'d closes (rolling windows count
    # valid closes, not bars); `_at_latest_close` maps each bar to its latest
    # valid close, and to NaN before the first one.
    close = close_all.dropna()
    sma_fast_valid = close.rolling(_SMA_FAST).mean()
    last = _at_latest_close(close.to_numpy(), n_closes)
    sma50 = _at_latest_close(sma_fast_valid.to_numpy(), n_closes)
    sma200 = _at_latest_close(close.rolling(_SMA_SLOW).mean().to_numpy(), n_closes)
    sma50_lagged = _at_latest_close(sma_fast_valid.shift(_SLOPE_LOOKBACK).to_numpy(), n_closes)
    has_sma50 = n_closes >= _SMA_FAST
    has_sma200 = n_closes >= _SMA_SLOW

    has_cross = has_sma50 & has_sma200
    sma_cross = np.select(
        [has_cross & (sma50 > sma200), has_cross & (sma50 < sma200)],
        ["golden", "death"],
        "none",
    )

    # The channel is the prior N bars, excluding the current one (shift(1)),
    # over raw High/Low rows; min_periods=1 because Series.max() skips NaN.
    # rolling() also treats a ±inf High/Low as missing. That is deliberate (a
    # corrupt tick must not pin the channel at infinity) and differs from the
    # pre-refactor Series.max(); see ADR 0007.
    prior_high = (
        daily["High"].astype(float).rolling(_DONCHIAN_DAYS, min_periods=1).max().shift(1).to_numpy()
    )
    prior_low = (
        daily["Low"].astype(float).rolling(_DONCHIAN_DAYS, min_periods=1).min().shift(1).to_numpy()
    )
    has_channel = n_closes >= _DONCHIAN_DAYS + 1
    breakout = np.select(
        [
            has_channel & (last >= prior_high * (1.0 - _BREAKOUT_TOL)),
            has_channel & (last <= prior_low * (1.0 + _BREAKOUT_TOL)),
        ],
        ["up", "down"],
        "none",
    )

    strength = _adx_proxy_series(daily).to_numpy()

    # Direction: combine MA structure + recent slope. Require BOTH price above
    # sma50 AND a positive 50/200 relationship (or rising sma50) for 'up'.
    slope_ref = np.where(n_closes >= _SMA_FAST + _SLOPE_LOOKBACK, sma50_lagged, sma50)
    sma50_rising = sma50 >= slope_ref
    direction = np.select(
        [
            has_sma50 & (last > sma50) & sma50_rising & (sma_cross != "death"),
            has_sma50 & (last < sma50) & ~sma50_rising & (sma_cross != "golden"),
        ],
        ["up", "down"],
        "sideways",
    )

    with np.errstate(divide="ignore", invalid="ignore"):
        bps = np.where(has_sma50 & (sma50 > 0), (last - sma50) / sma50 * 10_000.0, np.nan)

    frame["direction"] = np.where(sufficient, direction, "sideways").astype(object)
    frame["donchian_breakout"] = np.where(sufficient, breakout, "none").astype(object)
    frame["sma_50_200_cross"] = np.where(sufficient, sma_cross, "none").astype(object)
    frame["trend_strength"] = np.where(sufficient, strength, 0.0)
    frame["close_vs_sma50_bps"] = np.where(sufficient, bps, np.nan)
    frame["sufficient_data"] = sufficient
    return frame


def build_daily_trend(daily: pd.DataFrame) -> DailyTrend:
    """Classify the daily trend from a daily OHLC frame — the verdict for its
    last bar in `compute_trend_frame`.

    Defensive: returns a neutral 'sideways'/'none' verdict when there are too
    few bars to judge.
    """
    neutral = DailyTrend(
        direction="sideways", donchian_breakout="none", sma_50_200_cross="none",
        trend_strength=0.0, close_vs_sma50_bps=None,
        summary="insufficient daily data",
    )
    if daily is None or "Close" not in getattr(daily, "columns", []):
        return neutral
    frame = compute_trend_frame(daily)
    if frame.empty or not bool(frame["sufficient_data"].iloc[-1]):
        return neutral

    row = frame.iloc[-1]
    direction = str(row["direction"])
    breakout = str(row["donchian_breakout"])
    sma_cross = str(row["sma_50_200_cross"])
    strength = float(row["trend_strength"])
    bps = float(row["close_vs_sma50_bps"])

    summary = (
        f"daily {direction} trend (strength {strength:.0f}/100), "
        f"Donchian breakout {breakout}, MA cross {sma_cross}"
    )
    return DailyTrend(
        direction=direction,
        donchian_breakout=breakout,
        sma_50_200_cross=sma_cross,
        trend_strength=round(strength, 1),
        close_vs_sma50_bps=None if np.isnan(bps) else round(bps, 1),
        summary=summary,
    )
