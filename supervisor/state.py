from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class WatcherState:
    last_kill_switch_at: str | None = None
    last_alert_at: str | None = None
    restart_attempts_last_hour: int = 0
    last_restart_at: str | None = None  # ISO-8601 UTC of most recent restart_bot remediation
    # Maps "{rule}::{key}" → ISO-8601 UTC timestamp of the most recent
    # emission. Used by the watcher to dedupe noisy rules (see fix #6).
    rule_dedup: dict[str, str] = field(default_factory=dict)
    # ISO-8601 UTC of the last successful decision-log rotation
    # (review 2026-04-29-adhoc-1 #8). Used to throttle rotation to
    # at most once per ``decision_log_rotation_interval_hours``.
    last_decision_log_rotation_at: str | None = None


def load_state(path: Path) -> WatcherState:
    if not path.exists():
        return WatcherState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return WatcherState()
    # Be forgiving about unknown keys from older state files.
    allowed = {f for f in WatcherState.__dataclass_fields__}
    filtered = {k: v for k, v in data.items() if k in allowed}
    # Defensive normalisation: rule_dedup must be dict[str, str].
    rd = filtered.get("rule_dedup")
    if not isinstance(rd, dict):
        filtered["rule_dedup"] = {}
    else:
        filtered["rule_dedup"] = {str(k): str(v) for k, v in rd.items()
                                  if isinstance(v, str)}
    return WatcherState(**filtered)


def save_state(path: Path, state: WatcherState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)
