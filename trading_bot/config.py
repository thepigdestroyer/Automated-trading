from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from trading_bot.instruments import DEFAULT_ALLOWED_ASSET_CLASSES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_provider: Literal["openai", "azure_openai", "openrouter"] = "openai"
    openai_api_key: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_base_url: str | None = None
    # NOTE 2026-05-20: the `openrouter_*` fields are an inherited misnomer.
    # When `LLM_PROVIDER=openrouter` is set, the bot just uses the OpenAI
    # Python SDK with `base_url=openrouter_base_url` + `api_key=openrouter_api_key`
    # (i.e. any OpenAI-compatible endpoint). The current OCI deployment
    # points these fields at NVIDIA NIM (`https://integrate.api.nvidia.com/v1`)
    # for Kimi K2.6 hosting after OpenRouter→NVIDIA migration. Renaming the
    # fields to `llm_base_url` / `llm_api_key` would be cleaner but requires
    # a config rename across .env files; deferred.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str | None = None
    openrouter_app_title: str | None = "CFD Trading Bot"
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = (
        "medium"
    )
    # Per review 2026-04-30-token-burn #H: override reasoning effort for the
    # three subagents (market research, news synthesiser, symbol research).
    # The Portfolio Manager / decision call keeps the higher effort. Default
    # 'low' cuts subagent token consumption ~30% with negligible quality
    # impact for tasks that are mostly summarisation.
    openai_subagent_reasoning_effort: Literal[
        "minimal", "low", "medium", "high", "xhigh"
    ] = "low"
    # Per review 2026-05-02-news-synth-json: optional per-subagent model
    # overrides. Defaults to None (= use openai_model). Set on the news
    # synthesiser if your primary model produces malformed JSON for the
    # complex NewsSynthesisEnvelope schema (observed 50%+ failure rate
    # with Fireworks-routed gpt-5.4-mini at ~3500 token outputs). A
    # JSON-discipline model like 'openai/gpt-4o-mini' or
    # 'anthropic/claude-3.5-haiku' fits this niche well.
    news_synthesis_model: str | None = None
    # Per review 2026-04-30-token-burn #F: cap LLM output tokens per call
    # to prevent runaway long completions on the structured envelopes.
    # Defaults are sized for typical envelopes; raise per .env if you see
    # truncation in production. None / 0 disables the cap (legacy
    # behaviour).
    #
    # 2026-05-02 update: news_synth cap raised 2000 -> 4000 after observing
    # 50%+ of news_synthesis calls failing with JSONDecodeError (output
    # truncated mid-JSON at chars 5300-5800 = ~1500-1800 tokens). The
    # NewsSynthesisEnvelope schema produces multi-paragraph themes plus
    # per-symbol summaries plus regime implications plus open-position
    # assessments — easily 3000+ tokens for 5-6 symbols.
    #
    # 2026-05-02 follow-up (review 2026-05-02-adhoc-1 #2): news cap
    # tightened back to 2500 now that the brevity prompt + JSON
    # discipline directive are in place. Larger output windows
    # encourage the model to over-generate and produce malformed JSON
    # on second attempts — observed retry payloads at 11-12 KB even
    # with the brevity prompt. 2500 tokens (~9 KB JSON) is sufficient
    # for 4-6 symbols with the concise prompt and forces the model
    # to keep summaries short on the retry path.
    # 2026-05-20: bumped market 1500→6000 and news 2500→6000 after the
    # NVIDIA NIM + Kimi K2.6 thinking-mode migration. With
    # chat_template_kwargs={"thinking": True}, reasoning tokens are
    # consumed BEFORE the content channel emits — production logs
    # showed completion_tokens=1500 with content_len=0 (entire budget
    # spent on internal thinking, nothing surfaced). 6000 leaves room
    # for ~3-4K thinking tokens plus a complete JSON envelope.
    subagent_max_output_tokens_market: int = 6000
    subagent_max_output_tokens_news: int = 6000
    # 2026-05-14: bumped 2500→6000 for kimi-k2.6 DeepInfra reasoning-channel
    # leakage workaround on 38K-token symbol_research prompts.
    # 2026-05-20: same value retained for NVIDIA NIM thinking-mode.
    subagent_max_output_tokens_symbol: int = 6000
    # 2026-05-20: PM decision cap bumped 4000→8000 for the same
    # thinking-mode headroom rationale. Decision JSON envelope is
    # larger (multi-symbol trade plan) so needs more space.
    decision_max_output_tokens: int = 8000
    # Per review 2026-05-02-adhoc-1 #2: also cap the input headlines
    # payload by total bytes (in addition to the existing item-count
    # caps) to prevent prompt bloat on busy headline windows that
    # encourages the model to generate over-long synthesis outputs.
    headline_prompt_max_total_bytes: int = 8192
    # Per review 2026-05-02-adhoc-1 #6: lightweight memory diagnostic
    # snapshots to track which subsystem is holding memory. On
    # 2026-05-02 the bot peaked 350 MB / 400 MB MemoryMax in 3h
    # (88%); without per-allocator data we can't tell what's
    # leaking. Setting cycles_per_snapshot to 0 disables. Default
    # 10 cycles ≈ 5h at 30-min intervals — light enough not to
    # affect runtime, dense enough to spot leaks.
    memory_profile_enabled: bool = True
    memory_profile_cycles_per_snapshot: int = 10
    memory_profile_top_n: int = 5
    # Per review 2026-05-05-adhoc-1 #6: soft-restart trigger when the
    # bot's RSS approaches its systemd MemoryMax cap. systemd's hard
    # SIGKILL at 100% leaves no time to release the position lock or
    # flush logs. By voluntarily exiting at this fraction, systemd's
    # Restart=on-failure handles the bounce cleanly.
    # Set to 0.0 to disable.
    memory_soft_restart_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    # Approximate memory cap in bytes (mirrors systemd MemoryMax=400M
    # on the OCI deployment). Used as the denominator for the soft-
    # restart threshold check. Override if MemoryMax changes.
    memory_max_bytes: int = Field(default=400 * 1024 * 1024, gt=0)
    # Rate-limit between consecutive soft-restarts. If the bot would
    # cross the threshold again within this many seconds of the last
    # one, the trigger is suppressed (prevents restart loops; alert
    # is still emitted for operator visibility).
    memory_soft_restart_min_interval_seconds: int = Field(default=3600, ge=0)
    # Per review 2026-04-30-token-burn #C: TTL caches for the two most
    # repetitive subagents. Macro themes don't shift every 15-30 minutes,
    # so caching market_research / news_synthesis across cycles is
    # nearly free quality-wise but a major token saver. The cache is
    # also invalidated when too many fresh top-tier headlines arrive
    # (breaking-news guard) — see breaking_news_invalidation_threshold.
    market_research_cache_minutes: int = 90
    news_synthesis_cache_minutes: int = 60
    breaking_news_invalidation_threshold: int = 2
    # Per review 2026-04-30-token-burn #G: tighter compression on the
    # headline payloads sent to LLMs. The retention/memory cap
    # (max_news_headline_age_hours=36) is preserved for NewsMemory so
    # the bot still tracks novelty correctly; this is a SEPARATE
    # tighter cap applied to the LLM prompt payloads only.
    headline_prompt_max_age_hours: int = 12
    headline_prompt_max_market_items: int = 20
    headline_prompt_max_per_symbol: int = 8
    headline_prompt_dedupe_enabled: bool = True
    headline_prompt_max_title_chars: int = 200
    headline_prompt_max_snippet_chars: int = 300
    # Per review 2026-05-02-rate-limit: provider-side 429s (observed
    # 80 in a single afternoon from Fireworks via OpenRouter BYOK)
    # benefit from short exponential backoff before retry. Without
    # backoff the news-synthesiser's existing two-attempt retry just
    # fires the second request immediately into the same throttled
    # window and amplifies the failure rate. Set max_retries=0 to
    # disable. Total worst-case delay = sum of base*2^i for i in
    # range(max_retries) plus jitter.
    llm_rate_limit_max_retries: int = 3
    llm_rate_limit_base_delay_seconds: float = 5.0
    llm_rate_limit_jitter_seconds: float = 3.0

    broker: Literal["paper", "ig"] = "paper"

    allow_live_trading: bool = False
    dry_run: bool = True
    allow_shorts: bool = False

    ig_username: str | None = None
    ig_password: str | None = None
    ig_api_key: str | None = None
    ig_account_id: str | None = None
    ig_mode: Literal["demo", "live"] = "demo"
    ig_epics: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    ig_confirm_timeout_seconds: int = 15
    ig_confirm_poll_seconds: float = 0.5

    prt_enabled: bool = False
    prt_data_path: Path = Path("prorealtime/snapshot.json")
    prt_max_data_age_seconds: int = 900
    prt_confirmation_mode: Literal["off", "advisory", "required"] = "advisory"
    prt_export_watchlist_path: Path | None = Path("prorealtime/watchlist.csv")

    symbols: Annotated[list[str], NoDecode] = Field(default_factory=list)
    auto_discover_symbols: bool = True
    max_discovered_symbols: int = 6
    allowed_asset_classes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_ASSET_CLASSES)
    )
    enable_research_subagents: bool = True
    allow_position_reversals: bool = True
    poll_interval_seconds: int = 900

    price_lookback_days: int = 90
    price_interval: str = "60m"
    higher_tf_enabled: bool = True
    vix_context_enabled: bool = True
    correlation_context_enabled: bool = True
    econ_calendar_enabled: bool = True
    econ_calendar_lookahead_hours: int = 24
    high_impact_event_block_minutes: int = 30
    # Post-event block window (minutes after the scheduled event time). Default
    # 30 to symmetrise the gate; previously hardcoded to max(15, pre//2).
    high_impact_post_event_block_minutes: int = 30
    earnings_block_hours: int = 24

    entity_tracking_enabled: bool = True
    news_memory_hours: int = 48
    news_memory_max_items: int = 2000
    news_synthesiser_enabled: bool = True
    llm_input_token_budget: int = 120000
    news_lookback_hours: int = 24
    news_per_symbol: int = 4
    max_news_headline_age_hours: int = 36
    preferred_news_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "bloomberg.com",
            "reuters.com",
            "cnbc.com",
            "marketwatch.com",
            "finance.yahoo.com",
            "ft.com",
            "wsj.com",
        ]
    )
    news_analysis_terms: str = (
        "analysis OR outlook OR forecast OR commentary OR strategist OR opinion OR note"
    )
    max_news_source_domains_per_fetch: int = 4
    news_per_query_limit: int = 3
    market_news_query: str = (
        "stocks OR equities OR fed OR inflation OR gold OR silver OR oil OR natural gas "
        "OR copper OR forex OR dollar OR yields"
    )

    min_model_confidence: float = 0.72
    max_open_positions: int = 2
    max_new_trades_per_cycle: int = 1
    # Per review 2026-05-05-adhoc-1 #1 follow-up: raised from 0.005 to
    # 0.025 (5×). After the qty-rederivation fix in commit d250617,
    # ×100-scaled futures (BRENT/WTI/NATGAS/GOLD) at the previous 0.5%
    # budget produced sub-min_deal_size qty → broker clamp to 1.0 →
    # SIZE_VS_BUDGET_BREACH rejection at the 0.5% × 1.25 ceiling.
    # 2.5% restores tradability while still preventing the 5.27% loss
    # observed on 2026-05-05 (which was 100× over budget pre-fix).
    # User explicitly approved the 5× scope/risk tradeoff.
    risk_per_trade_pct: float = 0.025
    risk_per_trade_pct_base: float = 0.025
    # Confidence-to-size multiplier ceiling. FLATTENED to 1.0 (2026-05
    # adversarial review): the LLM's emitted confidence is uncalibrated and
    # clusters 0.72-0.85 just above the 0.72 entry gate. At a 0% win rate,
    # scaling size UP on that confidence (old 2.5x ceiling -> up to 6.25%
    # risk/trade) demonstrably levered the worst signals. Hold at 1.0 (flat
    # sizing) until a reliability diagram / Brier score proves the confidence
    # is calibrated, then re-introduce graded sizing with a Kelly-fraction basis.
    risk_confidence_scaling_max: float = 1.0
    atr_stop_multiplier: float = 1.5
    stop_loss_pct_floor: float = 0.005
    slippage_buffer_bps: int = 5
    swap_annualised_bps_default: int = 200
    max_notional_per_trade: float = 300.0
    stop_loss_pct: float = 0.012
    take_profit_pct: float = 0.024
    # Phase 1.5 (2026-05): the executor now sets the take-profit distance as
    # stop_distance × risk_reward_ratio, owning the RR deterministically rather
    # than deriving it from an LLM-supplied take_profit_pct/stop_loss_pct ratio
    # (which the code discarded for placement anyway). 2.0 = target 2R per 1R risk.
    risk_reward_ratio: float = 2.0
    # Per review 2026-05-05-adhoc-1 #1: tolerance for the
    # SIZE_VS_BUDGET_BREACH guard. Plans whose computed
    # cash_risk_at_stop exceeds target_cash_risk × (1 + tolerance)
    # are rejected as a defence against scaling / config drift.
    risk_size_vs_budget_tolerance: float = Field(default=0.25, ge=0.0, le=2.0)
    # Per-IG-point value in account currency. Default 1.0 covers the
    # current £1/pt CFD instruments (BRENT/WTI/NATGAS/GOLD/etc); can be
    # overridden when onboarding instruments quoted at a different
    # per-point value.
    point_value_per_unit: float = Field(default=1.0, gt=0.0)
    cooldown_minutes: int = 60
    max_daily_loss_pct: float = Field(default=0.03, gt=0.0, lt=1.0)
    auto_flatten_on_daily_loss: bool = True
    default_max_holding_hours: int = Field(default=72, ge=1, le=720)
    trailing_exit_activation_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    trailing_exit_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    max_symbol_volatility_20d: float | None = Field(default=None, gt=0.0)
    entry_start_hour_utc: int = Field(default=0, ge=0, le=23)
    entry_end_hour_utc: int = Field(default=24, ge=1, le=24)
    max_total_leverage: float = 3.0
    max_currency_exposure_pct: float = 1.5
    correlation_cap: float = 0.7
    consecutive_loss_limit: int = 3
    consecutive_loss_pause_hours: int = 24
    weekly_max_drawdown_pct: float = 0.06
    monthly_max_drawdown_pct: float = 0.10
    ig_session_refresh_margin_seconds: int = 60
    ig_rate_limit_per_minute: int = 20
    ig_max_retries: int = 3
    # If IG's minDealSize forces the normalised order size to exceed the bot's
    # raw requested size by more than this multiple, the trade is rejected.
    # Prevents the GOLD-style 7.8x size overrun observed on 2026-04-28.
    max_size_overrun_tolerance: float = Field(default=3.0, gt=1.0)
    # Pre-submit sanity gate: reject the plan if the configured stop_distance
    # is smaller than (min_stop_multiplier_of_spread × current_spread). On
    # 2026-04-29 three WTI BUYs were submitted with stop_distance=12 on a
    # market with ~4–8 unit spread — every position got chewed up by entry
    # slippage and stopped out within 70s. A 2.0× multiplier would have
    # blocked all three. Set to 0 to disable the gate.
    min_stop_multiplier_of_spread: float = Field(default=2.0, ge=0.0)
    # Per review 2026-04-29-adhoc-1 #2: reject plans whose
    # take_profit_distance (in the SAME units as stop_distance — i.e.
    # post-ig_price_scale rescaling) is below this floor. Mirrors the
    # min_stop check. IG silently clamps limitLevel to its symbol minimum,
    # turning 1:1.6 plans into 1:1 trades with negative expectancy after
    # spread. Default 0.0 = disabled (preserves prior behaviour); set
    # explicitly per deployment to enable. Recommended ≥ 1.5 × IG
    # min_limit_distance for the symbols you trade most.
    min_take_profit_distance: float = Field(default=0.0, ge=0.0)
    kill_switch_file: Path | None = None
    json_log_path: Path | None = Path("logs/events.jsonl")
    # Flat, analysis-ready decision records (one row per cycle×symbol) for the
    # forward-walk paper evaluation. Separate from events.jsonl which is the
    # operational/watcher log. raw_io archives full LLM prompt+completion for
    # audit (large; disable by setting to None).
    decision_records_path: Path | None = Path("logs/decision_records.jsonl")
    llm_raw_io_path: Path | None = Path("logs/llm_raw_io.jsonl")
    alerts_dir: Path = Path("alerts")
    alert_webhook_url: str | None = None
    paper_pnl_simulation_enabled: bool = True
    paper_pnl_path: Path = Path("paper_pnl.json")

    simulated_account_balance: float = 10_000.0
    decision_log_dir: Path = Path("logs")
    runtime_state_path: Path = Path("runtime_state.json")
    supervisor_blacklist_path: Path = Path("supervisor/blacklist.json")

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: list[str] | str) -> list[str]:
        if value in ("", "[]"):
            return []
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        return [str(item).strip().upper() for item in value if str(item).strip()]

    @field_validator("allowed_asset_classes", mode="before")
    @classmethod
    def parse_allowed_asset_classes(cls, value: list[str] | str | None) -> list[str]:
        if value in (None, "", "[]"):
            return list(DEFAULT_ALLOWED_ASSET_CLASSES)
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        parsed = [str(item).strip().lower() for item in value if str(item).strip()]
        return parsed or list(DEFAULT_ALLOWED_ASSET_CLASSES)

    @field_validator("preferred_news_domains", mode="before")
    @classmethod
    def parse_preferred_news_domains(cls, value: list[str] | str | None) -> list[str]:
        if value in (None, "", "[]"):
            return [
                "bloomberg.com",
                "reuters.com",
                "cnbc.com",
                "marketwatch.com",
                "finance.yahoo.com",
                "ft.com",
                "wsj.com",
            ]
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        return [str(item).strip().lower() for item in value if str(item).strip()]

    @field_validator("ig_epics", mode="before")
    @classmethod
    def parse_ig_epics(cls, value: dict[str, str] | str | None) -> dict[str, str]:
        if value in (None, "", "{}"):
            return {}
        if isinstance(value, dict):
            return {
                str(symbol).strip().upper(): str(epic).strip()
                for symbol, epic in value.items()
                if str(symbol).strip() and str(epic).strip()
            }

        mapping: dict[str, str] = {}
        for item in re.split(r"[;,]\s*", value.strip()):
            if not item:
                continue
            if "=" not in item:
                raise ValueError(
                    "IG_EPICS entries must use SYMBOL=EPIC format separated by ';' or ','"
                )
            symbol, epic = item.split("=", 1)
            symbol = symbol.strip().upper()
            epic = epic.strip()
            if symbol and epic:
                mapping[symbol] = epic
        return mapping

    @field_validator("azure_openai_base_url", mode="before")
    @classmethod
    def normalize_azure_openai_base_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None

        base_url = str(value).strip().rstrip("/")
        if base_url.endswith("/openai/v1"):
            return f"{base_url}/"
        if ".openai.azure.com" in base_url:
            return f"{base_url}/openai/v1/"
        return f"{base_url}/"

    @field_validator("openrouter_base_url", mode="before")
    @classmethod
    def normalize_openrouter_base_url(cls, value: str | None) -> str:
        if value in (None, ""):
            return "https://openrouter.ai/api/v1"
        return str(value).strip().rstrip("/")

    @field_validator(
        "trailing_exit_activation_pct",
        "trailing_exit_pct",
        "max_symbol_volatility_20d",
        mode="before",
    )
    @classmethod
    def normalize_optional_floats(cls, value: float | str | None) -> float | None:
        if value in (None, "", "none", "null"):
            return None
        return float(value)

    @field_validator(
        "decision_log_dir",
        "prt_data_path",
        "runtime_state_path",
        "alerts_dir",
        "paper_pnl_path",
        "supervisor_blacklist_path",
        mode="before",
    )
    @classmethod
    def normalize_paths(cls, value: str | Path) -> Path:
        return Path(value)

    @field_validator(
        "prt_export_watchlist_path",
        "kill_switch_file",
        "json_log_path",
        "decision_records_path",
        "llm_raw_io_path",
        mode="before",
    )
    @classmethod
    def normalize_optional_paths(cls, value: str | Path | None) -> Path | None:
        if value in (None, "", "none", "null"):
            return None
        return Path(value)

    @model_validator(mode="after")
    def validate_llm_config(self) -> "Settings":
        if self.llm_provider == "openai":
            if not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        elif self.llm_provider == "azure_openai":
            if not self.azure_openai_api_key:
                raise ValueError(
                    "AZURE_OPENAI_API_KEY is required when LLM_PROVIDER=azure_openai"
                )
            if not self.azure_openai_base_url:
                raise ValueError(
                    "AZURE_OPENAI_BASE_URL is required when LLM_PROVIDER=azure_openai"
                )
        elif self.llm_provider == "openrouter":
            if not self.openrouter_api_key:
                raise ValueError(
                    "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter"
                )
        if self.entry_start_hour_utc >= self.entry_end_hour_utc:
            raise ValueError("ENTRY_START_HOUR_UTC must be lower than ENTRY_END_HOUR_UTC")
        if (self.trailing_exit_activation_pct is None) != (self.trailing_exit_pct is None):
            raise ValueError(
                "TRAILING_EXIT_ACTIVATION_PCT and TRAILING_EXIT_PCT must either both be set or both be empty"
            )
        return self

    @property
    def has_ig_credentials(self) -> bool:
        return bool(self.ig_username and self.ig_password and self.ig_api_key)

    @property
    def execution_mode(self) -> Literal["demo", "live"]:
        if self.broker == "ig":
            return self.ig_mode
        return "demo"

    @property
    def ig_base_url(self) -> str:
        if self.ig_mode == "live":
            return "https://api.ig.com/gateway/deal"
        return "https://demo-api.ig.com/gateway/deal"

    @property
    def llm_api_key(self) -> str:
        if self.llm_provider == "azure_openai":
            return self.azure_openai_api_key or ""
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key or ""
        return self.openai_api_key or ""

    @property
    def llm_base_url(self) -> str | None:
        if self.llm_provider == "azure_openai":
            return self.azure_openai_base_url
        if self.llm_provider == "openrouter":
            return self.openrouter_base_url
        return None

    @property
    def llm_default_headers(self) -> dict[str, str]:
        if self.llm_provider != "openrouter":
            return {}

        # Per 2026-05-20 OpenRouter→NVIDIA NIM swap: only attach the
        # OpenRouter-branded headers (`HTTP-Referer`, `X-OpenRouter-Title`)
        # when the configured base_url actually points at openrouter.ai.
        # NVIDIA NIM ignores unknown headers, but sending OpenRouter-branded
        # ones to an unrelated provider is noisy and confuses metrics.
        if "openrouter.ai" not in (self.openrouter_base_url or ""):
            return {}

        headers: dict[str, str] = {}
        if self.openrouter_site_url:
            headers["HTTP-Referer"] = self.openrouter_site_url
        if self.openrouter_app_title:
            headers["X-OpenRouter-Title"] = self.openrouter_app_title
        return headers

    def validate_runtime(self) -> list[str]:
        errors: list[str] = []
        if self.broker == "ig" and not self.has_ig_credentials:
            errors.append("BROKER=ig requires IG_USERNAME, IG_PASSWORD, IG_API_KEY")
        if self.broker == "paper" and self.allow_live_trading:
            errors.append("ALLOW_LIVE_TRADING is nonsensical with BROKER=paper")
        if self.allow_live_trading and self.max_notional_per_trade > 1000:
            errors.append(
                "ALLOW_LIVE_TRADING=true combined with MAX_NOTIONAL_PER_TRADE>1000 requires manual override"
            )
        if self.kill_switch_file and Path(self.kill_switch_file).exists():
            errors.append(f"KILL_SWITCH_FILE exists at startup: {self.kill_switch_file}")
        try:
            self.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            errors.append(f"Runtime state path unwritable: {exc}")
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            errors.append("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY")
        return errors
