from __future__ import annotations

import gzip
import re
import shutil
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from supervisor import blacklist as bl_mod


def touch_kill_switch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def blacklist_symbol_action(path: Path, *, symbol: str, reason: str, ttl_hours: float = 24.0) -> None:
    bl_mod.add_symbol(path, symbol, reason=reason, ttl_hours=ttl_hours)


def rotate_log(log_file: Path) -> Path:
    """Compress current log to bot.log.YYYYMMDD-HHMMSS.gz and truncate the original."""
    if not log_file.exists():
        raise FileNotFoundError(log_file)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = log_file.with_name(f"{log_file.name}.{stamp}.gz")
    with log_file.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    log_file.write_text("", encoding="utf-8")
    return archive


_CYCLE_LOG_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")


def rotate_decision_logs(
    logs_dir: Path,
    *,
    gzip_after_days: float = 3.0,
    delete_after_days: float = 30.0,
    archive_subdir: str = "archive",
    now: datetime | None = None,
) -> dict[str, int]:
    """Per-cycle decision-log retention (review 2026-04-29-adhoc-1 #8).

    The bot writes a per-cycle JSON file to ``logs/YYYYMMDDTHHMMSSZ.json``.
    On 2026-04-29 the directory grew to 67 files (~5MB) in 24h; at
    that cadence it will hit ~1GB in 5 days.

    Policy:
      - Keep raw JSON for the most recent ``gzip_after_days`` days.
      - Anything older is gzipped into ``logs/archive/YYYY-MM/``.
      - Anything (gzipped or raw) older than ``delete_after_days`` is
        deleted.

    Filenames must match ``YYYYMMDDTHHMMSSZ.json`` so that legitimate
    files written outside the bot are not accidentally rotated.

    Returns a small counter dict for journalling: ``{"gzipped": N,
    "deleted": M, "scanned": K}``.
    """
    if not logs_dir.exists() or not logs_dir.is_dir():
        return {"gzipped": 0, "deleted": 0, "scanned": 0}

    now = now or datetime.now(timezone.utc)
    gzip_cutoff = now - timedelta(days=max(0.0, float(gzip_after_days)))
    delete_cutoff = now - timedelta(days=max(0.0, float(delete_after_days)))

    archive_root = logs_dir / archive_subdir
    counters = {"gzipped": 0, "deleted": 0, "scanned": 0}

    # Raw JSON files in logs_dir (top level).
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        if not _CYCLE_LOG_RE.match(path.name):
            continue
        counters["scanned"] += 1
        try:
            stamp = datetime.strptime(path.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp < delete_cutoff:
            try:
                path.unlink()
                counters["deleted"] += 1
            except OSError:
                pass
            continue
        if stamp < gzip_cutoff:
            year_month = stamp.strftime("%Y-%m")
            target_dir = archive_root / year_month
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{path.name}.gz"
            try:
                with path.open("rb") as src, gzip.open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path.unlink()
                counters["gzipped"] += 1
            except OSError:
                pass

    # Already-gzipped archive files older than the delete cutoff.
    if archive_root.exists():
        for sub in archive_root.iterdir():
            if not sub.is_dir():
                continue
            for gz in sub.glob("*.json.gz"):
                stem = gz.name[:-len(".json.gz")]
                try:
                    stamp = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                counters["scanned"] += 1
                if stamp < delete_cutoff:
                    try:
                        gz.unlink()
                        counters["deleted"] += 1
                    except OSError:
                        pass

    return counters


class RestartManager:
    """Caps bot-restart attempts to N per hour to avoid flapping."""

    def __init__(self, *, max_per_hour: int = 3, spawn: Callable[[], int]):
        self.max_per_hour = max_per_hour
        self.spawn = spawn
        self._attempts: deque[float] = deque()

    def attempt_restart(self) -> int | None:
        """Call spawn() if under the per-hour cap. Returns spawned PID or None if capped."""
        now = time.monotonic()
        cutoff = now - 3600.0
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
        if len(self._attempts) >= self.max_per_hour:
            return None
        self._attempts.append(now)
        return self.spawn()
