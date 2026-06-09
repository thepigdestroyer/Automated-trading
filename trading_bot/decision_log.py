"""Flat, analysis-ready decision record logging.

Precondition for the forward-walk paper evaluation (2026-05 adversarial
review, findings ML-eng-#7 and backtest-F9): the existing `decision` event is
a nested pydantic dump that is painful to analyse and does not join the
LLM's output to the deterministic signal context, the resulting execution
plan, or (later) the realised outcome.

This module writes ONE flat JSONL row per (cycle, symbol) decision into a
dedicated `decision_records.jsonl`, plus archives the raw LLM prompt/completion
so decisions can be audited (NOT replayed — the LLM is non-deterministic and
training-contaminated; see the amended roadmap). A separate join key
(`decision_id`) lets a later trade_close be matched back to the decision that
opened it, enabling confidence-vs-outcome calibration and expectancy stats.

Pure builders + a thin writer so the schema can be unit-tested without I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# Schema version — bump when fields change so downstream analysis can adapt.
DECISION_RECORD_SCHEMA_VERSION = 1


def make_decision_id(cycle_id: str, symbol: str) -> str:
    """Stable id joining a decision to its later outcome."""
    return f"{cycle_id}:{symbol}".strip(":")


def prompt_sha(prompt: str) -> str:
    """Short content hash of the rendered prompt — lets analysis attribute
    P&L to a specific prompt version without storing the full text in every
    flat row."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def build_decision_record(
    *,
    cycle_id: str,
    cycle_ts: str,
    symbol: str,
    decision: dict[str, Any],
    signals: dict[str, Any] | None,
    indicators: dict[str, Any] | None,
    correlation_context: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    account: dict[str, Any] | None,
    regime: str | None,
    prompt_hash: str | None,
    model: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Build a single flat decision row. All inputs are plain dicts (already
    model_dump'd) so this stays pure and testable.

    The row is intentionally FLAT (no deep nesting) for the columns that
    matter to analysis; richer objects (signals, correlation) are kept as
    compact sub-dicts but the high-signal scalars are hoisted to top level.
    """
    sig = signals or {}
    ind = indicators or {}
    pl = plan or {}
    acct = account or {}
    return {
        "schema_version": DECISION_RECORD_SCHEMA_VERSION,
        "decision_id": make_decision_id(cycle_id, symbol),
        "cycle_id": cycle_id,
        "cycle_ts": cycle_ts,
        "symbol": symbol,
        # --- LLM decision (hoisted scalars) ---
        "action": decision.get("action"),
        "confidence": decision.get("confidence"),
        "news_bias": decision.get("news_bias"),
        "max_holding_hours": decision.get("max_holding_hours"),
        # --- deterministic signal context (hoisted) ---
        "trend": sig.get("trend"),
        "momentum": sig.get("momentum"),
        "rsi_state": sig.get("rsi_state"),
        "mean_reversion_z": sig.get("mean_reversion_z"),
        "bollinger_position": sig.get("bollinger_position"),
        "donchian_position": sig.get("donchian_position"),
        "vol_regime": sig.get("vol_regime"),
        "volume_state": sig.get("volume_state"),
        "rsi_14": ind.get("rsi_14"),
        "atr_14": ind.get("atr_14"),
        "price": ind.get("price"),
        # --- resulting plan (None when HOLD / no plan) ---
        "plan_quantity": pl.get("quantity"),
        "plan_stop_distance": pl.get("stop_distance"),
        "plan_take_profit_distance": pl.get("take_profit_distance"),
        # --- context ---
        "regime": regime,
        "account_equity": acct.get("total_funds"),
        "account_free": acct.get("free_funds"),
        "correlation_note": (correlation_context or {}).get("note"),
        # --- provenance ---
        "prompt_sha": prompt_hash,
        "model": model,
        "dry_run": dry_run,
    }


class DecisionLogger:
    """Writes flat decision rows + archives raw LLM I/O. Best-effort: never
    raises into the cycle."""

    def __init__(
        self,
        *,
        records_path: Path | None,
        raw_io_path: Path | None = None,
    ):
        self.records_path = records_path
        self.raw_io_path = raw_io_path
        for p in (records_path, raw_io_path):
            if p is not None:
                p.parent.mkdir(parents=True, exist_ok=True)

    def log_record(self, record: dict[str, Any]) -> None:
        if self.records_path is None:
            return
        try:
            with self.records_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to write decision record: %s", exc)

    def log_raw_io(
        self,
        *,
        cycle_id: str,
        subagent: str,
        prompt: str,
        completion: str,
        model: str | None,
    ) -> None:
        """Archive the raw prompt + completion for later audit. Stored
        separately from the flat rows because it is large and only needed for
        forensic inspection, not routine analysis."""
        if self.raw_io_path is None:
            return
        try:
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "cycle_id": cycle_id,
                "subagent": subagent,
                "model": model,
                "prompt_sha": prompt_sha(prompt),
                "prompt": prompt,
                "completion": completion,
            }
            with self.raw_io_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to write raw LLM I/O: %s", exc)
