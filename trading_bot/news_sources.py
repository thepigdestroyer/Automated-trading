"""Multi-provider market news.

Before this module the bot had exactly ONE news source, Google News RSS
(`news.GoogleNewsClient`). That is a single point of failure and it is
structurally late: Google rebroadcasts rather than originates, so a headline
arrives roughly 15-30 minutes after the wire carried it. For a bot trading
energy and indices on macro catalysts, that delay spans the whole move.

Design rules, in priority order:

1. **Failure isolation.** Adding sources must reduce fragility, not add new
   crash paths. Every provider is called inside its own ``try/except``; a
   raise, a timeout, a 403 page or a zero-item return is logged and skipped
   and never propagates to the cycle.
2. **No network in unit tests.** ``RssWireProvider`` takes an injectable
   ``fetcher``; the default is a real HTTP GET, tests pass a lambda.
3. **Untrusted text.** Every title/snippet from every provider goes through
   ``news.scrub_headline`` before it can reach an LLM prompt — enforced at
   the merge boundary, not left to each provider's good manners.
4. **Latency-aware dedup.** When the same story arrives from a live wire and
   from a delayed aggregator, the fast copy wins, and realtime items are
   ordered first so the (unchanged) prompt caps spend the budget on the
   freshest information.

Only feed URLs personally verified to return parseable RSS belong here. A
guessed URL becomes a silent, permanent zero-item provider.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Protocol
from urllib.parse import urlparse

import feedparser

from trading_bot.config import Settings
from trading_bot.models import Headline
from trading_bot.news import headline_url_hash, normalize_title_for_dedup, scrub_headline

LOGGER = logging.getLogger(__name__)

# Lower is fresher. Used both to pick the winner when the same story shows up
# twice and to order the merged list ahead of the prompt caps.
LATENCY_RANK: dict[str, int] = {"realtime": 0, "official": 1, "aggregator": 2}
_UNKNOWN_LATENCY_RANK = len(LATENCY_RANK)

# Some feeds (financialjuice.com among them) reject the default urllib agent.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def latency_rank(latency_class: str | None) -> int:
    """Rank of a latency class; unknown/missing sorts last."""
    if not latency_class:
        return _UNKNOWN_LATENCY_RANK
    return LATENCY_RANK.get(str(latency_class), _UNKNOWN_LATENCY_RANK)


class NewsProvider(Protocol):
    """One market-news source."""

    name: str
    latency_class: str

    def fetch_market_headlines(self) -> list[Headline]:  # pragma: no cover - protocol
        ...


def _http_fetcher(timeout_seconds: float) -> Callable[[str], str]:
    """Real HTTP GET. Only used in production; tests inject their own."""

    def fetch(url: str) -> str:
        import requests

        response = requests.get(
            url,
            timeout=timeout_seconds,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        response.raise_for_status()
        return response.text

    return fetch


def _extract_domain(url: str) -> str | None:
    if not url:
        return None
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or None


def _parse_entry_timestamp(entry) -> datetime | None:
    """RFC-2822 ``pubDate`` (or ``updated``), falling back to feedparser's
    pre-parsed struct_time. Returns tz-aware UTC or None."""
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


class RssWireProvider:
    """A flat RSS feed (``title``/``link``/``pubDate`` items).

    ``fetcher`` is injectable so unit tests never hit the network. Parse
    failures surface as an empty list rather than an exception where the feed
    body is simply not RSS (a 403 HTML page, say); transport errors raised by
    the fetcher propagate to ``NewsAggregator``, which isolates them.
    """

    def __init__(
        self,
        name: str,
        feed_url: str,
        latency_class: str,
        *,
        max_items: int,
        title_prefix_strip: str | None = None,
        fetcher: Callable[[str], str] | None = None,
        timeout_seconds: float = 12.0,
        source_label: str | None = None,
    ):
        self.name = name
        self.feed_url = feed_url
        self.latency_class = latency_class
        self.max_items = int(max_items)
        self.title_prefix_strip = title_prefix_strip
        self.source_label = source_label or name
        self._fetcher = fetcher or _http_fetcher(timeout_seconds)

    def fetch_market_headlines(self) -> list[Headline]:
        body = self._fetcher(self.feed_url)
        feed = feedparser.parse(body or "")
        domain = _extract_domain(self.feed_url)
        now = datetime.now(timezone.utc)
        headlines: list[Headline] = []
        for entry in feed.entries:
            if self.max_items > 0 and len(headlines) >= self.max_items:
                break
            raw_title = str(entry.get("title", "") or "").strip()
            if not raw_title:
                continue
            if self.title_prefix_strip and raw_title.startswith(self.title_prefix_strip):
                raw_title = raw_title[len(self.title_prefix_strip) :].strip()
            title = scrub_headline(raw_title)
            if not title:
                continue
            link = str(entry.get("link", "") or "").strip()
            if not link:
                continue
            published_at = _parse_entry_timestamp(entry)
            headlines.append(
                Headline(
                    title=title,
                    source=self.source_label,
                    url=link,
                    published_at=published_at.isoformat() if published_at else None,
                    source_domain=domain,
                    # Deliberately left None: source_priority drives the
                    # breaking-news cache-invalidation counter in bot.py, and
                    # stamping 40 squawk lines as top-tier would invalidate the
                    # LLM caches every cycle. latency_class carries the
                    # freshness signal instead.
                    source_priority=None,
                    query_label=f"wire:{self.name}",
                    topic_label=f"wire:{self.name}",
                    latency_class=self.latency_class,
                    age_hours=(
                        (now - published_at).total_seconds() / 3600.0
                        if published_at
                        else None
                    ),
                )
            )
        return headlines


class GoogleNewsProvider:
    """Adapts the existing ``GoogleNewsClient`` to the provider protocol.

    Google News stays in the mix — it is broad and query-driven — but it is
    tagged ``aggregator`` because it rebroadcasts with delay, so dedup prefers
    any faster copy of the same story.
    """

    name = "google_news"
    latency_class = "aggregator"

    def __init__(self, client):
        self.client = client

    def fetch_market_headlines(self) -> list[Headline]:
        return list(self.client.fetch_market_headlines() or [])


class NewsAggregator:
    """Fans out to every provider, isolates failures, dedups latency-aware."""

    def __init__(self, providers, settings: Settings, *, event_logger=None, cycle_id: str | None = None):
        self.providers = list(providers or [])
        self.settings = settings
        self.event_logger = event_logger
        self.cycle_id = cycle_id

    def fetch_market_headlines(self) -> list[Headline]:
        collected: list[Headline] = []
        for provider in self.providers:
            name = str(getattr(provider, "name", provider.__class__.__name__))
            provider_latency = getattr(provider, "latency_class", None)
            try:
                items = list(provider.fetch_market_headlines() or [])
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                LOGGER.warning("news provider %s failed: %s", name, exc)
                self._emit(
                    "news_provider_failure",
                    {
                        "provider": name,
                        "latency_class": provider_latency,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue
            if not items:
                LOGGER.warning("news provider %s returned zero items", name)
                self._emit(
                    "news_provider_empty",
                    {"provider": name, "latency_class": provider_latency},
                )
                continue
            for item in items:
                normalised = self._normalise(item, provider_latency)
                if normalised is not None:
                    collected.append(normalised)
        return self._dedupe(collected)

    # -- internals ---------------------------------------------------------

    def _normalise(self, item: Headline, provider_latency: str | None) -> Headline | None:
        """Stamp the provider's latency class when missing and re-scrub.

        Scrubbing here (not only in each provider) means a future provider
        that forgets to scrub still cannot inject text into the prompt.
        """
        try:
            updates: dict[str, object] = {}
            if item.latency_class is None and provider_latency:
                updates["latency_class"] = provider_latency
            scrubbed_title = scrub_headline(item.title)
            if scrubbed_title != item.title:
                updates["title"] = scrubbed_title
            if item.snippet:
                scrubbed_snippet = scrub_headline(item.snippet)
                if scrubbed_snippet != item.snippet:
                    updates["snippet"] = scrubbed_snippet
            return item.model_copy(update=updates) if updates else item
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the merge
            LOGGER.warning("dropping malformed headline: %s", exc)
            return None

    def _dedupe(self, headlines: list[Headline]) -> list[Headline]:
        """Collapse duplicates on URL hash and on normalised title.

        The surviving copy is the one with the lowest latency rank (realtime <
        official < aggregator), so a Google rebroadcast never displaces the
        wire original.
        """
        kept: list[Headline] = []
        index_by_key: dict[str, int] = {}
        for item in headlines:
            keys = self._dedupe_keys(item)
            existing = next((index_by_key[k] for k in keys if k in index_by_key), None)
            if existing is None:
                kept.append(item)
                for key in keys:
                    index_by_key[key] = len(kept) - 1
                continue
            if latency_rank(item.latency_class) < latency_rank(kept[existing].latency_class):
                kept[existing] = item
            for key in keys:
                index_by_key.setdefault(key, existing)
        return self._interleave_by_latency(kept)

    @staticmethod
    def _interleave_by_latency(headlines: list[Headline]) -> list[Headline]:
        """Round-robin the latency classes, freshest class first.

        The downstream prompt cap (``headline_prompt_max_market_items``, 20)
        simply takes the head of this list, so ordering decides which sources
        the LLM actually sees. Measured on live feeds: ordering strictly by
        latency gave all 20 slots to FinancialJuice's 40 items and pushed
        Google News out completely — trading one single-source dependency for
        another, which is the opposite of the point. Interleaving keeps the
        freshest item first while guaranteeing every class a share of the
        fixed budget, so one misbehaving feed cannot own the whole prompt.

        Order within a class is untouched, preserving each provider's own
        ranking (Google's source-priority/novelty sort in particular).
        """
        buckets: dict[int, list[Headline]] = {}
        for item in headlines:
            buckets.setdefault(latency_rank(item.latency_class), []).append(item)
        ranks = sorted(buckets)
        ordered: list[Headline] = []
        while len(ordered) < len(headlines):
            for rank in ranks:
                bucket = buckets[rank]
                if bucket:
                    ordered.append(bucket.pop(0))
        return ordered

    @staticmethod
    def _dedupe_keys(item: Headline) -> list[str]:
        keys: list[str] = []
        if item.url:
            keys.append(f"url:{headline_url_hash(item.url)}")
        title_key = normalize_title_for_dedup(item.title or "")
        if title_key:
            keys.append(f"title:{title_key}")
        return keys

    def _emit(self, event: str, data: dict) -> None:
        if self.event_logger is None:
            return
        try:
            self.event_logger.log(event, data, cycle_id=self.cycle_id)
        except Exception as exc:  # noqa: BLE001 - telemetry is not a crash path
            LOGGER.debug("failed to emit %s: %s", event, exc)


def build_market_news_providers(
    settings: Settings,
    *,
    google_client,
    fetcher: Callable[[str], str] | None = None,
) -> list[NewsProvider]:
    """Provider list for market headlines, in fetch order.

    Only the two feeds verified live from this machine are included.
    ECB (`/rss/press.html`), Bank of England (`/rss/news`), Baker Hughes and
    the EIA natural-gas RSS all failed probing (connection failure / HTTP 403)
    and are deliberately absent: an unverified URL is a permanently silent
    provider that looks healthy.
    """
    timeout = float(getattr(settings, "news_fetch_timeout_seconds", 12.0))
    providers: list[NewsProvider] = [GoogleNewsProvider(google_client)]
    if getattr(settings, "financialjuice_enabled", False):
        providers.append(
            RssWireProvider(
                name="financialjuice",
                feed_url=settings.financialjuice_feed_url,
                latency_class="realtime",
                max_items=settings.financialjuice_max_items,
                title_prefix_strip="FinancialJuice: ",
                fetcher=fetcher,
                timeout_seconds=timeout,
                source_label="FinancialJuice",
            )
        )
    if getattr(settings, "fed_press_enabled", False):
        providers.append(
            RssWireProvider(
                name="fed_press",
                feed_url=settings.fed_press_feed_url,
                latency_class="official",
                max_items=settings.fed_press_max_items,
                fetcher=fetcher,
                timeout_seconds=timeout,
                source_label="Federal Reserve",
            )
        )
    return providers
