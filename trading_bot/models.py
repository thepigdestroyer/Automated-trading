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
    # Freshness class of the ORIGIN, not of our fetch. "realtime" = live
    # newswire/squawk (sub-minute), "official" = primary source (central bank,
    # agency), "aggregator" = rebroadcast with delay (Google News: ~15-30 min).
    # Lets the LLM weight a live wire above a delayed rebroadcast, and lets
    # dedup keep the fastest copy of a duplicated story.
    latency_class: Literal["realtime", "official", "aggregator"] | None = None


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


class SignalPack(StrictModel):
    """Pre-classified quantitative signals derived deterministically from the
    same OHLCV frame as IndicatorSnapshot. The LLM consumes these LABELS
    (plus a few diagnostic numbers) instead of having to mentally arithmetic
    raw RSI/MACD/ATR floats — number-crunching is exactly what LLMs are
    unreliable at and what a few lines of numpy do perfectly. Phase 1.1/1.2
    of the 2026-05 profitability roadmap. Augments (does not replace)
    IndicatorSnapshot."""

    # Trend: SMA stack alignment + 200d distance.
    trend: Literal["up_strong", "up", "neutral", "down", "down_strong"]
    # Momentum: MACD histogram trajectory + RSI direction.
    momentum: Literal["accelerating", "steady", "fading", "reversing"]
    # RSI regime.
    rsi_state: Literal["overbought", "rising", "neutral", "falling", "oversold"]
    # Mean-reversion: z-score of price vs 20-bar mean (= Bollinger position
    # in std units). Negative = below band (potential long), positive = above.
    mean_reversion_z: float
    bollinger_position: Literal["above_upper", "upper_half", "lower_half", "below_lower"]
    # Donchian channel position over 20 bars (0.0 = at N-bar low, 1.0 = high).
    donchian_position: float
    # VWAP deviation in bps (intraday session VWAP); None when volume absent.
    vwap_deviation_bps: float | None = None
    # Volatility regime: ATR-as-%-of-price percentile vs its own 100-bar history.
    vol_regime: Literal["compressed", "normal", "expanding"]
    # Volume state.
    volume_state: Literal["surge", "normal", "thin", "unknown"]
    # A compact human-readable one-liner the LLM can quote in its reasoning.
    summary: str


class DailyTrend(StrictModel):
    """Daily-timeframe trend classification (Phase 2 strategy pivot, 2026-05).

    The adversarial review concluded the only durable retail edge is timeframe
    + patience (trend-following), not intraday news-chasing. This summarises
    the DAILY bar structure so the executor can enforce trend-alignment: only
    take longs in a daily uptrend, shorts in a daily downtrend.

    direction: the daily trend (up / down / sideways) from a Donchian + MA
    structure. donchian_breakout: whether the latest daily close is breaking
    the N-day high (long signal) or low (short signal). adx_proxy: a 0-100
    trend-strength proxy (higher = stronger directional move)."""

    direction: Literal["up", "down", "sideways"]
    donchian_breakout: Literal["up", "down", "none"]
    sma_50_200_cross: Literal["golden", "death", "none"]
    trend_strength: float  # 0-100 proxy (ADX-like)
    close_vs_sma50_bps: float | None = None
    summary: str


