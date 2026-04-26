from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from threading import Event, Lock

from t212_cfd_bot.broker_factory import build_broker
from t212_cfd_bot.calendars import EarningsCalendarClient, EconomicCalendarClient, HolidayCalendar
from t212_cfd_bot.config import Settings
from t212_cfd_bot.entities import EntityNewsClient, entities_for_symbol
from t212_cfd_bot.market_data import YahooMarketDataClient
from t212_cfd_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    ExecutionResult,
    ExecutionPlan,
    Headline,
    IndicatorSnapshot,
    MarketResearchEnvelope,
    OpenPosition,
    OperatorCommandEnvelope,
    ProRealTimeSnapshotEnvelope,
    ProRealTimeSymbolSnapshot,
    SymbolContext,
    SymbolResearchEnvelope,
    TradeDecision,
)
from t212_cfd_bot.news import GoogleNewsClient
from t212_cfd_bot.news_memory import NewsMemory
from t212_cfd_bot.instruments import get_instrument, list_supported_instruments
from t212_cfd_bot.openai_decision import OpenAIDecisionEngine
from t212_cfd_bot.prorealtime import ProRealTimeDataClient
from t212_cfd_bot.risk import RiskManager
from t212_cfd_bot.vix import VixContextClient

LOGGER = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_data_client = YahooMarketDataClient(settings)
        self.prorealtime_client = ProRealTimeDataClient(settings)
        self.news_client = GoogleNewsClient(settings)
        self.entity_news_client = EntityNewsClient(settings)
        self.vix_client = VixContextClient(settings)
        self.econ_calendar = EconomicCalendarClient(settings)
        self.earnings_calendar = EarningsCalendarClient(settings)
        self.holiday_calendar = HolidayCalendar()
        self.decision_engine = OpenAIDecisionEngine(settings)
        self.risk_manager = RiskManager(settings)
        self.broker = build_broker(settings)
        self._current_cycle_id: str = ""
        self._current_entity_news: dict[str, list[Headline]] = {}
        self._current_market_vix = None
        self._current_econ_events: list = []
        self.last_trade_times: dict[str, datetime] = {}
        self.operator_lock = Lock()
        self.model_lock = Lock()
        self.wake_event = Event()
        self.stop_event = Event()
        self.operator_notes: list[str] = []
        self.focused_symbols: set[str] = set()
        self.excluded_symbols: set[str] = set()
        self.forced_close_symbols: set[str] = set()
        self.paused = False
        self.last_cycle_report: dict | None = None
        self.runtime_state = self._load_runtime_state()

    def run(self, once: bool = False) -> None:
        if self.settings.execution_mode == "live" and not self.settings.allow_live_trading:
            raise RuntimeError(
                "Refusing to run in live mode without ALLOW_LIVE_TRADING=true"
            )

        self.broker.connect()
        self._reconcile_on_startup()

        while not self.stop_event.is_set():
            if self._is_paused():
                self.wake_event.wait(timeout=1.0)
                self.wake_event.clear()
                continue

            try:
                self.run_cycle()
            except Exception:
                LOGGER.exception("Trading cycle failed")
                if once:
                    raise

            if once:
                return

            self.wake_event.wait(timeout=self.settings.poll_interval_seconds)
            self.wake_event.clear()

    def run_cycle(self) -> list[ExecutionResult]:
        cycle_started_at = datetime.now(timezone.utc)
        self._current_cycle_id = f"c_{uuid.uuid4().hex[:8]}"
        account_snapshot = self.broker.get_account_snapshot()
        self._refresh_runtime_state(account_snapshot, cycle_started_at)
        operator_context = self._get_operator_context(account_snapshot)

        news_memory_state = self.runtime_state.setdefault(
            "news_memory", {"items": [], "last_purged_at": None}
        )
        news_memory = NewsMemory(
            state=news_memory_state,
            retention_hours=self.settings.news_memory_hours,
            max_items=self.settings.news_memory_max_items,
        )

        market_headlines = self.news_client.fetch_market_headlines()
        market_headlines = news_memory.classify(market_headlines, cycle_ts=cycle_started_at.isoformat())

        entity_news = (
            self.entity_news_client.fetch_all() if self.settings.entity_tracking_enabled else {}
        )
        for key in list(entity_news.keys()):
            entity_news[key] = news_memory.classify(
                entity_news[key], cycle_ts=cycle_started_at.isoformat()
            )
        self._current_entity_news = entity_news
        self._current_market_vix = self.vix_client.fetch()
        self._current_econ_events = self.econ_calendar.events_next_hours(
            hours=self.settings.econ_calendar_lookahead_hours,
            impact_min="medium",
            currencies=None,
            now=cycle_started_at,
        )

        prorealtime_snapshot = self.prorealtime_client.load_snapshot()
        market_research = self._build_market_research(
            account_snapshot=account_snapshot,
            market_headlines=market_headlines,
            operator_context=operator_context,
            prorealtime_snapshot=prorealtime_snapshot,
        )
        active_symbols = self._select_active_symbols(
            account_snapshot=account_snapshot,
            market_headlines=market_headlines,
            market_research=market_research,
            operator_context=operator_context,
            prorealtime_snapshot=prorealtime_snapshot,
        )
        symbol_contexts = self._collect_symbol_contexts(
            active_symbols,
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
        )

        if not symbol_contexts:
            LOGGER.warning(
                "No symbol contexts were collected; skipping decision and execution for this cycle"
            )
            return []

        guardrail_context = self._build_guardrail_context(
            account_snapshot=account_snapshot,
            symbol_contexts=symbol_contexts,
            prorealtime_snapshot=prorealtime_snapshot,
            now=cycle_started_at,
        )

        news_synthesis = None
        if self.settings.news_synthesiser_enabled:
            try:
                with self.model_lock:
                    news_synthesis = self.decision_engine.synthesise_news(
                        market_headlines=market_headlines,
                        per_symbol_headlines={
                            symbol: ctx.headlines for symbol, ctx in symbol_contexts.items()
                        },
                        entity_news=self._current_entity_news,
                        open_positions=list(account_snapshot.open_positions),
                        regime_history=self.runtime_state.get("regime_history", []),
                        active_symbols=list(symbol_contexts.keys()),
                    )
            except Exception as exc:
                LOGGER.warning("News synthesiser failed: %s", exc)
                news_synthesis = None
        self._current_news_synthesis = news_synthesis

        symbol_research = self._build_symbol_research(
            account_snapshot=account_snapshot,
            symbol_contexts=symbol_contexts,
            market_headlines=market_headlines,
            market_research=market_research,
            news_synthesis=news_synthesis,
            operator_context=operator_context,
            guardrail_context=guardrail_context,
        )
        with self.model_lock:
            decision = self.decision_engine.decide(
                account_snapshot=account_snapshot,
                symbol_contexts=symbol_contexts,
                market_headlines=market_headlines,
                market_research=market_research,
                symbol_research=symbol_research,
                news_synthesis=news_synthesis,
                regime_history=self.runtime_state.get("regime_history", []),
                open_position_invalidations=self.runtime_state.get("open_position_invalidations", {}),
                operator_context=operator_context,
                guardrail_context=guardrail_context,
            )
        regime_hist = self.runtime_state.setdefault("regime_history", [])
        regime_hist.append(
            {
                "cycle_at": cycle_started_at.isoformat(),
                "regime": decision.market_regime,
            }
        )
        self.runtime_state["regime_history"] = regime_hist[-16:]
        decision = self._apply_operator_overrides(
            decision=decision,
            account_snapshot=account_snapshot,
            operator_context=operator_context,
        )
        decision = self._apply_guardrail_overrides(
            decision=decision,
            account_snapshot=account_snapshot,
            guardrail_context=guardrail_context,
        )
        self._log_prorealtime_snapshot(prorealtime_snapshot)
        self._log_market_research(market_research)
        self._log_guardrail_context(guardrail_context)
        self._log_decision_summary(decision)
        plans = self.risk_manager.build_execution_plans(
            decision=decision,
            symbol_contexts=symbol_contexts,
            account_snapshot=account_snapshot,
            last_trade_times=self.last_trade_times,
            guardrail_context=guardrail_context,
        )
        self._log_execution_plans(plans)

        results: list[ExecutionResult] = []
        blocked_symbols: set[str] = set()
        for plan in plans:
            if plan.symbol in blocked_symbols and plan.action != "CLOSE":
                results.append(
                    ExecutionResult(
                        plan=plan,
                        status="failed",
                        submitted_at=datetime.now(timezone.utc).isoformat(),
                        error=(
                            f"Skipped {plan.action} for {plan.symbol} because a required close "
                            "failed or did not confirm safely in this cycle"
                        ),
                    )
                )
                LOGGER.warning(
                    "Skipping %s %s because a required close failed or did not confirm safely",
                    plan.action,
                    plan.symbol,
                )
                continue

            LOGGER.info("Executing %s %s: %s", plan.action, plan.symbol, plan.reason)
            result = self.broker.execute(plan=plan, dry_run=self.settings.dry_run, cycle_id=self._current_cycle_id)
            results.append(result)
            if result.status in {"submitted", "dry_run"}:
                self.last_trade_times[plan.symbol] = cycle_started_at
            if plan.action == "CLOSE" and not self._close_result_allows_follow_on_entry(result):
                blocked_symbols.add(plan.symbol)
            self._log_execution_result(result)

        invalidations = self.runtime_state.setdefault("open_position_invalidations", {})
        for result in results:
            if result.plan.action in {"BUY", "SELL"} and result.status in {"submitted", "dry_run"}:
                pid = (
                    ((result.broker_response or {}).get("ack") or {}).get("dealReference")
                    or f"{result.plan.symbol}:{cycle_started_at.isoformat()}"
                )
                invalidations[pid] = {
                    "symbol": result.plan.symbol,
                    "condition": result.plan.reason,
                    "entry_at": result.submitted_at,
                    "cycle_id": self._current_cycle_id,
                }
            if result.plan.action == "CLOSE" and result.status in {"submitted", "dry_run"}:
                invalidations.pop(result.plan.position_id or "", None)

        self._refresh_runtime_state(account_snapshot, cycle_started_at)
        self.prorealtime_client.export_watchlist(
            active_symbols=active_symbols,
            market_research=market_research,
            decision=decision,
            plans=plans,
        )
        self._write_cycle_log(
            cycle_started_at=cycle_started_at,
            account_snapshot=account_snapshot.model_dump(mode="json"),
            market_headlines=[headline.model_dump(mode="json") for headline in market_headlines],
            symbol_contexts={
                symbol: context.model_dump(mode="json")
                for symbol, context in symbol_contexts.items()
            },
            active_symbols=active_symbols,
            operator_context=operator_context,
            prorealtime_snapshot=(
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            guardrail_context=guardrail_context,
            market_research=market_research.model_dump(mode="json") if market_research else None,
            symbol_research=symbol_research.model_dump(mode="json") if symbol_research else None,
            decision=decision.model_dump(mode="json"),
            plans=[plan.model_dump(mode="json") for plan in plans],
            results=[result.model_dump(mode="json") for result in results],
        )
        self.last_cycle_report = {
            "cycle_started_at": cycle_started_at.isoformat(),
            "active_symbols": active_symbols,
            "operator_context": operator_context,
            "prorealtime_snapshot": (
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            "guardrail_context": guardrail_context,
            "market_research": market_research.model_dump(mode="json") if market_research else None,
            "symbol_research": symbol_research.model_dump(mode="json") if symbol_research else None,
            "decision": decision.model_dump(mode="json"),
            "plans": [plan.model_dump(mode="json") for plan in plans],
            "results": [result.model_dump(mode="json") for result in results],
            "account_snapshot": account_snapshot.model_dump(mode="json"),
        }
        news_memory.purge(now=cycle_started_at)
        self._save_runtime_state()
        LOGGER.info(
            "Cycle complete: %d decisions approved, %d results recorded",
            len(plans),
            len(results),
        )
        return results

    def _select_active_symbols(
        self,
        *,
        account_snapshot: AccountSnapshot,
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None = None,
        operator_context: dict | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> list[str]:
        configured_symbols = [symbol.upper() for symbol in self.settings.symbols]
        discovered_symbols: list[str] = []
        prorealtime_symbols = [
            item.symbol.upper()
            for item in (prorealtime_snapshot.symbols[: self.settings.max_discovered_symbols] if prorealtime_snapshot else [])
            if item.symbol.strip()
        ]
        focused_symbols = [
            symbol.upper() for symbol in (operator_context or {}).get("focused_symbols", [])
        ]
        excluded_symbols = {
            symbol.upper() for symbol in (operator_context or {}).get("excluded_symbols", [])
        }
        forced_close_symbols = {
            symbol.upper() for symbol in (operator_context or {}).get("forced_close_symbols", [])
        }

        if self.settings.auto_discover_symbols or not configured_symbols:
            if market_research is not None:
                discovered_symbols = [
                    candidate.symbol.upper()
                    for candidate in market_research.candidates[: self.settings.max_discovered_symbols]
                ]
            else:
                try:
                    discovered_symbols = self.decision_engine.discover_symbols(
                        market_headlines=market_headlines,
                        max_symbols=self.settings.max_discovered_symbols,
                        existing_symbols=configured_symbols,
                        operator_context=operator_context,
                        prorealtime_snapshot=prorealtime_snapshot,
                    )
                except Exception as exc:
                    LOGGER.warning("Automatic symbol discovery failed: %s", exc)

        open_position_symbols = [
            position.symbol.upper()
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        ]
        open_position_symbol_set = set(open_position_symbols)

        candidate_symbols = self._dedupe_symbols(
            configured_symbols
            + focused_symbols
            + discovered_symbols
            + prorealtime_symbols
            + open_position_symbols
            + list(forced_close_symbols)
        )
        if not candidate_symbols:
            fallback_symbols = [
                instrument.symbol.upper()
                for instrument in list_supported_instruments(self.settings.allowed_asset_classes)[
                    : self.settings.max_discovered_symbols
                ]
            ]
            candidate_symbols = self._dedupe_symbols(open_position_symbols + fallback_symbols)
            LOGGER.warning(
                "No candidate symbols were available after discovery; falling back to liquid universe: %s",
                ", ".join(candidate_symbols),
            )

        tradeable_symbols: list[str] = []
        for symbol in candidate_symbols:
            if (
                symbol in excluded_symbols
                and symbol not in open_position_symbol_set
                and symbol not in forced_close_symbols
            ):
                LOGGER.info("Skipping %s because the operator excluded it from new trading", symbol)
                continue
            if symbol in open_position_symbol_set:
                tradeable_symbols.append(symbol)
                continue
            if self._is_tradeable_symbol(symbol):
                tradeable_symbols.append(symbol)
            else:
                LOGGER.info(
                    "Skipping %s because the broker could not resolve it as tradeable",
                    symbol,
                )

        if not tradeable_symbols:
            LOGGER.warning(
                "No tradeable symbols remained after broker filtering; cycle will be skipped"
            )
            return []

        LOGGER.info("Active symbols for this cycle: %s", ", ".join(tradeable_symbols))
        return tradeable_symbols

    def _build_market_research(
        self,
        *,
        account_snapshot: AccountSnapshot,
        market_headlines: list[Headline],
        operator_context: dict | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> MarketResearchEnvelope | None:
        if not self.settings.enable_research_subagents:
            return None
        try:
            with self.model_lock:
                return self.decision_engine.research_market(
                    market_headlines,
                    max_symbols=self.settings.max_discovered_symbols,
                    account_snapshot=account_snapshot,
                    existing_symbols=self.settings.symbols,
                    operator_context=operator_context,
                    prorealtime_snapshot=prorealtime_snapshot,
                )
        except Exception as exc:
            LOGGER.warning("Market research subagent failed: %s", exc)
            return None

    def _build_symbol_research(
        self,
        *,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None,
        news_synthesis=None,
        operator_context: dict | None = None,
        guardrail_context: dict | None = None,
    ) -> SymbolResearchEnvelope | None:
        if not self.settings.enable_research_subagents:
            return None
        try:
            with self.model_lock:
                return self.decision_engine.research_symbols(
                    account_snapshot=account_snapshot,
                    symbol_contexts=symbol_contexts,
                    market_headlines=market_headlines,
                    market_research=market_research,
                    news_synthesis=news_synthesis,
                    operator_context=operator_context,
                    guardrail_context=guardrail_context,
                )
        except Exception as exc:
            LOGGER.warning("Symbol research subagent failed: %s", exc)
            return None

    def _collect_symbol_contexts(
        self,
        symbols: list[str],
        *,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> dict[str, SymbolContext]:
        symbol_contexts: dict[str, SymbolContext] = {}
        open_positions_by_symbol = {
            position.symbol.upper(): position
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        for symbol in symbols:
            prorealtime_symbol = self.prorealtime_client.get_symbol_snapshot(
                symbol=symbol,
                snapshot=prorealtime_snapshot,
            )
            try:
                per_symbol_headlines = self.news_client.fetch_symbol_headlines(symbol)
            except Exception as exc:
                LOGGER.warning("Headline collection degraded for %s: %s", symbol, exc)
                per_symbol_headlines = []

            entity_items: list[Headline] = []
            for entity in entities_for_symbol(symbol):
                entity_items.extend(self._current_entity_news.get(entity.key, []))
            headlines: list[Headline] = per_symbol_headlines + entity_items

            try:
                symbol_contexts[symbol] = self.market_data_client.build_symbol_context(
                    symbol=symbol,
                    headlines=headlines,
                    prorealtime=prorealtime_symbol,
                )
                continue
            except Exception as exc:
                if prorealtime_symbol and (prorealtime_symbol.last_price or 0.0) > 0:
                    LOGGER.warning(
                        "Using ProRealTime fallback context for %s due to market-data error: %s",
                        symbol,
                        exc,
                    )
                    symbol_contexts[symbol] = self.market_data_client.build_fallback_symbol_context(
                        symbol=symbol,
                        fallback_price=float(prorealtime_symbol.last_price or 0.0),
                        headlines=headlines,
                        note=(
                            "Fallback context from ProRealTime sidecar snapshot because Yahoo "
                            "Finance data was unavailable"
                        ),
                        prorealtime=prorealtime_symbol,
                    )
                    continue
                open_position = open_positions_by_symbol.get(symbol.upper())
                fallback_context = self._build_position_fallback_context(
                    symbol=symbol,
                    open_position=open_position,
                    headlines=headlines,
                    prorealtime=prorealtime_symbol,
                )
                if fallback_context is not None:
                    LOGGER.warning(
                        "Using fallback broker snapshot context for %s due to market-data error: %s",
                        symbol,
                        exc,
                    )
                    symbol_contexts[symbol] = fallback_context
                    continue
                LOGGER.warning("Skipping %s due to data collection error: %s", symbol, exc)
        return symbol_contexts

    def _build_position_fallback_context(
        self,
        *,
        symbol: str,
        open_position: OpenPosition | None,
        headlines: list[Headline],
        prorealtime: ProRealTimeSymbolSnapshot | None = None,
    ) -> SymbolContext | None:
        fallback_price = 0.0
        if prorealtime and (prorealtime.last_price or 0.0) > 0:
            fallback_price = float(prorealtime.last_price or 0.0)
        elif open_position is not None and (open_position.current_price or 0.0) > 0:
            fallback_price = float(open_position.current_price or 0.0)

        if fallback_price <= 0:
            return None

        instrument = get_instrument(symbol)
        price = fallback_price
        indicators = IndicatorSnapshot(
            price=price,
            sma_20=price,
            sma_50=price,
            ema_12=price,
            ema_26=price,
            rsi_14=50.0,
            macd=0.0,
            macd_signal=0.0,
            atr_14=0.0,
            volatility_20d=0.0,
            return_1d=0.0,
            return_5d=0.0,
        )
        return SymbolContext(
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            as_of=datetime.now(timezone.utc).isoformat(),
            indicators=indicators,
            headlines=headlines,
            chart_note=(
                "Fallback context from broker open-position snapshot because market data was unavailable"
                if open_position is not None and (open_position.current_price or 0.0) > 0
                else "Fallback context from ProRealTime sidecar snapshot because market data was unavailable"
            ),
            prorealtime=prorealtime,
        )

    def add_operator_note(self, note: str) -> None:
        normalized = note.strip()
        if not normalized:
            return
        with self.operator_lock:
            self.operator_notes.append(normalized)

    def add_focus_symbols(self, symbols: list[str]) -> list[str]:
        normalized = self._normalize_symbol_list(symbols)
        if not normalized:
            return []
        with self.operator_lock:
            self.focused_symbols.update(normalized)
        return normalized

    def add_excluded_symbols(self, symbols: list[str]) -> list[str]:
        normalized = self._normalize_symbol_list(symbols)
        if not normalized:
            return []
        with self.operator_lock:
            self.excluded_symbols.update(normalized)
        return normalized

    def queue_forced_close(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("A symbol is required for close requests")
        with self.operator_lock:
            self.forced_close_symbols.add(normalized)
        return normalized

    def clear_operator_guidance(self) -> None:
        with self.operator_lock:
            self.operator_notes.clear()
            self.focused_symbols.clear()
            self.excluded_symbols.clear()
            self.forced_close_symbols.clear()

    def pause(self) -> None:
        with self.operator_lock:
            self.paused = True
        self.wake_event.set()

    def resume(self) -> None:
        with self.operator_lock:
            self.paused = False
        self.wake_event.set()

    def request_run_now(self) -> None:
        self.wake_event.set()

    def request_stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def answer_operator_question(self, question: str) -> str:
        runtime_state = self.build_runtime_state()
        with self.model_lock:
            return self.decision_engine.answer_operator_question(
                question,
                runtime_state=runtime_state,
            )

    def interpret_operator_input(self, text: str) -> OperatorCommandEnvelope:
        runtime_state = self.build_runtime_state()
        with self.model_lock:
            return self.decision_engine.interpret_operator_input(
                text,
                runtime_state=runtime_state,
            )

    def build_runtime_state(self) -> dict:
        with self.operator_lock:
            operator_context = {
                "notes": list(self.operator_notes),
                "focused_symbols": sorted(self.focused_symbols),
                "excluded_symbols": sorted(self.excluded_symbols),
                "forced_close_symbols": sorted(self.forced_close_symbols),
                "paused": self.paused,
            }
        return {
            "settings": {
                "broker": self.settings.broker,
                "mode": self.settings.execution_mode,
                "dry_run": self.settings.dry_run,
                "allow_shorts": self.settings.allow_shorts,
                "max_open_positions": self.settings.max_open_positions,
                "max_new_trades_per_cycle": self.settings.max_new_trades_per_cycle,
                "prt_enabled": self.settings.prt_enabled,
                "prt_confirmation_mode": self.settings.prt_confirmation_mode,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "default_max_holding_hours": self.settings.default_max_holding_hours,
            },
            "operator_context": operator_context,
            "runtime_state": self.runtime_state,
            "last_cycle_report": self.last_cycle_report,
        }

    def get_status_report(self) -> str:
        runtime_state = self.build_runtime_state()
        last_cycle = runtime_state.get("last_cycle_report") or {}
        operator_context = runtime_state["operator_context"]
        lines = [
            f"paused: {operator_context['paused']}",
            f"notes: {len(operator_context['notes'])}",
            f"focused_symbols: {', '.join(operator_context['focused_symbols']) or '-'}",
            f"excluded_symbols: {', '.join(operator_context['excluded_symbols']) or '-'}",
            f"forced_close_symbols: {', '.join(operator_context['forced_close_symbols']) or '-'}",
            f"tracked_positions: {len((runtime_state.get('runtime_state') or {}).get('positions', {}))}",
        ]
        daily_guardrail = (runtime_state.get("runtime_state") or {}).get("daily", {})
        if daily_guardrail:
            lines.append(
                "daily_guardrail: "
                f"date={daily_guardrail.get('date', '-')}, "
                f"starting_equity={daily_guardrail.get('starting_equity', '-')}"
            )
        if last_cycle:
            lines.extend(
                [
                    f"last_cycle_started_at: {last_cycle.get('cycle_started_at', '-')}",
                    f"last_active_symbols: {', '.join(last_cycle.get('active_symbols', [])) or '-'}",
                    f"last_plan_count: {len(last_cycle.get('plans', []))}",
                    f"last_result_count: {len(last_cycle.get('results', []))}",
                ]
            )
            for plan in last_cycle.get("plans", []):
                lines.append(
                    f"plan: {plan.get('action')} {plan.get('symbol')} because {plan.get('reason', '')}"
                )
            for result in last_cycle.get("results", []):
                plan = result.get("plan", {})
                lines.append(
                    f"result: {result.get('status')} {plan.get('action')} {plan.get('symbol')} "
                    f"{result.get('error') or ''}".strip()
                )
        else:
            lines.append("last_cycle: no cycles have completed yet")
        return "\n".join(lines)

    def _get_operator_context(self, account_snapshot: AccountSnapshot) -> dict:
        open_symbols = {
            position.symbol.upper()
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        with self.operator_lock:
            self.forced_close_symbols.intersection_update(open_symbols)
            return {
                "notes": list(self.operator_notes),
                "focused_symbols": sorted(self.focused_symbols),
                "excluded_symbols": sorted(self.excluded_symbols),
                "forced_close_symbols": sorted(self.forced_close_symbols),
                "paused": self.paused,
            }

    def _apply_operator_overrides(
        self,
        *,
        decision: DecisionEnvelope,
        account_snapshot: AccountSnapshot,
        operator_context: dict,
    ) -> DecisionEnvelope:
        forced_close_symbols = {
            symbol.upper() for symbol in operator_context.get("forced_close_symbols", [])
        }
        if not forced_close_symbols:
            return decision

        open_positions_by_symbol = {
            position.symbol.upper(): position
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        trades = list(decision.trades)
        existing_close_symbols = {
            trade.symbol.upper()
            for trade in trades
            if trade.action == "CLOSE"
        }
        for symbol in sorted(forced_close_symbols):
            if symbol not in open_positions_by_symbol or symbol in existing_close_symbols:
                continue
            trades.append(
                TradeDecision(
                    symbol=symbol,
                    action="CLOSE",
                    confidence=1.0,
                    thesis="Operator-requested close from terminal interaction",
                    news_bias="neutral",
                    invalidate_if="Operator request withdrawn",
                    close_position_id=open_positions_by_symbol[symbol].position_id,
                )
            )
        if len(trades) == len(decision.trades):
            return decision
        return DecisionEnvelope(
            market_regime=decision.market_regime,
            summary=decision.summary,
            trades=trades,
        )

    def _load_runtime_state(self) -> dict:
        path = self.settings.runtime_state_path
        if not path.exists():
            return {"daily": {}, "positions": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Failed to load runtime state from %s: %s", path, exc)
            return {"daily": {}, "positions": {}}
        if not isinstance(payload, dict):
            return {"daily": {}, "positions": {}}
        payload.setdefault("daily", {})
        payload.setdefault("positions", {})
        return payload

    def _save_runtime_state(self) -> None:
        path = self.settings.runtime_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.runtime_state, handle, indent=2)
        os.replace(tmp, path)

    def _reconcile_on_startup(self) -> None:
        LOGGER.info("Reconciling runtime state with broker on startup")
        try:
            snapshot = self.broker.get_account_snapshot()
        except Exception as exc:
            LOGGER.warning("Startup reconcile skipped (broker call failed): %s", exc)
            return
        broker_ids = {p.position_id for p in snapshot.open_positions if p.position_id}
        positions_state = self.runtime_state.setdefault("positions", {})
        stale = [pid for pid in positions_state if pid not in broker_ids]
        for pid in stale:
            LOGGER.info("Dropping stale local position %s (not at broker)", pid)
            positions_state.pop(pid, None)
        invalidations = self.runtime_state.setdefault("open_position_invalidations", {})
        for pid in list(invalidations.keys()):
            if pid not in broker_ids:
                invalidations.pop(pid, None)
        self._save_runtime_state()

    def _refresh_runtime_state(
        self,
        account_snapshot: AccountSnapshot,
        now: datetime,
    ) -> None:
        positions_state = self.runtime_state.setdefault("positions", {})
        open_position_ids: set[str] = set()

        for position in account_snapshot.open_positions:
            if not position.position_id:
                continue
            open_position_ids.add(position.position_id)
            current_price = float(position.current_price or 0.0)
            item = positions_state.setdefault(
                position.position_id,
                {
                    "symbol": position.symbol.upper(),
                    "first_seen_at": now.isoformat(),
                    "entry_price": current_price or None,
                    "highest_price": current_price or None,
                    "lowest_price": current_price or None,
                    "max_holding_hours": self.settings.default_max_holding_hours,
                },
            )
            item["symbol"] = position.symbol.upper()
            item["direction"] = self._normalize_position_direction(position)
            item["updated_at"] = now.isoformat()
            if current_price > 0:
                if not item.get("entry_price"):
                    item["entry_price"] = current_price
                item["highest_price"] = max(float(item.get("highest_price") or current_price), current_price)
                item["lowest_price"] = min(float(item.get("lowest_price") or current_price), current_price)
                item["last_price"] = current_price

        for position_id in list(positions_state):
            if position_id not in open_position_ids:
                positions_state.pop(position_id, None)

    def _build_guardrail_context(
        self,
        *,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        now: datetime,
    ) -> dict:
        daily_state = self.runtime_state.setdefault("daily", {})
        current_date = now.date().isoformat()
        if daily_state.get("date") != current_date:
            daily_state.clear()
            daily_state["date"] = current_date
            daily_state["starting_equity"] = float(account_snapshot.total_funds)

        starting_equity = max(float(daily_state.get("starting_equity") or account_snapshot.total_funds), 0.01)
        drawdown_cash = max(starting_equity - float(account_snapshot.total_funds), 0.0)
        drawdown_pct = drawdown_cash / starting_equity if starting_equity > 0 else 0.0
        daily_loss_breached = drawdown_pct >= self.settings.max_daily_loss_pct
        entry_window_open = self._entry_window_is_open(now)

        block_reasons: list[str] = []
        if daily_loss_breached:
            block_reasons.append(
                f"daily drawdown {drawdown_pct:.2%} breached limit {self.settings.max_daily_loss_pct:.2%}"
            )
        if not entry_window_open:
            block_reasons.append(
                f"new entries are outside the configured UTC window {self.settings.entry_start_hour_utc:02d}:00-"
                f"{self.settings.entry_end_hour_utc:02d}:00"
            )
        if prorealtime_snapshot and prorealtime_snapshot.flatten_all:
            block_reasons.append("ProRealTime sidecar requested flatten_all")

        close_requests = self._collect_guardrail_close_requests(
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
            now=now,
            daily_loss_breached=daily_loss_breached,
        )

        position_guardrails = []
        for position in account_snapshot.open_positions:
            state = self.runtime_state.get("positions", {}).get(position.position_id, {})
            position_guardrails.append(
                {
                    "symbol": position.symbol.upper(),
                    "position_id": position.position_id,
                    "age_hours": self._position_age_hours(state, now),
                    "max_holding_hours": state.get("max_holding_hours") or self.settings.default_max_holding_hours,
                    "current_price": position.current_price,
                }
            )

        from t212_cfd_bot.instruments import get_instrument as _get_inst
        econ_events = [e.model_dump(mode="json") for e in (self._current_econ_events or [])]
        earnings_events: dict[str, dict] = {}
        for sym in symbol_contexts:
            try:
                event = self.earnings_calendar.next_earnings_date(sym)
                if event is not None:
                    earnings_events[sym] = event.model_dump(mode="json")
            except Exception:
                pass
        session_state: dict[str, bool] = {}
        for sym in symbol_contexts:
            try:
                inst = _get_inst(sym)
                session_state[sym] = self.holiday_calendar.is_session_open(inst.exchange_code, now)
            except Exception:
                session_state[sym] = True

        return {
            "daily_loss": {
                "date": current_date,
                "starting_equity": starting_equity,
                "drawdown_cash": drawdown_cash,
                "drawdown_pct": drawdown_pct,
                "breached": daily_loss_breached,
            },
            "entry_window": {
                "start_hour_utc": self.settings.entry_start_hour_utc,
                "end_hour_utc": self.settings.entry_end_hour_utc,
                "open": entry_window_open,
            },
            "prorealtime": {
                "enabled": self.settings.prt_enabled,
                "confirmation_mode": self.settings.prt_confirmation_mode,
                "flatten_all": bool(prorealtime_snapshot and prorealtime_snapshot.flatten_all),
                "source": prorealtime_snapshot.source if prorealtime_snapshot else None,
            },
            "block_new_entries": bool(block_reasons),
            "block_reasons": block_reasons,
            "close_requests": close_requests,
            "position_guardrails": position_guardrails,
            "symbol_count": len(symbol_contexts),
            "econ_events": econ_events,
            "earnings_events": earnings_events,
            "session_open": session_state,
            "vix_context": (
                self._current_market_vix.model_dump(mode="json") if self._current_market_vix else None
            ),
            "consecutive_loss_state": {
                "pause_until": self.runtime_state.get("consecutive_loss_pause_until"),
            },
        }

    def _collect_guardrail_close_requests(
        self,
        *,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        now: datetime,
        daily_loss_breached: bool,
    ) -> list[dict]:
        close_requests: list[dict] = []
        seen_position_ids: set[str] = set()

        def add_close(position: OpenPosition, reason: str) -> None:
            if not position.position_id or position.position_id in seen_position_ids:
                return
            seen_position_ids.add(position.position_id)
            close_requests.append(
                {
                    "symbol": position.symbol.upper(),
                    "position_id": position.position_id,
                    "reason": reason,
                }
            )

        if prorealtime_snapshot and prorealtime_snapshot.flatten_all:
            for position in account_snapshot.open_positions:
                add_close(position, "ProRealTime sidecar requested flatten_all")

        if daily_loss_breached and self.settings.auto_flatten_on_daily_loss:
            for position in account_snapshot.open_positions:
                add_close(position, "Daily loss guardrail breached; flattening open exposure")

        for position in account_snapshot.open_positions:
            state = self.runtime_state.get("positions", {}).get(position.position_id, {})
            age_hours = self._position_age_hours(state, now)
            max_holding_hours = int(
                state.get("max_holding_hours") or self.settings.default_max_holding_hours
            )
            if age_hours is not None and age_hours >= max_holding_hours:
                add_close(
                    position,
                    f"Max holding time exceeded: {age_hours:.1f}h >= {max_holding_hours}h",
                )
                continue

            trailing_reason = self._trailing_exit_reason(position, state)
            if trailing_reason:
                add_close(position, trailing_reason)

        return close_requests

    def _apply_guardrail_overrides(
        self,
        *,
        decision: DecisionEnvelope,
        account_snapshot: AccountSnapshot,
        guardrail_context: dict,
    ) -> DecisionEnvelope:
        close_requests = guardrail_context.get("close_requests") or []
        if not close_requests:
            return decision

        open_positions_by_id = {
            position.position_id: position
            for position in account_snapshot.open_positions
            if position.position_id
        }
        trades = list(decision.trades)
        existing_close_ids = {
            trade.close_position_id
            for trade in trades
            if trade.action == "CLOSE" and trade.close_position_id
        }

        for item in close_requests:
            position_id = str(item.get("position_id") or "").strip()
            position = open_positions_by_id.get(position_id)
            if not position or position_id in existing_close_ids:
                continue
            trades.append(
                TradeDecision(
                    symbol=position.symbol.upper(),
                    action="CLOSE",
                    confidence=1.0,
                    thesis=str(item.get("reason") or "Guardrail-triggered close"),
                    news_bias="neutral",
                    invalidate_if="Guardrail condition cleared before execution",
                    close_position_id=position_id,
                )
            )

        if len(trades) == len(decision.trades):
            return decision
        return DecisionEnvelope(
            market_regime=decision.market_regime,
            summary=decision.summary,
            trades=trades,
        )

    @staticmethod
    def _position_age_hours(state: dict, now: datetime) -> float | None:
        first_seen_at = str(state.get("first_seen_at") or "").strip()
        if not first_seen_at:
            return None
        normalized = first_seen_at[:-1] + "+00:00" if first_seen_at.endswith("Z") else first_seen_at
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((now - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.0)

    def _trailing_exit_reason(self, position: OpenPosition, state: dict) -> str | None:
        activation = self.settings.trailing_exit_activation_pct
        trailing = self.settings.trailing_exit_pct
        current_price = float(position.current_price or 0.0)
        entry_price = float(state.get("entry_price") or 0.0)
        if activation is None or trailing is None or current_price <= 0 or entry_price <= 0:
            return None

        direction = self._normalize_position_direction(position)
        highest_price = float(state.get("highest_price") or entry_price)
        lowest_price = float(state.get("lowest_price") or entry_price)

        if direction == "buy":
            if highest_price < entry_price * (1.0 + activation):
                return None
            if current_price <= highest_price * (1.0 - trailing):
                return (
                    f"Trailing exit for long position: current price {current_price:.4f} retraced "
                    f"from peak {highest_price:.4f}"
                )
            return None

        if direction == "sell":
            if lowest_price > entry_price * (1.0 - activation):
                return None
            if current_price >= lowest_price * (1.0 + trailing):
                return (
                    f"Trailing exit for short position: current price {current_price:.4f} rebounded "
                    f"from low {lowest_price:.4f}"
                )
        return None

    def _entry_window_is_open(self, now: datetime) -> bool:
        return self.settings.entry_start_hour_utc <= now.hour < self.settings.entry_end_hour_utc

    @staticmethod
    def _normalize_position_direction(position: OpenPosition) -> str:
        if position.direction in {"buy", "sell"}:
            return position.direction
        if position.quantity < 0:
            return "sell"
        return "buy"

    def _log_market_research(self, market_research: MarketResearchEnvelope | None) -> None:
        if market_research is None:
            return
        LOGGER.info("Market regime: %s", market_research.market_regime)
        LOGGER.info("Research summary: %s", market_research.summary)

    def _log_prorealtime_snapshot(
        self,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
    ) -> None:
        if prorealtime_snapshot is None:
            return
        LOGGER.info(
            "ProRealTime sidecar: source=%s rows=%d flatten_all=%s",
            prorealtime_snapshot.source or "unknown",
            len(prorealtime_snapshot.symbols),
            prorealtime_snapshot.flatten_all,
        )

    def _log_guardrail_context(self, guardrail_context: dict) -> None:
        daily_loss = guardrail_context.get("daily_loss") or {}
        entry_window = guardrail_context.get("entry_window") or {}
        LOGGER.info(
            "Guardrails: daily_drawdown=%.2f%% breached=%s entry_window_open=%s",
            float(daily_loss.get("drawdown_pct", 0.0)) * 100.0,
            daily_loss.get("breached"),
            entry_window.get("open"),
        )
        for reason in guardrail_context.get("block_reasons") or []:
            LOGGER.info("Entry block: %s", reason)
        for request in guardrail_context.get("close_requests") or []:
            LOGGER.info(
                "Guardrail close: %s because %s",
                request.get("symbol"),
                request.get("reason"),
            )

    def _log_decision_summary(self, decision: DecisionEnvelope) -> None:
        LOGGER.info("Decision summary: %s", decision.summary)
        for trade in decision.trades:
            LOGGER.info(
                "Model trade: %s %s (confidence %.2f) because %s",
                trade.action,
                trade.symbol,
                trade.confidence,
                trade.thesis,
            )

    def _log_execution_plans(self, plans: list[ExecutionPlan]) -> None:
        if not plans:
            LOGGER.info("No execution plans were approved for this cycle")
            return
        for plan in plans:
            LOGGER.info(
                "Approved plan: %s %s qty=%s stop=%s take_profit=%s reason=%s",
                plan.action,
                plan.symbol,
                plan.quantity,
                plan.stop_distance,
                plan.take_profit_distance,
                plan.reason,
            )

    @staticmethod
    def _log_execution_result(result: ExecutionResult) -> None:
        plan = result.plan
        if result.status == "failed":
            LOGGER.warning(
                "Execution failed: %s %s because %s",
                plan.action,
                plan.symbol,
                result.error or "unknown error",
            )
            return
        LOGGER.info(
            "Execution %s: %s %s",
            result.status,
            plan.action,
            plan.symbol,
        )

    @staticmethod
    def _normalize_symbol_list(symbols: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            item = symbol.strip().upper()
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized

    def _is_paused(self) -> bool:
        with self.operator_lock:
            return self.paused

    def _close_result_allows_follow_on_entry(self, result: ExecutionResult) -> bool:
        if result.plan.action != "CLOSE":
            return True
        if result.status == "dry_run":
            return True
        if result.status != "submitted":
            return False
        if self.settings.broker != "ig":
            return True

        broker_response = result.broker_response or {}
        if not isinstance(broker_response, dict):
            return False
        confirmation = broker_response.get("confirm")
        if not isinstance(confirmation, dict):
            return False
        return str(confirmation.get("dealStatus", "")).upper() == "ACCEPTED"

    def _is_tradeable_symbol(self, symbol: str) -> bool:
        checker = getattr(self.broker, "is_tradeable_symbol", None)
        if callable(checker):
            return bool(checker(symbol))
        return True

    @staticmethod
    def _dedupe_symbols(symbols: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            normalized = symbol.strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _write_cycle_log(
        self,
        *,
        cycle_started_at: datetime,
        account_snapshot: dict,
        market_headlines: list[dict],
        symbol_contexts: dict[str, dict],
        active_symbols: list[str],
        operator_context: dict,
        prorealtime_snapshot: dict | None,
        guardrail_context: dict,
        market_research: dict | None,
        symbol_research: dict | None,
        decision: dict,
        plans: list[dict],
        results: list[dict],
    ) -> None:
        log_dir = self.settings.decision_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = cycle_started_at.strftime("%Y%m%dT%H%M%SZ.json")
        payload = {
            "cycle_started_at": cycle_started_at.isoformat(),
            "settings": {
                "broker": self.settings.broker,
                "symbols": self.settings.symbols,
                "auto_discover_symbols": self.settings.auto_discover_symbols,
                "allowed_asset_classes": self.settings.allowed_asset_classes,
                "enable_research_subagents": self.settings.enable_research_subagents,
                "mode": self.settings.execution_mode,
                "dry_run": self.settings.dry_run,
                "allow_shorts": self.settings.allow_shorts,
                "allow_position_reversals": self.settings.allow_position_reversals,
                "prt_enabled": self.settings.prt_enabled,
                "prt_confirmation_mode": self.settings.prt_confirmation_mode,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "default_max_holding_hours": self.settings.default_max_holding_hours,
            },
            "operator_context": operator_context,
            "prorealtime_snapshot": prorealtime_snapshot,
            "guardrail_context": guardrail_context,
            "runtime_state": self.runtime_state,
            "active_symbols": active_symbols,
            "account_snapshot": account_snapshot,
            "market_headlines": market_headlines,
            "market_research": market_research,
            "symbol_contexts": symbol_contexts,
            "symbol_research": symbol_research,
            "decision": decision,
            "execution_plans": plans,
            "execution_results": results,
        }
        with (log_dir / filename).open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
