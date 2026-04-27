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
