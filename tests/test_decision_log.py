"""Tests for the flat decision-record logging (forward-walk eval precondition)."""

from __future__ import annotations

import json
from pathlib import Path

from trading_bot.decision_log import (
    DECISION_RECORD_SCHEMA_VERSION,
    DecisionLogger,
    build_decision_record,
    make_decision_id,
    prompt_sha,
)


def test_make_decision_id():
    assert make_decision_id("c_abc", "WTI") == "c_abc:WTI"


def test_prompt_sha_stable_and_short():
    a = prompt_sha("hello world")
    b = prompt_sha("hello world")
    c = prompt_sha("hello worlx")
    assert a == b
    assert a != c
    assert len(a) == 16


def _decision(**over):
    base = {
        "symbol": "WTI", "action": "BUY", "confidence": 0.78,
        "news_bias": "positive", "max_holding_hours": 24,
    }
    base.update(over)
    return base


def _signals(**over):
    base = {
        "trend": "up", "momentum": "accelerating", "rsi_state": "neutral",
        "mean_reversion_z": -0.4, "bollinger_position": "lower_half",
        "donchian_position": 0.8, "vol_regime": "normal", "volume_state": "normal",
    }
    base.update(over)
    return base


def test_build_record_hoists_key_scalars():
    rec = build_decision_record(
        cycle_id="c1", cycle_ts="2026-05-21T00:00:00+00:00", symbol="WTI",
        decision=_decision(), signals=_signals(),
        indicators={"rsi_14": 55.0, "atr_14": 1.2, "price": 90.5},
        correlation_context={"note": "decoupled: WTI/DXY"},
        plan={"quantity": 0.03, "stop_distance": 1.8, "take_profit_distance": 3.6},
        account={"total_funds": 6980.0, "free_funds": 6950.0},
        regime="energy-supply-shock", prompt_hash="abc123", model="kimi",
        dry_run=True,
    )
    assert rec["schema_version"] == DECISION_RECORD_SCHEMA_VERSION
    assert rec["decision_id"] == "c1:WTI"
    assert rec["action"] == "BUY"
    assert rec["confidence"] == 0.78
    assert rec["trend"] == "up"
    assert rec["mean_reversion_z"] == -0.4
    assert rec["rsi_14"] == 55.0
    assert rec["plan_quantity"] == 0.03
    assert rec["plan_stop_distance"] == 1.8
    assert rec["regime"] == "energy-supply-shock"
    assert rec["account_equity"] == 6980.0
    assert rec["correlation_note"] == "decoupled: WTI/DXY"
    assert rec["dry_run"] is True
    assert rec["model"] == "kimi"


def test_build_record_tolerates_missing_signals_and_plan():
    # A HOLD decision has no plan and may have a context but we pass None.
    rec = build_decision_record(
        cycle_id="c2", cycle_ts="t", symbol="GOLD",
        decision=_decision(action="HOLD", confidence=0.5),
        signals=None, indicators=None, correlation_context=None,
        plan=None, account=None, regime=None, prompt_hash=None,
        model=None, dry_run=True,
    )
    assert rec["action"] == "HOLD"
    assert rec["trend"] is None
    assert rec["plan_quantity"] is None
    assert rec["account_equity"] is None


def test_logger_writes_jsonl(tmp_path: Path):
    p = tmp_path / "logs" / "decision_records.jsonl"
    logger = DecisionLogger(records_path=p, raw_io_path=None)
    rec = build_decision_record(
        cycle_id="c1", cycle_ts="t", symbol="WTI", decision=_decision(),
        signals=_signals(), indicators={"rsi_14": 55.0},
        correlation_context=None, plan=None, account=None, regime="r",
        prompt_hash=None, model=None, dry_run=True,
    )
    logger.log_record(rec)
    logger.log_record(rec)
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["decision_id"] == "c1:WTI"


def test_logger_none_path_is_noop(tmp_path: Path):
    logger = DecisionLogger(records_path=None, raw_io_path=None)
    # Must not raise.
    logger.log_record({"x": 1})
    logger.log_raw_io(cycle_id="c", subagent="s", prompt="p", completion="c", model="m")


def test_logger_raw_io_archive(tmp_path: Path):
    p = tmp_path / "llm_raw_io.jsonl"
    logger = DecisionLogger(records_path=None, raw_io_path=p)
    logger.log_raw_io(
        cycle_id="c1", subagent="decide", prompt="the prompt",
        completion="the completion", model="kimi",
    )
    rec = json.loads(p.read_text().strip())
    assert rec["subagent"] == "decide"
    assert rec["prompt"] == "the prompt"
    assert rec["completion"] == "the completion"
    assert rec["prompt_sha"] == prompt_sha("the prompt")
