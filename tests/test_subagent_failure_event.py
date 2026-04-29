"""Structured subagent_failure events + watcher streak rule
(review 2026-04-29-adhoc-1 #9).

The 2026-04-29 incident: 2 distinct LLM provider failures in 80min
(OpenRouter no-choices + Fireworks 503) only emitted WARNING log lines.
The watcher couldn't correlate them. This change wires:

- bot._emit_subagent_failure(subagent, error, attempt=...)
- bot._build_market_research / _build_symbol_research /
  _invoke_news_synthesiser all emit on failure
- supervisor.rules.subagent_failure_streak fires when ≥3 such events
  appear within 30min
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.json_log import JsonEventLogger
from supervisor.rules import subagent_failure_streak
from supervisor.watcher import WatcherConfig, run_one_tick


# --- watcher rule unit tests ------------------------------------------


def _sf(provider: str = "openai", subagent: str = "market_research",
        *, age_minutes: float = 0.0,
        error_class: str = "RuntimeError") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "event": "subagent_failure",
        "timestamp": ts.isoformat(),
        "data": {"provider": provider, "subagent": subagent,
                 "error_class": error_class, "error_message": "boom"},
    }


def test_streak_fires_at_threshold():
    events = [_sf() for _ in range(3)]
    ev = subagent_failure_streak(recent_events=events, threshold=3)
    assert ev is not None
    assert ev.rule == "subagent_failure_streak"
    assert ev.action == "alert_only"
    assert ev.detail["count"] == 3


def test_streak_silent_below_threshold():
    events = [_sf() for _ in range(2)]
    ev = subagent_failure_streak(recent_events=events, threshold=3)
    assert ev is None


def test_streak_only_counts_recent_events():
    events = [_sf(age_minutes=60) for _ in range(5)]
    ev = subagent_failure_streak(recent_events=events, threshold=3,
                                 window_minutes=30.0)
    assert ev is None


def test_streak_aggregates_providers_and_subagents_into_detail():
    events = [
        _sf(provider="openai", subagent="market_research"),
        _sf(provider="openrouter", subagent="symbol_research"),
        _sf(provider="azure_openai", subagent="news_synthesiser"),
    ]
    ev = subagent_failure_streak(recent_events=events, threshold=3)
    assert ev is not None
    assert sorted(ev.detail["providers"]) == ["azure_openai", "openai", "openrouter"]
    assert sorted(ev.detail["subagents"]) == ["market_research", "news_synthesiser", "symbol_research"]


def test_streak_window_is_minutes_not_hours():
    """The default window is 30 *minutes*, distinct from the
    risk_gate_degraded streak which uses *hours*. Confirm the cutoff
    is interpreted in minutes."""
    events = [_sf(age_minutes=20) for _ in range(3)]
    ev = subagent_failure_streak(recent_events=events, threshold=3,
                                 window_minutes=30.0)
    assert ev is not None
    events = [_sf(age_minutes=20) for _ in range(3)]
    ev = subagent_failure_streak(recent_events=events, threshold=3,
                                 window_minutes=15.0)
    assert ev is None


def test_streak_ignores_non_subagent_failure_events():
    events = [
        {"event": "data_quality", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": {}}
        for _ in range(5)
    ]
    ev = subagent_failure_streak(recent_events=events, threshold=3)
    assert ev is None


# --- bot _emit_subagent_failure helper --------------------------------


def _make_bot(tmp_path: Path) -> TradingBot:
    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill",
        openai_api_key="test",
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot._current_cycle_id = "c_abc12345"
    return bot


def _read_events(path: Path) -> list[dict]:
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


def test_emit_subagent_failure_writes_event(tmp_path):
    bot = _make_bot(tmp_path)
    bot._emit_subagent_failure("market_research", RuntimeError("kaput"))

    events = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "subagent_failure"]
    assert len(events) == 1
    data = events[0]["data"]
    assert data["subagent"] == "market_research"
    assert data["error_class"] == "RuntimeError"
    assert data["error_message"] == "kaput"
    assert data["provider"] == bot.settings.llm_provider
    assert events[0]["cycle_id"] == "c_abc12345"


def test_emit_subagent_failure_includes_attempt_number(tmp_path):
    bot = _make_bot(tmp_path)
    bot._emit_subagent_failure("news_synthesiser", ValueError("bad json"), attempt=2)

    events = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "subagent_failure"]
    assert events[0]["data"]["attempt"] == 2


def test_emit_subagent_failure_truncates_long_message(tmp_path):
    bot = _make_bot(tmp_path)
    bot._emit_subagent_failure("symbol_research", RuntimeError("x" * 1000))

    events = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "subagent_failure"]
    assert len(events[0]["data"]["error_message"]) <= 500


def test_emit_subagent_failure_never_raises(tmp_path):
    """Even with a broken json_log, the helper must not raise."""
    bot = _make_bot(tmp_path)
    bot.json_log = MagicMock()
    bot.json_log.log.side_effect = RuntimeError("disk full")
    # Must not raise
    bot._emit_subagent_failure("market_research", RuntimeError("x"))


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
        log_silence_minutes=600.0,
        max_log_mb=10_000.0,
    )


def test_watcher_journals_streak_alert(tmp_path):
    cfg = _baseline_cfg(tmp_path)
    events = [_sf() for _ in range(3)]
    with cfg.events_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    text = cfg.journal_file.read_text(encoding="utf-8")
    assert "subagent_failure_streak" in text
