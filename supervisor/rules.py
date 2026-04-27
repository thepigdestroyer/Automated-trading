from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class RuleEvent:
    """A rule that fired, with optional payload describing the trigger."""
    rule: str
    action: str  # "alert_only" | "kill_switch" | "blacklist_symbol" | "restart_bot" | "rotate_log"
    detail: dict[str, Any] = field(default_factory=dict)


def process_dead(*, pid_file: Path, pid_alive: Callable[[int], bool]) -> RuleEvent | None:
    """Return an event if bot.pid is missing or its PID is not running."""
    if not pid_file.exists():
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file missing"})
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file unreadable"})
    if not pid_alive(pid):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid not running", "pid": pid})
    return None
