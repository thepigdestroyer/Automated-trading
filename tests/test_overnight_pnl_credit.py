"""Overnight P&L carry-over (review 2026-04-30-adhoc-1 #1).

On 2026-04-30 a single losing WTI trade opened at 23:23 UTC the prior
day and closed overnight via the IG-side stop. The reconcile path that
ran on the next bot startup (after the 00:00 UTC date roll) attributed
the £519 loss to today's daily-loss budget, breaching the 3% guardrail
within minutes and freezing the bot for the entire trading session.

The fix tags overnight broker-driven closes (positions whose
``first_seen_at`` is on a prior UTC date) and credits their cash P&L to
``runtime_state.daily.overnight_pnl_credit`` so the guardrail's drawdown
calc subtracts it from gross drawdown.

Equivalently: a loss realised on a position opened yesterday should not
consume today's daily-loss budget.
"""

from __future__ import annotations

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
        max_daily_loss_pct=0.03,
        openai_api_key="test",
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    bot._current_market_vix = None
    bot._current_econ_events = []
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.forced_close_symbols = set()
    return bot


def _account(*, total: float, free: float | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=total, free_funds=free if free is not None else total,
        open_positions=[],
    )


# --- _emit_reconciled_trade_close: overnight detection ----------------


def test_overnight_position_tagged(tmp_path):
    """A position opened yesterday and closed today is tagged
    overnight=True in the trade_close payload."""
    bot = _make_bot(tmp_path)
    stale_pos = {
        "symbol": "WTI",
        "direction": "buy",
        "entry_price": 108.50,
        "last_price": 107.00,
        "first_seen_at": "2026-04-29T23:23:11+00:00",
    }
    # Mock now()
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 0, 26, 36, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close(
            "DI001", stale_pos,
            broker_tx={"profitAndLoss": "-519.87", "level": "10704.8"},
        )
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    closed = bot.runtime_state["closed_trades"]
    assert len(closed) == 1
    assert closed[0]["overnight"] is True
    assert closed[0]["opened_date"] == "2026-04-29"
    assert closed[0]["pnl_cash"] == pytest.approx(-519.87)


def test_intraday_position_not_tagged_overnight(tmp_path):
    """A position opened and closed on the same UTC date is not flagged."""
    bot = _make_bot(tmp_path)
    stale_pos = {
        "symbol": "WTI",
        "direction": "buy",
        "entry_price": 65.0,
        "last_price": 64.5,
        "first_seen_at": "2026-04-30T09:30:00+00:00",
    }
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close(
            "DI002", stale_pos,
            broker_tx={"profitAndLoss": "-50.00", "level": "64.5"},
        )
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    closed = bot.runtime_state["closed_trades"]
    assert closed[0]["overnight"] is False
    assert closed[0]["opened_date"] == "2026-04-30"
    # No overnight_pnl_credit for intraday closes
    assert "overnight_pnl_credit" not in bot.runtime_state.get("daily", {})


def test_overnight_loss_credits_to_daily(tmp_path):
    """An overnight loss (negative pnl_cash) is added to
    daily.overnight_pnl_credit as a positive magnitude."""
    bot = _make_bot(tmp_path)
    stale_pos = {
        "symbol": "WTI",
        "direction": "buy",
        "entry_price": 108.5,
        "last_price": 107.0,
        "first_seen_at": "2026-04-29T23:23:11+00:00",
    }
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 0, 26, 36, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close(
            "DI001", stale_pos,
            broker_tx={"profitAndLoss": "-519.87", "level": "10704.8"},
        )
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    daily = bot.runtime_state["daily"]
    assert daily["overnight_pnl_credit"] == pytest.approx(519.87)


def test_overnight_gain_NOT_credited(tmp_path):
    """An overnight WIN is not credited (would unfairly inflate today's
    risk budget)."""
    bot = _make_bot(tmp_path)
    stale_pos = {
        "symbol": "WTI",
        "direction": "buy",
        "entry_price": 108.5,
        "last_price": 110.0,
        "first_seen_at": "2026-04-29T23:23:11+00:00",
    }
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 0, 26, 36, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close(
            "DI001", stale_pos,
            broker_tx={"profitAndLoss": "+200.00", "level": "110.0"},
        )
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    daily = bot.runtime_state["daily"]
    assert daily.get("overnight_pnl_credit", 0.0) == 0.0


def test_multiple_overnight_losses_accumulate(tmp_path):
    """Two overnight losses → credit sums their absolute magnitudes."""
    bot = _make_bot(tmp_path)
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 0, 30, 0, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close(
            "DI001",
            {"symbol": "WTI", "direction": "buy", "entry_price": 108.5,
             "first_seen_at": "2026-04-29T23:00:00+00:00"},
            broker_tx={"profitAndLoss": "-300.00"},
        )
        bot._emit_reconciled_trade_close(
            "DI002",
            {"symbol": "GOLD", "direction": "buy", "entry_price": 4700.0,
             "first_seen_at": "2026-04-29T22:00:00+00:00"},
            broker_tx={"profitAndLoss": "-150.00"},
        )
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    assert bot.runtime_state["daily"]["overnight_pnl_credit"] == pytest.approx(450.0)


