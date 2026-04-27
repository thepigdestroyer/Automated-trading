from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class WatcherState:
    last_kill_switch_at: str | None = None
    last_alert_at: str | None = None
    restart_attempts_last_hour: int = 0


def load_state(path: Path) -> WatcherState:
    if not path.exists():
        return WatcherState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return WatcherState()
    return WatcherState(**data)


def save_state(path: Path, state: WatcherState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)
