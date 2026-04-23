from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser

from t212_cfd_bot.config import Settings
from t212_cfd_bot.instruments import NewsTopic, get_instrument
from t212_cfd_bot.models import Headline

LOGGER = logging.getLogger(__name__)

_PROMPT_INJECTION_RE = re.compile(
    r"(?i)\b(ignore|disregard|override|forget)\b[^.!?]*?\b(instruction|prompt|rule|system|previous|prior)\b",
)


def scrub_headline(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _PROMPT_INJECTION_RE.sub("[scrubbed]", text)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return cleaned.strip()


def headline_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class GoogleNewsClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch_symbol_headlines(self, symbol: str) -> list[Headline]:
        instrument = get_instrument(symbol)
        topics = tuple(instrument.news_topics) if instrument.news_topics else (
            NewsTopic(
                label="symbol_base",
                query=instrument.news_query,
                weight=1.0,
                max_age_hours=self.settings.max_news_headline_age_hours,
            ),
        )
        queries = self._expand_topics(topics, scope="symbol")
        return self._fetch_ranked(queries=queries, limit=self.settings.news_per_symbol)

    def fetch_market_headlines(self) -> list[Headline]:
        base = NewsTopic(
            label="market_base",
            query=self.settings.market_news_query,
            weight=1.0,
            max_age_hours=self.settings.max_news_headline_age_hours,
        )
        queries = self._expand_topics((base,), scope="market")
        limit = max(self.settings.news_per_symbol, 6)
        return self._fetch_ranked(queries=queries, limit=limit)

    def fetch_query_headlines(self, label: str, query: str, *, max_age_hours: int, limit: int) -> list[Headline]:
        topic = NewsTopic(label=label, query=query, weight=1.0, max_age_hours=max_age_hours)
        queries = self._expand_topics((topic,), scope="custom")
        return self._fetch_ranked(queries=queries, limit=limit)

    def _expand_topics(self, topics: tuple[NewsTopic, ...], *, scope: str) -> list[tuple[NewsTopic, str, str]]:
        queries: list[tuple[NewsTopic, str, str]] = []
        for topic in topics:
            queries.append((topic, f"{scope}:{topic.label}", topic.query))
            analysis = f"({topic.query}) ({self.settings.news_analysis_terms})"
            queries.append((topic, f"{scope}:{topic.label}_analysis", analysis))
            for domain in self.settings.preferred_news_domains[: self.settings.max_news_source_domains_per_fetch]:
                queries.append((topic, f"{scope}:{topic.label}_source:{domain}", f"({topic.query}) site:{domain}"))
        return queries

    def _fetch_ranked(self, *, queries: list[tuple[NewsTopic, str, str]], limit: int) -> list[Headline]:
        per_query_limit = max(2, self.settings.news_per_query_limit)
        now = datetime.now(timezone.utc)
        combined: list[Headline] = []
        seen_urls: set[str] = set()
        for topic, label, query in queries:
            cutoff = now - timedelta(hours=topic.max_age_hours)
            for headline in self._fetch_one(query=query, limit=per_query_limit, cutoff=cutoff, topic=topic, label=label):
                if headline.url in seen_urls:
                    continue
                seen_urls.add(headline.url)
                combined.append(headline)
        ranked = self._rank(combined)
        return ranked[:limit]

    def _fetch_one(self, *, query: str, limit: int, cutoff: datetime, topic: NewsTopic, label: str) -> list[Headline]:
        scoped_query = f"{query} when:{topic.max_age_hours}h"
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(scoped_query)}&hl=en-US&gl=US&ceid=US:en"
        )
        feed = feedparser.parse(url)
        headlines: list[Headline] = []
        for entry in feed.entries:
            raw_title = str(entry.get("title", "")).strip()
            if not raw_title:
                continue
            published_at = self._parse_timestamp(entry)
            if published_at and published_at < cutoff:
                continue

            source = "Unknown"
            source_obj = entry.get("source")
            if isinstance(source_obj, dict):
                source = str(source_obj.get("title", source)).strip() or source

            link = str(entry.get("link", "")).strip()
            if not link:
                continue

            source_domain = self._extract_domain(link)
            source_priority = self._source_priority(source_domain)
            snippet_raw = str(entry.get("summary", "") or entry.get("description", "")).strip()

            headline = Headline(
                title=scrub_headline(raw_title),
                source=source,
                url=link,
                published_at=published_at.isoformat() if published_at else None,
                source_domain=source_domain,
                source_priority=source_priority,
                query_label=label,
                snippet=scrub_headline(snippet_raw) or None,
                topic_label=topic.label,
                age_hours=(
                    (datetime.now(timezone.utc) - published_at).total_seconds() / 3600.0
                    if published_at
                    else None
                ),
            )
            headlines.append(headline)
            if len(headlines) >= limit:
                break
        return headlines

    def _rank(self, headlines: list[Headline]) -> list[Headline]:
        def key(item: Headline) -> tuple[int, int, int, int, float]:
            novelty_rank = {"new": 0, "updated": 1, "known": 2}.get(item.novelty, 2)
            source_rank = item.source_priority if item.source_priority is not None else 999
            recency_rank = int((item.age_hours or 999.0) // 6)
            topic_rank = 0
            if item.topic_label and item.topic_label.endswith("_analysis"):
                topic_rank -= 1
            return (novelty_rank, source_rank, recency_rank, topic_rank, -self._published_ts(item))

        return sorted(headlines, key=key)

    @staticmethod
    def _published_ts(item: Headline) -> float:
        if not item.published_at:
            return 0.0
        normalized = item.published_at[:-1] + "+00:00" if item.published_at.endswith("Z") else item.published_at
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _extract_domain(url: str) -> str | None:
        if not url:
            return None
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname or None

    def _source_priority(self, domain: str | None) -> int:
        if not domain:
            return len(self.settings.preferred_news_domains) + 10
        for index, preferred in enumerate(self.settings.preferred_news_domains):
            if domain == preferred or domain.endswith(f".{preferred}"):
                return index
        return len(self.settings.preferred_news_domains) + 1

    @staticmethod
    def _parse_timestamp(entry) -> datetime | None:
        published = entry.get("published") or entry.get("updated")
        if not published:
            return None
        try:
            parsed = parsedate_to_datetime(published)
        except (TypeError, ValueError, IndexError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
