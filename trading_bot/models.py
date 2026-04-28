from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Headline(StrictModel):
    title: str
    source: str
    url: str
    published_at: str | None = None
    source_domain: str | None = None
    source_priority: int | None = None
    query_label: str | None = None
    snippet: str | None = None
    topic_label: str | None = None
    age_hours: float | None = None
    novelty: Literal["new", "updated", "known"] = "new"
    entity_source: str | None = None


class IndicatorSnapshot(StrictModel):
    price: float
    sma_20: float
    sma_50: float
    ema_12: float
    ema_26: float
    rsi_14: float
    macd: float
    macd_signal: float
    atr_14: float
    volatility_20d: float
    return_1d: float
    return_5d: float
    daily_sma_200: float | None = None
    daily_sma_200_distance_bps: float | None = None
    weekly_return_5w: float | None = None
    volume_ratio_vs_20d: float | None = None
    obv_slope_5d: float | None = None


class ProRealTimeSymbolSnapshot(StrictModel):
    symbol: str
    as_of: str | None = None
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    trend_bias: Literal["bullish", "bearish", "mixed", "neutral"] = "neutral"
    entry_signal: Literal["long", "short", "flat", "none"] = "none"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    screener_rank: int | None = None
    support: float | None = None
    resistance: float | None = None
    note: str | None = None
    source: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_prorealtime_symbol_snapshot(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        trend_bias = str(
            normalized.get("trend_bias")
            or normalized.get("bias")
            or normalized.get("trend")
            or normalized.get("sentiment")
            or "neutral"
        ).lower()
        trend_bias = {
            "positive": "bullish",
            "negative": "bearish",
            "buy": "bullish",
            "sell": "bearish",
            "long": "bullish",
            "short": "bearish",
        }.get(trend_bias, trend_bias)
        if trend_bias not in {"bullish", "bearish", "mixed", "neutral"}:
            trend_bias = "neutral"

        entry_signal = str(
            normalized.get("entry_signal")
            or normalized.get("signal")
            or normalized.get("trade_signal")
            or normalized.get("setup")
            or "none"
        ).lower()
        entry_signal = {
            "buy": "long",
            "bullish": "long",
            "sell": "short",
            "bearish": "short",
            "close": "flat",
            "exit": "flat",
            "hold": "none",
            "neutral": "none",
        }.get(entry_signal, entry_signal)
        if entry_signal not in {"long", "short", "flat", "none"}:
            entry_signal = "none"

        return {
            "symbol": str(normalized.get("symbol") or "").strip().upper(),
            "as_of": normalized.get("as_of") or normalized.get("timestamp"),
            "last_price": normalized.get("last_price")
            or normalized.get("last")
            or normalized.get("price"),
            "bid": normalized.get("bid"),
            "ask": normalized.get("ask"),
            "trend_bias": trend_bias,
            "entry_signal": entry_signal,
            "confidence": normalized.get("confidence") or normalized.get("score"),
            "screener_rank": normalized.get("screener_rank") or normalized.get("rank"),
            "support": normalized.get("support"),
            "resistance": normalized.get("resistance"),
            "note": normalized.get("note") or normalized.get("comment"),
            "source": normalized.get("source"),
        }


class ProRealTimeSnapshotEnvelope(StrictModel):
    as_of: str | None = None
    source: str | None = None
    note: str | None = None
    flatten_all: bool = False
    symbols: list[ProRealTimeSymbolSnapshot] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_prorealtime_snapshot_envelope(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"symbols": value}
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "symbols" not in normalized:
            normalized["symbols"] = (
                normalized.get("rows")
                or normalized.get("items")
                or normalized.get("instruments")
                or []
            )
        return {
            "as_of": normalized.get("as_of") or normalized.get("generated_at"),
            "source": normalized.get("source") or normalized.get("origin"),
            "note": normalized.get("note") or normalized.get("summary"),
            "flatten_all": bool(
                normalized.get("flatten_all")
                or normalized.get("close_all")
                or normalized.get("emergency_flatten")
                or False
            ),
            "symbols": normalized.get("symbols", []),
        }


class SymbolContext(StrictModel):
    symbol: str
    display_name: str
    asset_class: str
    as_of: str
    indicators: IndicatorSnapshot
    headlines: list[Headline] = Field(default_factory=list)
    chart_note: str
    prorealtime: ProRealTimeSymbolSnapshot | None = None


class OpenPosition(StrictModel):
    symbol: str
    position_id: str
    quantity: float
    current_price: float | None = None
    pnl: float | None = None
    direction: Literal["buy", "sell", "unknown"] = "unknown"


class AccountSnapshot(StrictModel):
    mode: Literal["demo", "live"]
    currency: str
    total_funds: float
    free_funds: float
    open_positions: list[OpenPosition] = Field(default_factory=list)


class SymbolCandidate(StrictModel):
    symbol: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @model_validator(mode="before")
    @classmethod
    def normalize_symbol_candidate(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "reason" not in normalized:
            normalized["reason"] = (
                normalized.get("rationale")
                or normalized.get("thesis")
                or normalized.get("summary")
                or "No reason provided"
            )
        if "confidence" not in normalized:
            normalized["confidence"] = normalized.get("score", 0.5)
        return {
            "symbol": normalized.get("symbol"),
            "confidence": normalized.get("confidence"),
            "reason": normalized.get("reason"),
        }


class SymbolDiscoveryEnvelope(StrictModel):
    summary: str
    symbols: list[SymbolCandidate] = Field(default_factory=list)


class MarketResearchCandidate(StrictModel):
    symbol: str
    bias: Literal["bullish", "bearish", "mixed", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @model_validator(mode="before")
    @classmethod
    def normalize_market_candidate(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "bias" not in normalized:
            normalized["bias"] = _normalize_bias(
                normalized.get("direction")
                or normalized.get("sentiment")
                or normalized.get("view")
            )
        if "reason" not in normalized:
            normalized["reason"] = (
                normalized.get("rationale")
                or normalized.get("thesis")
                or normalized.get("catalyst")
                or "No reason provided"
            )
        if "confidence" not in normalized:
            normalized["confidence"] = normalized.get("score", 0.5)
        return {
            "symbol": normalized.get("symbol"),
            "bias": normalized.get("bias"),
            "confidence": normalized.get("confidence"),
            "reason": normalized.get("reason"),
        }


class MarketResearchEnvelope(StrictModel):
    market_regime: str
    summary: str
    key_themes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    candidates: list[MarketResearchCandidate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_market_research(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "candidates" not in normalized and "selected_symbols" in normalized:
            sentiment = normalized.get("sentiment", {}) or {}
            confidence_scores = normalized.get("confidence_scores", {}) or {}
            catalysts = normalized.get("key_catalysts", {}) or {}
            normalized["candidates"] = [
                {
                    "symbol": symbol,
                    "bias": _normalize_bias(sentiment.get(symbol)),
                    "confidence": confidence_scores.get(symbol, 0.5),
                    "reason": catalysts.get(symbol, normalized.get("rationale", "No reason provided")),
                }
                for symbol in normalized.get("selected_symbols", [])
            ]
        if "market_regime" not in normalized:
            normalized["market_regime"] = (
                normalized.get("regime")
                or normalized.get("market_view")
                or normalized.get("market_state")
                or "mixed"
            )
        if "summary" not in normalized:
            normalized["summary"] = (
                normalized.get("rationale")
                or normalized.get("overview")
                or normalized.get("thesis")
                or "No summary provided"
            )
        return {
            "market_regime": normalized.get("market_regime"),
            "summary": normalized.get("summary"),
            "key_themes": normalized.get("key_themes", []),
            "risks": normalized.get("risks", []),
            "candidates": normalized.get("candidates", []),
        }


class SymbolResearchNote(StrictModel):
    symbol: str
    bias: Literal["bullish", "bearish", "mixed", "neutral"]
    preferred_trade: Literal["long_cfd", "short_cfd", "close_or_reduce", "no_trade"]
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_symbol_research_note(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "bias" not in normalized:
            normalized["bias"] = _normalize_bias(
                normalized.get("direction")
                or normalized.get("sentiment")
                or normalized.get("view")
            )
        if "preferred_trade" not in normalized:
            normalized["preferred_trade"] = _normalize_preferred_trade(
                normalized.get("preferred_trade")
                or normalized.get("trade_action")
                or normalized.get("recommendation")
                or normalized.get("action")
                or normalized.get("direction")
            )
        if "thesis" not in normalized:
            normalized["thesis"] = (
                normalized.get("rationale")
                or normalized.get("summary")
                or normalized.get("reason")
                or "No thesis provided"
            )
        if "confidence" not in normalized:
            normalized["confidence"] = normalized.get("score", 0.5)
        return {
            "symbol": normalized.get("symbol"),
            "bias": normalized.get("bias"),
            "preferred_trade": normalized.get("preferred_trade"),
            "confidence": normalized.get("confidence"),
            "thesis": normalized.get("thesis"),
            "risks": normalized.get("risks", []),
        }


class SymbolResearchEnvelope(StrictModel):
    summary: str
    notes: list[SymbolResearchNote] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_symbol_research(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "notes" not in normalized:
            normalized["notes"] = (
                normalized.get("analyses")
                or normalized.get("symbols")
                or normalized.get("research")
                or []
            )
        if "summary" not in normalized:
            normalized["summary"] = (
                normalized.get("overview")
                or normalized.get("rationale")
                or normalized.get("thesis")
                or "No summary provided"
            )
        return {
            "summary": normalized.get("summary"),
            "notes": normalized.get("notes", []),
        }


class TradeDecision(StrictModel):
    symbol: str
    action: Literal["BUY", "SELL", "HOLD", "CLOSE"]
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    news_bias: Literal["positive", "negative", "mixed", "neutral"]
    stop_loss_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    take_profit_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    max_holding_hours: int | None = Field(default=None, ge=1, le=336)
    invalidate_if: str
    close_position_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_trade_decision(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "action" not in normalized:
            normalized["action"] = _normalize_trade_action(
                normalized.get("trade_action")
                or normalized.get("recommendation")
                or normalized.get("direction")
                or normalized.get("preferred_trade")
            )
        if "thesis" not in normalized:
            normalized["thesis"] = (
                normalized.get("rationale")
                or normalized.get("summary")
                or normalized.get("reason")
                or "No thesis provided"
            )
        if "news_bias" not in normalized:
            normalized["news_bias"] = _normalize_news_bias(
                normalized.get("bias")
                or normalized.get("sentiment")
                or normalized.get("direction")
            )
        if "close_position_id" not in normalized and "position_id" in normalized:
            normalized["close_position_id"] = normalized.get("position_id")
        if "confidence" not in normalized:
            normalized["confidence"] = normalized.get("score", 0.5)
        return {
            "symbol": normalized.get("symbol"),
            "action": normalized.get("action"),
            "confidence": normalized.get("confidence"),
            "thesis": normalized.get("thesis"),
            "news_bias": normalized.get("news_bias"),
            "stop_loss_pct": normalized.get("stop_loss_pct"),
            "take_profit_pct": normalized.get("take_profit_pct"),
            "max_holding_hours": normalized.get("max_holding_hours"),
            "invalidate_if": normalized.get("invalidate_if"),
            "close_position_id": normalized.get("close_position_id"),
        }

    @model_validator(mode="after")
    def validate_trade_fields(self) -> "TradeDecision":
        if self.action in {"BUY", "SELL"}:
            if self.stop_loss_pct is None:
                raise ValueError("stop_loss_pct is required for BUY or SELL decisions")
            if self.take_profit_pct is None:
                raise ValueError("take_profit_pct is required for BUY or SELL decisions")
        return self


class DecisionEnvelope(StrictModel):
    market_regime: str
    summary: str
    trades: list[TradeDecision] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_decision_envelope(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "market_regime" not in normalized:
            normalized["market_regime"] = (
                normalized.get("regime")
                or normalized.get("market_view")
                or normalized.get("market_state")
                or "mixed"
            )
        if "summary" not in normalized:
            normalized["summary"] = (
                normalized.get("overview")
                or normalized.get("rationale")
                or normalized.get("thesis")
                or "No summary provided"
            )
        if "trades" not in normalized:
            normalized["trades"] = (
                normalized.get("decisions")
                or normalized.get("orders")
                or normalized.get("ideas")
                or []
            )
        return {
            "market_regime": normalized.get("market_regime"),
            "summary": normalized.get("summary"),
            "trades": normalized.get("trades", []),
        }


class OperatorAction(StrictModel):
    action: Literal[
        "status",
        "pause",
        "resume",
        "run",
        "note",
        "focus",
        "avoid",
        "close",
        "clear",
        "quit",
        "ask",
    ]
    content: str | None = None
    symbols: list[str] = Field(default_factory=list)
    reason: str

    @model_validator(mode="before")
    @classmethod
    def normalize_operator_action(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        action = str(
            normalized.get("action")
            or normalized.get("type")
            or normalized.get("intent")
            or normalized.get("command")
            or "note"
        ).strip().lower()
        action_map = {
            "question": "ask",
            "query": "ask",
            "answer": "ask",
            "message": "note",
            "guidance": "note",
            "instruction": "note",
            "immediate_run": "run",
            "refresh": "run",
            "continue": "resume",
            "stop": "pause",
            "exit_position": "close",
            "flatten": "close",
        }
        action = action_map.get(action, action)
        content = normalized.get("content")
        if content is None:
            content = (
                normalized.get("text")
                or normalized.get("message")
                or normalized.get("note")
                or normalized.get("question")
            )
        symbols = normalized.get("symbols") or normalized.get("symbol") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        return {
            "action": action,
            "content": content,
            "symbols": [str(item).strip().upper() for item in symbols if str(item).strip()],
            "reason": normalized.get("reason") or "No reason provided",
        }


class OperatorCommandEnvelope(StrictModel):
    summary: str
    actions: list[OperatorAction] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_operator_command_envelope(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "summary" not in normalized:
            normalized["summary"] = (
                normalized.get("rationale")
                or normalized.get("overview")
                or normalized.get("thesis")
                or "No summary provided"
            )
        if "actions" not in normalized:
            normalized["actions"] = (
                normalized.get("commands")
                or normalized.get("intents")
                or normalized.get("steps")
                or []
            )
        return {
            "summary": normalized.get("summary"),
            "actions": normalized.get("actions", []),
        }


class ExecutionPlan(StrictModel):
    symbol: str
    action: Literal["BUY", "SELL", "CLOSE"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    quantity: float | None = None
    target_price: float | None = None
    stop_distance: float | None = None
    take_profit_distance: float | None = None
    position_id: str | None = None
    max_holding_hours: int | None = None


class ExecutionResult(StrictModel):
    plan: ExecutionPlan
    status: Literal["dry_run", "submitted", "failed"]
    submitted_at: str
    broker_response: dict[str, Any] | None = None
    error: str | None = None


class VolRegimeContext(StrictModel):
    as_of: str
    vix_spot: float | None = None
    vix_9d: float | None = None
    vix_3m: float | None = None
    term_structure_flag: Literal["contango", "backwardation", "flat", "unknown"] = "unknown"


class EconomicEvent(StrictModel):
    title: str
    when: str
    impact: Literal["low", "medium", "high"]
    currency: str
    country: str | None = None
    source: str | None = None


class EarningsEvent(StrictModel):
    symbol: str
    date: str
    estimated_eps: float | None = None
    source: str = "yfinance"


class NewsTheme(StrictModel):
    title: str
    narrative: str
    direction_by_symbol: dict[str, Literal["bullish", "bearish", "mixed", "neutral"]] = Field(default_factory=dict)
    conviction: float = Field(ge=0.0, le=1.0, default=0.5)
    developing: bool = False
    contradictions: list[str] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    key_sources: list[Headline] = Field(default_factory=list)


class NewsSynthesisEnvelope(StrictModel):
    themes: list[NewsTheme] = Field(default_factory=list)
    per_symbol_summaries: dict[str, str] = Field(default_factory=dict)
    regime_implications: str = ""
    open_position_assessments: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_news_synthesis(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        out = dict(value)
        if "themes" not in out:
            out["themes"] = out.get("topics") or out.get("narratives") or []
        if "per_symbol_summaries" not in out:
            out["per_symbol_summaries"] = out.get("symbols") or {}
        if "open_position_assessments" not in out:
            out["open_position_assessments"] = out.get("positions") or {}
        if "regime_implications" not in out:
            out["regime_implications"] = out.get("regime") or ""
        return {
            "themes": out.get("themes", []),
            "per_symbol_summaries": out.get("per_symbol_summaries", {}),
            "regime_implications": out.get("regime_implications", ""),
            "open_position_assessments": out.get("open_position_assessments", {}),
        }


def _normalize_bias(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"bullish", "buy", "long", "positive"}:
        return "bullish"
    if normalized in {"bearish", "sell", "short", "negative"}:
        return "bearish"
    if normalized in {"mixed", "both"}:
        return "mixed"
    return "neutral"


def _normalize_preferred_trade(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"long", "buy", "bullish", "long_cfd"}:
        return "long_cfd"
    if normalized in {"short", "sell", "bearish", "short_cfd"}:
        return "short_cfd"
    if normalized in {"close", "close_or_reduce", "reduce", "exit"}:
        return "close_or_reduce"
    return "no_trade"


def _normalize_trade_action(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"LONG", "BUY", "LONG_CFD"}:
        return "BUY"
    if normalized in {"SHORT", "SELL", "SHORT_CFD"}:
        return "SELL"
    if normalized in {"CLOSE", "EXIT", "REDUCE", "CLOSE_OR_REDUCE"}:
        return "CLOSE"
    return "HOLD"


def _normalize_news_bias(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"positive", "bullish", "buy", "long"}:
        return "positive"
    if normalized in {"negative", "bearish", "sell", "short"}:
        return "negative"
    if normalized in {"mixed", "both"}:
        return "mixed"
    return "neutral"
