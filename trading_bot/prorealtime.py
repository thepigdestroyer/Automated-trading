from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.config import Settings
from trading_bot.models import (
    DecisionEnvelope,
    ExecutionPlan,
    MarketResearchEnvelope,
    ProRealTimeSnapshotEnvelope,
    ProRealTimeSymbolSnapshot,
)

LOGGER = logging.getLogger(__name__)


class ProRealTimeDataClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def load_snapshot(self) -> ProRealTimeSnapshotEnvelope | None:
        if not self.settings.prt_enabled:
            return None

        path = self.settings.prt_data_path
        if not path.exists():
            LOGGER.info("ProRealTime snapshot path does not exist yet: %s", path)
            return None

        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = self._load_csv_rows(path)
            envelope = ProRealTimeSnapshotEnvelope.model_validate(payload)
        except Exception as exc:
            LOGGER.warning("Failed to load ProRealTime snapshot from %s: %s", path, exc)
            return None

        fresh_symbols = [
            symbol
            for symbol in envelope.symbols
            if self._is_symbol_fresh(symbol, envelope.as_of)
        ]
        if envelope.symbols and not fresh_symbols:
            LOGGER.warning(
                "Ignoring ProRealTime snapshot from %s because all symbol rows were stale",
                path,
            )
            return None

        return ProRealTimeSnapshotEnvelope(
            as_of=envelope.as_of,
            source=envelope.source or f"file:{path.name}",
            note=envelope.note,
            flatten_all=envelope.flatten_all,
            symbols=fresh_symbols,
        )

    def get_symbol_snapshot(
        self,
        *,
        symbol: str,
        snapshot: ProRealTimeSnapshotEnvelope | None,
    ) -> ProRealTimeSymbolSnapshot | None:
        if snapshot is None:
            return None
        normalized = symbol.strip().upper()
        for item in snapshot.symbols:
            if item.symbol.upper() == normalized:
                return item
        return None

    def export_watchlist(
        self,
        *,
        active_symbols: list[str],
        market_research: MarketResearchEnvelope | None,
        decision: DecisionEnvelope,
        plans: list[ExecutionPlan],
    ) -> None:
        path = self.settings.prt_export_watchlist_path
        if path is None:
            return

        rows: list[dict[str, str]] = []
        market_candidates = {
            candidate.symbol.upper(): candidate
            for candidate in (market_research.candidates if market_research else [])
        }
        decisions = {trade.symbol.upper(): trade for trade in decision.trades}
        plans_by_symbol = {plan.symbol.upper(): plan for plan in plans}

        for symbol in active_symbols:
            candidate = market_candidates.get(symbol)
            trade = decisions.get(symbol)
            plan = plans_by_symbol.get(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "market_bias": candidate.bias if candidate else "",
                    "market_confidence": f"{candidate.confidence:.2f}" if candidate else "",
                    "model_action": trade.action if trade else "",
                    "model_confidence": f"{trade.confidence:.2f}" if trade else "",
                    "approved_action": plan.action if plan else "",
                    "reason": (plan.reason if plan else (trade.thesis if trade else ""))[:400],
                }
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "symbol",
                    "market_bias",
                    "market_confidence",
                    "model_action",
                    "model_confidence",
                    "approved_action",
                    "reason",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _load_csv_rows(path: Path) -> dict:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return {
                "source": f"file:{path.name}",
                "symbols": list(reader),
            }

    def _is_symbol_fresh(
        self,
        symbol: ProRealTimeSymbolSnapshot,
        envelope_as_of: str | None,
    ) -> bool:
        timestamp = symbol.as_of or envelope_as_of
        if not timestamp:
            return True
        try:
            parsed = _parse_timestamp(timestamp)
        except ValueError:
            return True
        age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
        return age_seconds <= self.settings.prt_max_data_age_seconds


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
