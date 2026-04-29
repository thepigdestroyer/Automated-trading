"""Auto-blacklist on repeated same-symbol-same-error events
(review 2026-04-29-adhoc-1 #4).

When the bot loops on a symbol that the broker / pre-submit gate keeps
rejecting for the same reason, the watcher should add the symbol to the
supervisor blacklist for a short TTL — both to stop the LLM wasting
cycles re-deciding into a known-blocked symbol AND to free the second
"best idea" slot for other opportunities.

Action: ``blacklist_symbol`` with detail.ttl_hours.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supervisor import blacklist as bl_mod
from supervisor.rules import auto_blacklist_repeated_errors
from supervisor.watcher import WatcherConfig, run_one_tick


def _risk_block(symbol: str, reason: str, *, age_minutes: float = 0.0) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "event": "risk_block",
        "timestamp": ts.isoformat(),
        "data": {"symbol": symbol, "reason": reason},
    }


def _exec_event(ts, results):
    return {"event": "execution", "timestamp": ts, "data": results}


def _failed_result(symbol, error):
    return {"plan": {"symbol": symbol, "action": "BUY"}, "status": "failed",
            "submitted_at": "2026-04-29T11:00:00+00:00", "error": error}


# --- pure rule unit tests --------------------------------------------


def test_eight_wti_blocks_trigger_blacklist_at_threshold_5():
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(8)]
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=5)
    assert ev is not None
    assert ev.rule == "auto_blacklist_repeated_errors"
    assert ev.action == "blacklist_symbol"
    assert ev.detail["symbol"] == "WTI"
    assert ev.detail["error"] == "risk_block:STOP_TOO_TIGHT_VS_SPREAD"
    assert ev.detail["count"] == 8
    assert ev.detail["ttl_hours"] == 4.0


def test_below_threshold_no_blacklist():
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(3)]
    # default threshold=5
    ev = auto_blacklist_repeated_errors(recent_events=events)
    assert ev is None


def test_threshold_can_be_lowered():
    events = [_risk_block("WTI", "X") for _ in range(3)]
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=3)
    assert ev is not None
    assert ev.detail["symbol"] == "WTI"


def test_old_blocks_outside_window_excluded():
    events = [
        _risk_block("WTI", "X", age_minutes=60 * 10) for _ in range(10)
    ]
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=5,
                                        window_hours=4.0)
    assert ev is None


def test_execution_failures_also_count():
    """A pure execution.error stream of 5 same-symbol-same-error → blacklist."""
    now = datetime.now(timezone.utc)
    events = [_exec_event(now.isoformat(),
                          [_failed_result("WTI", "ATTACHED_ORDER_LEVEL_ERROR")])
              for _ in range(5)]
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=5)
    assert ev is not None
    assert ev.detail["symbol"] == "WTI"
    assert ev.detail["error"] == "ATTACHED_ORDER_LEVEL_ERROR"


def test_grouped_by_symbol_and_reason():
    """Symbol fired only when same reason hits threshold — different reasons
    don't combine."""
    events = (
        [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(3)]
        + [_risk_block("WTI", "ATTACHED_ORDER_LEVEL_ERROR") for _ in range(3)]
    )
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=5)
    # Each bucket has 3, neither hits 5
    assert ev is None


def test_ttl_hours_propagated_to_detail():
    events = [_risk_block("WTI", "X") for _ in range(5)]
    ev = auto_blacklist_repeated_errors(recent_events=events, threshold=5,
                                        ttl_hours=2.5)
    assert ev is not None
    assert ev.detail["ttl_hours"] == 2.5


# --- end-to-end through the watcher --------------------------------


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
        log_silence_minutes=600.0,  # disable
        max_log_mb=10_000.0,
    )


def test_watcher_writes_blacklist_entry_when_rule_fires(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    # Stuff 5 same-symbol-same-reason risk_blocks into events.jsonl
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(5)]
    with cfg.events_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    active = bl_mod.read_active(cfg.blacklist_file)
    assert "WTI" in active


def test_watcher_journals_remediation_when_blacklist_fires(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(5)]
    with cfg.events_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    journal = cfg.journal_file.read_text(encoding="utf-8").splitlines()
    rem_lines = [
        line for line in journal
        if "remediation" in line and "blacklist_symbol" in line and "WTI" in line
    ]
    assert rem_lines, "expected blacklist_symbol remediation in journal"


def test_watcher_below_threshold_does_not_blacklist(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    # Only 3 events — below default threshold=5 in watcher
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(3)]
    with cfg.events_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    active = bl_mod.read_active(cfg.blacklist_file)
    assert "WTI" not in active


def test_watcher_blacklist_ttl_default_is_4h(tmp_path):
    """Confirm default TTL on the watcher path is 4h (not 24h)."""
    cfg = _baseline_cfg(tmp_path)
    assert cfg.auto_blacklist_ttl_hours == 4.0
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(5)]
    with cfg.events_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    raw = json.loads(cfg.blacklist_file.read_text(encoding="utf-8"))
    entry = raw["symbols"]["WTI"]
    added = datetime.fromisoformat(entry["added"])
    expires = datetime.fromisoformat(entry["expires"])
    delta_h = (expires - added).total_seconds() / 3600.0
    # Allow ±5 minute tolerance for clock jitter / float rounding
    assert 3.9 < delta_h < 4.1
