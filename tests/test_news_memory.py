from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_bot.models import Headline
from trading_bot.news_memory import NewsMemory


def _h(title: str, url: str, published: datetime) -> Headline:
    return Headline(title=title, source="Reuters", url=url, published_at=published.isoformat())


def test_new_headline_is_new_then_known():
    now = datetime(2026, 4, 23, 12, tzinfo=timezone.utc)
    memory = NewsMemory(state={"items": [], "last_purged_at": None}, retention_hours=48, max_items=500)
    h = _h("Iran tensions", "https://x/1", now - timedelta(hours=1))
    tagged = memory.classify([h], cycle_ts=now.isoformat())
    assert tagged[0].novelty == "new"
    tagged2 = memory.classify([h], cycle_ts=now.isoformat())
    assert tagged2[0].novelty == "known"


def test_updated_when_title_changes_for_same_url():
    now = datetime(2026, 4, 23, 12, tzinfo=timezone.utc)
    memory = NewsMemory(state={"items": [], "last_purged_at": None}, retention_hours=48, max_items=500)
    h1 = _h("Breaking: Iran attack", "https://x/2", now - timedelta(hours=1))
    memory.classify([h1], cycle_ts=now.isoformat())
    h2 = _h("Updated: Iran attack confirmed", "https://x/2", now)
    tagged = memory.classify([h2], cycle_ts=now.isoformat())
    assert tagged[0].novelty == "updated"


def test_purge_drops_old_items():
    now = datetime(2026, 4, 23, 12, tzinfo=timezone.utc)
    memory = NewsMemory(state={"items": [], "last_purged_at": None}, retention_hours=6, max_items=500)
    old = _h("Old news", "https://x/3", now - timedelta(hours=48))
    memory.classify([old], cycle_ts=now.isoformat())
    memory.purge(now=now)
    assert len(memory.state["items"]) == 0