class DataQuality(StrictModel):
    """Per-symbol price-feed integrity report for one fetch.

    Outlier bars are neutralised in place (time-interpolated) rather than
    dropped, so the bar index stays contiguous and rolling/EWM indicators
    remain valid. `is_degraded` is the signal the executor and the LLM see.

    Every count is a typed field. Consumers must not parse `note`, which is
    prose for humans and carries no compatibility guarantee. Use
    `imputed_ratio` rather than dividing by `bars_total`, which is 0 for a
    fallback context with no history at all.
    """

    bars_total: int
    bars_imputed: int = 0
    # Wholly-empty bars removed by fetch_history's dropna(how="all") BEFORE
    # bars_total was measured. These DO gap the index -- the exact failure this
    # module exists to eliminate -- so the count needs to be machine-readable.
    # It used to live only in `note`; string-parsing prose is not a contract.
    bars_dropped_empty: int = 0
    # Bars whose Volume was NaN on arrival and has been interpolated. Counted
    # separately from `bars_imputed` so that field keeps meaning "fabricated
    # OHLC" (see imputed_ratio's docstring).
    bars_volume_filled: int = 0
    # Subsets of the three counts above that lie OUTSIDE the indicator lookback
    # (market_data.DEGRADATION_LOOKBACK_BARS). Still reported, but they do not
    # set `is_degraded`: a bar no current indicator reads cannot degrade it.
    bars_imputed_stale: int = 0
    bars_dropped_empty_stale: int = 0
    bars_volume_filled_stale: int = 0
    # Extreme moves in the newest bars that have not yet had the full revert
    # window to show whether they are a spike or a level shift. Left intact
    # (the live edge is never fabricated) but counted towards `is_degraded`.
    bars_unconfirmed: int = 0
    max_gap_pct: float | None = None
    is_degraded: bool = False
    note: str | None = None

    @property
    def imputed_ratio(self) -> float:
        """Fraction of bars with fabricated OHLC. 1.0 when there are no bars
        at all (the fallback context), so a missing history reads as fully
        degraded rather than dividing by zero."""
        if self.bars_total <= 0:
            return 1.0
        return self.bars_imputed / self.bars_total


class SymbolContext(StrictModel):
    symbol: str
    display_name: str
    asset_class: str
    as_of: str
    indicators: IndicatorSnapshot
    signals: SignalPack | None = None
    daily_trend: DailyTrend | None = None
    headlines: list[Headline] = Field(default_factory=list)
    chart_note: str
    prorealtime: ProRealTimeSymbolSnapshot | None = None
    data_quality: DataQuality | None = None


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
    # Phase 1.5: the LLM's view of how long the setup needs to play out.
    # Advisory input to max_holding_hours selection; code clamps to config.
    signal_horizon: Literal["intraday", "swing", "position"] | None = None
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
            "signal_horizon": normalized.get("signal_horizon"),
            "max_holding_hours": normalized.get("max_holding_hours"),
            "invalidate_if": normalized.get("invalidate_if"),
            "close_position_id": normalized.get("close_position_id"),
        }

    @model_validator(mode="after")
    def validate_trade_fields(self) -> "TradeDecision":
        # Phase 1.5 (2026-05 adversarial review): stop_loss_pct / take_profit_pct
        # are NO LONGER required. The executor (risk.py) owns ALL stop/TP/size
        # math deterministically (ATR-based stop, risk_reward_ratio TP). The LLM
        # now only needs to express direction + confidence + horizon + thesis.
        # The pct fields are accepted-if-present for backward compatibility but
        # are advisory only; risk.py ignores them for placement.
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


# --- Phase 4 free macro data (FRED/ALFRED, Finnhub, CFTC COT) -----------


class FredSeriesObservation(StrictModel):
    """A single FRED/ALFRED series observation, vintage-annotated.

    `date` is what the data is ABOUT; `vintage_date` is when it was first
    PUBLISHED (realtime_start). `staleness_days` = now - date, so the LLM
    cannot mistake last month's CPI print for today's news."""
    series_id: str
    value: float | None = None
    date: str | None = None
    vintage_date: str | None = None
    staleness_days: int | None = None
    units: str | None = None


class CotPositioning(StrictModel):
    """CFTC Commitments-of-Traders net non-commercial (speculative)
    positioning for one market, with explicit publication lag (COT is
    released Friday for the prior Tuesday — never treat as real-time)."""
    market: str
    report_date: str | None = None
    publication_lag_days: int | None = None
    noncommercial_long: int | None = None
    noncommercial_short: int | None = None
    net_noncommercial: int | None = None
    net_change_1w: int | None = None


