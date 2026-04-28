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


# ---------------------------------------------------------------------------
# Task 4.7: Flag-only rules
# ---------------------------------------------------------------------------
from supervisor.rules import (
    llm_error_spike, ig_allowance_burst, news_volume_drop, decision_empty_run,
)


def test_llm_error_spike_fires():
    log_lines = ["ERROR openai: timeout"] * 4
    ev = llm_error_spike(recent_log_lines=log_lines, threshold=3)
    assert ev is not None
    assert ev.action == "alert_only"


def test_llm_error_spike_silent_below_threshold():
    log_lines = ["ERROR openai: timeout"] * 2
    ev = llm_error_spike(recent_log_lines=log_lines, threshold=3)
    assert ev is None


def test_ig_allowance_burst_fires():
    log_lines = ["IG API error: error.public-api.exceeded-api-key-allowance"] * 3
    ev = ig_allowance_burst(recent_log_lines=log_lines, threshold=2)
    assert ev is not None


def test_news_volume_drop_fires():
    cycles = [
        {"type": "cycle", "symbols": ["NVDA"], "ts": f"2026-04-27T{h:02}:00:00+00:00", "headlines_per_symbol": {"NVDA": 0}}
        for h in range(6)
    ]
    ev = news_volume_drop(recent_cycles=cycles, symbol="NVDA", min_cycles_zero=6)
    assert ev is not None


def test_decision_empty_run_fires():
    cycles = [
        {"type": "cycle", "trades_approved": 0, "trades_rejected": 0, "ts": "t1"},
        {"type": "cycle", "trades_approved": 0, "trades_rejected": 0, "ts": "t2"},
    ]
    ev = decision_empty_run(recent_cycles=cycles, threshold=2)
    assert ev is not None


# ---------------------------------------------------------------------------
# Task 4.8: Remaining rules
# ---------------------------------------------------------------------------
from supervisor.rules import (
    weekly_drawdown_breach, holding_too_long, position_book_divergence,
    data_stale, confidence_outcome_divergence, repeated_startup_failures, disk_log_growth,
)


def test_weekly_drawdown_breach_fires():
    closes = [{"type": "trade_close", "pnl_pct": -2.0, "ts": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}] * 4
    ev = weekly_drawdown_breach(recent_trades=closes, max_loss_pct=6.0)
    assert ev is not None


def test_weekly_drawdown_silent_in_cap():
    closes = [{"type": "trade_close", "pnl_pct": -1.0, "ts": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}] * 2
    ev = weekly_drawdown_breach(recent_trades=closes, max_loss_pct=6.0)
    assert ev is None


def test_holding_too_long_fires():
    opened = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    positions = [{"symbol": "NVDA", "opened_at": opened}]
    ev = holding_too_long(positions=positions, max_hours=72)
    assert ev is not None
    assert ev.detail["symbol"] == "NVDA"


def test_holding_too_long_silent_when_within_window():
    opened = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    positions = [{"symbol": "NVDA", "opened_at": opened}]
    ev = holding_too_long(positions=positions, max_hours=72)
    assert ev is None


def test_position_book_divergence_fires():
    local = {"NVDA", "WTI"}
    broker = {"WTI"}
    ev = position_book_divergence(local_positions=local, broker_positions=broker)
    assert ev is not None
    assert "NVDA" in ev.detail["only_local"]


def test_position_book_divergence_silent_when_match():
    ev = position_book_divergence(local_positions={"NVDA"}, broker_positions={"NVDA"})
    assert ev is None


def test_data_stale_fires():
    last_bar = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    ev = data_stale(last_bar_ts=last_bar, symbol="NVDA", max_age_hours=2)
    assert ev is not None


def test_data_stale_silent_when_fresh():
    last_bar = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    ev = data_stale(last_bar_ts=last_bar, symbol="NVDA", max_age_hours=2)
    assert ev is None


def test_confidence_outcome_divergence_fires():
    closes = [
        {"type": "trade_close", "confidence": 0.85, "pnl_pct": -1.0, "ts": f"t{i}"}
        for i in range(10)
    ]
    closes[0]["pnl_pct"] = 1.0
    closes[1]["pnl_pct"] = 1.0
    ev = confidence_outcome_divergence(recent_trades=closes, min_n=10, min_confidence=0.80, min_win_rate=0.30)
    assert ev is not None


def test_confidence_outcome_divergence_silent_when_winning():
    closes = [
        {"type": "trade_close", "confidence": 0.85, "pnl_pct": 1.0, "ts": f"t{i}"}
        for i in range(10)
    ]
    ev = confidence_outcome_divergence(recent_trades=closes, min_n=10, min_confidence=0.80, min_win_rate=0.30)
    assert ev is None


