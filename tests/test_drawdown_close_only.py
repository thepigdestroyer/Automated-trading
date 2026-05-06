"""CLOSE-only LLM mode on drawdown gate (review 2026-05-05-adhoc-1 #5).

When daily drawdown breaches the cap AND positions are open, the bot
should NOT short-circuit. Instead it runs the LLM in CLOSE-ONLY mode
(prompt-suffix + risk-gate filter), letting the model time the close
intelligently. When no positions are open, the existing short-circuit
path (no LLM, ~80% token saving) is preserved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    IndicatorSnapshot,
    OpenPosition,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager


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
        max_daily_loss_pct=0.03,
    )


def _make_bot(tmp_path: Path) -> TradingBot:
    settings = _settings(tmp_path)
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot.runtime_state = {"daily": {}, "positions": {}}
    bot.broker = MagicMock()
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    return bot


def _account(equity: float, *, positions: list[OpenPosition] | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=equity, free_funds=equity,
        open_positions=positions or [],
    )


def _ctx(symbol: str, price: float) -> SymbolContext:
    return SymbolContext(
        symbol=symbol, display_name=symbol, asset_class="energy",
        as_of="2026-05-05T12:00:00+00:00",
        indicators=IndicatorSnapshot(
            price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
            rsi_14=50.0, macd=0.0, macd_signal=0.0,
            atr_14=price * 0.02, volatility_20d=0.1,
            return_1d=0.0, return_5d=0.0,
        ),
        chart_note="t",
    )


# --- _compute_entry_block_lite: drawdown_close_only state machine ---


def test_drawdown_close_only_true_when_breached_and_positions_open(tmp_path):
    """Daily drawdown breached + open positions → drawdown_close_only flag set,
    block_new_entries also True (still listed as a 'block' so the lite output
    is internally consistent), and the cycle loop's special-case test
    (only_drawdown_blocks) routes to the LLM path instead of short-circuit."""
    bot = _make_bot(tmp_path)
    # Seed yesterday's starting equity so today's £-400 looks like a 5% drawdown
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 8_000.0,
    }
    pos = OpenPosition(
        symbol="BRENT", position_id="DI001", quantity=1.0,
        current_price=113.0, direction="buy",
    )
    acct = _account(equity=7_580.0, positions=[pos])  # 5.25% drawdown

    out = bot._compute_entry_block_lite(
        account_snapshot=acct, prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert out["daily_loss_breached"] is True
    assert out["drawdown_close_only"] is True
    assert out["block_new_entries"] is True
    # Drawdown is the ONLY block reason (sessions open via mocked calendar)
    assert len(out["block_reasons"]) == 1
    assert "daily drawdown" in out["block_reasons"][0]


def test_drawdown_close_only_false_when_breached_but_no_positions(tmp_path):
    """Drawdown breached but no open positions → close_only flag False,
    short-circuit behaviour unchanged. Today's 13:03 → 20:07 UTC behaviour
    on 2026-05-05 (where bot was empty AND in skip-mode)."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 8_000.0,
    }
    acct = _account(equity=7_580.0, positions=[])  # 5.25% drawdown, no positions

    out = bot._compute_entry_block_lite(
        account_snapshot=acct, prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert out["daily_loss_breached"] is True
    assert out["drawdown_close_only"] is False
    # block_new_entries still True (still need to short-circuit), but the
    # close-only LLM path won't be taken because the flag is False.
    assert out["block_new_entries"] is True


def test_drawdown_close_only_false_when_drawdown_within_cap(tmp_path):
    """No drawdown breach → close_only flag False (normal flow)."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 8_000.0,
    }
    acct = _account(equity=7_950.0, positions=[])  # 0.625% drawdown

    out = bot._compute_entry_block_lite(
        account_snapshot=acct, prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert out["daily_loss_breached"] is False
    assert out["drawdown_close_only"] is False


# --- RiskManager: close_only_mode plan filter ---


def _decision_buy(symbol: str = "BRENT") -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="energy-supply-shock",
        summary="t",
        trades=[TradeDecision(
            symbol=symbol, action="BUY", confidence=0.85,
            thesis="t", news_bias="neutral", invalidate_if="x",
            stop_loss_pct=0.012, take_profit_pct=0.024,
        )],
    )


def _decision_close(symbol: str, position_id: str) -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="energy-supply-shock",
        summary="t",
        trades=[TradeDecision(
            symbol=symbol, action="CLOSE", confidence=1.0,
            thesis="time to close", news_bias="neutral",
            close_position_id=position_id, invalidate_if="x",
        )],
    )


def _decision_hold(symbol: str = "BRENT") -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="event-risk-block",
        summary="t",
        trades=[TradeDecision(
            symbol=symbol, action="HOLD", confidence=0.5,
            thesis="t", news_bias="neutral", invalidate_if="x",
        )],
    )


def _close_only_guardrail(symbol: str) -> dict:
    return {
        "block_new_entries": False,  # in LLM path, lite-block is bypassed
        "block_reasons": [],
        "close_requests": [],
        "econ_events": [],
        "earnings_events": {},
        "session_open": {symbol: True},
        "close_only_mode": True,  # the new flag
    }


def test_close_only_mode_rejects_buy_plan(caplog):
    s = Settings()
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision_buy("BRENT"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0)},
        account_snapshot=_account(equity=8_000.0),
        last_trade_times={},
        guardrail_context=_close_only_guardrail("BRENT"),
    )
    assert plans == [], "BUY must be rejected in close_only_mode"


def test_close_only_mode_passes_close_plan():
    s = Settings()
    rm = RiskManager(s)
    pos = OpenPosition(
        symbol="BRENT", position_id="DI001", quantity=1.0,
        current_price=113.0, direction="buy",
    )
    plans = rm.build_execution_plans(
        decision=_decision_close("BRENT", "DI001"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0)},
        account_snapshot=_account(equity=8_000.0, positions=[pos]),
        last_trade_times={},
        guardrail_context=_close_only_guardrail("BRENT"),
    )
    assert len(plans) == 1
    assert plans[0].action == "CLOSE"
    assert plans[0].position_id == "DI001"


def test_close_only_mode_passes_hold_plan_silently():
    """HOLD never produces a plan anyway — must remain silent in close_only_mode."""
    s = Settings()
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision_hold("BRENT"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0)},
        account_snapshot=_account(equity=8_000.0),
        last_trade_times={},
        guardrail_context=_close_only_guardrail("BRENT"),
    )
    assert plans == []


def test_close_only_mode_off_allows_buy():
    """When close_only_mode is False/absent, BUY plans go through the
    normal pipeline (no spurious rejection)."""
    s = Settings()
    rm = RiskManager(s)
    guardrail = _close_only_guardrail("BRENT")
    guardrail["close_only_mode"] = False
    plans = rm.build_execution_plans(
        decision=_decision_buy("BRENT"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0)},
        account_snapshot=_account(equity=8_000.0),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert len(plans) == 1
    assert plans[0].action == "BUY"


def test_close_only_mode_rejects_sell_plan():
    """SELL is also a non-CLOSE/non-HOLD action and must be rejected."""
    s = Settings(allow_shorts=True)
    rm = RiskManager(s)
    decision = DecisionEnvelope(
        market_regime="risk-off-macro",
        summary="t",
        trades=[TradeDecision(
            symbol="BRENT", action="SELL", confidence=0.85,
            thesis="t", news_bias="neutral", invalidate_if="x",
            stop_loss_pct=0.012, take_profit_pct=0.024,
        )],
    )
    plans = rm.build_execution_plans(
        decision=decision,
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0)},
        account_snapshot=_account(equity=8_000.0),
        last_trade_times={},
        guardrail_context=_close_only_guardrail("BRENT"),
    )
    assert plans == []
