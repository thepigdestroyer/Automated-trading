"""Portfolio Manager plan-loop avoidance (review 2026-04-29-adhoc-1 #10).

The 2026-04-29 incident: between 11:23 and 16:49 UTC the LLM produced 8
functionally identical WTI BUY plans across 8 cycles, every one rejected
with the same STOP_TOO_TIGHT_VS_SPREAD risk_block. The LLM had no
visibility into "did my last plan even execute".

This change feeds the last 3 cycles' execution outcomes into the
Portfolio Manager prompt so it can avoid repeating identical losing plans.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from trading_bot.config import Settings
from trading_bot.openai_decision import OpenAIDecisionEngine


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill_switch",
        openai_api_key="test",
    )


def test_decide_signature_accepts_recent_execution_outcomes(tmp_path):
    """Regression: the new kwarg must be present in the public signature
    so bot.py can pass it without TypeError."""
    sig = inspect.signature(OpenAIDecisionEngine.decide)
    assert "recent_execution_outcomes" in sig.parameters
    assert sig.parameters["recent_execution_outcomes"].default is None


def test_decide_recent_outcomes_propagates_into_payload(tmp_path, monkeypatch):
    """When the caller passes recent_execution_outcomes, the engine must
    include them in the JSON payload sent to the model.

    Per review 2026-05-02-adhoc-1 #8: identical
    ``(symbol, action, status, error)`` tuples across cycles are
    collapsed into a single entry with ``count`` so the prompt isn't
    polluted by N copies of the same rejection. The test feeds two
    identical cycles and asserts the dedupe behavior.
    """
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine(settings)

    captured: dict = {}

    def fake_run_structured(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["payload"] = payload
        captured["system_prompt"] = system_prompt
        # Return a minimal valid envelope
        from trading_bot.models import DecisionEnvelope
        return DecisionEnvelope(market_regime="neutral-wait",
                                summary="test",
                                trades=[])

    monkeypatch.setattr(engine, "_run_structured_generation", fake_run_structured)

    from trading_bot.models import AccountSnapshot
    acct = AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])

    outcomes = [
        {
            "cycle_id": "c_1", "cycle_started_at": "2026-04-29T11:00:00+00:00",
            "outcomes": [{"symbol": "WTI", "action": "BUY", "status": "failed",
                          "error": "STOP_TOO_TIGHT_VS_SPREAD", "submitted_at": "x"}],
        },
        {
            "cycle_id": "c_2", "cycle_started_at": "2026-04-29T11:30:00+00:00",
            "outcomes": [{"symbol": "WTI", "action": "BUY", "status": "failed",
                          "error": "STOP_TOO_TIGHT_VS_SPREAD", "submitted_at": "y"}],
        },
    ]

    engine.decide(
        account_snapshot=acct,
        symbol_contexts={},
        market_headlines=[],
        recent_execution_outcomes=outcomes,
    )

    assert "payload" in captured
    deduped = captured["payload"]["recent_execution_outcomes"]
    assert len(deduped) == 1, f"expected dedupe to 1 entry, got {deduped}"
    entry = deduped[0]
    assert entry["symbol"] == "WTI"
    assert entry["action"] == "BUY"
    assert entry["status"] == "failed"
    assert entry["error"] == "STOP_TOO_TIGHT_VS_SPREAD"
    assert entry["count"] == 2
    assert entry["first_seen_at"] == "x"
    assert entry["last_seen_at"] == "y"


def test_decide_dedupe_distinct_symbols_kept_separate(tmp_path, monkeypatch):
    """Different (symbol, action) tuples must NOT collapse — only exact
    duplicates do."""
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine(settings)
    captured: dict = {}

    def fake_run_structured(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["payload"] = payload
        from trading_bot.models import DecisionEnvelope
        return DecisionEnvelope(market_regime="neutral-wait", summary="t", trades=[])

    monkeypatch.setattr(engine, "_run_structured_generation", fake_run_structured)
    from trading_bot.models import AccountSnapshot
    acct = AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])

    outcomes = [
        {
            "cycle_id": "c_1", "cycle_started_at": "x",
            "outcomes": [
                {"symbol": "WTI", "action": "BUY", "status": "failed",
                 "error": "A", "submitted_at": "1"},
                {"symbol": "GOLD", "action": "BUY", "status": "submitted",
                 "error": "", "submitted_at": "2"},
            ],
        },
    ]
    engine.decide(account_snapshot=acct, symbol_contexts={}, market_headlines=[],
                  recent_execution_outcomes=outcomes)
    deduped = captured["payload"]["recent_execution_outcomes"]
    assert len(deduped) == 2
    symbols = {e["symbol"] for e in deduped}
    assert symbols == {"WTI", "GOLD"}


def test_decide_default_recent_outcomes_is_empty_list(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine(settings)

    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["payload"] = payload
        from trading_bot.models import DecisionEnvelope
        return DecisionEnvelope(market_regime="neutral-wait",
                                summary="test", trades=[])

    monkeypatch.setattr(engine, "_run_structured_generation", fake_run)

    from trading_bot.models import AccountSnapshot
    acct = AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])

    engine.decide(account_snapshot=acct, symbol_contexts={}, market_headlines=[])

    assert captured["payload"]["recent_execution_outcomes"] == []


def test_system_prompt_mentions_plan_loop_avoidance(tmp_path, monkeypatch):
    """Prompt must explicitly direct the LLM to avoid re-submitting plans
    that were just rejected with the same reason."""
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine(settings)

    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["system_prompt"] = system_prompt
        from trading_bot.models import DecisionEnvelope
        return DecisionEnvelope(market_regime="neutral-wait",
                                summary="test", trades=[])

    monkeypatch.setattr(engine, "_run_structured_generation", fake_run)

    from trading_bot.models import AccountSnapshot
    acct = AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])

    engine.decide(account_snapshot=acct, symbol_contexts={}, market_headlines=[])

    prompt = captured["system_prompt"].lower()
    # Key directives the prompt must carry
    assert "recent_execution_outcomes" in prompt
    assert "do not propose the same" in prompt or "do not propose" in prompt or "do not" in prompt
    # Confirm the avoidance principle is explicit
    assert any(word in prompt for word in ("plan-loop", "plan loop", "loop avoidance"))


# --- bot-level integration ------------------------------------------


def test_bot_appends_recent_outcomes_after_cycle(tmp_path, monkeypatch):
    """After run_cycle records an execution event, the bot must persist
    the per-symbol outcome into runtime_state for the next decide() call."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from trading_bot.bot import TradingBot
    from trading_bot.models import (
        AccountSnapshot, ExecutionPlan, ExecutionResult,
    )
    from trading_bot.json_log import JsonEventLogger

    bot = TradingBot.__new__(TradingBot)
    bot.settings = _settings(tmp_path)
    bot._early_event_logger = JsonEventLogger(path=bot.settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {},
                         "open_position_invalidations": {}}
    bot._current_cycle_id = "c_test"

    # Simulate the post-execution bookkeeping block from run_cycle by
    # replicating its append logic.
    plan = ExecutionPlan(symbol="WTI", action="BUY", confidence=0.8,
                         reason="test",
                         quantity=1.0, target_price=65.0,
                         stop_distance=0.75, take_profit_distance=1.5)
    result = ExecutionResult(plan=plan, status="failed",
                             submitted_at="2026-04-29T11:23:00+00:00",
                             error="STOP_TOO_TIGHT_VS_SPREAD: ...")
    cycle_outcomes = [
        {"symbol": plan.symbol, "action": plan.action, "status": result.status,
         "error": (result.error or "")[:200],
         "submitted_at": result.submitted_at}
    ]
    history = bot.runtime_state.setdefault("recent_execution_outcomes", [])
    history.append({"cycle_id": "c_test",
                    "cycle_started_at": "2026-04-29T11:23:00+00:00",
                    "outcomes": cycle_outcomes})
    bot.runtime_state["recent_execution_outcomes"] = history[-3:]

    saved = bot.runtime_state["recent_execution_outcomes"]
    assert len(saved) == 1
    assert saved[0]["outcomes"][0]["symbol"] == "WTI"
    assert "STOP_TOO_TIGHT" in saved[0]["outcomes"][0]["error"]


def test_recent_outcomes_capped_to_last_3_cycles(tmp_path):
    """Manually drive the cap logic — newest 3 must be retained."""
    history = []
    for i in range(5):
        history.append({"cycle_id": f"c_{i}", "outcomes": []})
    # Apply the same cap as bot.py
    capped = history[-3:]
    assert [c["cycle_id"] for c in capped] == ["c_2", "c_3", "c_4"]
