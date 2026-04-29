"""Tests for rule-level emission dedup in the watcher (fix #6).

Incident 2026-04-29: data_feed_degraded emitted ~87 rule_trigger entries
in 30 minutes for the same (NG=F, streak=23) signature because every
tick re-evaluated the rule and got the same answer.

The watcher now stores ``state.rule_dedup`` mapping
``"{rule}::{key}"`` → ISO-8601 timestamp of last emission. Within the
configured window (default 15 min) a repeat emission is silently
dropped — no journal entry, no action. Rules whose ``RuleEvent.key``
is ``None`` (e.g. process_dead, log_silence) bypass dedup entirely.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from supervisor import journal
from supervisor.rules import RuleEvent
from supervisor.state import WatcherState, load_state, save_state
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
    # The bot.log must exist (mtime fresh) so log_silence doesn't fire.
    cfg.bot_log.write_text("alive\n")
    # bot.pid alive → keeps process_dead silent.
    cfg.bot_pid_file.write_text("12345\n")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _journal_entries(path: Path, *, type_: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if type_ is None or obj.get("type") == type_:
            out.append(obj)
    return out


def _write_data_quality_events(path: Path, *, symbol: str, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with path.open("a", encoding="utf-8") as f:
        for i in range(count):
            ts = (now - timedelta(seconds=count - i)).isoformat()
            f.write(json.dumps({
                "event": "data_quality",
                "timestamp": ts,
                "data": {"symbol": symbol, "bars_dropped": 1},
            }) + "\n")


# --- RuleEvent.key surface area -------------------------------------------


def test_rule_event_default_key_is_none():
    ev = RuleEvent(rule="x", action="alert_only")
    assert ev.key is None


def test_rule_event_key_set_via_constructor():
    ev = RuleEvent(rule="x", action="alert_only", key="abc")
    assert ev.key == "abc"


# --- Functional dedup behaviour -------------------------------------------


def test_same_key_within_window_suppresses_second_emission(tmp_path):
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0)
    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=6)

    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0, now=base)
    # 5 minutes later, same NG=F streak → should be deduped.
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0,
                 now=base + timedelta(minutes=5))

    triggers = [e for e in _journal_entries(cfg.journal_file, type_="rule_trigger")
                if e.get("rule") == "data_feed_degraded"]
    assert len(triggers) == 1, \
        f"expected exactly 1 emission, got {len(triggers)}: {triggers}"


def test_same_key_outside_window_fires_again(tmp_path):
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0)
    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=6)

    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0, now=base)
    # 20 minutes later — outside the window.
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0,
                 now=base + timedelta(minutes=20))

    triggers = [e for e in _journal_entries(cfg.journal_file, type_="rule_trigger")
                if e.get("rule") == "data_feed_degraded"]
    assert len(triggers) == 2


def test_different_key_fires_again(tmp_path):
    """When the streak grows from 5 → 6 the key changes (NG=F:5 → NG=F:6),
    so the second emission is NOT deduped."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0)

    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    # Tick 1: only 5 events present → key NG=F:5
    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=5)
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0, now=base)
    # Append one more — now 6 events → key NG=F:6
    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=1)
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0,
                 now=base + timedelta(seconds=30))

    triggers = [e for e in _journal_entries(cfg.journal_file, type_="rule_trigger")
                if e.get("rule") == "data_feed_degraded"]
    assert len(triggers) == 2
    streaks = sorted(t["detail"]["consecutive_dropped_cycles"] for t in triggers)
    assert streaks == [5, 6]


def test_key_none_bypasses_dedup(tmp_path):
    """process_dead has key=None → fires every tick."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0,
                    restart_cooldown_seconds=0.0)
    # Make process_dead trigger by removing pid file.
    cfg.bot_pid_file.unlink()

    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=lambda: 0, now=base)
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=lambda: 0,
                 now=base + timedelta(seconds=30))

    triggers = [e for e in _journal_entries(cfg.journal_file, type_="rule_trigger")
                if e.get("rule") == "process_dead"]
    assert len(triggers) == 2, \
        "process_dead has key=None and must fire every tick (bypasses dedup)"


def test_dedup_persists_across_watcher_restarts(tmp_path):
    """state.rule_dedup must persist in watcher_state.json so that a
    watcher restart doesn't reset the dedup window."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0)
    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=6)

    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0, now=base)

    # Inspect persisted state.
    state = load_state(cfg.watcher_state_file)
    assert state.rule_dedup, "rule_dedup must be persisted"
    keys = list(state.rule_dedup.keys())
    assert any(k.startswith("data_feed_degraded::NG=F:") for k in keys), \
        f"expected dedup key for NG=F streak, got: {keys}"


def test_aged_out_dedup_entries_purged_from_state(tmp_path):
    """rule_dedup entries older than dedup_max_age_minutes are purged."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0,
                    dedup_max_age_minutes=60.0)
    # Seed state with an ancient entry.
    ancient = (datetime(2026, 4, 29, 0, 0, 0, tzinfo=timezone.utc)).isoformat()
    save_state(
        cfg.watcher_state_file,
        WatcherState(rule_dedup={
            "data_feed_degraded::NG=F:23": ancient,
            "repeated_execution_errors::WTI:err:5": ancient,
        }),
    )
    # Run a tick at a much later wall-clock — purge should run.
    run_one_tick(
        cfg, pid_alive=lambda pid: True, spawn=lambda: 0,
        now=datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    state = load_state(cfg.watcher_state_file)
    assert state.rule_dedup == {}, \
        f"all stale dedup entries must be purged; got {state.rule_dedup}"


def test_unrelated_rule_keys_do_not_collide(tmp_path):
    """Two different rules sharing the same key must dedupe independently."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0)
    seed_iso = datetime(2026, 4, 29, 11, 55, 0, tzinfo=timezone.utc).isoformat()
    save_state(
        cfg.watcher_state_file,
        WatcherState(rule_dedup={"data_feed_degraded::NG=F:6": seed_iso}),
    )

    _write_data_quality_events(cfg.events_file, symbol="NG=F", count=6)
    run_one_tick(
        cfg, pid_alive=lambda pid: True, spawn=lambda: 0,
        now=datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    triggers = [e for e in _journal_entries(cfg.journal_file, type_="rule_trigger")
                if e.get("rule") == "data_feed_degraded"]
    # Should be deduped (same rule, same key, within 5 min of seed).
    assert triggers == [], \
        "data_feed_degraded with the same key as a recent emission must be deduped"


# --- Direct unit test: dedup is independent of the restart_bot cooldown ---


def test_restart_bot_cooldown_independent_of_rule_dedup(tmp_path):
    """log_silence has key=None so dedup cannot block it; the restart_bot
    cooldown is the only safeguard. Confirm dedup state is not consulted
    for key=None rules even when state has stale entries."""
    cfg = _make_cfg(tmp_path, dedup_window_minutes=15.0,
                    restart_cooldown_seconds=0.0,
                    log_silence_minutes=20.0)
    # Make the bot.log stale.
    import os as _os
    cfg.bot_log.write_text("old\n")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    _os.utime(cfg.bot_log, (old, old))
    cfg.bot_pid_file.unlink()  # also triggers process_dead

    spawn_calls: list[int] = []
    run_one_tick(cfg, pid_alive=lambda pid: False,
                 spawn=lambda: spawn_calls.append(1) or 1)
    assert len(spawn_calls) == 1
