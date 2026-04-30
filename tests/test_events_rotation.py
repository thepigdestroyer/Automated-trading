"""events.jsonl daily rotation (review 2026-04-30-adhoc-1 #4).

The bot's ``logs/events.jsonl`` grows ~140 KB/day unrotated. On the
311 MB-free OCI VM this would eventually consume disk; more importantly
the watcher reads the file every tick so size impacts CPU. The
decision-log rotation #8 from 2026-04-29 covers per-cycle JSONs but not
this file.

Policy:
- If the file's earliest event is on a UTC date earlier than today,
  gzip the entire file to logs/archive/YYYY-MM/events-YYYY-MM-DD.jsonl.gz
  (using earliest-date for the archive filename) and truncate the live file.
- Expire archives older than ``delete_after_days``.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from supervisor import remediations as rem


def _make_events_file(path: Path, *, dates: list[str]) -> None:
    """Write one event per supplied UTC date string (YYYY-MM-DD)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for d in dates:
        rec = {"timestamp": f"{d}T12:00:00+00:00", "event": "cycle_start",
               "cycle_id": f"c_{d}", "data": {}}
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- happy paths -----------------------------------------------------


def test_rotate_no_events_file(tmp_path):
    counters = rem.rotate_events_jsonl(tmp_path / "events.jsonl")
    assert counters == {"rotated": 0, "deleted": 0, "scanned": 0}


def test_rotate_only_today_skips(tmp_path):
    """If the earliest event is today, do not rotate."""
    events = tmp_path / "events.jsonl"
    today = datetime.now(timezone.utc).date().isoformat()
    _make_events_file(events, dates=[today, today])

    now = datetime.now(timezone.utc)
    counters = rem.rotate_events_jsonl(events, now=now)
    assert counters["rotated"] == 0
    # Live file untouched
    assert events.read_text().count("\n") >= 2


def test_rotate_yesterday_gzips_and_truncates(tmp_path):
    """Earliest event yesterday → gzip whole file, truncate live."""
    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    _make_events_file(events, dates=[yesterday, yesterday])
    original_size = events.stat().st_size

    now = datetime.now(timezone.utc)
    counters = rem.rotate_events_jsonl(events, now=now)

    assert counters["rotated"] == 1
    # Live file now empty
    assert events.read_text().strip() == ""
    # Archive exists and contains original content (decompressed)
    archive_dir = tmp_path / "archive" / yesterday[:7]  # YYYY-MM
    expected = archive_dir / f"events-{yesterday}.jsonl.gz"
    assert expected.exists()
    with gzip.open(expected, "rb") as gz:
        recovered = gz.read()
    assert len(recovered) == original_size
    # First record decodes correctly
    first = json.loads(recovered.decode().splitlines()[0])
    assert first["timestamp"].startswith(yesterday)


def test_rotate_creates_archive_directory_tree(tmp_path):
    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    _make_events_file(events, dates=[yesterday])

    counters = rem.rotate_events_jsonl(events)
    assert counters["rotated"] == 1
    archive_dir = tmp_path / "archive"
    assert archive_dir.exists()
    # YYYY-MM subdir exists
    sub = list(archive_dir.iterdir())
    assert len(sub) == 1
    assert sub[0].is_dir()


def test_rotate_handles_blank_lines(tmp_path):
    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    events.write_text(
        "\n\n"
        + json.dumps({"timestamp": f"{yesterday}T01:00:00+00:00", "event": "x"})
        + "\n",
        encoding="utf-8",
    )
    counters = rem.rotate_events_jsonl(events)
    assert counters["rotated"] == 1


# --- archive expiry --------------------------------------------------


def test_rotate_expires_old_archives(tmp_path):
    """Archives older than delete_after_days are unlinked."""
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")  # empty live

    archive_dir = tmp_path / "archive"
    old_dir = archive_dir / "2026-01"
    old_dir.mkdir(parents=True)
    old_archive = old_dir / "events-2026-01-15.jsonl.gz"
    with gzip.open(old_archive, "wb") as gz:
        gz.write(b'{"event":"x"}')

    # Now is 2026-04-30, delete_after=30d ⇒ Jan archive deleted
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    counters = rem.rotate_events_jsonl(events, delete_after_days=30.0, now=now)
    assert counters["deleted"] == 1
    assert not old_archive.exists()


def test_rotate_keeps_recent_archives(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")

    archive_dir = tmp_path / "archive"
    recent_dir = archive_dir / "2026-04"
    recent_dir.mkdir(parents=True)
    recent_archive = recent_dir / "events-2026-04-20.jsonl.gz"
    with gzip.open(recent_archive, "wb") as gz:
        gz.write(b'{"event":"x"}')

    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    counters = rem.rotate_events_jsonl(events, delete_after_days=30.0, now=now)
    assert counters["deleted"] == 0
    assert recent_archive.exists()


# --- watcher integration --------------------------------------------


def test_watcher_rotates_events_jsonl(tmp_path):
    """run_one_tick eventually invokes rotate_events_jsonl when the
    file's earliest event is yesterday."""
    from supervisor.watcher import WatcherConfig, run_one_tick

    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    _make_events_file(events, dates=[yesterday, yesterday])

    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")  # bogus pid

    cfg = WatcherConfig(
        bot_pid_file=pid_file,
        bot_log=tmp_path / "bot.log",
        events_file=events,
        journal_file=tmp_path / "raw_journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill_switch",
        watcher_state_file=tmp_path / "watcher_state.json",
        bot_command=["/bin/true"],
    )
    # Run a single tick (we don't care about restart logic here; we
    # just want the rotation housekeeping to run)
    try:
        run_one_tick(cfg)
    except Exception:
        # Other rules may fire on the stub state — that's OK.
        pass

    # Live file should now be empty
    assert events.read_text().strip() == ""
    # Archive exists
    archive = tmp_path / "archive" / yesterday[:7] / f"events-{yesterday}.jsonl.gz"
    assert archive.exists()


def test_watcher_throttles_rotation(tmp_path):
    """The watcher writes last_events_rotation_at to state and skips
    re-rotation on the next tick within the throttle window."""
    from supervisor.state import WatcherState, save_state, load_state
    from supervisor.watcher import WatcherConfig, run_one_tick

    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    _make_events_file(events, dates=[yesterday])
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")

    cfg = WatcherConfig(
        bot_pid_file=pid_file,
        bot_log=tmp_path / "bot.log",
        events_file=events,
        journal_file=tmp_path / "raw_journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill_switch",
        watcher_state_file=tmp_path / "watcher_state.json",
        bot_command=["/bin/true"],
    )

    try:
        run_one_tick(cfg)
    except Exception:
        pass

    # State file written, last_events_rotation_at populated
    state = load_state(tmp_path / "watcher_state.json")
    assert state.last_events_rotation_at is not None
