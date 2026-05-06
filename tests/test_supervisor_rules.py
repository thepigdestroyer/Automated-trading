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


# ---------------------------------------------------------------------------
# data_feed_degraded — per-symbol streak (bug fix 2026-04-29)
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone

from supervisor.rules import data_feed_degraded


def _dq(symbol: str, bars_dropped: int = 1, *, age_minutes: float = 0.0) -> dict:
    """A synthetic data_quality event. Default timestamp = now.

    Pass ``age_minutes=N`` to backdate the event N minutes into the past.
    """
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {"event": "data_quality",
            "timestamp": ts.isoformat(),
            "data": {"symbol": symbol, "bars_dropped": bars_dropped}}


def test_data_feed_degraded_simple_streak():
    events = [_dq("NG=F") for _ in range(5)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is not None
    assert ev.rule == "data_feed_degraded"
    assert ev.detail["symbol"] == "NG=F"
    assert ev.detail["consecutive_dropped_cycles"] == 5


def test_data_feed_degraded_below_threshold():
    events = [_dq("NG=F") for _ in range(4)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is None


def test_data_feed_degraded_streak_breaks_on_clean_event():
    """A bars_dropped=0 entry breaks the streak for THAT symbol."""
    events = [
        _dq("NG=F", 1), _dq("NG=F", 1),
        _dq("NG=F", 0),  # streak breaker
        _dq("NG=F", 1), _dq("NG=F", 1), _dq("NG=F", 1),
    ]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is None  # only 3 trailing dropped events for NG=F


def test_data_feed_degraded_ignores_other_symbols_interleaved():
    """Bug fix 2026-04-29: NG=F outliers interleaved with CL=F clean events
    must NOT reset NG=F's streak."""
    events = []
    # Build interleaved stream: CL=F clean, NG=F bad, repeat × 6
    for _ in range(6):
        events.append(_dq("CL=F", 0))
        events.append(_dq("NG=F", 1))
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is not None, \
        "NG=F streak must persist across interleaved CL=F events"
    assert ev.detail["consecutive_dropped_cycles"] == 6


def test_data_feed_degraded_fires_for_both_symbols_independently():
    """6 NG=F outliers and 6 CL=F outliers, fully interleaved — both fire."""
    events = []
    for _ in range(6):
        events.append(_dq("CL=F", 1))
        events.append(_dq("NG=F", 1))

    ev_ng = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    ev_cl = data_feed_degraded(recent_events=events, symbol="CL=F", min_streak=5)
    assert ev_ng is not None and ev_ng.detail["consecutive_dropped_cycles"] == 6
    assert ev_cl is not None and ev_cl.detail["consecutive_dropped_cycles"] == 6


def test_data_feed_degraded_other_symbol_clean_does_not_reset_target():
    """Bug-for-bug: previous code broke NG=F's streak the moment ANY other
    symbol appeared in the stream. Confirm the new behaviour ignores it."""
    events = [
        _dq("NG=F", 1),
        _dq("NG=F", 1),
        _dq("CL=F", 1),  # other symbol — old code: break; new code: ignore
        _dq("NG=F", 1),
        _dq("NG=F", 1),
        _dq("NG=F", 1),
    ]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is not None
    assert ev.detail["consecutive_dropped_cycles"] == 5


def test_data_feed_degraded_no_target_events():
    events = [_dq("CL=F", 1) for _ in range(10)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is None


def test_data_feed_degraded_handles_string_bars_dropped():
    now_iso = datetime.now(timezone.utc).isoformat()
    events = [{"event": "data_quality",
               "timestamp": now_iso,
               "data": {"symbol": "NG=F", "bars_dropped": "1"}} for _ in range(5)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F", min_streak=5)
    assert ev is not None
    assert ev.detail["consecutive_dropped_cycles"] == 5


def test_data_feed_degraded_infers_symbol_from_last_event():
    events = [_dq("NG=F") for _ in range(5)]
    ev = data_feed_degraded(recent_events=events, min_streak=5)
    assert ev is not None
    assert ev.detail["symbol"] == "NG=F"


# ---------------------------------------------------------------------------
# data_feed_degraded — recency window (bug fix 2026-04-29 #5)
# ---------------------------------------------------------------------------


def test_data_feed_degraded_all_recent_fires():
    """6 NG=F events all newer than the recency window → fires."""
    events = [_dq("NG=F", age_minutes=i) for i in range(6)]  # 0..5 min old
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is not None
    assert ev.detail["consecutive_dropped_cycles"] == 6


def test_data_feed_degraded_all_stale_silent():
    """6 NG=F events all older than 30min → silent (recency cull)."""
    events = [_dq("NG=F", age_minutes=60 + i) for i in range(6)]  # 60..65 min
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is None


def test_data_feed_degraded_mixed_old_and_recent_uses_only_recent():
    """3 stale + 4 recent for the same symbol → only 4 count, below min=5."""
    events = (
        [_dq("NG=F", age_minutes=120 + i) for i in range(3)]  # stale
        + [_dq("NG=F", age_minutes=i) for i in range(4)]      # recent
    )
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is None  # only 4 recent → below threshold


def test_data_feed_degraded_stale_target_recent_other_symbol_fires_for_other():
    """6 stale NG=F + 5 recent CL=F → fires for CL=F, not for NG=F.

    Reproduces the live incident: NG=F's stale streak must NOT keep firing
    while CL=F's fresh streak must still fire.
    """
    events = (
        [_dq("NG=F", age_minutes=120 + i) for i in range(6)]
        + [_dq("CL=F", age_minutes=i) for i in range(5)]
    )
    ev_ng = data_feed_degraded(recent_events=events, symbol="NG=F",
                               min_streak=5, max_age_minutes=30.0)
    ev_cl = data_feed_degraded(recent_events=events, symbol="CL=F",
                               min_streak=5, max_age_minutes=30.0)
    assert ev_ng is None, "stale NG=F streak must NOT fire"
    assert ev_cl is not None, "recent CL=F streak must fire"
    assert ev_cl.detail["consecutive_dropped_cycles"] == 5


def test_data_feed_degraded_max_age_reported_in_detail():
    events = [_dq("NG=F", age_minutes=i) for i in range(5)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is not None
    assert ev.detail["max_age_minutes"] == 30.0


def test_data_feed_degraded_default_window_is_30_minutes():
    """Sanity check on the default kwarg; if changed, update other callers."""
    import inspect
    sig = inspect.signature(data_feed_degraded)
    assert sig.parameters["max_age_minutes"].default == 30.0


def test_data_feed_degraded_supports_ts_alias():
    """Some events use 'ts' rather than 'timestamp'. Both must work."""
    now_iso = datetime.now(timezone.utc).isoformat()
    events = [{"event": "data_quality", "ts": now_iso,
               "data": {"symbol": "NG=F", "bars_dropped": 1}} for _ in range(5)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is not None
    assert ev.detail["consecutive_dropped_cycles"] == 5


def test_data_feed_degraded_no_timestamp_treated_as_stale():
    """Events without timestamp/ts should be filtered out (treated as old)
    rather than firing forever. This is a safer default than assuming 'now'."""
    events = [{"event": "data_quality",
               "data": {"symbol": "NG=F", "bars_dropped": 1}} for _ in range(5)]
    ev = data_feed_degraded(recent_events=events, symbol="NG=F",
                            min_streak=5, max_age_minutes=30.0)
    assert ev is None


# ---------------------------------------------------------------------------
# data_feed_stale (review 2026-05-02-adhoc-1 #9)
# ---------------------------------------------------------------------------
from supervisor.rules import data_feed_stale


def test_data_feed_stale_fires_on_consecutive_same_last_good_ts():
    """Per review 2026-05-02-adhoc-1 #9: ≥N consecutive data_quality
    events with the SAME last_good_ts → alert_only."""
    same_ts = "2026-05-01T20:00:00+00:00"
    events = [
        {"event": "data_quality", "timestamp": f"2026-05-02T{h:02d}:00:00+00:00",
         "data": {"symbol": "NG=F", "last_good_ts": same_ts, "bars_dropped": 1}}
        for h in range(8, 16)  # 8 events
    ]
    ev = data_feed_stale(recent_events=events, symbol="NG=F", min_streak=8)
    assert ev is not None
    assert ev.rule == "data_feed_stale"
    assert ev.action == "alert_only"
    assert ev.detail["symbol"] == "NG=F"
    assert ev.detail["consecutive_same_last_good_ts"] == 8
    assert ev.detail["last_good_ts"] == same_ts


def test_data_feed_stale_silent_when_streak_below_threshold():
    """Below min_streak should not fire."""
    same_ts = "2026-05-01T20:00:00+00:00"
    events = [
        {"event": "data_quality", "timestamp": f"2026-05-02T{h:02d}:00:00+00:00",
         "data": {"symbol": "NG=F", "last_good_ts": same_ts, "bars_dropped": 1}}
        for h in range(13, 16)  # 3 events only
    ]
    ev = data_feed_stale(recent_events=events, symbol="NG=F", min_streak=8)
    assert ev is None


def test_data_feed_stale_silent_when_last_good_ts_advances():
    """If last_good_ts advances mid-streak, the rule must NOT fire —
    that's a healthy feed, not a stuck one."""
    events = [
        {"event": "data_quality", "timestamp": f"2026-05-02T{h:02d}:00:00+00:00",
         "data": {"symbol": "NG=F",
                  "last_good_ts": f"2026-05-0{(h % 2) + 1}T20:00:00+00:00",
                  "bars_dropped": 1}}
        for h in range(8, 18)  # alternating last_good_ts
    ]
    ev = data_feed_stale(recent_events=events, symbol="NG=F", min_streak=8)
    assert ev is None


def test_data_feed_stale_per_symbol_isolation():
    """Events from a different symbol must not break the per-symbol streak
    counting for the target symbol."""
    same_ts = "2026-05-01T20:00:00+00:00"
    events = []
    for h in range(8, 18):
        events.append({
            "event": "data_quality",
            "timestamp": f"2026-05-02T{h:02d}:00:00+00:00",
            "data": {"symbol": "NG=F", "last_good_ts": same_ts, "bars_dropped": 1},
        })
        events.append({
            "event": "data_quality",
            "timestamp": f"2026-05-02T{h:02d}:01:00+00:00",
            "data": {"symbol": "CL=F",
                     "last_good_ts": f"2026-05-02T{h:02d}:00:00+00:00",
                     "bars_dropped": 1},
        })
    ev = data_feed_stale(recent_events=events, symbol="NG=F", min_streak=8)
    assert ev is not None
    assert ev.detail["consecutive_same_last_good_ts"] == 10


# ---------------------------------------------------------------------------
# Per review 2026-05-05-adhoc-1 #3: cycle_stall_with_open_position
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone

from supervisor.rules import cycle_stall_with_open_position


def _hb(*, age_minutes: float = 0.0) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "event": "cycle_heartbeat",
        "timestamp": ts.isoformat(),
        "data": {"cycle_id": "c_x", "phase": "start"},
    }


def test_cycle_stall_with_open_position_fires_when_stalled_and_position_open():
    """The 2026-05-05 12:31 → 13:03 gap was 32 min — below the 50-min
    heartbeat_silence threshold but well above the 20-min default
    here. With a position open during the gap, the rule must fire."""
    events = [_hb(age_minutes=32)]
    runtime_state = {
        "account": {"open_positions": [{"symbol": "BRENT", "position_id": "DI001"}]}
    }
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is not None
    assert ev.rule == "cycle_stall_with_open_position"
    assert ev.action == "alert_only"
    assert ev.detail["age_minutes"] >= 31.0
    assert ev.detail["open_position_count"] == 1


def test_cycle_stall_silent_when_stalled_but_no_open_positions():
    """Stall without open positions = routine quiet cycle, must NOT fire."""
    events = [_hb(age_minutes=32)]
    runtime_state = {"account": {"open_positions": []}}
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is None


def test_cycle_stall_silent_when_recent_heartbeat():
    """Bot is healthy (recent heartbeat) — must NOT fire even with
    positions open."""
    events = [_hb(age_minutes=5)]
    runtime_state = {
        "account": {"open_positions": [{"symbol": "WTI", "position_id": "DI002"}]}
    }
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is None


def test_cycle_stall_silent_when_runtime_state_unavailable():
    """If runtime_state.json couldn't be read, be permissive — defer to
    heartbeat_silence at the longer threshold."""
    events = [_hb(age_minutes=32)]
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=None,
        max_silence_minutes=20.0,
    )
    assert ev is None


def test_cycle_stall_falls_back_to_positions_dict():
    """If account.open_positions is missing/malformed, fall back to the
    bot's runtime_state.positions dict."""
    events = [_hb(age_minutes=25)]
    runtime_state = {
        "account": {},  # no open_positions field
        "positions": {"DI003": {"symbol": "GOLD", "direction": "buy"}},
    }
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is not None
    assert ev.detail["open_position_count"] == 1


def test_cycle_stall_handles_open_positions_as_count():
    """``account.open_positions`` may be persisted as an int count."""
    events = [_hb(age_minutes=25)]
    runtime_state = {"account": {"open_positions": 2}}
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is not None
    assert ev.detail["open_position_count"] == 2


def test_cycle_stall_fires_with_no_heartbeats_when_other_events_and_position():
    """Bot is writing other events but heartbeats stopped while a
    position is open — surface this as an alert."""
    events = [
        {"event": "data_quality", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": {"symbol": "BRENT"}},
    ]
    runtime_state = {
        "account": {"open_positions": [{"symbol": "BRENT", "position_id": "DI004"}]}
    }
    ev = cycle_stall_with_open_position(
        recent_events=events,
        runtime_state=runtime_state,
        max_silence_minutes=20.0,
    )
    assert ev is not None
    assert ev.detail["reason"] == "no heartbeat events in window"
