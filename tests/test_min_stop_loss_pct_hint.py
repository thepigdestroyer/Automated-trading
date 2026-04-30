"""Per-symbol min_stop_loss_pct prompt hint (review 2026-04-30-adhoc-1 #3).

The 2026-04-29 NATGAS rejections at 09:36, 09:42, and 22:07 UTC all had
``stop_loss_pct ≈ 1%`` (=$0.0275 on $2.68 spot), which translates to
~2.75 IG pts vs IG's 20pt minimum stop_distance for NATGAS. Even with
the 2026-04-29 #1 unit-fix (×100 scaling), the LLM was generating
floor-tight stops indiscriminately because it did not know the
per-symbol broker floor.

This test pins:
1. ``min_stop_loss_pct_for(symbol)`` returns the right floors.
2. The Portfolio Manager prompt payload includes a
   ``constraints.min_stop_loss_pct_by_symbol`` map for every symbol
   in the cycle's symbol_contexts.
3. The prompt text references the floor.
"""

from __future__ import annotations

import json

import pytest

from trading_bot.instruments import min_stop_loss_pct_for


# --- min_stop_loss_pct_for() values ----------------------------------


def test_natgas_floor_is_75_basis_points():
    """NATGAS needs the widest %-stop because spot is small (~$2.68)
    relative to IG's 20pt = $0.20 minimum stop_distance."""
    assert min_stop_loss_pct_for("NATGAS") == pytest.approx(0.075)


def test_wti_floor_is_150_basis_points():
    assert min_stop_loss_pct_for("WTI") == pytest.approx(0.015)


def test_brent_floor_is_150_basis_points():
    assert min_stop_loss_pct_for("BRENT") == pytest.approx(0.015)


def test_gold_floor_is_40_basis_points():
    assert min_stop_loss_pct_for("GOLD") == pytest.approx(0.004)


def test_silver_floor_is_metal_default():
    assert min_stop_loss_pct_for("SILVER") == pytest.approx(0.004)


def test_eurusd_floor_is_30_basis_points():
    assert min_stop_loss_pct_for("EURUSD") == pytest.approx(0.003)


def test_usdjpy_floor_is_fx_default():
    assert min_stop_loss_pct_for("USDJPY") == pytest.approx(0.003)


def test_aapl_floor_is_equity_default():
    assert min_stop_loss_pct_for("AAPL") == pytest.approx(0.010)


def test_unknown_symbol_falls_back_to_equity():
    """An unknown symbol returns the equity default (1%) rather than NoneType."""
    assert min_stop_loss_pct_for("DOESNOTEXIST") == pytest.approx(0.010)


def test_lowercase_symbol_normalised():
    assert min_stop_loss_pct_for("natgas") == pytest.approx(0.075)


# --- payload includes min_stop_loss_pct_by_symbol --------------------


def test_decide_payload_includes_min_stop_loss_pct(monkeypatch, tmp_path):
    """The Portfolio Manager call payload must carry a per-symbol
    min_stop_loss_pct map for every symbol in symbol_contexts."""
    from datetime import datetime, timezone

    from trading_bot.config import Settings
    from trading_bot.models import (
        AccountSnapshot,
        Headline,
        IndicatorSnapshot,
        SymbolContext,
    )
    from trading_bot.openai_decision import OpenAIDecisionEngine

    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        openai_api_key="test",
    )

    client = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    client.settings = settings

    captured = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload):
        captured["system_prompt"] = system_prompt
        captured["payload"] = payload
        # Return a minimal valid envelope shape (mocked)
        from trading_bot.models import DecisionEnvelope

        return DecisionEnvelope(
            market_regime="neutral-wait",
            summary="test",
            trades=[],
        )

    client._run_structured_generation = fake_run  # type: ignore[assignment]

    indicators = IndicatorSnapshot(
        price=2.68, sma_20=2.7, sma_50=2.75, ema_12=2.7, ema_26=2.72,
        rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=0.05,
        volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
    )
    natgas_ctx = SymbolContext(
        symbol="NATGAS", display_name="Nat Gas", asset_class="energy",
        as_of=datetime.now(timezone.utc).isoformat(),
        indicators=indicators, headlines=[], chart_note="t",
    )
    wti_ctx = SymbolContext(
        symbol="WTI", display_name="WTI", asset_class="energy",
        as_of=datetime.now(timezone.utc).isoformat(),
        indicators=indicators.model_copy(update={"price": 65.42, "atr_14": 0.5}),
        headlines=[], chart_note="t",
    )

    account = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=10_000.0, free_funds=10_000.0, open_positions=[],
    )

    client.decide(
        account_snapshot=account,
        symbol_contexts={"NATGAS": natgas_ctx, "WTI": wti_ctx},
        market_headlines=[],
    )

    constraints = captured["payload"]["constraints"]
    assert "min_stop_loss_pct_by_symbol" in constraints
    floors = constraints["min_stop_loss_pct_by_symbol"]
    assert floors["NATGAS"] == pytest.approx(0.075)
    assert floors["WTI"] == pytest.approx(0.015)


def test_system_prompt_mentions_min_stop_floor(monkeypatch, tmp_path):
    """The system prompt must explicitly reference the per-symbol
    floor so the LLM can use it (otherwise the payload field is dead weight)."""
    from datetime import datetime, timezone

    from trading_bot.config import Settings
    from trading_bot.models import (
        AccountSnapshot,
        Headline,
        IndicatorSnapshot,
        SymbolContext,
    )
    from trading_bot.openai_decision import OpenAIDecisionEngine

    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        openai_api_key="test",
    )

    client = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    client.settings = settings

    captured = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload):
        captured["system_prompt"] = system_prompt
        from trading_bot.models import DecisionEnvelope
        return DecisionEnvelope(
            market_regime="neutral-wait", summary="test", trades=[],
        )

    client._run_structured_generation = fake_run  # type: ignore[assignment]
    indicators = IndicatorSnapshot(
        price=65.42, sma_20=65, sma_50=64, ema_12=65, ema_26=65,
        rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=0.5,
        volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
    )
    ctx = SymbolContext(
        symbol="WTI", display_name="WTI", asset_class="energy",
        as_of=datetime.now(timezone.utc).isoformat(),
        indicators=indicators, headlines=[], chart_note="t",
    )
    account = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=10_000.0, free_funds=10_000.0, open_positions=[],
    )

    client.decide(
        account_snapshot=account,
        symbol_contexts={"WTI": ctx},
        market_headlines=[],
    )

    prompt = captured["system_prompt"]
    # The prompt should reference the floor mechanism and the worst
    # offender (NATGAS) so the LLM internalises it.
    assert "min_stop_loss_pct_by_symbol" in prompt
    assert "NATGAS" in prompt
    assert "7.5%" in prompt
