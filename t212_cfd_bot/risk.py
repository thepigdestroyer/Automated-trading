from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from t212_cfd_bot.config import Settings
from t212_cfd_bot.models import AccountSnapshot, DecisionEnvelope, ExecutionPlan, OpenPosition, SymbolContext

LOGGER = logging.getLogger(__name__)


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
                if (target_price is None or target_price <= 0) and self.settings.broker == "trading212":
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

            max_cash_risk = max(account_snapshot.free_funds, 0.0) * self.settings.risk_per_trade_pct
            if max_cash_risk <= 0:
                if reversing_position:
                    LOGGER.info(
                        "Converted %s %s into CLOSE because no free funds were available for the reverse leg",
                        action,
                        symbol,
                    )
                    continue
                LOGGER.info("Rejected %s %s: no free funds available", action, symbol)
                continue

            notional_cap_from_risk = max_cash_risk / stop_loss_pct
            notional = min(
                notional_cap_from_risk,
                self.settings.max_notional_per_trade,
                account_snapshot.free_funds,
            )
            quantity = round(notional / price, 4)
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

            stop_distance = max(round(price * stop_loss_pct, 4), 0.01)
            take_profit_distance = max(round(price * take_profit_pct, 4), 0.01)

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
