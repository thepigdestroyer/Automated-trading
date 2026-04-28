from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from trading_bot.models import IndicatorSnapshot


def _last_value(series: pd.Series) -> float:
    cleaned = series.dropna()
    if cleaned.empty:
        raise ValueError("indicator series is empty after dropping NaNs")
    return float(cleaned.iloc[-1])


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low),
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def _volume_ratio(volume: pd.Series) -> float | None:
    if volume.isna().all() or (volume == 0).all():
        return None
    sma20 = volume.rolling(20).mean()
    ratio = volume / sma20.replace(0.0, np.nan)
    cleaned = ratio.dropna()
    if cleaned.empty:
        return None
    return float(cleaned.iloc[-1])


def _obv_slope(close: pd.Series, volume: pd.Series, window: int = 5) -> float | None:
    if volume.isna().all() or (volume == 0).all():
        return None
    direction = np.sign(close.diff().fillna(0.0))
    obv = (direction * volume).cumsum()
    tail = obv.tail(window).dropna()
    if len(tail) < 2:
        return None
    x = np.arange(len(tail), dtype=float)
    slope = float(np.polyfit(x, tail.values.astype(float), 1)[0])
    return slope


def build_indicator_snapshot(frame: pd.DataFrame) -> IndicatorSnapshot:
    required_columns = {"Open", "High", "Low", "Close"}
    missing = required_columns.difference(frame.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    if len(frame.index) < 50:
        raise ValueError("at least 50 bars are required to compute indicators safely")

    close = frame["Close"].astype(float)
    volume = frame.get("Volume")
    if volume is None:
        volume = pd.Series(0.0, index=close.index)
    else:
        volume = volume.astype(float)

    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    returns = close.pct_change()

    return IndicatorSnapshot(
        price=_last_value(close),
        sma_20=_last_value(close.rolling(20).mean()),
        sma_50=_last_value(close.rolling(50).mean()),
        ema_12=_last_value(ema_12),
        ema_26=_last_value(ema_26),
        rsi_14=_last_value(_rsi(close, period=14)),
        macd=_last_value(macd_line),
        macd_signal=_last_value(macd_signal),
        atr_14=_last_value(_atr(frame, period=14)),
        volatility_20d=_last_value(returns.rolling(20).std() * np.sqrt(252)),
        return_1d=_last_value(returns),
        return_5d=_last_value(close.pct_change(5)),
        volume_ratio_vs_20d=_volume_ratio(volume),
        obv_slope_5d=_obv_slope(close, volume),
    )


def compute_higher_tf_fields(*, daily: pd.DataFrame, weekly: pd.DataFrame) -> dict[str, Any]:
    daily_close = daily["Close"].astype(float).dropna()
    weekly_close = weekly["Close"].astype(float).dropna()

    fields: dict[str, Any] = {
        "daily_sma_200": None,
        "daily_sma_200_distance_bps": None,
        "weekly_return_5w": None,
    }
    if len(daily_close) >= 200:
        sma200 = daily_close.rolling(200).mean().iloc[-1]
        price = float(daily_close.iloc[-1])
        fields["daily_sma_200"] = float(sma200)
        if sma200 > 0:
            fields["daily_sma_200_distance_bps"] = float((price - sma200) / sma200 * 10_000.0)
    if len(weekly_close) >= 6:
        fields["weekly_return_5w"] = float(
            (weekly_close.iloc[-1] - weekly_close.iloc[-6]) / weekly_close.iloc[-6]
        )
    return fields
