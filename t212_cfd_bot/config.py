from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from t212_cfd_bot.instruments import DEFAULT_ALLOWED_ASSET_CLASSES


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
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str | None = None
    openrouter_app_title: str | None = "CFD Trading Bot"
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = (
        "medium"
    )

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
    risk_per_trade_pct: float = 0.005
    max_notional_per_trade: float = 300.0
    stop_loss_pct: float = 0.012
    take_profit_pct: float = 0.024
    cooldown_minutes: int = 60
    max_daily_loss_pct: float = Field(default=0.03, gt=0.0, lt=1.0)
    auto_flatten_on_daily_loss: bool = True
    default_max_holding_hours: int = Field(default=72, ge=1, le=720)
    trailing_exit_activation_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    trailing_exit_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    max_symbol_volatility_20d: float | None = Field(default=None, gt=0.0)
    entry_start_hour_utc: int = Field(default=0, ge=0, le=23)
    entry_end_hour_utc: int = Field(default=24, ge=1, le=24)

    simulated_account_balance: float = 10_000.0
    decision_log_dir: Path = Path("logs")
    runtime_state_path: Path = Path("runtime_state.json")

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
        mode="before",
    )
    @classmethod
    def normalize_paths(cls, value: str | Path) -> Path:
        return Path(value)

    @field_validator("prt_export_watchlist_path", mode="before")
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

        headers: dict[str, str] = {}
        if self.openrouter_site_url:
            headers["HTTP-Referer"] = self.openrouter_site_url
        if self.openrouter_app_title:
            headers["X-OpenRouter-Title"] = self.openrouter_app_title
        return headers
