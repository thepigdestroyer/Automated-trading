"""Deterministic quantitative signal pre-classification.

Phase 1.1/1.2 of the 2026-05 profitability roadmap. Converts a raw OHLCV
frame into a ``SignalPack`` of human-readable LABELS (trend / momentum /
mean-reversion z-score / Bollinger / Donchian / VWAP / vol-regime /
volume-state) that the LLM consumes alongside the existing raw indicators.

Rationale: the bot previously fed the LLM ~15 raw floats (rsi_14=73.2,
macd=0.084, ...) and asked it to reason about whether they mean "overbought"
or "trending" in prose, every cycle. LLMs are unreliable at arithmetic and
threshold comparison; numpy is not. Pre-classifying the signals deterministically
lets the LLM do what it is good at (narrative integration of news + context)
and removes the lowest-edge surface in the stack.

All functions are pure and defensive: short frames, all-NaN columns, and
zero-volume instruments degrade to neutral labels rather than raising.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.models import IndicatorSnapshot, SignalPack


def _safe_last(series: pd.Series) -> float | None:
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    return float(cleaned.iloc[-1])


def _classify_trend(snap: IndicatorSnapshot) -> str:
    """SMA stack alignment + long-term 200d distance.

    up_strong: price > sma20 > sma50 AND well above 200d
    up:        price > sma20 > sma50
    down_strong / down: mirror
    neutral:   mixed stack
    """
    price, sma20, sma50 = snap.price, snap.sma_20, snap.sma_50
    dist_200 = snap.daily_sma_200_distance_bps  # may be None
    stacked_up = price > sma20 > sma50
    stacked_down = price < sma20 < sma50
    if stacked_up:
        if dist_200 is not None and dist_200 > 300:  # >3% above 200d
            return "up_strong"
        return "up"
    if stacked_down:
        if dist_200 is not None and dist_200 < -300:
            return "down_strong"
        return "down"
    return "neutral"


def _classify_momentum(snap: IndicatorSnapshot, *, macd_hist_prev: float | None) -> str:
    """MACD histogram trajectory cross-checked with RSI level.

    accelerating: histogram positive and growing (or negative and shrinking
                  toward zero from a recovering RSI)
    fading:       histogram positive but shrinking
    reversing:    histogram crossed sign vs prior bar
    steady:       otherwise
    """
    hist = snap.macd - snap.macd_signal
    if macd_hist_prev is None:
        # No prior bar: infer from sign + RSI.
        if hist > 0 and snap.rsi_14 >= 50:
            return "accelerating"
        if hist < 0 and snap.rsi_14 <= 50:
            return "reversing"
        return "steady"
    crossed = (hist > 0) != (macd_hist_prev > 0)
    if crossed:
        # A genuine sign change of the histogram = momentum reversing.
        return "reversing"
    # Within a consistent sign, GROWING magnitude = momentum accelerating in
    # that direction (whether bullish hist>0 or bearish hist<0); SHRINKING
    # magnitude = momentum fading toward a possible cross. The prior version
    # inverted this for hist<=0 (called a growing bearish histogram
    # "reversing" and a fading one "steady"), feeding the LLM a backwards
    # momentum label on every down-move.
    growing = abs(hist) > abs(macd_hist_prev)
    return "accelerating" if growing else "fading"


def _classify_rsi(rsi: float, *, rsi_prev: float | None) -> str:
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    if rsi_prev is not None:
        if rsi - rsi_prev > 3:
            return "rising"
        if rsi_prev - rsi > 3:
            return "falling"
    return "neutral"


def _mean_reversion_z(close: pd.Series, window: int = 20) -> float:
    """Z-score of the latest price vs its rolling N-bar mean/std.

    This is the Bollinger position expressed in standard deviations:
    z = (price - sma) / std. 0 = at mean, +2 = upper 2σ band, -2 = lower.
    """
    if len(close) < window:
        return 0.0
    sma = close.rolling(window).mean().iloc[-1]
    std = close.rolling(window).std().iloc[-1]
    last = float(close.iloc[-1])
    if std is None or np.isnan(std) or std == 0:
        return 0.0
    return float((last - sma) / std)


def _bollinger_position(z: float) -> str:
    if z >= 2.0:
        return "above_upper"
    if z >= 0.0:
        return "upper_half"
    if z > -2.0:
        return "lower_half"
    return "below_lower"


def _donchian_position(frame: pd.DataFrame, window: int = 20) -> float:
    """Where the latest close sits in the N-bar high/low channel.

    0.0 = at the N-bar low, 1.0 = at the N-bar high. Returns 0.5 when the
    channel is degenerate (high == low) or the frame is too short.
    """
    if len(frame) < window:
        window = len(frame)
    if window < 2:
        return 0.5
    high = frame["High"].astype(float).tail(window).max()
    low = frame["Low"].astype(float).tail(window).min()
    last = float(frame["Close"].astype(float).iloc[-1])
    if high <= low:
        return 0.5
    return float((last - low) / (high - low))


def _vwap_deviation_bps(frame: pd.DataFrame, window: int = 20) -> float | None:
    """Deviation of the latest close from the volume-weighted average price
    over the last N bars, in basis points. None when volume is unavailable.
    """
    volume = frame.get("Volume")
    if volume is None:
        return None
    vol = volume.astype(float).tail(window)
    if vol.isna().all() or (vol.fillna(0.0) == 0).all():
        return None
    typical = (
        frame["High"].astype(float)
        + frame["Low"].astype(float)
        + frame["Close"].astype(float)
    ) / 3.0
    typical = typical.tail(window)
    denom = float(vol.sum())
    if denom <= 0:
        return None
    vwap = float((typical * vol).sum() / denom)
    last = float(frame["Close"].astype(float).iloc[-1])
    if vwap <= 0:
        return None
    return float((last - vwap) / vwap * 10_000.0)


def _classify_vol_regime(frame: pd.DataFrame, snap: IndicatorSnapshot,
                         lookback: int = 100) -> str:
    """ATR-as-%-of-price percentile vs its own recent history.

    compressed: current ATR% in the bottom tercile of the last `lookback` bars
    expanding:  top tercile
    normal:     middle
    """
    close = frame["Close"].astype(float)
    if len(frame) < 30 or snap.price <= 0:
        return "normal"
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    atr_pct = (atr / close).dropna().tail(lookback)
    if len(atr_pct) < 10:
        return "normal"
    current = float(atr_pct.iloc[-1])
    lo = float(atr_pct.quantile(0.33))
    hi = float(atr_pct.quantile(0.67))
    if current <= lo:
        return "compressed"
    if current >= hi:
        return "expanding"
    return "normal"


def _classify_volume(snap: IndicatorSnapshot) -> str:
    ratio = snap.volume_ratio_vs_20d
    if ratio is None:
        return "unknown"
    if ratio >= 1.75:
        return "surge"
    if ratio <= 0.5:
        return "thin"
    return "normal"


def build_signal_pack(
    frame: pd.DataFrame,
    snapshot: IndicatorSnapshot,
) -> SignalPack:
    """Derive a SignalPack from the same OHLCV frame used for the snapshot.

    Defensive: never raises on short/NaN/zero-volume input — degrades to
    neutral labels. The snapshot carries the higher-TF fields (200d distance)
    that some classifiers consult.
    """
    close = frame["Close"].astype(float)

    # Prior-bar context for trajectory classifiers.
    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    macd_sig = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - macd_sig
    macd_hist_prev = float(hist.iloc[-2]) if len(hist.dropna()) >= 2 else None

    # RSI prior bar (recompute lightweight).
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    period = 14
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_series = (100 - (100 / (1 + rs))).fillna(50.0)
    rsi_prev = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 else None

    trend = _classify_trend(snapshot)
    momentum = _classify_momentum(snapshot, macd_hist_prev=macd_hist_prev)
    rsi_state = _classify_rsi(snapshot.rsi_14, rsi_prev=rsi_prev)
    z = _mean_reversion_z(close)
    bollinger = _bollinger_position(z)
    donchian = _donchian_position(frame)
    vwap_dev = _vwap_deviation_bps(frame)
    vol_regime = _classify_vol_regime(frame, snapshot)
    volume_state = _classify_volume(snapshot)

    summary = (
        f"{trend} trend, {momentum} momentum, RSI {rsi_state} ({snapshot.rsi_14:.0f}), "
        f"price {z:+.1f}σ vs 20-bar mean ({bollinger}), "
        f"Donchian {donchian:.0%}, vol {vol_regime}, volume {volume_state}"
    )

    return SignalPack(
        trend=trend,
        momentum=momentum,
        rsi_state=rsi_state,
        mean_reversion_z=round(z, 3),
        bollinger_position=bollinger,
        donchian_position=round(donchian, 3),
        vwap_deviation_bps=round(vwap_dev, 1) if vwap_dev is not None else None,
        vol_regime=vol_regime,
        volume_state=volume_state,
        summary=summary,
    )
