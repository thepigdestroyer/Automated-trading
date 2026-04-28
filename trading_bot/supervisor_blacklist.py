from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def read_active_blacklist(path: Path) -> set[str]:
    """Return the set of currently-blacklisted symbols.

    Reads supervisor/blacklist.json and filters entries whose `expires` is in the past.
    Any read or parse failure returns an empty set — the bot must continue trading
    even if the supervisor is down.
    """
    try:
        if not path.exists():
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    now = datetime.now(timezone.utc).isoformat()
    return {sym for sym, v in data.get("symbols", {}).items() if v.get("expires", "") > now}
