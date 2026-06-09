from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from trading_bot.config import Settings
from trading_bot.models import AccountSnapshot, DecisionEnvelope, ExecutionPlan, OpenPosition, SymbolContext
from trading_bot.instruments import get_instrument
from trading_bot.portfolio import CorrelationCap, CurrencyExposureAggregator

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


def _round_trip_cost_price_units(
    *,
    price: float,
    spread_bps: float,
    slippage_bps: float,
    swap_annualised_bps: float,
    hold_hours: float,
) -> float:
    """Estimate the round-trip trading cost of a position, in PRICE-units.

    Components (all as a fraction of price, then × price):
      - spread: paid once on a round trip (the quoted spread already covers
        buy@ask / sell@bid), so spread_bps once.
      - slippage: applied on BOTH entry and exit → 2 × slippage_bps.
      - financing/swap: annualised bps pro-rated over the holding period
        (hold_hours / hours_per_year).

    Used by the cost-vs-edge gate to refuse entries whose targeted reward
    cannot clear frictions. Returns 0.0 for non-positive price.
    """
    if price <= 0:
        return 0.0
    hours_per_year = 365.25 * 24.0
    spread_frac = spread_bps / 10_000.0
    slippage_frac = 2.0 * slippage_bps / 10_000.0
    financing_frac = (swap_annualised_bps / 10_000.0) * (
        max(hold_hours, 0.0) / hours_per_year
    )
    return price * (spread_frac + slippage_frac + financing_frac)


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

        # Per review 2026-05-05-adhoc-1 #5: when the cycle is running in
        # close-only mode (daily drawdown breached AND positions open),
        # reject any non-CLOSE/HOLD plan with reason
        # ``drawdown_close_only_mode``. Layered on top of the existing
        # auto_flatten_on_daily_loss path: that path injects CLOSE
        # plans deterministically; this filter prevents the LLM from
        # ALSO emitting BUY/SELL ideas through the prompt that would
        # otherwise progress to broker submission.
        close_only_mode = bool(
            (guardrail_context or {}).get("close_only_mode")
        )

        for trade in decision.trades:
            symbol = trade.symbol.upper()
            action = trade.action

            if action == "HOLD":
                continue

            # Per review 2026-05-05-adhoc-1 #5: hard filter for
            # close-only mode. Must precede every other gate so we never
            # spend cycles validating a plan we would reject anyway.
            if close_only_mode and action not in {"CLOSE", "HOLD"}:
                LOGGER.info(
                    "Rejected %s %s: drawdown_close_only_mode (daily "
                    "drawdown breached, only CLOSE/HOLD plans allowed)",
                    action, symbol,
                )
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

            # Phase 1.5: the executor owns stop/TP placement deterministically.
            # The stop distance is ATR-based (computed below); the take-profit
            # is stop_distance × risk_reward_ratio. The LLM's stop_loss_pct /
            # take_profit_pct (if present) are advisory only and NOT used for
            # placement — this removes the prior disconnect where the prompt
            # asked the model for a stop the code then discarded.
            risk_reward = max(float(self.settings.risk_reward_ratio), 0.1)

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
            
            instrument_for_scale = get_instrument(symbol)
            scale = float(getattr(instrument_for_scale, "ig_price_scale", 1.0) or 1.0)
            
            unscaled_stop = max(atr14 * self.settings.atr_stop_multiplier, price * self.settings.stop_loss_pct_floor, 0.01)
            stop_distance = round(unscaled_stop * scale, 4) if scale > 0 and scale != 1.0 else round(unscaled_stop, 4)
            take_profit_distance = round(max(stop_distance * risk_reward, 0.01), 4)
            
            point_value = float(getattr(self.settings, "point_value_per_unit", 1.0) or 1.0)
            
            if stop_distance <= 0 or price <= 0:
                quantity = 0.0
            else:
                quantity = target_cash_risk / (stop_distance * point_value)

            # Notional check (computed in price-units space for backward compatibility)
            unscaled_qty = quantity * scale if scale > 0 and scale != 1.0 else quantity
            notional = unscaled_qty * price
            if notional > self.settings.max_notional_per_trade:
                unscaled_qty = self.settings.max_notional_per_trade / price
                quantity = unscaled_qty / scale if scale > 0 and scale != 1.0 else unscaled_qty
                
            if unscaled_qty * price > account_snapshot.free_funds:
                unscaled_qty = account_snapshot.free_funds / price
                quantity = unscaled_qty / scale if scale > 0 and scale != 1.0 else unscaled_qty
                
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
            if atr14_val > 0 and unscaled_stop > atr14_val * 3.0:
                LOGGER.info("Rejected %s %s: stop wider than 3× ATR (%.4f > %.4f)", action, symbol, unscaled_stop, atr14_val * 3.0)
                continue
            if abs(unscaled_qty) * price > 2 * self.settings.max_notional_per_trade:
                LOGGER.info("Rejected %s %s: notional exceeds 2× cap (sanity)", action, symbol)
                continue

            # Per review 2026-05-05-adhoc-1 #1 (defence-in-depth):
            # SIZE_VS_BUDGET_BREACH guard. Even after the qty
            # re-derivation above, refuse to submit any plan whose
            # implied cash-risk-at-stop exceeds the per-trade budget by
            # more than ``risk_size_vs_budget_tolerance`` (default 25%).
            # Catches future scaling bugs / config inversions / non-uniform
            # point-value instruments before they hit the broker.
            #
            # cash_risk_at_stop = |qty| × stop_distance (in IG points)
            #                    × point_value (£/pt; assumed 1 for now,
            #                      configurable via settings.point_value
            #                      if/when we onboard non-£1/pt instruments).
            tolerance = float(
                getattr(self.settings, "risk_size_vs_budget_tolerance", 0.25) or 0.25
            )
            cash_risk_at_stop = abs(quantity) * stop_distance * point_value
            budget_ceiling = target_cash_risk * (1.0 + tolerance)
            if cash_risk_at_stop > budget_ceiling and target_cash_risk > 0:
                LOGGER.warning(
                    "Rejected %s %s: SIZE_VS_BUDGET_BREACH cash_risk=%.2f > budget=%.2f "
                    "(tolerance %.0f%%; qty=%s scaled_stop=%s point_value=%s)",
                    action, symbol, cash_risk_at_stop, budget_ceiling,
                    tolerance * 100, quantity, stop_distance, point_value,
                )
                continue

            # Per review 2026-04-29-adhoc-1 #2: reject plans whose
            # take_profit_distance is below the configured floor (default
            # disabled). Mirrors the stop-vs-spread gate; prevents IG
            # silently clamping the limit to its 12pt minimum and
            # producing a 1:1 RR product (incident 2026-04-29 09:56–10:04
            # WTI fills came back with limitLevel = entry + 12pts despite
            # planned TP of 215+ pts). Configure with min_take_profit_distance.
            min_tp = float(getattr(self.settings, "min_take_profit_distance", 0.0) or 0.0)
            if min_tp > 0 and take_profit_distance < min_tp:
                LOGGER.info(
                    "Rejected %s %s: take_profit_distance %.4f < min %.4f (target floor)",
                    action, symbol, take_profit_distance, min_tp,
                )
                continue

            # Cost-vs-edge gate (2026-05 adversarial review). Refuse any entry
            # whose targeted reward does not clear the round-trip cost by
            # cost_edge_multiple. All computed in PRICE-units (un-scale the
            # IG-point take_profit_distance) so spread/financing are comparable.
            cost_edge_multiple = float(
                getattr(self.settings, "cost_edge_multiple", 0.0) or 0.0
            )
            if cost_edge_multiple > 0 and price > 0:
                reward_price_units = (
                    take_profit_distance / scale if scale > 0 and scale != 1.0
                    else take_profit_distance
                )
                hold_hours = float(
                    trade.max_holding_hours or self.settings.default_max_holding_hours
                )
                cost_price_units = _round_trip_cost_price_units(
                    price=price,
                    spread_bps=float(instrument_for_scale.max_spread_bps),
                    slippage_bps=float(self.settings.slippage_buffer_bps),
                    swap_annualised_bps=float(self.settings.swap_annualised_bps_default),
                    hold_hours=hold_hours,
                )
                required = cost_price_units * cost_edge_multiple
                if reward_price_units < required:
                    LOGGER.info(
                        "Rejected %s %s: COST_VS_EDGE_BREACH reward=%.4f < %.1fx cost=%.4f "
                        "(spread=%dbps slippage=%dbps hold=%.0fh) — edge does not clear frictions",
                        action, symbol, reward_price_units, cost_edge_multiple,
                        cost_price_units, instrument_for_scale.max_spread_bps,
                        self.settings.slippage_buffer_bps, hold_hours,
                    )
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
            post_window = self.settings.high_impact_post_event_block_minutes
            hi = when + timedelta(minutes=post_window)
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