def test_overnight_credit_skipped_without_broker_tx(tmp_path):
    """Without broker_tx (no confirmed cash P&L) we skip the credit
    rather than guess from price drift × estimated quantity."""
    bot = _make_bot(tmp_path)
    stale_pos = {
        "symbol": "WTI",
        "direction": "buy",
        "entry_price": 108.5,
        "last_price": 107.0,
        "first_seen_at": "2026-04-29T23:23:11+00:00",
    }
    import trading_bot.bot as bot_mod
    real_dt = bot_mod.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_dt(2026, 4, 30, 0, 26, 36, tzinfo=timezone.utc)

    bot_mod.datetime = _FixedDT  # type: ignore[assignment]
    try:
        bot._emit_reconciled_trade_close("DI001", stale_pos, broker_tx=None)
    finally:
        bot_mod.datetime = real_dt  # type: ignore[assignment]

    # The trade_close event still fires (overnight=True tagged), but
    # no credit until broker confirms cash P&L.
    closed = bot.runtime_state["closed_trades"]
    assert closed[0]["overnight"] is True
    assert bot.runtime_state.get("daily", {}).get("overnight_pnl_credit", 0.0) == 0.0


# --- _build_guardrail_context: drawdown subtracts overnight credit ----


def test_drawdown_subtracts_overnight_credit(tmp_path):
    """If overnight_pnl_credit covers the loss, drawdown_pct → 0%."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-30",
        "starting_equity": 9638.13,
        "overnight_pnl_credit": 519.87,  # the WTI loss from yesterday
    }
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.forced_close_symbols = set()
    bot._current_econ_events = []

    # Simulate post-overnight-loss account
    snapshot = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=9118.26, free_funds=9118.26, open_positions=[],
    )
    ctx = bot._build_guardrail_context(
        account_snapshot=snapshot,
        symbol_contexts={},
        prorealtime_snapshot=None,
        now=datetime(2026, 4, 30, 1, 30, 0, tzinfo=timezone.utc),
    )
    # Drawdown should be ~0 after credit (gross 519, credit 519, net ~0)
    assert ctx["block_new_entries"] is False
    assert ctx["daily_loss"]["drawdown_pct"] < 0.01
    assert ctx["daily_loss"]["breached"] is False


def test_drawdown_partial_credit_still_blocks(tmp_path):
    """If today incurs a fresh loss on top of overnight, only the
    overnight portion is credited."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-30",
        "starting_equity": 9638.13,
        "overnight_pnl_credit": 100.0,  # small overnight loss
    }
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.forced_close_symbols = set()
    bot._current_econ_events = []

    # Account is 500 below starting; overnight credit = 100; net = 400 ≈ 4.15%
    snapshot = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=9138.13, free_funds=9138.13, open_positions=[],
    )
    ctx = bot._build_guardrail_context(
        account_snapshot=snapshot,
        symbol_contexts={},
        prorealtime_snapshot=None,
        now=datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc),
    )
    # 400 / 9638 = 4.15% > 3% threshold → still breached
    assert ctx["block_new_entries"] is True
    assert ctx["daily_loss"]["drawdown_pct"] == pytest.approx(0.0415, abs=0.005)
    assert ctx["daily_loss"]["breached"] is True


def test_drawdown_no_credit_field_works_normally(tmp_path):
    """Backward compat: legacy daily state without overnight_pnl_credit
    behaves identically to before."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-30",
        "starting_equity": 10_000.0,
    }
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.forced_close_symbols = set()
    bot._current_econ_events = []

    snapshot = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=9500.0, free_funds=9500.0, open_positions=[],
    )
    ctx = bot._build_guardrail_context(
        account_snapshot=snapshot,
        symbol_contexts={},
        prorealtime_snapshot=None,
        now=datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc),
    )
    # 5% drawdown, no credit → breached
    assert ctx["block_new_entries"] is True
    assert ctx["daily_loss"]["drawdown_pct"] == pytest.approx(0.05, abs=0.001)


def test_overnight_credit_surfaced_in_runtime_state(tmp_path):
    """The credit is mirrored at runtime_state top-level so /bot-status
    can surface why drawdown is muted."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["daily"] = {
        "date": "2026-04-30",
        "starting_equity": 10_000.0,
        "overnight_pnl_credit": 250.0,
    }
    snapshot = AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=9750.0, free_funds=9750.0, open_positions=[],
    )
    bot._refresh_runtime_state(snapshot, datetime.now(timezone.utc))

    assert bot.runtime_state.get("overnight_pnl_credit") == pytest.approx(250.0)
