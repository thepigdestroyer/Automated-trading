from __future__ import annotations

import logging
from datetime import datetime, timezone

from trading_bot.config import Settings
from trading_bot.ig_bridge import IGRestBridge
from trading_bot.models import AccountSnapshot, ExecutionPlan, ExecutionResult

LOGGER = logging.getLogger(__name__)


class PaperBroker:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self) -> None:
        LOGGER.info("Using paper broker; no external API calls will be made")

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            mode=self.settings.execution_mode,
            currency="GBP",
            total_funds=self.settings.simulated_account_balance,
            free_funds=self.settings.simulated_account_balance,
            open_positions=[],
        )

    def execute(self, plan: ExecutionPlan, dry_run: bool, *, cycle_id: str | None = None) -> ExecutionResult:
        return ExecutionResult(
            plan=plan,
            status="dry_run",
            submitted_at=datetime.now(timezone.utc).isoformat(),
            broker_response={"simulated": True, "reason": "paper broker"},
        )

    def is_tradeable_symbol(self, symbol: str) -> bool:
        return True


def build_broker(settings: Settings) -> IGRestBridge | PaperBroker:
    if settings.broker == "paper":
        return PaperBroker(settings)
    if settings.broker == "ig":
        if not settings.has_ig_credentials:
            raise RuntimeError("BROKER=ig requires IG_USERNAME, IG_PASSWORD, and IG_API_KEY")
        return IGRestBridge(settings)
    raise RuntimeError(f"Unsupported broker: {settings.broker}")
