from __future__ import annotations

import gzip
import shutil
import time
from collections import deque
from datetime import datetime, timezone
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
