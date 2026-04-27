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