def test_repeated_startup_failures_fires():
    triggers = [
        {"type": "rule_trigger", "rule": "process_dead", "ts": (datetime.now(timezone.utc) - timedelta(minutes=10*i)).isoformat()}
        for i in range(3)
    ]
    ev = repeated_startup_failures(recent_triggers=triggers, threshold=3, window_hours=4)
    assert ev is not None


def test_disk_log_growth_fires(tmp_path):
    log = tmp_path / "bot.log"
    with log.open("wb") as f:
        f.truncate(200 * 1024 * 1024)
    ev = disk_log_growth(log_file=log, max_mb=100)
    assert ev is not None


def test_disk_log_growth_silent_when_small(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("small")
    ev = disk_log_growth(log_file=log, max_mb=100)
    assert ev is None


# ---------------------------------------------------------------------------
# repeated_execution_errors — regression: execution.data is a *list* of
# ExecutionResult dicts, not a single dict. (Bug surfaced 2026-04-28: rule
# crashed with AttributeError 'list' object has no attribute 'get'.)
# ---------------------------------------------------------------------------
from supervisor.rules import repeated_execution_errors


def _exec_event(ts, results):
    return {"event": "execution", "timestamp": ts, "data": results}


def _failed_result(symbol, error):
    return {"plan": {"symbol": symbol, "action": "BUY"}, "status": "failed",
            "submitted_at": "2026-04-28T00:00:00+00:00", "error": error}


def _ok_result(symbol):
    return {"plan": {"symbol": symbol, "action": "BUY"}, "status": "submitted",
            "submitted_at": "2026-04-28T00:00:00+00:00", "broker_response": {"ack": {}}}


def test_repeated_execution_errors_does_not_crash_on_empty_data_list():
    """Empty data list (no plans approved) was the actual production shape that crashed."""
    now = datetime.now(timezone.utc)
    events = [_exec_event((now - timedelta(minutes=i * 10)).isoformat(), []) for i in range(5)]
    ev = repeated_execution_errors(recent_events=events)
    assert ev is None


def test_repeated_execution_errors_does_not_crash_when_data_is_list_of_results():
    now = datetime.now(timezone.utc)
    events = [_exec_event(now.isoformat(), [_ok_result("GOLD"), _ok_result("BRENT")])]
    ev = repeated_execution_errors(recent_events=events)
    assert ev is None  # no failures → no event


def test_repeated_execution_errors_fires_on_threshold_failures_same_symbol():
    now = datetime.now(timezone.utc)
    events = [
        _exec_event((now - timedelta(minutes=10 * i)).isoformat(),
                    [_failed_result("GOLD", "SIZE_INCREMENT")])
        for i in range(3)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4)
    assert ev is not None
    assert ev.rule == "repeated_execution_errors"
    assert ev.action == "alert_only"
    assert ev.detail["symbol"] == "GOLD"
    assert ev.detail["error"] == "SIZE_INCREMENT"
    assert ev.detail["count"] == 3


def test_repeated_execution_errors_silent_below_threshold():
    now = datetime.now(timezone.utc)
    events = [_exec_event(now.isoformat(),
                          [_failed_result("GOLD", "SIZE_INCREMENT")])]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4)
    assert ev is None


def test_repeated_execution_errors_ignores_successful_executions():
    """A successful trade should never count toward the error streak."""
    now = datetime.now(timezone.utc)
    events = [_exec_event(now.isoformat(),
                          [_ok_result("GOLD"), _ok_result("GOLD"), _ok_result("GOLD")])]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None


def test_repeated_execution_errors_groups_by_symbol_and_error():
    """3 failures spread across 2 symbols should not trigger a single-key match."""
    now = datetime.now(timezone.utc)
    events = [
        _exec_event(now.isoformat(), [_failed_result("GOLD", "SIZE_INCREMENT")]),
        _exec_event(now.isoformat(), [_failed_result("BRENT", "SIZE_INCREMENT")]),
        _exec_event(now.isoformat(), [_failed_result("GOLD", "MARKET_CLOSED")]),
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None  # no key reaches threshold=3


def test_repeated_execution_errors_respects_window_hours():
    """Old failures outside the window must be ignored."""
    now = datetime.now(timezone.utc)
    events = [
        _exec_event((now - timedelta(hours=10)).isoformat(),  # outside 4h window
                    [_failed_result("GOLD", "X")])
        for _ in range(5)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4)
    assert ev is None


def test_repeated_execution_errors_tolerates_dict_shaped_data_legacy():
    """Defensive: if a future log shape stores data as a single dict, don't crash."""
    now = datetime.now(timezone.utc)
    events = [
        {"event": "execution", "timestamp": now.isoformat(),
         "data": _failed_result("GOLD", "X")}  # dict, not list
        for _ in range(3)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4)
    assert ev is not None
    assert ev.detail["symbol"] == "GOLD"
