from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from t212_cfd_bot.config import Settings
from t212_cfd_bot.models import AccountSnapshot, DecisionEnvelope, ExecutionPlan, OpenPosition, SymbolContext
from t212_cfd_bot.instruments import get_instrument
from t212_cfd_bot.portfolio import CorrelationCap, CurrencyExposureAggregator

LOGGER = logging.getLogger(__name__)


def confidence_multiplier(
    confidence: float,
    *,
    min_conf: float,
    max_mult: float,
    ceil_conf: float = 0.95,
) -> float:
    if ceil_conf <= min_conf:
        return 1.0
    t = (confidence - min_conf) / (ceil_conf - min_conf)
    t = max(0.0, min(1.0, t))
    return 1.0 + t * (max_mult - 1.0)


def atr_based_quantity(
    *,
    target_cash_risk: float,
    price: float,
    atr14: float,
    atr_mult: float,
    stop_pct_floor: float,
) -> float:
    atr_stop = atr14 * atr_mult
    floor_stop = price * stop_pct_floor
    stop_distance = max(atr_stop, floor_stop)
    if stop_distance <= 0 or price <= 0:
        return 0.0
    return target_cash_risk / stop_distance


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_execution_plans(
        self,
        decision: DecisionEnvelope,
        symbol_contexts: dict[str, SymbolContext],
        account_snapshot: AccountSnapshot,
        last_trade_times: dict[str, datetime],
        guardrail_context: dict | None = None,
    ) -> list[ExecutionPlan]:
        plans: list[ExecutionPlan] = []
        open_positions = account_snapshot.open_positions
        open_positions_by_symbol = self._group_positions(open_positions)
        available_slots = max(self.settings.max_open_positions - len(open_positions), 0)
        new_trade_count = 0
        guardrail_context = guardrail_context or {}

        for trade in decision.trades:
            symbol = trade.symbol.upper()
            action = trade.action

            if action == "HOLD":
                continue

            if action != "CLOSE" and trade.confidence < self.settings.min_model_confidence:
                LOGGER.info("Rejected %s %s: confidence %.2f below threshold", action, symbol, trade.confidence)
                continue

            if action == "CLOSE":
                position = self._find_position(trade.close_position_id, symbol, open_positions_by_symbol)
                if not position:
                    LOGGER.info("Rejected CLOSE %s: no matching open position", symbol)
                    continue
                context = symbol_contexts.get(symbol)
                target_price = context.indicators.price if context else position.current_price
                if target_price is None or target_price <= 0:
                    LOGGER.info(
                        "Rejected CLOSE %s: no valid market price was available from context or open position",
                        symbol,
                    )
                    continue
                plans.append(
                    ExecutionPlan(
                        symbol=symbol,
                        action="CLOSE",
                        confidence=trade.confidence,
                        reason=trade.thesis,
                        target_price=target_price,
                        position_id=position.position_id,
                        max_holding_hours=trade.max_holding_hours,
                    )
                )
                continue

            if symbol not in symbol_contexts:
                LOGGER.info("Rejected %s %s: symbol not in current context", action, symbol)
                continue

            context = symbol_contexts[symbol]
            price = context.indicators.price
            if price <= 0:
                LOGGER.info("Rejected %s %s: invalid market price %.4f", action, symbol, price)
                continue

            positions_for_symbol = open_positions_by_symbol.get(symbol, [])
            if len(positions_for_symbol) > 1:
                LOGGER.info(
                    "Rejected %s %s: multiple open positions exist for the symbol; require explicit close_position_id",
                    action,
                    symbol,
                )
                continue

            existing_position = positions_for_symbol[0] if positions_for_symbol else None
            reversing_position = False
            if existing_position:
                existing_direction = self._normalize_position_direction(existing_position)
                desired_direction = "buy" if action == "BUY" else "sell"
                if existing_direction == desired_direction:
                    LOGGER.info("Rejected %s %s: position already open in the same direction", action, symbol)
                    continue
                close_plan = ExecutionPlan(
                    symbol=symbol,
                    action="CLOSE",
                    confidence=trade.confidence,
                    reason=f"Reverse existing position before {action}: {trade.thesis}",
                    target_price=price,
                    position_id=existing_position.position_id,
                    max_holding_hours=trade.max_holding_hours or self.settings.default_max_holding_hours,
                )
                plans.append(close_plan)

                if action == "SELL" and not self.settings.allow_shorts:
                    LOGGER.info(
                        "Converted SELL %s into CLOSE because shorting is disabled",
                        symbol,
                    )
                    continue

                if not self.settings.allow_position_reversals:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because reversals are disabled",
                        action,
                        symbol,
                    )
                    continue

                reversing_position = True

            entry_block_reason = self._entry_block_reason(
                symbol=symbol,
                action=action,
                context=context,
                guardrail_context=guardrail_context,
            )
            if entry_block_reason:
                if reversing_position:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because %s",
                        action,
                        symbol,
                        entry_block_reason,
                    )
                    continue
                LOGGER.info("Rejected %s %s: %s", action, symbol, entry_block_reason)
                continue

            if action == "SELL" and not self.settings.allow_shorts:
                LOGGER.info("Rejected SELL %s: shorting disabled", symbol)
                continue

            if self._in_cooldown(symbol, last_trade_times) and not reversing_position:
                LOGGER.info("Rejected %s %s: cooldown active", action, symbol)
                continue

            if new_trade_count >= self.settings.max_new_trades_per_cycle:
                if reversing_position:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because max new trades for cycle was reached",
                        action,
                        symbol,
                    )
                    continue
                LOGGER.info("Rejected %s %s: reached max new trades for cycle", action, symbol)
                continue
            if available_slots <= 0 and not reversing_position:
                LOGGER.info("Rejected %s %s: no account slots available", action, symbol)
                continue

            stop_loss_pct = trade.stop_loss_pct or self.settings.stop_loss_pct
            take_profit_pct = trade.take_profit_pct or self.settings.take_profit_pct
            if take_profit_pct <= stop_loss_pct:
                take_profit_pct = max(take_profit_pct, stop_loss_pct * 1.5)

            base_cash_risk = max(account_snapshot.free_funds, 0.0) * self.settings.risk_per_trade_pct_base
            if base_cash_risk <= 0:
                if reversing_position:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because no free funds were available for the reverse leg",
                        action,
                        symbol,
                    )
                    continue
                LOGGER.info("Rejected %s %s: no free funds available", action, symbol)
                continue

            mult = confidence_multiplier(
                trade.confidence,
                min_conf=self.settings.min_model_confidence,
                max_mult=self.settings.risk_confidence_scaling_max,
            )
            target_cash_risk = base_cash_risk * mult

            atr14 = float(context.indicators.atr_14 or 0.0)
            quantity = atr_based_quantity(
                target_cash_risk=target_cash_risk,
                price=price,
                atr14=atr14,
                atr_mult=self.settings.atr_stop_multiplier,
                stop_pct_floor=self.settings.stop_loss_pct_floor,
            )

            notional = quantity * price
            if notional > self.settings.max_notional_per_trade:
                quantity = self.settings.max_notional_per_trade / price
            if quantity * price > account_snapshot.free_funds:
                quantity = account_snapshot.free_funds / price
            quantity = round(quantity, 4)

            if quantity <= 0:
                if reversing_position:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because reverse quantity %.4f was invalid",
                        action,
                        symbol,
                        quantity,
                    )
                    continue
                LOGGER.info("Rejected %s %s: computed quantity %.4f is invalid", action, symbol, quantity)
                continue
            if action == "SELL":
                quantity = -quantity

            stop_distance = max(atr14 * self.settings.atr_stop_multiplier, price * self.settings.stop_loss_pct_floor, 0.01)
            stop_distance = round(stop_distance, 4)
            take_profit_distance = round(max(stop_distance * (take_profit_pct / max(stop_loss_pct, 0.0001)), 0.01), 4)

            instrument = get_instrument(symbol)
            if instrument is not None:
                instruments_map = {
                    get_instrument(s).symbol: get_instrument(s)
                    for s in ({symbol} | set(open_positions_by_symbol.keys()))
                    if get_instrument(s) is not None
                }

                corr = CorrelationCap(cap=self.settings.correlation_cap)
                if corr.would_breach(symbol, list(open_positions_by_symbol.keys()), instruments=instruments_map):
                    if reversing_position:
                        LOGGER.info("Converted %s %s into CLOSE (correlation cap)", action, symbol)
                        continue
                    LOGGER.info("Rejected %s %s: correlation cap", action, symbol)
                    continue

                abs_qty = abs(quantity)
                new_notional = abs_qty * price
                exposure = CurrencyExposureAggregator(instruments=instruments_map).aggregate(
                    open_positions, account_snapshot.total_funds
                )
                new_currency = CurrencyExposureAggregator._infer_currency(instrument)
                projected_pct = exposure.get(new_currency, 0.0) + (
                    new_notional / max(account_snapshot.total_funds, 1.0)
                )
                if projected_pct > self.settings.max_currency_exposure_pct:
                    if reversing_position:
                        LOGGER.info("Converted %s %s into CLOSE (currency exposure cap)", action, symbol)
                        continue
                    LOGGER.info(
                        "Rejected %s %s: currency exposure %.2f > cap %.2f",
                        action, symbol, projected_pct, self.settings.max_currency_exposure_pct,
                    )
                    continue

                total_notional = sum(
                    abs(p.quantity) * float(p.current_price or 0.0) for p in open_positions
                ) + new_notional
                if total_notional > self.settings.max_total_leverage * max(account_snapshot.total_funds, 1.0):
                    if reversing_position:
                        LOGGER.info("Converted %s %s into CLOSE (portfolio leverage cap)", action, symbol)
                        continue
                    LOGGER.info("Rejected %s %s: portfolio leverage exceeded", action, symbol)
                    continue

            atr14_val = float(context.indicators.atr_14 or 0.0)
            if atr14_val > 0 and stop_distance > atr14_val * 3.0:
                LOGGER.info("Rejected %s %s: stop wider than 3× ATR (%.4f > %.4f)", action, symbol, stop_distance, atr14_val * 3.0)
                continue
            if abs(quantity) * price > 2 * self.settings.max_notional_per_trade:
                LOGGER.info("Rejected %s %s: notional exceeds 2× cap (sanity)", action, symbol)
                continue

            plans.append(
                ExecutionPlan(
                    symbol=symbol,
                    action=action,
                    confidence=trade.confidence,
                    reason=trade.thesis,
                    quantity=quantity,
                    target_price=price,
                    stop_distance=stop_distance,
                    take_profit_distance=take_profit_distance,
                    max_holding_hours=trade.max_holding_hours or self.settings.default_max_holding_hours,
                )
            )
            if not reversing_position:
                available_slots -= 1
            new_trade_count += 1

        return plans

    def _in_cooldown(self, symbol: str, last_trade_times: dict[str, datetime]) -> bool:
        last_trade_at = last_trade_times.get(symbol)
        if last_trade_at is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.settings.cooldown_minutes)
        return last_trade_at > cutoff

    @staticmethod
    def _group_positions(open_positions: list[OpenPosition]) -> dict[str, list[OpenPosition]]:
        grouped: dict[str, list[OpenPosition]] = {}
        for position in open_positions:
            grouped.setdefault(position.symbol.upper(), []).append(position)
        return grouped

    @staticmethod
    def _find_position(
        close_position_id: str | None,
        symbol: str,
        open_positions_by_symbol: dict[str, list[OpenPosition]],
    ) -> OpenPosition | None:
        if close_position_id:
            for positions in open_positions_by_symbol.values():
                for position in positions:
                    if position.position_id == close_position_id:
                        return position
            return None

        positions = open_positions_by_symbol.get(symbol, [])
        if len(positions) == 1:
            return positions[0]
        return None

    @staticmethod
    def _normalize_position_direction(position: OpenPosition) -> str:
        if position.direction in {"buy", "sell"}:
            return position.direction
        if position.quantity < 0:
            return "sell"
        return "buy"

    def _entry_block_reason(
        self,
        *,
        symbol: str,
        action: str,
        context: SymbolContext,
        guardrail_context: dict,
    ) -> str | None:
        if action not in {"BUY", "SELL"}:
            return None

        if guardrail_context.get("block_new_entries"):
            reasons = guardrail_context.get("block_reasons") or []
            return "; ".join(str(reason) for reason in reasons if reason) or "new entries are blocked"

        session_open = (guardrail_context.get("session_open") or {}).get(symbol, True)
        if not session_open:
            return f"{symbol} exchange session is closed (holiday or outside hours)"

        now = datetime.now(timezone.utc)
        window_minutes = self.settings.high_impact_event_block_minutes
        for event in guardrail_context.get("econ_events") or []:
            try:
                when = datetime.fromisoformat(str(event.get("when")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            lo = when - timedelta(minutes=window_minutes)
            hi = when + timedelta(minutes=max(15, window_minutes // 2))
            impact = str(event.get("impact", "")).lower()
            if impact not in {"medium", "high"}:
                continue
            if lo <= now <= hi:
                return f"within {window_minutes}m of {impact.upper()} event: {event.get('title')}"

        earnings = (guardrail_context.get("earnings_events") or {}).get(symbol)
        if earnings is not None:
            try:
                edate_raw = earnings.get("date") if isinstance(earnings, dict) else None
                if edate_raw:
                    edate = datetime.fromisoformat(str(edate_raw).replace("Z", "+00:00"))
                    if edate.tzinfo is None:
                        edate = edate.replace(tzinfo=timezone.utc)
                    secs = (edate - now).total_seconds()
                    if 0 <= secs <= self.settings.earnings_block_hours * 3600:
                        return f"earnings within {self.settings.earnings_block_hours}h"
            except (ValueError, TypeError):
                pass

        pause_raw = (guardrail_context.get("consecutive_loss_state") or {}).get("pause_until")
        if pause_raw:
            try:
                pause_until = datetime.fromisoformat(str(pause_raw).replace("Z", "+00:00"))
                if pause_until.tzinfo is None:
                    pause_until = pause_until.replace(tzinfo=timezone.utc)
                if pause_until > now:
                    return f"consecutive-loss pause until {pause_until.isoformat()}"
            except (ValueError, TypeError):
                pass

        if (
            self.settings.max_symbol_volatility_20d is not None
            and context.indicators.volatility_20d > self.settings.max_symbol_volatility_20d
        ):
            return (
                f"20d volatility {context.indicators.volatility_20d:.2f} exceeds "
                f"configured limit {self.settings.max_symbol_volatility_20d:.2f}"
            )

        if self.settings.prt_confirmation_mode == "required":
            prt = context.prorealtime
            if prt is None:
                return "required ProRealTime confirmation was missing"
            if not self._prorealtime_confirms_trade(action, prt):
                return "required ProRealTime confirmation did not align with the trade direction"

        return None

    @staticmethod
    def _prorealtime_confirms_trade(action: str, prt) -> bool:
        entry_signal = prt.entry_signal
        trend_bias = prt.trend_bias
        if action == "BUY":
            return entry_signal == "long" or trend_bias == "bullish"
        if action == "SELL":
            return entry_signal == "short" or trend_bias == "bearish"
        return True
