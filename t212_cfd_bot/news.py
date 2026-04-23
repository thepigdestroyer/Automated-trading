from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from urllib.parse import quote_plus, urlparse

import feedparser

from t212_cfd_bot.config import Settings
from t212_cfd_bot.instruments import get_instrument
from t212_cfd_bot.models import Headline


class GoogleNewsClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch_symbol_headlines(self, symbol: str) -> list[Headline]:
        instrument = get_instrument(symbol)
        queries = self._build_queries(
            base_query=instrument.news_query,
            scope="symbol",
        )
        return self._fetch_multi_query_headlines(queries=queries, limit=self.settings.news_per_symbol)

    def fetch_market_headlines(self) -> list[Headline]:
        limit = max(self.settings.news_per_symbol, 6)
        queries = self._build_queries(
            base_query=self.settings.market_news_query,
            scope="market",
        )
        return self._fetch_multi_query_headlines(queries=queries, limit=limit)

    def _fetch_multi_query_headlines(
        self,
        *,
        queries: list[tuple[str, str]],
        limit: int,
    ) -> list[Headline]:
        per_query_limit = max(
            2,
            min(self.settings.news_per_query_limit, max(2, ceil(limit / max(len(queries), 1)) + 1)),
        )
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.settings.max_news_headline_age_hours
        )
        combined: list[Headline] = []
        seen_keys: set[str] = set()

        for label, query in queries:
            for headline in self._fetch_headlines(query=query, limit=per_query_limit, cutoff=cutoff, query_label=label):
                key = self._dedupe_key(headline)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                combined.append(headline)

        if not combined:
            return []

        ranked = self._rank_headlines(combined)
        selected: list[Headline] = []
        seen_domains: set[str] = set()

        for headline in ranked:
            domain = headline.source_domain or ""
            if domain and domain not in seen_domains:
                selected.append(headline)
                seen_domains.add(domain)
            if len(selected) >= limit:
                return selected[:limit]

        for headline in ranked:
            if headline in selected:
                continue
            selected.append(headline)
            if len(selected) >= limit:
                break

        return selected[:limit]

    def _fetch_headlines(
        self,
        *,
        query: str,
        limit: int,
        cutoff: datetime,
        query_label: str,
    ) -> list[Headline]:
        scoped_query = f"{query} when:{self.settings.news_lookback_hours}h"
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(scoped_query)}&hl=en-US&gl=US&ceid=US:en"
        )
        feed = feedparser.parse(url)

        headlines: list[Headline] = []
        seen_titles: set[str] = set()

        for entry in feed.entries:
            title = str(entry.get("title", "")).strip()
            if not title or title in seen_titles:
                continue

            published_at = self._parse_timestamp(entry)
            if published_at and published_at < cutoff:
                continue

            source = "Unknown"
            source_obj = entry.get("source")
            if isinstance(source_obj, dict):
                source = str(source_obj.get("title", source)).strip() or source

            link = str(entry.get("link", "")).strip()
            source_domain = self._extract_domain(link)
            source_priority = self._source_priority(source_domain)

            headlines.append(
                Headline(
                    title=title,
                    source=source,
                    url=link,
                    published_at=published_at.isoformat() if published_at else None,
                    source_domain=source_domain,
                    source_priority=source_priority,
                    query_label=query_label,
                )
            )
            seen_titles.add(title)

            if len(headlines) >= limit:
                break

        return headlines

    def _build_queries(self, *, base_query: str, scope: str) -> list[tuple[str, str]]:
        queries: list[tuple[str, str]] = [(f"{scope}_base", base_query)]
        analysis_query = f"({base_query}) ({self.settings.news_analysis_terms})"
        queries.append((f"{scope}_analysis", analysis_query))

        for domain in self.settings.preferred_news_domains[: self.settings.max_news_source_domains_per_fetch]:
            queries.append((f"{scope}_source:{domain}", f"({base_query}) site:{domain}"))
            queries.append(
                (
                    f"{scope}_analysis_source:{domain}",
                    f"({base_query}) ({self.settings.news_analysis_terms}) site:{domain}",
                )
            )
        return queries

    def _rank_headlines(self, headlines: list[Headline]) -> list[Headline]:
        return sorted(
            headlines,
            key=lambda item: (
                item.source_priority if item.source_priority is not None else 999,
                0 if item.query_label and "analysis" in item.query_label else 1,
                -self._published_timestamp(item),
            ),
        )

    @staticmethod
    def _published_timestamp(item: Headline) -> float:
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
    def _dedupe_key(item: Headline) -> str:
        if item.url:
            return item.url
        return item.title.strip().lower()

    @staticmethod
    def _parse_timestamp(entry: feedparser.FeedParserDict) -> datetime | None:
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
