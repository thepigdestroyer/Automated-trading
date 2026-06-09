"""Daily-timeframe trend classification (Phase 2 strategy pivot, 2026-05).

The 4-expert adversarial review concluded the bot's intraday news-chasing has
no edge, and the only durable retail edge is timeframe + patience: daily
trend-following / breakout. This module derives a `DailyTrend` from daily OHLC
bars so the executor can enforce TREND ALIGNMENT — only permit longs in a
daily uptrend and shorts in a daily downtrend, and prefer Donchian breakouts.

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


def _adx_proxy(frame: pd.DataFrame, period: int = 14) -> float:
    """A lightweight ADX-like trend-strength proxy in [0, 100].

    Full Wilder ADX is heavy; this uses the absolute directional movement
    normalised by ATR, smoothed — same intuition (strong directional move =
    high value, chop = low) without the full DI+/DI- apparatus.
    """
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


def build_daily_trend(daily: pd.DataFrame) -> DailyTrend:
    """Classify the daily trend from a daily OHLC frame.

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
    close = daily["Close"].astype(float).dropna()
    if len(close) < 30:
        return neutral

    last = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    # SMA 50/200 cross (golden/death) — only when both available.
    sma_cross = "none"
    if sma50 is not None and sma200 is not None:
        if sma50 > sma200:
            sma_cross = "golden"
        elif sma50 < sma200:
            sma_cross = "death"

    # Donchian breakout over the last N days (exclude the current bar from the
    # channel so "breaking the prior N-day high" is meaningful).
    breakout = "none"
    if len(close) >= _DONCHIAN_DAYS + 1:
        high = daily["High"].astype(float)
        low = daily["Low"].astype(float)
        prior_high = float(high.iloc[-(_DONCHIAN_DAYS + 1):-1].max())
        prior_low = float(low.iloc[-(_DONCHIAN_DAYS + 1):-1].min())
        if last >= prior_high * (1.0 - _BREAKOUT_TOL):
            breakout = "up"
        elif last <= prior_low * (1.0 + _BREAKOUT_TOL):
            breakout = "down"

    strength = _adx_proxy(daily)

    # Direction: combine MA structure + recent slope. Require BOTH price above
    # sma50 AND a positive 50/200 relationship (or rising sma50) for 'up'.
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
