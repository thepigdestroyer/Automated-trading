from pathlib import Path

from supervisor.rules import RuleEvent


def test_rule_event_basic():
    e = RuleEvent(rule="x", action="alert_only", detail={"k": "v"})
    assert e.rule == "x"
    assert e.action == "alert_only"
    assert e.detail == {"k": "v"}


# ---------------------------------------------------------------------------
# Task 4.2: process_dead
# ---------------------------------------------------------------------------
from supervisor.rules import process_dead


def test_process_dead_returns_event_when_pid_missing(tmp_path):
    pid_file = tmp_path / "no.pid"
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: False)
    assert ev is not None
    assert ev.rule == "process_dead"
    assert ev.action == "restart_bot"


def test_process_dead_silent_when_alive(tmp_path):
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: True)
    assert ev is None


def test_process_dead_returns_event_when_pid_not_running(tmp_path):
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: False)
    assert ev is not None


# ---------------------------------------------------------------------------
# Task 4.3: log_silence
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone
from supervisor.rules import log_silence


def test_log_silence_fires_after_threshold(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("old line\n")
    import os
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
    os.utime(log, (old, old))
    ev = log_silence(log_file=log, max_silence_minutes=20)
    assert ev is not None
    assert ev.rule == "log_silence"


def test_log_silence_silent_when_recent(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("recent\n")
    ev = log_silence(log_file=log, max_silence_minutes=20)
    assert ev is None


def test_log_silence_silent_when_log_missing(tmp_path):
    ev = log_silence(log_file=tmp_path / "nope.log", max_silence_minutes=20)
    assert ev is None


# ---------------------------------------------------------------------------
# Task 4.4: consecutive_losses
# ---------------------------------------------------------------------------
from supervisor.rules import consecutive_losses


def test_consecutive_losses_fires():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "2026-04-27T10:00:00+00:00"},
        {"type": "trade_close", "pnl_pct": -1.0, "ts": "2026-04-27T11:00:00+00:00"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "2026-04-27T12:00:00+00:00"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is not None
    assert ev.action == "kill_switch"


def test_consecutive_losses_silent_with_win_in_streak():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "t1"},
        {"type": "trade_close", "pnl_pct":  0.8, "ts": "t2"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "t3"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is None


def test_consecutive_losses_silent_below_threshold():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "t1"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "t2"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is None


# ---------------------------------------------------------------------------
# Task 4.5: same_symbol_stop_streak
# ---------------------------------------------------------------------------
from supervisor.rules import same_symbol_stop_streak


def test_same_symbol_stop_streak_fires():
    closes = [
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.0, "ts": "t1"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -0.8, "ts": "t2"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.2, "ts": "t3"},
    ]
    ev = same_symbol_stop_streak(recent_trades=closes, threshold=3)
    assert ev is not None
    assert ev.action == "blacklist_symbol"
    assert ev.detail["symbol"] == "NVDA"


def test_same_symbol_silent_when_different_symbol():
    closes = [
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.0, "ts": "t1"},
        {"type": "trade_close", "symbol": "WTI",  "reason": "stop", "pnl_pct": -0.8, "ts": "t2"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.2, "ts": "t3"},
    ]
    ev = same_symbol_stop_streak(recent_trades=closes, threshold=3)
    assert ev is None


# ---------------------------------------------------------------------------
# Task 4.6: daily_drawdown_breach
# ---------------------------------------------------------------------------
from supervisor.rules import daily_drawdown_breach


def test_daily_drawdown_breach_fires():
    rollup = {"net_pnl_pct": -3.5}
    ev = daily_drawdown_breach(rollup=rollup, max_loss_pct=3.0)
    assert ev is not None
    assert ev.action == "kill_switch"


def test_daily_drawdown_silent_within_cap():
    ev = daily_drawdown_breach(rollup={"net_pnl_pct": -1.0}, max_loss_pct=3.0)
    assert ev is None


def test_daily_drawdown_silent_when_positive():
    ev = daily_drawdown_breach(rollup={"net_pnl_pct": 1.5}, max_loss_pct=3.0)
    assert ev is None
