from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VALID_TYPES = {"cycle", "trade_open", "trade_close", "rule_trigger", "remediation", "daily_rollup"}


def append_event(path: Path, event: dict[str, Any]) -> None:
    """Append a single event to the JSONL journal. Adds ts if missing."""
    if event.get("type") not in VALID_TYPES:
        raise ValueError(f"invalid event type: {event.get('type')!r}")
    record = dict(event)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_recent(path: Path, *, since: str | None = None) -> list[dict[str, Any]]:
    """Read events from the journal, optionally filtered to those at or after `since` (ISO 8601)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None and obj.get("ts", "") < since:
                continue
            out.append(obj)
    return out


def iter_events(path: Path) -> Iterable[dict[str, Any]]:
    """Lazy iterator over all events. Used by rollups."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
