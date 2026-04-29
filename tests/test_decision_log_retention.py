"""Decision-log retention policy (review 2026-04-29-adhoc-1 #8).

The bot writes a per-cycle JSON to ``logs/YYYYMMDDTHHMMSSZ.json``. On
2026-04-29 the directory grew to 67 files (~5MB) in 24h and would have
hit ~1GB in 5 days unrotated.

Policy implemented in ``supervisor.remediations.rotate_decision_logs``:
- Raw JSON kept for the most recent N days (default 3).
- Older files gzipped into ``logs/archive/YYYY-MM/<name>.gz``.
- Anything (raw or gzipped) older than M days (default 30) is deleted.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from supervisor import remediations as rem
from supervisor.watcher import WatcherConfig, run_one_tick


def _make_log(logs_dir: Path, *, age_days: float, content: str = '{"x":1}') -> Path:
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    name = stamp.strftime("%Y%m%dT%H%M%SZ.json")
    path = logs_dir / name
    path.write_text(content, encoding="utf-8")
    # Set mtime too in case anything relies on it
    ts = stamp.timestamp()
    os.utime(path, (ts, ts))
    return path


def test_rotate_no_logs_dir_returns_empty(tmp_path):
    counters = rem.rotate_decision_logs(tmp_path / "nope")
    assert counters == {"gzipped": 0, "deleted": 0, "scanned": 0}


def test_recent_files_left_alone(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    fresh = _make_log(logs, age_days=0.5)
    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)
    assert fresh.exists()
    assert counters["gzipped"] == 0


def test_old_files_gzipped_to_archive(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old = _make_log(logs, age_days=5.0, content='{"hello":"world"}')

    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)

    assert not old.exists(), "raw file should have been removed after gzip"
    assert counters["gzipped"] == 1

    # Archive should contain the .gz file under YYYY-MM/
    archive_root = logs / "archive"
    assert archive_root.exists()
    gzipped = list(archive_root.glob("**/*.gz"))
    assert len(gzipped) == 1
    # And the content round-trips
    with gzip.open(gzipped[0], "rb") as f:
        assert json.loads(f.read()) == {"hello": "world"}


def test_very_old_raw_files_deleted_not_gzipped(tmp_path):
    """A raw JSON file older than delete_after_days should be deleted
    outright, not gzipped (no point archiving something we'll then
    delete on the next pass)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    ancient = _make_log(logs, age_days=45.0)

    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)

    assert not ancient.exists()
    assert counters["deleted"] == 1
    assert counters["gzipped"] == 0
    archive_root = logs / "archive"
    assert not archive_root.exists() or list(archive_root.glob("**/*.gz")) == []


def test_old_gzipped_archives_deleted(tmp_path):
    """Anything older than delete_after_days must be deleted, including
    files already gzipped into the archive."""
    logs = tmp_path / "logs"
    archive = logs / "archive" / "2026-01"
    archive.mkdir(parents=True)
    # A 60-day-old gzipped file
    old_stamp = datetime.now(timezone.utc) - timedelta(days=60)
    name = old_stamp.strftime("%Y%m%dT%H%M%SZ.json.gz")
    archive_file = archive / name
    archive_file.write_bytes(b"x")

    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)

    assert not archive_file.exists()
    assert counters["deleted"] >= 1


def test_unrelated_files_in_logs_dir_ignored(tmp_path):
    """Only files matching YYYYMMDDTHHMMSSZ.json are considered cycle logs.
    Other files (e.g. events.jsonl) must be left alone."""
    logs = tmp_path / "logs"
    logs.mkdir()
    events = logs / "events.jsonl"
    events.write_text("noop\n")
    # Also put some other file with the right naming: should stay
    fresh = _make_log(logs, age_days=0.5)
    # Old file that should rotate
    old = _make_log(logs, age_days=10.0)

    rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)

    assert events.exists(), "events.jsonl must not be rotated"
    assert fresh.exists()
    assert not old.exists()


def test_malformed_filenames_skipped(tmp_path):
    """A file in logs/ whose name doesn't match the pattern is skipped."""
    logs = tmp_path / "logs"
    logs.mkdir()
    junk = logs / "not-a-cycle-log.json"
    junk.write_text("{}", encoding="utf-8")
    ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(junk, (ts, ts))

    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)
    assert junk.exists(), "non-matching files are not touched"
    assert counters["scanned"] == 0


def test_multiple_files_rotated_in_one_pass(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(5):
        _make_log(logs, age_days=5.0 + i * 0.1)
    for i in range(3):
        _make_log(logs, age_days=0.5 + i * 0.1)

    counters = rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)
    assert counters["gzipped"] == 5

    # 3 fresh remain at top level
    raw_remaining = [p for p in logs.iterdir() if p.is_file() and p.suffix == ".json"]
    assert len(raw_remaining) == 3


def test_archive_subdir_format_year_month(tmp_path):
    """The archive subdir is YYYY-MM derived from the file's timestamp."""
    logs = tmp_path / "logs"
    logs.mkdir()
    old = _make_log(logs, age_days=10.0)
    age_dt = datetime.strptime(old.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    expected_subdir = age_dt.strftime("%Y-%m")

    rem.rotate_decision_logs(logs, gzip_after_days=3.0, delete_after_days=30.0)

    target = logs / "archive" / expected_subdir
    assert target.exists()
    assert any(p.suffix == ".gz" for p in target.iterdir())


# --- watcher wiring -------------------------------------------------


def _baseline_cfg(tmp_path: Path, *, decision_log_dir: Path | None) -> WatcherConfig:
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
        decision_log_dir=decision_log_dir,
    )


def test_watcher_invokes_rotation_when_dir_configured(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _make_log(logs, age_days=10.0)
    cfg = _baseline_cfg(tmp_path, decision_log_dir=logs)

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    archive = logs / "archive"
    assert archive.exists()
    assert any(archive.glob("**/*.gz"))


def test_watcher_does_not_rotate_when_no_dir(tmp_path):
    cfg = _baseline_cfg(tmp_path, decision_log_dir=None)
    # Should not crash even with no dir configured
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)


def test_watcher_throttles_rotation_to_once_per_interval(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _make_log(logs, age_days=10.0)
    cfg = _baseline_cfg(tmp_path, decision_log_dir=logs)
    cfg.decision_log_rotation_interval_hours = 24.0

    # First tick rotates
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)
    archive_files_after_first = list((logs / "archive").glob("**/*.gz"))

    # Add another old file
    _make_log(logs, age_days=5.0)

    # Second tick — within throttle window — must NOT rotate again
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)
    archive_files_after_second = list((logs / "archive").glob("**/*.gz"))

    assert len(archive_files_after_first) == len(archive_files_after_second), \
        "rotation should be throttled to once per interval"


def test_watcher_journals_rotation_remediation(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _make_log(logs, age_days=10.0)
    cfg = _baseline_cfg(tmp_path, decision_log_dir=logs)

    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)

    journal_text = cfg.journal_file.read_text(encoding="utf-8") if cfg.journal_file.exists() else ""
    assert "rotate_decision_logs" in journal_text


def test_watcher_default_retention_is_3_and_30_days(tmp_path):
    cfg = WatcherConfig(
        bot_pid_file=tmp_path / "bot.pid",
        bot_log=tmp_path / "bot.log",
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        bot_command=["/bin/true"],
    )
    assert cfg.decision_log_gzip_after_days == 3.0
    assert cfg.decision_log_delete_after_days == 30.0
