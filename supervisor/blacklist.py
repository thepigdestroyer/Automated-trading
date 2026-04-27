from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"symbols": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"symbols": {}}
    if "symbols" not in data:
        data["symbols"] = {}
    return data


def _save_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def add_symbol(path: Path, symbol: str, *, reason: str, ttl_hours: float = 24.0) -> None:
    data = _load(path)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    data["symbols"][symbol.upper()] = {
        "added": now.isoformat(),
        "reason": reason,
        "expires": expires.isoformat(),
    }
    _save_atomic(path, data)


def read_active(path: Path) -> set[str]:
    """Return the set of currently-active (non-expired) blacklisted symbols."""
    data = _load(path)
    now = datetime.now(timezone.utc).isoformat()
    return {sym for sym, v in data.get("symbols", {}).items() if v.get("expires", "") > now}


def remove_expired(path: Path) -> int:
    """Drop expired entries. Returns number removed."""
    data = _load(path)
    now = datetime.now(timezone.utc).isoformat()
    before = len(data["symbols"])
    data["symbols"] = {s: v for s, v in data["symbols"].items() if v.get("expires", "") > now}
    removed = before - len(data["symbols"])
    if removed:
        _save_atomic(path, data)
    return removed


def clear_symbol(path: Path, symbol: str) -> None:
    data = _load(path)
    data["symbols"].pop(symbol.upper(), None)
    _save_atomic(path, data)
