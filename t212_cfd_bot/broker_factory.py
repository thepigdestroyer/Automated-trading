from __future__ import annotations

import logging
from datetime import datetime, timezone

from t212_cfd_bot.config import Settings
from t212_cfd_bot.ig_bridge import IGRestBridge
from t212_cfd_bot.models import AccountSnapshot, ExecutionPlan, ExecutionResult
from t212_cfd_bot.trading212_bridge import Trading212LegacyBridge

LOGGER = logging.getLogger(__name__)


class PaperBroker:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self) -> None:
        LOGGER.info("Using paper broker fallback because no live broker credentials were provided")

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            mode=self.settings.execution_mode,
            currency="GBP",
            total_funds=self.settings.simulated_account_balance,
            free_funds=self.settings.simulated_account_balance,
            open_positions=[],
        )

    def execute(self, plan: ExecutionPlan, dry_run: bool) -> ExecutionResult:
        return ExecutionResult(
            plan=plan,
            status="dry_run",
            submitted_at=datetime.now(timezone.utc).isoformat(),
            broker_response={"simulated": True, "reason": "paper broker fallback"},
        )

    def is_tradeable_symbol(self, symbol: str) -> bool:
        return True


def build_broker(settings: Settings) -> Trading212LegacyBridge | IGRestBridge | PaperBroker:
    if settings.broker == "paper":
        return PaperBroker(settings)
    if settings.broker == "ig":
        if settings.has_ig_credentials:
            return IGRestBridge(settings)
        raise RuntimeError("BROKER=ig requires IG_USERNAME, IG_PASSWORD, and IG_API_KEY")
    if settings.broker == "trading212":
        if settings.has_trading212_credentials:
            return Trading212LegacyBridge(settings)
        raise RuntimeError(
            "BROKER=trading212 requires TRADING212_USERNAME and TRADING212_PASSWORD"
        )
    raise RuntimeError(f"Unsupported broker setting: {settings.broker}")
