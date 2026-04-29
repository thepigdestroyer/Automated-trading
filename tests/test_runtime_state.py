"""runtime_state.json must reflect broker-confirmed account state every
cycle (review 2026-04-29-adhoc-1 #6).

On 2026-04-29 the supervisor's /bot-status and watcher rules read
runtime_state.json and saw account_balance=null, starting_balance=null,
daily_pnl=null, closed_trades=[] all day even though the bot's internal
Guardrails logger was reporting daily_drawdown=0.36% and account=£9,638.13.

This test verifies _refresh_runtime_state and _emit_reconciled_trade_close
populate the supervisor-visible fields.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import AccountSnapshot, OpenPosition


def _make_bot(tmp_path: Path) -> TradingBot:
    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill_switch",
        openai_api_key="test",
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    return bot


def _account(*, total: float = 9638.13, free: float = 9638.13,
             positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=total, free_funds=free,
        open_positions=positions or [],
    )


# --- account.balance / mode / currency populated -----------------


def test_account_balance_populated(tmp_path):
    bot = _make_bot(tmp_path)
    bot._refresh_runtime_state(_account(total=9638.13), datetime.now(timezone.utc))

    state = bot.runtime_state
    assert state["account"]["balance"] == pytest.approx(9638.13)
    assert state["account_balance"] == pytest.approx(9638.13)


def test_account_currency_and_mode_populated(tmp_path):
    bot = _make_bot(tmp_path)
    bot._refresh_runtime_state(_account(), datetime.now(timezone.utc))

    state = bot.runtime_state
    assert state["account"]["currency"] == "GBP"
    assert state["account"]["mode"] == "demo"


def test_account_free_funds_populated(tmp_path):
    bot = _make_bot(tmp_path)
    bot._refresh_runtime_state(_account(total=10_000.0, free=8_500.0),
                               datetime.now(timezone.utc))

    assert bot.runtime_state["account"]["free_funds"] == pytest.approx(8500.0)


def test_account_updated_at_iso_string(tmp_path):
    bot = _make_bot(tmp_path)
    bot._refresh_runtime_state(_account(), datetime.now(timezone.utc))

    updated = bot.runtime_state["account"]["updated_at"]
    # Round-trip
    parsed = datetime.fromisoformat(updated)
    assert parsed.tzinfo is not None


# --- starting_balance / daily_pnl ---------------------------------


def test_starting_balance_and_daily_pnl_populated(tmp_path):
    bot = _make_bot(tmp_path)
    # Set up daily guardrail state as _build_guardrail_context would
    bot.runtime_state["daily"] = {
        "date": "2026-04-29",
        "starting_equity": 10_000.0,
    }
    bot._refresh_runtime_state(_account(total=9638.13),
                               datetime.now(timezone.utc))

    assert bot.runtime_state["starting_balance"] == 10_000.0
    assert bot.runtime_state["daily_pnl"] == pytest.approx(-361.87)


def test_daily_pnl_positive_when_account_grows(tmp_path):
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-29",
        "starting_equity": 10_000.0,
    }
    bot._refresh_runtime_state(_account(total=10_500.0),
                               datetime.now(timezone.utc))

    assert bot.runtime_state["daily_pnl"] == pytest.approx(500.0)


def test_starting_balance_skipped_when_daily_not_initialised(tmp_path):
    bot = _make_bot(tmp_path)
    # daily is empty (first cycle of the day not yet)
    bot._refresh_runtime_state(_account(), datetime.now(timezone.utc))

    assert "starting_balance" not in bot.runtime_state


# --- closed_trades --------------------------------------------------


def test_closed_trades_appended_on_vanish(tmp_path):
    bot = _make_bot(tmp_path)
    bot.broker.get_transaction_history = MagicMock(return_value=[])

    pos = OpenPosition(symbol="WTI", position_id="DI001", quantity=1.0,
                       current_price=65.42, direction="buy")
    bot._refresh_runtime_state(_account(positions=[pos]),
                               datetime.now(timezone.utc))

    # Cycle 2: vanished
    bot._refresh_runtime_state(_account(positions=[]),
                               datetime.now(timezone.utc))

    closed = bot.runtime_state["closed_trades"]
    assert len(closed) == 1
    assert closed[0]["position_id"] == "DI001"
    assert closed[0]["symbol"] == "WTI"
    assert "closed_at" in closed[0]


def test_multiple_closes_accumulate(tmp_path):
    bot = _make_bot(tmp_path)
    bot.broker.get_transaction_history = MagicMock(return_value=[])

    p1 = OpenPosition(symbol="WTI", position_id="DI100", quantity=1.0,
                      current_price=65.0, direction="buy")
    p2 = OpenPosition(symbol="GOLD", position_id="DI101", quantity=0.5,
                      current_price=4700.0, direction="buy")
    bot._refresh_runtime_state(_account(positions=[p1, p2]),
                               datetime.now(timezone.utc))
    bot._refresh_runtime_state(_account(positions=[]),
                               datetime.now(timezone.utc))

    closed = bot.runtime_state["closed_trades"]
    assert len(closed) == 2
    pids = sorted([c["position_id"] for c in closed])
    assert pids == ["DI100", "DI101"]


def test_closed_trades_capped_at_500(tmp_path):
    """Sanity: we cap the list to avoid unbounded growth."""
    bot = _make_bot(tmp_path)
    # Pre-populate 500
    bot.runtime_state["closed_trades"] = [{"position_id": f"x{i}"} for i in range(500)]
    bot.broker.get_transaction_history = MagicMock(return_value=[])

    pos = OpenPosition(symbol="WTI", position_id="DI999", quantity=1.0,
                       current_price=65.0, direction="buy")
    bot._refresh_runtime_state(_account(positions=[pos]),
                               datetime.now(timezone.utc))
    bot._refresh_runtime_state(_account(positions=[]),
                               datetime.now(timezone.utc))

    closed = bot.runtime_state["closed_trades"]
    assert len(closed) == 500
    # Newest entry must be at the end
    assert closed[-1]["position_id"] == "DI999"


# --- supervisor end-to-end shape --------------------------------


def test_runtime_state_serializes_with_supervisor_keys(tmp_path):
    """Ensure the new keys round-trip through the on-disk JSON file
    used by supervisor /bot-status."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-29", "starting_equity": 10_000.0,
    }
    bot._refresh_runtime_state(_account(total=9750.0), datetime.now(timezone.utc))

    # Persist
    bot._save_runtime_state()
    on_disk = json.loads(
        bot.settings.runtime_state_path.read_text(encoding="utf-8")
    )

    assert "account" in on_disk
    assert on_disk["account"]["balance"] == pytest.approx(9750.0)
    assert on_disk["account_balance"] == pytest.approx(9750.0)
    assert on_disk["starting_balance"] == 10_000.0
    assert on_disk["daily_pnl"] == pytest.approx(-250.0)
