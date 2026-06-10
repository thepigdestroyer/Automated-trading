"""Track-A: deterministic signals-only twin strategy (Phase 3 pivot).

A mechanical daily-trend-following strategy with NO LLM in the loop:

    enter long  when daily_trend.direction == "up"   AND donchian_breakout == "up"
    enter short when daily_trend.direction == "down" AND donchian_breakout == "down"
    (both gated on trend_strength >= min_trend_strength and shorts permission)

Stops/TP mirror the live executor exactly (risk.py:290): stop =
max(ATR14 x atr_stop_multiplier, price x stop_loss_pct_floor, 0.01),
TP = stop x risk_reward_ratio — in UNSCALED data-feed price units, because
the paper simulator operates on Yahoo prices, not IG's scaled levels.

Track-A trades flow through the SAME cost-aware PaperPnL simulator as the
LLM track but into a separate state file, so `forward_eval` can produce two
comparable reports. The comparison isolates the LLM's value-add: if Track-A's
expectancy >= the LLM track's, the LLM regime-veto is not earning its tokens.

Unlike the LLM, Track-A is backtest-VALID (pure functions of price history,
no training-cutoff contamination) — but forward-walk both tracks on the same
window for the cleanest comparison.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from trading_bot.config import Settings
from trading_bot.instruments import get_instrument
from trading_bot.paper_pnl import PaperPnL

LOGGER = logging.getLogger(__name__)

# Confidence is intentionally None on Track-A rows: there is no probabilistic
# claim to calibrate — Brier/calibration sections apply to the LLM track only.


@dataclass(frozen=True)
class TrackASignal:
    symbol: str
    side: str  # "buy" | "sell"
    entry: float
    stop: float
    tp: float
    reason: str


def decide_track_a(context: Any, settings: Settings) -> TrackASignal | None:
    """Mechanical entry decision from the daily trend block. Pure; returns
    None when any requirement is missing (degrade-closed: Track-A only
    trades textbook setups)."""
    trend = getattr(context, "daily_trend", None)
    indicators = getattr(context, "indicators", None)
    if trend is None or indicators is None:
        return None
    price = float(indicators.price or 0.0)
    if price <= 0:
        return None
    min_strength = float(getattr(settings, "min_trend_strength", 0.0) or 0.0)
    if trend.trend_strength < min_strength:
        return None

    if trend.direction == "up" and trend.donchian_breakout == "up":
        side = "buy"
    elif trend.direction == "down" and trend.donchian_breakout == "down":
        if not settings.allow_shorts:
            return None
        side = "sell"
    else:
        return None

    atr14 = float(indicators.atr_14 or 0.0)
    stop_distance = max(
        atr14 * settings.atr_stop_multiplier,
        price * settings.stop_loss_pct_floor,
        0.01,
    )
    tp_distance = stop_distance * settings.risk_reward_ratio
    if side == "buy":
        stop, tp = price - stop_distance, price + tp_distance
    else:
        stop, tp = price + stop_distance, price - tp_distance
    return TrackASignal(
        symbol=str(getattr(context, "symbol", "?")),
        side=side,
        entry=price,
        stop=stop,
        tp=tp,
        reason=(
            f"daily {trend.direction} trend (strength {trend.trend_strength:.0f}) "
            f"+ Donchian breakout {trend.donchian_breakout}"
        ),
    )


def run_track_a_cycle(
    symbol_contexts: Mapping[str, Any],
    paper: PaperPnL,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[TrackASignal]:
    """Evaluate every symbol context, open paper entries for fresh signals,
    then mark the book. Returns the signals opened this cycle.

    Guards: one open Track-A position per symbol; per-symbol re-entry
    cooldown (track_a_cooldown_hours) so a persisting breakout does not
    machine-gun entries. Defensive per-symbol: one bad context cannot kill
    the cycle.
    """
    now = now or datetime.now(timezone.utc)
    open_symbols = {item.get("symbol") for item in paper.state.get("open", [])}
    last_entry: dict[str, str] = paper.state.setdefault("last_entry", {})
    cooldown_h = float(getattr(settings, "track_a_cooldown_hours", 0.0) or 0.0)

    opened: list[TrackASignal] = []
    for symbol, context in symbol_contexts.items():
        try:
            if symbol in open_symbols:
                continue
            prev = last_entry.get(symbol)
            if prev and cooldown_h > 0:
                prev_dt = datetime.fromisoformat(prev)
                if (now - prev_dt).total_seconds() < cooldown_h * 3600.0:
                    continue
            sig = decide_track_a(context, settings)
            if sig is None:
                continue
            instrument = get_instrument(symbol)
            paper.record_entry(
                symbol=symbol,
                side=sig.side,
                entry=sig.entry,
                stop=sig.stop,
                tp=sig.tp,
                opened_at=now.isoformat(),
                data_symbol=instrument.data_symbol,
                decision_id=f"trackA:{now.strftime('%Y%m%dT%H%M%S')}:{symbol}",
                confidence=None,
                spread_bps=float(instrument.max_spread_bps),
                slippage_bps=float(settings.slippage_buffer_bps),
                swap_annualised_bps=float(settings.swap_annualised_bps_default),
                max_holding_hours=float(settings.default_max_holding_hours),
            )
            last_entry[symbol] = now.isoformat()
            opened.append(sig)
            LOGGER.info("Track-A entry: %s %s — %s", sig.side.upper(), symbol, sig.reason)
        except Exception as exc:  # pragma: no cover - defensive per-symbol
            LOGGER.warning("Track-A failed for %s: %s", symbol, exc)
    paper.evaluate(now=now)
    return opened
