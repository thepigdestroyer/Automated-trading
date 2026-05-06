"""Memory-pressure soft-restart (review 2026-05-05-adhoc-1 #6).

When RSS crosses ``memory_soft_restart_threshold × memory_max_bytes``,
the bot must:
  1. Emit a structured event ``memory_pressure_soft_restart_requested``.
  2. Set ``_soft_restart_requested`` so the cycle loop exits cleanly at
     the next boundary (not mid-cycle, not mid-LLM-call).
  3. Rate-limit at ``memory_soft_restart_min_interval_seconds`` so a
     persistent pressure condition doesn't loop-restart.

systemd's ``Restart=on-failure`` (already configured on the OCI VM)
brings the process back up. The 6h ``RuntimeMaxSec`` provides an
additional ceiling.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill_switch",
        openai_api_key="test",
    )
    base.update(overrides)
    return Settings(**base)


def _make_bot(tmp_path: Path, **settings_overrides) -> TradingBot:
    settings = _settings(tmp_path, **settings_overrides)
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    from trading_bot.json_log import JsonEventLogger
    bot.json_log = JsonEventLogger(path=settings.json_log_path)
    bot._cycle_counter = 0
    bot._soft_restart_requested = False
    bot._last_soft_restart_at = None
    bot._current_cycle_id = "c_test"
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


# --- _get_rss_bytes ---


def test_get_rss_bytes_reads_proc_self_status():
    """On Linux the helper should return a positive int."""
    rss = TradingBot._get_rss_bytes()
    # Linux test runner: RSS is real and positive
    assert rss is not None
    assert rss > 0


def test_get_rss_bytes_returns_none_when_proc_unreadable():
    """Simulate /proc/self/status missing (non-Linux container)."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        rss = TradingBot._get_rss_bytes()
    assert rss is None


# --- _maybe_request_memory_soft_restart ---


def test_soft_restart_fires_above_threshold(tmp_path, caplog):
    """RSS at 90% of cap (above 85% threshold) must request a restart."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
    )
    # 360 MB = 90% of 400 MB
    with patch.object(TradingBot, "_get_rss_bytes", return_value=360 * 1024 * 1024):
        with caplog.at_level(logging.WARNING, logger="trading_bot.bot"):
            bot._maybe_request_memory_soft_restart()

    assert bot._soft_restart_requested is True
    assert bot._last_soft_restart_at is not None
    # Event was emitted
    events = _read_events(bot.settings.json_log_path)
    pressure = [
        e for e in events
        if e.get("event") == "memory_pressure_soft_restart_requested"
    ]
    assert len(pressure) == 1
    data = pressure[0]["data"]
    assert data["ratio"] >= 0.85
    assert data["threshold"] == 0.85
    # WARN log emitted
    assert any(
        "Memory pressure soft-restart requested" in rec.getMessage()
        for rec in caplog.records
    )


def test_soft_restart_silent_below_threshold(tmp_path):
    """RSS at 50% of cap must NOT request a restart."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
    )
    with patch.object(TradingBot, "_get_rss_bytes", return_value=200 * 1024 * 1024):
        bot._maybe_request_memory_soft_restart()

    assert bot._soft_restart_requested is False
    assert bot._last_soft_restart_at is None


def test_soft_restart_disabled_when_threshold_zero(tmp_path):
    """memory_soft_restart_threshold=0.0 disables the check entirely."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.0,  # disabled
        memory_max_bytes=400 * 1024 * 1024,
    )
    with patch.object(TradingBot, "_get_rss_bytes", return_value=395 * 1024 * 1024):
        bot._maybe_request_memory_soft_restart()
    assert bot._soft_restart_requested is False


def test_soft_restart_silent_when_proc_unavailable(tmp_path):
    """If /proc/self/status can't be read (non-Linux), be silent."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
    )
    with patch.object(TradingBot, "_get_rss_bytes", return_value=None):
        bot._maybe_request_memory_soft_restart()
    assert bot._soft_restart_requested is False


def test_soft_restart_rate_limited_within_interval(tmp_path, caplog):
    """A second trigger within ``memory_soft_restart_min_interval_seconds``
    must NOT escalate but must log a WARN so operators see persistent
    pressure."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
        memory_soft_restart_min_interval_seconds=3600,  # 1h cooldown
    )
    # First trigger - normal
    with patch.object(TradingBot, "_get_rss_bytes", return_value=360 * 1024 * 1024):
        bot._maybe_request_memory_soft_restart()
    assert bot._soft_restart_requested is True
    first_request = bot._last_soft_restart_at

    # Reset _soft_restart_requested as if the bot had restarted, but
    # keep _last_soft_restart_at so the rate-limiter sees the recent
    # request (this simulates the re-acquired state on a fast bounce).
    bot._soft_restart_requested = False

    # Second trigger - within cooldown (5 min later)
    bot._last_soft_restart_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with patch.object(TradingBot, "_get_rss_bytes", return_value=380 * 1024 * 1024):
        with caplog.at_level(logging.WARNING, logger="trading_bot.bot"):
            bot._maybe_request_memory_soft_restart()

    # Rate-limiter suppressed the second trigger
    assert bot._soft_restart_requested is False
    # But a WARN line was logged for visibility
    assert any(
        "Memory pressure persisted" in rec.getMessage()
        for rec in caplog.records
    )


def test_soft_restart_allowed_after_cooldown_elapsed(tmp_path):
    """After cooldown elapses, the next trigger should fire normally."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
        memory_soft_restart_min_interval_seconds=3600,
    )
    # Set last request to 2h ago (well past 1h cooldown)
    bot._last_soft_restart_at = datetime.now(timezone.utc) - timedelta(hours=2)

    with patch.object(TradingBot, "_get_rss_bytes", return_value=360 * 1024 * 1024):
        bot._maybe_request_memory_soft_restart()

    assert bot._soft_restart_requested is True


def test_soft_restart_threshold_boundary(tmp_path):
    """RSS exactly AT threshold (85.0%) should trigger; just below should not."""
    bot = _make_bot(
        tmp_path,
        memory_soft_restart_threshold=0.85,
        memory_max_bytes=400 * 1024 * 1024,
    )
    # Just below threshold: 84.9% of 400MB = 339.6 MB
    with patch.object(TradingBot, "_get_rss_bytes", return_value=int(0.849 * 400 * 1024 * 1024)):
        bot._maybe_request_memory_soft_restart()
    assert bot._soft_restart_requested is False

    # At threshold: exactly 85%
    with patch.object(TradingBot, "_get_rss_bytes", return_value=int(0.85 * 400 * 1024 * 1024)):
        bot._maybe_request_memory_soft_restart()
    assert bot._soft_restart_requested is True
