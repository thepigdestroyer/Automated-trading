"""Watcher liveness heartbeat (review 2026-04-30-adhoc-1 #6).

The watcher's raw_journal stays silent for many hours during healthy
operation because rules don't trigger when there is nothing to do. On
2026-04-30 the journal had no entries for 10+ hours despite the
watcher running normally — externally indistinguishable from a dead
watcher.

The fix: emit a ``watcher_heartbeat`` event every
``watcher_heartbeat_interval_minutes`` (default 30min). Cheap (one
line of JSON every 60 ticks), unambiguous liveness signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from supervisor.journal import VALID_TYPES, append_event, read_recent
from supervisor.state import WatcherState, load_state, save_state
from supervisor.watcher import WatcherConfig, run_one_tick


# --- valid event type -----------------------------------------------


def test_watcher_heartbeat_is_valid_journal_type():
    assert "watcher_heartbeat" in VALID_TYPES


def test_append_watcher_heartbeat_round_trip(tmp_path):
    path = tmp_path / "journal.jsonl"
    append_event(path, {"type": "watcher_heartbeat", "pid": 12345})
    events = read_recent(path)
    assert len(events) == 1
    assert events[0]["type"] == "watcher_heartbeat"
    assert events[0]["pid"] == 12345
    assert "ts" in events[0]


# --- watcher emits heartbeat on first tick --------------------------


def _basic_cfg(tmp_path: Path) -> WatcherConfig:
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")  # bogus
    return WatcherConfig(
        bot_pid_file=pid_file,
        bot_log=tmp_path / "bot.log",
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "raw_journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill_switch",
        watcher_state_file=tmp_path / "watcher_state.json",
        bot_command=["/bin/true"],
    )


def test_first_tick_emits_heartbeat(tmp_path):
    cfg = _basic_cfg(tmp_path)
    try:
        run_one_tick(cfg)
    except Exception:
        # Other rules may try to fire; we only care about journal contents.
        pass

    events = read_recent(cfg.journal_file)
    hbs = [e for e in events if e.get("type") == "watcher_heartbeat"]
    assert len(hbs) >= 1
    assert "pid" in hbs[0]
    assert hbs[0]["interval_minutes"] == 30.0


def test_state_records_last_heartbeat_at(tmp_path):
    cfg = _basic_cfg(tmp_path)
    try:
        run_one_tick(cfg)
    except Exception:
        pass

    state = load_state(cfg.watcher_state_file)
    assert state.last_heartbeat_at is not None


# --- throttling ------------------------------------------------------


def test_subsequent_tick_within_interval_does_not_emit(tmp_path):
    """Two ticks 1s apart ⇒ only one heartbeat (default interval=30min)."""
    cfg = _basic_cfg(tmp_path)
    try:
        run_one_tick(cfg)
        run_one_tick(cfg)
    except Exception:
        pass

    events = read_recent(cfg.journal_file)
    hbs = [e for e in events if e.get("type") == "watcher_heartbeat"]
    assert len(hbs) == 1


def test_heartbeat_emits_again_after_interval(tmp_path):
    """If the watcher_state's last_heartbeat_at is older than the
    interval, a fresh heartbeat is emitted."""
    cfg = _basic_cfg(tmp_path)
    # Pre-seed an old heartbeat timestamp
    state = WatcherState(
        last_heartbeat_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    )
    save_state(cfg.watcher_state_file, state)

    try:
        run_one_tick(cfg)
    except Exception:
        pass

    events = read_recent(cfg.journal_file)
    hbs = [e for e in events if e.get("type") == "watcher_heartbeat"]
    assert len(hbs) >= 1


def test_short_interval_emits_on_every_tick(tmp_path):
    """Configure the interval to 0 minutes and the heartbeat fires
    every tick."""
    cfg = _basic_cfg(tmp_path)
    cfg.watcher_heartbeat_interval_minutes = 0.0

    try:
        run_one_tick(cfg)
        run_one_tick(cfg)
        run_one_tick(cfg)
    except Exception:
        pass

    events = read_recent(cfg.journal_file)
    hbs = [e for e in events if e.get("type") == "watcher_heartbeat"]
    assert len(hbs) == 3
