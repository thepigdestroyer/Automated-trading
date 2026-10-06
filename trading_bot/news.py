from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser

from trading_bot.config import Settings
from trading_bot.instruments import NewsTopic, get_instrument
from trading_bot.models import Headline

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


def normalize_title_for_dedup(title: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace; keep first 80 chars.

    Detects near-duplicate headlines where two outlets carry the same wire
    story. Canonical home for the rule: it is applied twice in the pipeline —
    once when merging providers (``news_sources.NewsAggregator``, to keep the
    lowest-latency copy) and once when building the prompt
    (``openai_decision.compress_headlines_for_prompt``). Two copies of the
    rule could drift apart, so both import this one.

    Note the matching is textual, not semantic: a genuinely reworded headline
    still survives as its own item.
    """
    if not title:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:80]


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

            # Per review 2026-05-02-news-sources: Google News RSS returns
            # redirector URLs (news.google.com/rss/articles/...) which made
            # _extract_domain(link) always return "news.google.com",
            # collapsing source_priority to the lowest tier for every
            # headline. The RSS <source> element exposes the actual
            # publisher (href = "https://www.bloomberg.com" + title =
            # "Bloomberg") — use that first; fall back to the link
            # hostname (and to the source title text) when href is
            # missing.
            source_domain = self._resolve_publisher_domain(link, source_obj, source)
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

    def _resolve_publisher_domain(
        self,
        link: str,
        source_obj: object,
        source_title: str,
    ) -> str | None:
        """Resolve the publisher domain from an RSS entry.

        Order of preference:
        1. ``source_obj["href"]`` (the RSS ``<source>`` element's href —
           the actual publisher URL). This is set by Google News for
           every entry.
        2. The ``link`` URL hostname, only if it isn't a Google News
           redirector domain.
        3. A heuristic match of the source title (e.g. "Bloomberg" →
           ``bloomberg.com``) against the configured preferred domain
           list. Catches cases where Google's RSS omits the source
           href.
        """
        if isinstance(source_obj, dict):
            href = source_obj.get("href") or ""
            if href:
                hostname = self._extract_domain(str(href))
                if hostname and not self._is_aggregator_domain(hostname):
                    return hostname

        link_domain = self._extract_domain(link)
        if link_domain and not self._is_aggregator_domain(link_domain):
            return link_domain

        # Heuristic: match the source-title text against the preferred
        # domains the bot is configured to prefer. "Bloomberg" -> bloomberg.com.
        if source_title:
            title_lower = source_title.lower().strip()
            for preferred in self.settings.preferred_news_domains:
                base = preferred.split(".")[0]
                if base and base in title_lower:
                    return preferred
        return link_domain

    @staticmethod
    def _is_aggregator_domain(domain: str) -> bool:
        """True for Google-News-style redirector / aggregator domains.

        We never want these to be treated as the source: the actual
        publisher should win priority ranking.
        """
        if not domain:
            return False
        d = domain.lower()
        return d == "news.google.com" or d.endswith(".news.google.com")

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