class EiaSeriesObservation(StrictModel):
    """A US Energy Information Administration weekly series observation
    (e.g. crude-oil ending stocks, natural-gas storage). change_1w is the
    week-over-week build(+)/draw(-) that markets actually react to; a larger
    crude DRAW is bullish oil, a BUILD bearish. Weekly cadence with a few-day
    publication lag — context, not timing (staleness_days makes the lag
    explicit)."""
    series_id: str
    value: float | None = None
    period: str | None = None
    change_1w: float | None = None
    units: str | None = None
    staleness_days: int | None = None
    description: str | None = None


class MacroContext(StrictModel):
    """Aggregated free macro context attached to the decision payload."""
    as_of: str
    fred_series: list[FredSeriesObservation] = Field(default_factory=list)
    finnhub_news_count: int | None = None
    finnhub_summary: str | None = None
    cot: list[CotPositioning] = Field(default_factory=list)
    eia_series: list[EiaSeriesObservation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CorrelationPair(StrictModel):
    """A notable pairwise relationship between two reference/active assets."""
    a: str
    b: str
    corr: float  # rolling Pearson correlation of returns over the window
    # Decoupling: the pair is normally strongly correlated (|baseline|>=0.6)
    # but the recent short-window correlation has collapsed toward zero — a
    # regime tell that often precedes a directional move in the laggard.
    decoupled: bool = False


class CorrelationContext(StrictModel):
    """Cross-asset correlation snapshot (Phase 1.3 roadmap). Replaces the
    coarse string-equality correlation_group test with real rolling-return
    correlations across a fixed macro reference basket. Surfaces the most
    notable positive/negative pairs and any decoupling events."""
    as_of: str
    window_bars: int = 0
    # Flattened notable pairs (|corr| above a salience threshold), capped.
    notable_pairs: list[CorrelationPair] = Field(default_factory=list)
    # Pairs that have decoupled vs their longer-window baseline.
    decoupled_pairs: list[CorrelationPair] = Field(default_factory=list)
    note: str | None = None


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


def _coerce_summary_dict(raw: Any) -> dict[str, str]:
    """Coerce a free-form dict-of-strings field into ``dict[str, str]``.

    Per review 2026-04-30-adhoc-1 #7: the LLM occasionally returns
    nested dicts or lists where the schema demands a flat string
    (observed 2026-04-30: ``open_position_assessments={"WTI": {}}``
    triggered a Pydantic ValidationError and a subagent_failure event).
    This coercion accepts:
      - dict[str, str]   → unchanged
      - dict[str, dict]  → values flattened to JSON-ish strings
      - dict[str, list]  → values joined with semicolons
      - dict[str, None]  → values replaced with ""
      - non-dict input   → empty dict
    All keys are stringified.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k)
        if v is None:
            out[key] = ""
        elif isinstance(v, str):
            out[key] = v
        elif isinstance(v, (list, tuple)):
            out[key] = "; ".join(str(item) for item in v if item is not None)
        elif isinstance(v, dict):
            # Empty dict → empty string. Non-empty → flat key=value;...
            if not v:
                out[key] = ""
            else:
                out[key] = "; ".join(
                    f"{ki}={vi}" for ki, vi in v.items() if vi is not None
                )
        else:
            out[key] = str(v)
    return out


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
        # regime_implications: dict/list → flatten; None → ""
        regime_impl = out.get("regime_implications", "")
        if regime_impl is None:
            regime_impl = ""
        elif not isinstance(regime_impl, str):
            if isinstance(regime_impl, (list, tuple)):
                regime_impl = "; ".join(str(item) for item in regime_impl if item is not None)
            elif isinstance(regime_impl, dict):
                regime_impl = "; ".join(
                    f"{k}={v}" for k, v in regime_impl.items() if v is not None
                ) if regime_impl else ""
            else:
                regime_impl = str(regime_impl)
        return {
            "themes": out.get("themes", []),
            # Per review 2026-04-30-adhoc-1 #7: tolerate dict/list values
            # the LLM occasionally produces in place of strings.
            "per_symbol_summaries": _coerce_summary_dict(out.get("per_symbol_summaries", {})),
            "regime_implications": regime_impl,
            "open_position_assessments": _coerce_summary_dict(
                out.get("open_position_assessments", {})
            ),
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
