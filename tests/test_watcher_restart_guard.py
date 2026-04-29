"""Tests for watcher cooldown + liveness guard on restart_bot.

Incident 2026-04-29: ``log_silence`` fired 3 times in 90 seconds, each
spawning a new bot while the previous one was still booting. Two
guards prevent that:

  (a) cooldown: after issuing restart_bot for ANY rule, suppress further
      restart_bot actions for cfg.restart_cooldown_seconds;
  (b) liveness: if bot.pid is still alive, do nothing this tick.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supervisor.watcher import WatcherConfig, run_one_tick


def _make_cfg(tmp_path, **overrides) -> WatcherConfig:
    cfg = WatcherConfig(
        bot_pid_file=tmp_path / "bot.pid",
        bot_log=tmp_path / "bot.log",
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "raw_journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        bot_command=["/bin/true"],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _journal_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _stale_log_silence(log_file: Path) -> None:
    """Make the bot log look stale to trigger log_silence."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("old log line\n")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    import os as _os
    _os.utime(log_file, (old, old))


def test_three_rapid_log_silences_only_first_restarts(tmp_path):
    """3 consecutive ticks with log_silence within cooldown — only one spawn."""
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=60.0, log_silence_minutes=20.0)
    _stale_log_silence(cfg.bot_log)
    # No bot.pid → process_dead also fires, but both want restart_bot;
    # cooldown applies to "any" restart_bot, so only first spawn.
    spawn_calls: list[int] = []

    def spawn():
        spawn_calls.append(1)
        return 12345

    base = datetime(2026, 4, 29, 9, 56, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=spawn, now=base)
    run_one_tick(
        cfg, pid_alive=lambda pid: False, spawn=spawn,
        now=base + timedelta(seconds=30),
    )
    run_one_tick(
        cfg, pid_alive=lambda pid: False, spawn=spawn,
        now=base + timedelta(seconds=55),
    )
    assert len(spawn_calls) == 1, f"expected exactly 1 spawn, got {len(spawn_calls)}"

    entries = _journal_entries(cfg.journal_file)
    suppressed = [e for e in entries if e.get("type") == "remediation_suppressed"]
    assert any(e.get("reason") == "cooldown_active" for e in suppressed), \
        "expected at least one cooldown_active suppression"


def test_cooldown_expires_allows_new_restart(tmp_path):
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=60.0)
    _stale_log_silence(cfg.bot_log)
    spawn_calls: list[int] = []

    def spawn():
        spawn_calls.append(1)
        return 12345

    base = datetime(2026, 4, 29, 9, 56, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=spawn, now=base)
    # 90s later, cooldown has expired
    run_one_tick(
        cfg, pid_alive=lambda pid: False, spawn=spawn,
        now=base + timedelta(seconds=90),
    )
    assert len(spawn_calls) == 2


def test_liveness_check_suppresses_when_pid_alive(tmp_path):
    """If bot.pid is alive, restart_bot must be a no-op."""
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=0.0, log_silence_minutes=20.0)
    _stale_log_silence(cfg.bot_log)
    cfg.bot_pid_file.write_text("12345\n")

    spawn_calls: list[int] = []

    def spawn():
        spawn_calls.append(1)
        return 12345

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=spawn)
    assert spawn_calls == [], "must NOT spawn when previous pid is still alive"

    entries = _journal_entries(cfg.journal_file)
    suppressed = [e for e in entries if e.get("type") == "remediation_suppressed"]
    assert any(e.get("reason") == "previous_pid_still_alive" for e in suppressed)


def test_liveness_check_allows_when_pid_dead(tmp_path):
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=0.0)
    _stale_log_silence(cfg.bot_log)
    cfg.bot_pid_file.write_text("12345\n")

    spawn_calls: list[int] = []

    def spawn():
        spawn_calls.append(1)
        return 67890

    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=spawn)
    assert len(spawn_calls) == 1


def test_state_persists_last_restart_at(tmp_path):
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=60.0)
    _stale_log_silence(cfg.bot_log)
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=lambda: 1)

    assert cfg.watcher_state_file.exists()
    state = json.loads(cfg.watcher_state_file.read_text())
    assert state.get("last_restart_at"), "watcher state must record last_restart_at"


def test_cooldown_does_not_block_other_actions(tmp_path):
    """kill_switch must still fire even when restart cooldown is active."""
    cfg = _make_cfg(tmp_path, restart_cooldown_seconds=60.0)
    _stale_log_silence(cfg.bot_log)
    # Seed prior restart_at into state to put cooldown in effect.
    from supervisor.state import WatcherState, save_state
    save_state(cfg.watcher_state_file, WatcherState(last_restart_at=datetime.now(timezone.utc).isoformat()))

    # Generate three losing trades so consecutive_losses → kill_switch fires.
    from supervisor.journal import append_event
    for _ in range(3):
        append_event(
            cfg.journal_file,
            {"type": "trade_close", "symbol": "NVDA",
             "exit": 100, "pnl_pct": -1.0, "reason": "stop", "held_minutes": 30},
        )
    cfg.bot_pid_file.write_text("12345")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)
    assert cfg.kill_switch_file.exists(), \
        "kill_switch must fire even while restart_bot cooldown is active"
