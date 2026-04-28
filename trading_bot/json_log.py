from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class JsonEventLogger:
    def __init__(self, *, path: Path | None):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, data: Any, *, cycle_id: str | None = None) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "cycle_id": cycle_id,
            "data": data,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except Exception as exc:
            LOGGER.warning("Failed to write JSON event: %s", exc)


class AlertSink:
    def __init__(self, *, directory: Path, webhook_url: str | None):
        self.directory = directory
        self.webhook_url = webhook_url
        directory.mkdir(parents=True, exist_ok=True)

    def alert(self, event: str, data: dict[str, Any]) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.directory / f"{day}.log"
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "data": data}
        )
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:
            LOGGER.warning("Failed to write alert: %s", exc)
        if self.webhook_url:
            try:
                import requests
                requests.post(self.webhook_url, json={"event": event, "data": data}, timeout=5)
            except Exception as exc:
                LOGGER.warning("Alert webhook failed: %s", exc)
