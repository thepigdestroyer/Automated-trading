from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from t212_cfd_bot.config import Settings
from t212_cfd_bot.instruments import list_supported_instruments
from t212_cfd_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    Headline,
    MarketResearchEnvelope,
    OperatorCommandEnvelope,
    ProRealTimeSnapshotEnvelope,
    TradeDecision,
    SymbolContext,
    SymbolDiscoveryEnvelope,
    SymbolResearchEnvelope,
)


class OpenAIDecisionEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        client_kwargs = {"api_key": settings.llm_api_key}
        if settings.llm_base_url:
            client_kwargs["base_url"] = settings.llm_base_url
        if settings.llm_default_headers:
            client_kwargs["default_headers"] = settings.llm_default_headers
        self.client = OpenAI(**client_kwargs)

    def research_market(
        self,
        market_headlines: list[Headline],
        *,
        max_symbols: int,
        account_snapshot: AccountSnapshot | None = None,
        existing_symbols: list[str] | None = None,
        operator_context: dict[str, Any] | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> MarketResearchEnvelope:
        candidate_instruments = list_supported_instruments(self.settings.allowed_asset_classes)
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "account": account_snapshot.model_dump(mode="json") if account_snapshot else None,
            "market_headlines": [headline.model_dump(mode="json") for headline in market_headlines],
            "existing_symbols": existing_symbols or [],
            "operator_context": operator_context or None,
            "prorealtime_snapshot": (
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            "max_symbols": max_symbols,
            "candidate_instruments": [
                instrument.discovery_payload() for instrument in candidate_instruments
            ],
            "constraints": {
                "allow_shorts": self.settings.allow_shorts,
                "allowed_asset_classes": self.settings.allowed_asset_classes,
                "focus_on_cfd_tradeable_markets": True,
                "news_priority": "primary",
                "bot_research_priority": "primary",
                "prorealtime_priority": "secondary_confirmation_only",
            },
        }

        system_prompt = f"""
You are Subagent 1: Macro Market Researcher for an autonomous CFD trading bot.

Review the supplied market headlines and identify the strongest current macro themes.
Choose only from the supplied candidate_instruments universe.
The universe may contain equities, ETFs, indices, forex pairs, metals, and energy markets.
Prefer liquid CFD-friendly markets with direct headline catalysts and clear directional drivers.
Respect operator_context when it does not conflict with the supplied market/account state or safety constraints.
If ProRealTime sidecar data is present, use it only as secondary confirmation and timing context.
Primary weight must remain on supplied headlines and the bot's own research process.
When headlines include source_priority and query_label metadata, give more weight to high-priority market sources
and analysis-focused headlines, especially Bloomberg, Reuters, CNBC, MarketWatch, FT, WSJ, and Yahoo Finance.
You may be bullish or bearish on a candidate. Bearish ideas are valid only when supported by clear negative catalysts.
Return no more than {max_symbols} candidate symbols.
This is research output, not the final execution decision.
""".strip()

        return self._run_structured_generation(
            schema_model=MarketResearchEnvelope,
            schema_name="market_research_envelope",
            system_prompt=system_prompt,
            payload=payload,
        )

    def discover_symbols(
        self,
        market_headlines: list[Headline],
        *,
        max_symbols: int,
        existing_symbols: list[str] | None = None,
        operator_context: dict[str, Any] | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> list[str]:
        market_research = self.research_market(
            market_headlines,
            max_symbols=max_symbols,
            existing_symbols=existing_symbols,
            operator_context=operator_context,
            prorealtime_snapshot=prorealtime_snapshot,
        )
        discovery_envelope = SymbolDiscoveryEnvelope(
            summary=market_research.summary,
            symbols=[
                {"symbol": candidate.symbol, "confidence": candidate.confidence, "reason": candidate.reason}
                for candidate in market_research.candidates
            ],
        )
        return self._normalize_discovered_symbols(discovery_envelope, max_symbols)

    def research_symbols(
        self,
        *,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None = None,
        operator_context: dict[str, Any] | None = None,
        guardrail_context: dict[str, Any] | None = None,
    ) -> SymbolResearchEnvelope:
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "account": account_snapshot.model_dump(mode="json"),
            "market_headlines": [headline.model_dump(mode="json") for headline in market_headlines],
            "market_research": market_research.model_dump(mode="json") if market_research else None,
            "operator_context": operator_context or None,
            "guardrail_context": guardrail_context or None,
            "symbols": [
                symbol_context.model_dump(mode="json")
                for symbol_context in symbol_contexts.values()
            ],
            "constraints": {
                "allow_shorts": self.settings.allow_shorts,
                "review_existing_positions": True,
                "focus_on_cfd_position_management": True,
                "news_priority": "primary",
                "bot_research_priority": "primary",
                "prorealtime_priority": "secondary_confirmation_only",
            },
        }

        system_prompt = """
You are Subagent 2: Cross-Asset Symbol Researcher for a CFD trading bot.

Review each supplied symbol independently using only the provided headlines, indicators, market research, and account state.
Respect operator_context when it does not conflict with the supplied market/account state or safety constraints.
If a symbol includes ProRealTime sidecar fields, use them as secondary timing and confirmation signals only.
Primary weight must remain on headlines, account state, and the supplied technical indicators.
When headlines include source_priority and query_label metadata, give more weight to high-priority market sources
and analysis-focused headlines, especially Bloomberg and other established financial outlets.
For every symbol, state whether the strongest setup is:
- long_cfd
- short_cfd
- close_or_reduce
- no_trade

Be conservative. If evidence is mixed, use no_trade.
If a symbol already has an open position and the evidence has materially deteriorated or reversed, you may use close_or_reduce.
This is research output, not the final execution decision.
""".strip()

        return self._run_structured_generation(
            schema_model=SymbolResearchEnvelope,
            schema_name="symbol_research_envelope",
            system_prompt=system_prompt,
            payload=payload,
        )

    def decide(
        self,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None = None,
        symbol_research: SymbolResearchEnvelope | None = None,
        operator_context: dict[str, Any] | None = None,
        guardrail_context: dict[str, Any] | None = None,
    ) -> DecisionEnvelope:
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "account": account_snapshot.model_dump(mode="json"),
            "market_headlines": [headline.model_dump(mode="json") for headline in market_headlines],
            "market_research": market_research.model_dump(mode="json") if market_research else None,
            "symbol_research": symbol_research.model_dump(mode="json") if symbol_research else None,
            "operator_context": operator_context or None,
            "guardrail_context": guardrail_context or None,
            "symbols": [
                symbol_context.model_dump(mode="json")
                for symbol_context in symbol_contexts.values()
            ],
            "constraints": {
                "max_open_positions": self.settings.max_open_positions,
                "max_new_trades_per_cycle": self.settings.max_new_trades_per_cycle,
                "allow_shorts": self.settings.allow_shorts,
                "min_model_confidence": self.settings.min_model_confidence,
                "stop_loss_pct_fallback": self.settings.stop_loss_pct,
                "take_profit_pct_fallback": self.settings.take_profit_pct,
                "dry_run": self.settings.dry_run,
                "trading_mode": self.settings.execution_mode,
                "llm_provider": self.settings.llm_provider,
                "allow_position_reversals": self.settings.allow_position_reversals,
                "manage_open_positions": True,
                "prorealtime_confirmation_mode": self.settings.prt_confirmation_mode,
                "news_priority": "primary",
                "bot_research_priority": "primary",
                "prorealtime_priority": "secondary_confirmation_only",
            },
        }

        system_prompt = f"""
You are the final Portfolio Manager for a conservative autonomous CFD trading bot.

You coordinate specialist research subagents, but you do not blindly trust them.
Use only the supplied account data, headlines, technical indicators, and subagent research.
Do not invent extra news, prices, positions, or symbols.
Default to HOLD if the signal is weak, conflicting, or under-supported.
The input symbols may represent CFD or other derivative exposures on equities, ETFs, indices, forex pairs, metals, or energy markets.

Review existing positions before proposing new entries.
Respect operator_context when it does not conflict with the supplied market/account state or risk constraints.
If ProRealTime sidecar fields are supplied, treat them as a secondary confirmation and timing layer.
Primary weight must remain on headlines, account state, and the bot's own research pipeline.
If guardrail_context indicates a risk breach or forced close condition, respect it.
When headlines include source_priority and query_label metadata, give more weight to high-priority market sources
and analysis-focused headlines, especially Bloomberg and other established financial outlets.
For BUY or SELL, return only symbols present in the symbol context input.
For CLOSE, you may close symbols that appear in account.open_positions even if detailed symbol context is missing.
Never exceed {self.settings.max_new_trades_per_cycle} new trade ideas.
Do not suggest adding to an existing position in the same direction.
If the account has multiple open positions in the same symbol and you want to close one, set close_position_id explicitly.
If an existing position is wrong-sided and the opposite setup is materially stronger, prefer reversing it by first closing and then taking the opposite side.
You may output:
- BUY to open a long CFD position
- SELL to open a short CFD position, but only if the negative setup is clear and materially supported
- CLOSE to manage an existing position
- HOLD when no action is justified

For BUY or SELL:
- stop_loss_pct and take_profit_pct must be decimal percentages like 0.012 for 1.2%.
- keep stop loss tighter than take profit.
- avoid martingale, averaging down, or revenge trading patterns.
""".strip()

        return self._run_structured_generation(
            schema_model=DecisionEnvelope,
            schema_name="decision_envelope",
            system_prompt=system_prompt,
            payload=payload,
        )

    def answer_operator_question(
        self,
        question: str,
        *,
        runtime_state: dict[str, Any],
    ) -> str:
        payload = {
            "question": question,
            "runtime_state": runtime_state,
        }
        system_prompt = """
You are the operator console assistant for an autonomous CFD trading bot.

Answer the operator's question using only the supplied runtime_state.
Be concise, practical, and explicit about uncertainty.
If the requested action cannot be inferred safely from runtime_state, say so.
""".strip()

        input_items = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ]

        if self.settings.llm_provider == "openrouter":
            response = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=input_items,
            )
            return (response.choices[0].message.content or "").strip() or "No answer returned."

        response = self.client.responses.create(
            model=self.settings.openai_model,
            reasoning={"effort": self.settings.openai_reasoning_effort},
            input=input_items,
        )
        output_text = getattr(response, "output_text", "")
        return output_text.strip() or "No answer returned."

    def interpret_operator_input(
        self,
        text: str,
        *,
        runtime_state: dict[str, Any],
    ) -> OperatorCommandEnvelope:
        payload = {
            "operator_input": text,
            "runtime_state": runtime_state,
        }
        system_prompt = """
You are the operator command interpreter for an autonomous CFD trading bot.

Translate the operator's free-form terminal input into one or more concrete bot actions.
Allowed actions are:
- status
- pause
- resume
- run
- note
- focus
- avoid
- close
- clear
- quit
- ask

Rules:
- Use close only when the operator is explicitly asking to exit or flatten a position.
- Use ask when the operator is asking a question about the bot's state or what it is doing.
- Use focus or avoid when the operator is steering instrument selection.
- Use note when the operator is giving qualitative guidance that should influence later model decisions.
- Prefer concise, high-signal actions. Do not invent symbols or instructions.
- Symbols must be uppercase tickers from the operator input when available.
- If the input is ambiguous, default to a single note action rather than a risky control action.
""".strip()

        return self._run_structured_generation(
            schema_model=OperatorCommandEnvelope,
            schema_name="operator_command_envelope",
            system_prompt=system_prompt,
            payload=payload,
        )

    def _run_structured_generation(
        self,
        *,
        schema_model: Any,
        schema_name: str,
        system_prompt: str,
        payload: dict[str, Any],
    ) -> Any:
        input_items = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ]

        if self.settings.llm_provider == "openrouter":
            return self._run_openrouter_structured_chat(
                schema_model=schema_model,
                input_items=input_items,
                error_label=schema_name,
            )

        parse_method = getattr(self.client.responses, "parse", None)
        if callable(parse_method):
            response = parse_method(
                model=self.settings.openai_model,
                reasoning={"effort": self.settings.openai_reasoning_effort},
                input=input_items,
                text_format=schema_model,
            )
            if response.output_parsed is None:
                raise RuntimeError(f"Model returned no parsed result for {schema_name}")
            return response.output_parsed

        response = self.client.responses.create(
            model=self.settings.openai_model,
            reasoning={"effort": self.settings.openai_reasoning_effort},
            input=input_items,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema_model.model_json_schema(),
                }
            },
        )

        output_text = getattr(response, "output_text", "")
        if not output_text:
            raise RuntimeError(f"Model returned no textual result for {schema_name}")
        return schema_model.model_validate_json(output_text)

    def _run_openrouter_structured_chat(
        self,
        *,
        schema_model: Any,
        input_items: list[dict[str, str]],
        error_label: str,
    ) -> Any:
        schema_json = json.dumps(schema_model.model_json_schema(), indent=2)
        system_content = (
            input_items[0]["content"]
            + "\n\n"
            + "Required JSON schema:\n"
            + schema_json
            + "\n\n"
            + "Return only valid JSON matching the required schema exactly. "
            + "Do not wrap the JSON in markdown fences."
        )
        response = self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": input_items[1]["content"]},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        if not content:
            raise RuntimeError(f"OpenRouter returned no textual result for {error_label}")
        raw_payload = json.loads(self._extract_json_text(content))
        try:
            return schema_model.model_validate(raw_payload)
        except ValidationError:
            return self._coerce_openrouter_payload(schema_model, raw_payload)

    def _coerce_openrouter_payload(self, schema_model: Any, raw_payload: Any) -> Any:
        if schema_model is MarketResearchEnvelope:
            return self._coerce_market_research(raw_payload)
        if schema_model is SymbolResearchEnvelope:
            return self._coerce_symbol_research(raw_payload)
        if schema_model is DecisionEnvelope:
            return self._coerce_decision_envelope(raw_payload)
        if schema_model is OperatorCommandEnvelope:
            return OperatorCommandEnvelope.model_validate(raw_payload if isinstance(raw_payload, dict) else {})
        raise RuntimeError(f"Unsupported OpenRouter schema coercion target: {schema_model}")

    def _coerce_market_research(self, raw_payload: Any) -> MarketResearchEnvelope:
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        bias_map = self._coerce_mapping(payload.get("bias") or payload.get("sentiment"))
        confidence_map = self._coerce_mapping(payload.get("confidence") or payload.get("confidences"))
        reason_map = self._coerce_mapping(payload.get("reasons") or payload.get("theses"))
        candidates = []

        for item in self._coerce_symbol_items(payload.get("candidates") or payload.get("symbols")):
            symbol = item["symbol"]
            bias_value = str(
                item.get("bias")
                or bias_map.get(symbol)
                or bias_map.get(symbol.upper())
                or "neutral"
            ).lower()
            if bias_value not in {"bullish", "bearish", "mixed", "neutral"}:
                bias_value = "neutral"
            confidence_value = self._coerce_float(
                item.get("confidence")
                or confidence_map.get(symbol)
                or confidence_map.get(symbol.upper()),
                default=0.5,
            )
            reason_value = str(
                item.get("reason")
                or reason_map.get(symbol)
                or reason_map.get(symbol.upper())
                or item.get("thesis")
                or payload.get("summary")
                or payload.get("reasoning")
                or payload.get("rationale")
                or f"{symbol} selected by market research subagent"
            ).strip()
            candidates.append(
                {
                    "symbol": symbol,
                    "bias": bias_value,
                    "confidence": confidence_value,
                    "reason": reason_value,
                }
            )

        return MarketResearchEnvelope.model_validate(
            {
                "market_regime": payload.get("market_regime")
                or payload.get("regime")
                or payload.get("market_state")
                or "mixed",
                "summary": payload.get("summary")
                or payload.get("reasoning")
                or payload.get("rationale")
                or "Market research generated from normalized OpenRouter output.",
                "key_themes": self._coerce_string_list(
                    payload.get("key_themes") or payload.get("themes") or payload.get("drivers")
                ),
                "risks": self._coerce_string_list(payload.get("risks") or payload.get("risk_factors")),
                "candidates": candidates,
            }
        )

    def _coerce_symbol_research(self, raw_payload: Any) -> SymbolResearchEnvelope:
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        bias_map = self._coerce_mapping(payload.get("bias") or payload.get("sentiment"))
        trade_map = self._coerce_mapping(
            payload.get("preferred_trade") or payload.get("trade_bias") or payload.get("trade_setup")
        )
        confidence_map = self._coerce_mapping(payload.get("confidence") or payload.get("confidences"))
        thesis_map = self._coerce_mapping(payload.get("thesis") or payload.get("theses") or payload.get("reasons"))
        risk_map = self._coerce_mapping(payload.get("risks") or payload.get("risk_factors"))
        notes = []

        for item in self._coerce_symbol_items(payload.get("notes") or payload.get("symbols") or payload.get("research")):
            symbol = item["symbol"]
            preferred_trade = str(
                item.get("preferred_trade")
                or trade_map.get(symbol)
                or trade_map.get(symbol.upper())
                or "no_trade"
            ).lower()
            preferred_trade = {
                "long": "long_cfd",
                "short": "short_cfd",
                "close": "close_or_reduce",
                "hold": "no_trade",
            }.get(preferred_trade, preferred_trade)
            if preferred_trade not in {"long_cfd", "short_cfd", "close_or_reduce", "no_trade"}:
                preferred_trade = "no_trade"
            bias_value = str(
                item.get("bias")
                or bias_map.get(symbol)
                or bias_map.get(symbol.upper())
                or "neutral"
            ).lower()
            if bias_value not in {"bullish", "bearish", "mixed", "neutral"}:
                bias_value = "neutral"
            risks_value = item.get("risks") or risk_map.get(symbol) or risk_map.get(symbol.upper())
            notes.append(
                {
                    "symbol": symbol,
                    "bias": bias_value,
                    "preferred_trade": preferred_trade,
                    "confidence": self._coerce_float(
                        item.get("confidence")
                        or confidence_map.get(symbol)
                        or confidence_map.get(symbol.upper()),
                        default=0.5,
                    ),
                    "thesis": str(
                        item.get("thesis")
                        or thesis_map.get(symbol)
                        or thesis_map.get(symbol.upper())
                        or item.get("reason")
                        or f"{symbol} analyzed by symbol research subagent"
                    ).strip(),
                    "risks": self._coerce_string_list(risks_value),
                }
            )

        return SymbolResearchEnvelope.model_validate(
            {
                "summary": payload.get("summary")
                or payload.get("reasoning")
                or payload.get("rationale")
                or "Symbol research generated from normalized OpenRouter output.",
                "notes": notes,
            }
        )

    def _coerce_decision_envelope(self, raw_payload: Any) -> DecisionEnvelope:
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        trade_items = payload.get("trades") or payload.get("decisions") or payload.get("actions") or []
        if isinstance(trade_items, dict):
            trade_items = [
                {"symbol": symbol, "action": action}
                for symbol, action in trade_items.items()
            ]

        trades = []
        for item in self._coerce_symbol_items(trade_items):
            action = str(item.get("action") or item.get("decision") or item.get("preferred_trade") or "HOLD").upper()
            action = {
                "LONG_CFD": "BUY",
                "SHORT_CFD": "SELL",
                "NO_TRADE": "HOLD",
                "CLOSE_OR_REDUCE": "CLOSE",
                "LONG": "BUY",
                "SHORT": "SELL",
            }.get(action, action)
            if action not in {"BUY", "SELL", "HOLD", "CLOSE"}:
                action = "HOLD"

            stop_loss_pct = self._coerce_positive_pct(
                item.get("stop_loss_pct"),
                item.get("stop"),
                fallback=self.settings.stop_loss_pct,
            )
            take_profit_pct = self._coerce_positive_pct(
                item.get("take_profit_pct"),
                item.get("target"),
                fallback=self.settings.take_profit_pct,
            )
            trade_payload = {
                "symbol": item["symbol"],
                "action": action,
                "confidence": self._coerce_float(item.get("confidence") or item.get("score"), default=0.5),
                "thesis": str(
                    item.get("thesis")
                    or item.get("reason")
                    or item.get("rationale")
                    or payload.get("summary")
                    or "Normalized decision output"
                ).strip(),
                "news_bias": self._normalize_news_bias(item.get("news_bias") or item.get("bias") or item.get("sentiment")),
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "max_holding_hours": item.get("max_holding_hours") or item.get("holding_hours"),
                "invalidate_if": str(item.get("invalidate_if") or "signal weakens or catalyst reverses"),
                "close_position_id": item.get("close_position_id") or item.get("position_id"),
            }
            trades.append(TradeDecision.model_validate(trade_payload))

        return DecisionEnvelope.model_validate(
            {
                "market_regime": payload.get("market_regime")
                or payload.get("regime")
                or payload.get("market_state")
                or "mixed",
                "summary": payload.get("summary")
                or payload.get("reasoning")
                or payload.get("rationale")
                or "Decision generated from normalized OpenRouter output.",
                "trades": trades,
            }
        )

    @staticmethod
    def _coerce_symbol_items(raw_items: Any) -> list[dict[str, Any]]:
        if raw_items is None:
            return []
        if isinstance(raw_items, dict):
            raw_items = [{"symbol": symbol, **(value if isinstance(value, dict) else {"action": value})} for symbol, value in raw_items.items()]

        items: list[dict[str, Any]] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, str):
                    symbol = item.strip().upper()
                    if symbol:
                        items.append({"symbol": symbol})
                elif isinstance(item, dict):
                    symbol = str(item.get("symbol") or item.get("ticker") or item.get("instrument") or "").strip().upper()
                    if symbol:
                        normalized = dict(item)
                        normalized["symbol"] = symbol
                        items.append(normalized)
        return items

    @staticmethod
    def _coerce_mapping(raw_value: Any) -> dict[str, Any]:
        return raw_value if isinstance(raw_value, dict) else {}

    @staticmethod
    def _coerce_string_list(raw_value: Any) -> list[str]:
        if raw_value is None:
            return []
        if isinstance(raw_value, str):
            parts = re.split(r"[;\n,]+", raw_value)
            return [part.strip() for part in parts if part.strip()]
        if isinstance(raw_value, list):
            return [str(item).strip() for item in raw_value if str(item).strip()]
        return []

    @staticmethod
    def _coerce_float(raw_value: Any, *, default: float) -> float:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return default
        return min(max(value, 0.0), 1.0)

    @staticmethod
    def _coerce_positive_pct(*candidates: Any, fallback: float) -> float:
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if 0.0 < value < 1.0:
                return value
        return fallback

    @staticmethod
    def _normalize_news_bias(raw_value: Any) -> str:
        normalized = str(raw_value or "neutral").strip().lower()
        if normalized in {"positive", "bullish"}:
            return "positive"
        if normalized in {"negative", "bearish"}:
            return "negative"
        if normalized in {"mixed", "neutral"}:
            return normalized
        return "neutral"

    @staticmethod
    def _extract_json_text(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped

        fenced_match = re.search(r"```json\s*(\{.*\})\s*```", stripped, re.DOTALL)
        if fenced_match:
            return fenced_match.group(1)

        generic_match = re.search(r"(\{.*\})", stripped, re.DOTALL)
        if generic_match:
            return generic_match.group(1)

        raise RuntimeError("Could not extract JSON object from model response")

    @staticmethod
    def _normalize_discovered_symbols(
        envelope: SymbolDiscoveryEnvelope,
        max_symbols: int,
    ) -> list[str]:
        symbols: list[str] = []
        seen: set[str] = set()

        for candidate in envelope.symbols:
            symbol = candidate.symbol.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) >= max_symbols:
                break

        return symbols
