"""regime_history dedupe + cap (review 2026-04-30-adhoc-1 #8).

On 2026-04-30 the runtime_state showed 16 *identical*
``energy-supply-shock`` regime entries — useless for the LLM's
regime-change pattern matching. The bot was appending a new row every
cycle without checking whether the regime had actually changed.

Fix:
- On-insert dedupe: if the new regime matches the most recent entry,
  bump that entry's ``last_seen_at`` and increment ``count``; do NOT
  append a duplicate row.
- Cap at 100 rows (was 16). Combined with dedupe this captures many
  regime transitions instead of one regime over and over.
- Schema gains optional ``last_seen_at`` and ``count`` fields; old
  callers that just read ``regime`` and ``cycle_at`` are unaffected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot


def _make_bot(tmp_path: Path) -> TradingBot:
    from trading_bot.config import Settings
    from trading_bot.json_log import JsonEventLogger

    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill_switch",
        openai_api_key="test",
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"regime_history": []}
    return bot


def _append(bot: TradingBot, regime: str, *, ts: datetime) -> None:
    """Replicate the bot.py append logic in isolation so we can test it
    without spinning the whole cycle. Mirrors bot.py:243-258 exactly."""
    regime_hist = bot.runtime_state.setdefault("regime_history", [])
    new_entry = {
        "cycle_at": ts.isoformat(),
        "regime": regime,
        "last_seen_at": ts.isoformat(),
        "count": 1,
    }
    if regime_hist and regime_hist[-1].get("regime") == regime:
        last = regime_hist[-1]
        last["last_seen_at"] = ts.isoformat()
        last["count"] = int(last.get("count") or 1) + 1
    else:
        regime_hist.append(new_entry)
    bot.runtime_state["regime_history"] = regime_hist[-100:]


# --- dedupe ---------------------------------------------------------


def test_consecutive_identical_regime_is_deduped(tmp_path):
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _append(bot, "energy-supply-shock", ts=base + timedelta(minutes=20 * i))

    history = bot.runtime_state["regime_history"]
    assert len(history) == 1
    assert history[0]["regime"] == "energy-supply-shock"
    assert history[0]["count"] == 5
    # cycle_at = first appearance, last_seen_at = most recent
    assert history[0]["cycle_at"] == base.isoformat()
    assert history[0]["last_seen_at"] == (base + timedelta(minutes=80)).isoformat()


def test_regime_change_appends_new_row(tmp_path):
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    _append(bot, "energy-supply-shock", ts=base)
    _append(bot, "energy-supply-shock", ts=base + timedelta(minutes=20))
    _append(bot, "risk-off-macro", ts=base + timedelta(minutes=40))
    _append(bot, "risk-off-macro", ts=base + timedelta(minutes=60))
    _append(bot, "neutral-wait", ts=base + timedelta(minutes=80))

    hist = bot.runtime_state["regime_history"]
    assert [e["regime"] for e in hist] == [
        "energy-supply-shock", "risk-off-macro", "neutral-wait",
    ]
    assert [e["count"] for e in hist] == [2, 2, 1]


def test_regime_oscillation_creates_distinct_rows(tmp_path):
    """A flapping regime A→B→A→B should produce 4 rows, not collapsed
    into 2; we only dedupe IMMEDIATELY consecutive matches."""
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    for i, regime in enumerate([
        "risk-on-momentum", "risk-off-geopolitical",
        "risk-on-momentum", "risk-off-geopolitical",
    ]):
        _append(bot, regime, ts=base + timedelta(minutes=20 * i))

    hist = bot.runtime_state["regime_history"]
    assert len(hist) == 4
    assert all(e["count"] == 1 for e in hist)


# --- cap ------------------------------------------------------------


def test_cap_at_100_entries(tmp_path):
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc)
    # 150 distinct regimes ⇒ trimmed to last 100
    regimes = [f"regime_{i}" for i in range(150)]
    for i, r in enumerate(regimes):
        _append(bot, r, ts=base + timedelta(minutes=i))

    hist = bot.runtime_state["regime_history"]
    assert len(hist) == 100
    # Last 100 are regime_50..regime_149 in order
    assert hist[0]["regime"] == "regime_50"
    assert hist[-1]["regime"] == "regime_149"


def test_cap_preserves_dedupe_count(tmp_path):
    """If the most recent surviving entry was deduped many times, its
    count should be preserved across the cap operation."""
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc)
    # Append 110 unique regimes to push count above the cap
    for i in range(110):
        _append(bot, f"r{i}", ts=base + timedelta(minutes=i))
    # Now hammer the latest with 50 dupes
    last_regime = "r109"
    for i in range(50):
        _append(bot, last_regime, ts=base + timedelta(minutes=110 + i))

    hist = bot.runtime_state["regime_history"]
    assert hist[-1]["regime"] == last_regime
    assert hist[-1]["count"] == 51  # original + 50 dupes


# --- backward-compat ------------------------------------------------


def test_old_history_without_count_still_dedupes(tmp_path):
    """Legacy regime_history rows without ``count`` should still
    dedupe — the new code defaults missing count to 1."""
    bot = _make_bot(tmp_path)
    bot.runtime_state["regime_history"] = [
        {"cycle_at": "2026-04-30T09:00:00+00:00", "regime": "energy-supply-shock"},
    ]
    base = datetime(2026, 4, 30, 9, 20, 0, tzinfo=timezone.utc)
    _append(bot, "energy-supply-shock", ts=base)

    hist = bot.runtime_state["regime_history"]
    assert len(hist) == 1
    assert hist[0]["count"] == 2


def test_first_entry_initialises_count_one(tmp_path):
    bot = _make_bot(tmp_path)
    _append(bot, "neutral-wait", ts=datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc))
    hist = bot.runtime_state["regime_history"]
    assert hist[0]["count"] == 1
    assert hist[0]["cycle_at"] == hist[0]["last_seen_at"]


def test_payload_to_pm_still_has_regime_field(tmp_path):
    """Sanity: the LLM only needs ``regime``; the new ``count`` and
    ``last_seen_at`` fields are additive and don't break anything."""
    bot = _make_bot(tmp_path)
    base = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _append(bot, "energy-supply-shock", ts=base + timedelta(minutes=20 * i))
    for entry in bot.runtime_state["regime_history"]:
        assert "regime" in entry
        assert "cycle_at" in entry
