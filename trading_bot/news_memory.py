from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from trading_bot.models import Headline


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class NewsMemory:
    def __init__(self, *, state: dict[str, Any], retention_hours: int, max_items: int):
        self.state = state
        self.state.setdefault("items", [])
        self.state.setdefault("last_purged_at", None)
        self.retention_hours = retention_hours
        self.max_items = max_items

    def classify(self, headlines: list[Headline], *, cycle_ts: str) -> list[Headline]:
        existing: dict[str, dict[str, Any]] = {item["url_hash"]: item for item in self.state["items"]}
        tagged: list[Headline] = []
        for headline in headlines:
            key = _url_hash(headline.url)
            prev = existing.get(key)
            if prev is None:
                novelty = "new"
                existing[key] = {
                    "url_hash": key,
                    "title": headline.title,
                    "source_domain": headline.source_domain,
                    "published_at": headline.published_at,
                    "first_seen_cycle_ts": cycle_ts,
                }
            elif prev.get("title") != headline.title:
                novelty = "updated"
                prev["title"] = headline.title
                prev["published_at"] = headline.published_at or prev.get("published_at")
            else:
                novelty = "known"
            tagged.append(headline.model_copy(update={"novelty": novelty}))
        self.state["items"] = list(existing.values())
        self._enforce_cap()
        return tagged

    def purge(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.retention_hours)
        kept = []
        for item in self.state["items"]:
            ts = item.get("published_at") or item.get("first_seen_cycle_ts")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(item)
        self.state["items"] = kept
        self.state["last_purged_at"] = now.isoformat()

    def _enforce_cap(self) -> None:
        items = self.state["items"]
        if len(items) <= self.max_items:
            return
        items.sort(key=lambda x: x.get("first_seen_cycle_ts") or "")
        self.state["items"] = items[-self.max_items :]
