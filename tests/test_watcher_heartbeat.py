"""Heartbeat-based silence detection (review 2026-04-29-adhoc-1 #7).

Replaces the file-mtime-based log_silence rule with a positive-signal
detector: the bot writes ``cycle_heartbeat`` events at the start and end
of every cycle; the watcher fires ``heartbeat_silence`` when no
heartbeat has been written for more than ``max_silence_minutes`` (default
50min ≈ 2× normal cycle cadence).

The 2026-04-29 incident: 22 log_silence triggers in 24h, 19 suppressed
by previous_pid_still_alive, 3 led to unnecessary restarts. The rule
was firing on idle gaps between cycles, not on actual hangs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supervisor.rules import heartbeat_silence
from supervisor.watcher import WatcherConfig, run_one_tick


def _hb(*, age_minutes: float = 0.0, phase: str = "start") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {"event": "cycle_heartbeat", "timestamp": ts.isoformat(),
            "data": {"cycle_id": "c_x", "phase": phase}}


# --- pure rule -------------------------------------------------------


def test_heartbeat_silence_fires_when_last_hb_too_old():
    events = [_hb(age_minutes=120)]  # 2h old, default threshold 50min
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=50)
    assert ev is not None
    assert ev.rule == "heartbeat_silence"
    assert ev.action == "restart_bot"
    assert ev.detail["age_minutes"] >= 119


def test_heartbeat_silence_silent_when_recent():
    events = [_hb(age_minutes=10)]
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=50)
    assert ev is None


def test_heartbeat_silence_silent_when_no_events_at_all():
    """Cold start (events file not yet created or filtered to empty)."""
    ev = heartbeat_silence(recent_events=[], max_silence_minutes=50)
    assert ev is None


def test_heartbeat_silence_fires_when_other_events_but_no_hb():
    """Bot is writing other events but heartbeats stopped → bot hung mid-cycle."""
    events = [
        {"event": "data_quality", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": {}},
    ]
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=50)
    assert ev is not None
    assert ev.rule == "heartbeat_silence"


def test_heartbeat_silence_uses_most_recent_hb():
    events = [_hb(age_minutes=120), _hb(age_minutes=10)]
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=50)
    assert ev is None


def test_heartbeat_silence_supports_ts_alias():
    """Some events may use 'ts' instead of 'timestamp'."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
    events = [{"event": "cycle_heartbeat", "ts": ts, "data": {}}]
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=50)
    assert ev is not None


def test_heartbeat_silence_threshold_configurable():
    events = [_hb(age_minutes=15)]
    # 10min threshold → fires
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=10)
    assert ev is not None
    # 30min threshold → silent
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=30)
    assert ev is None


def test_heartbeat_silence_includes_max_in_detail():
    events = [_hb(age_minutes=120)]
    ev = heartbeat_silence(recent_events=events, max_silence_minutes=42.0)
    assert ev is not None
    assert ev.detail["max_silence_minutes"] == 42.0


# --- watcher wiring --------------------------------------------------


def _baseline_cfg(tmp_path: Path) -> WatcherConfig:
    pid = tmp_path / "bot.pid"
    pid.write_text("1")
    log = tmp_path / "bot.log"
    log.write_text("ok\n")
    return WatcherConfig(
        bot_pid_file=pid,
        bot_log=log,
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        bot_command=["/bin/true"],
        log_silence_minutes=600.0,  # disable legacy rule
        heartbeat_silence_minutes=30.0,
        max_log_mb=10_000.0,
    )


def test_watcher_journals_heartbeat_silence_trigger(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    # Only stale heartbeats in the events file
    stale = _hb(age_minutes=120)
    with cfg.events_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps(stale) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    journal_lines = cfg.journal_file.read_text(encoding="utf-8").splitlines()
    triggers = [l for l in journal_lines if "heartbeat_silence" in l]
    assert triggers, "expected heartbeat_silence trigger to be journaled"


def test_watcher_silent_when_recent_heartbeat(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    fresh = _hb(age_minutes=5)
    with cfg.events_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps(fresh) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    journal_text = cfg.journal_file.read_text(encoding="utf-8") if cfg.journal_file.exists() else ""
    assert "heartbeat_silence" not in journal_text


def test_watcher_default_heartbeat_silence_minutes_is_50(tmp_path):
    """Default cadence guards against 25-min cycles producing false positives."""
    pid = tmp_path / "bot.pid"
    pid.write_text("1")
    cfg = WatcherConfig(
        bot_pid_file=pid,
        bot_log=tmp_path / "bot.log",
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        bot_command=["/bin/true"],
    )
    assert cfg.heartbeat_silence_minutes == 50.0
