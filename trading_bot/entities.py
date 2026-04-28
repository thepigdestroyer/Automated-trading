from __future__ import annotations

from dataclasses import dataclass

from trading_bot.config import Settings
from trading_bot.models import Headline
from trading_bot.news import GoogleNewsClient


@dataclass(frozen=True)
class Entity:
    key: str
    display_name: str
    queries: tuple[str, ...]
    impacts: tuple[str, ...]


ENTITIES: tuple[Entity, ...] = (
    Entity("Iran",    "Iran",    ("Iran oil", "Strait of Hormuz", "Iran sanctions"),       ("WTI", "BRENT", "GOLD")),
    Entity("Russia",  "Russia",  ("Russia Ukraine war", "Kremlin", "Russian oil"),         ("BRENT", "WTI", "NATGAS", "GOLD", "EURUSD")),
    Entity("Fed",     "Fed",     ("Federal Reserve", "FOMC", "Jerome Powell"),             ("*",)),
    Entity("ECB",     "ECB",     ("European Central Bank", "ECB rate decision"),           ("EURUSD", "GER40", "UK100")),
    Entity("BoE",     "BoE",     ("Bank of England", "BOE rate decision"),                 ("GBPUSD", "UK100")),
    Entity("BoJ",     "BoJ",     ("Bank of Japan", "yen intervention"),                    ("USDJPY",)),
    Entity("OPEC",    "OPEC",    ("OPEC", "OPEC+"),                                        ("WTI", "BRENT")),
    Entity("China",   "China",   ("China economy", "PBoC", "Chinese stimulus"),            ("COPPER", "SILVER", "AUDUSD", "*")),
    Entity("Israel",  "Israel",  ("Israel Gaza", "Middle East ceasefire"),                 ("WTI", "BRENT", "GOLD")),
)


def entities_for_symbol(symbol: str) -> list[Entity]:
    symbol = symbol.upper()
    return [e for e in ENTITIES if symbol in e.impacts or "*" in e.impacts]


def entities_market_wide() -> list[Entity]:
    return [e for e in ENTITIES if "*" in e.impacts]


class EntityNewsClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._news = GoogleNewsClient(settings)

    def fetch_all(self) -> dict[str, list[Headline]]:
        if not self.settings.entity_tracking_enabled:
            return {e.key: [] for e in ENTITIES}
        result: dict[str, list[Headline]] = {}
        per_entity = max(3, self.settings.news_per_query_limit)
        for entity in ENTITIES:
            combined: list[Headline] = []
            for query in entity.queries:
                label = f"entity:{entity.key}"
                hits = self._news.fetch_query_headlines(
                    label=label,
                    query=query,
                    max_age_hours=24,
                    limit=per_entity,
                )
                for h in hits:
                    tagged = h.model_copy(update={"entity_source": entity.key})
                    combined.append(tagged)
            seen: set[str] = set()
            deduped: list[Headline] = []
            for h in combined:
                if h.url in seen:
                    continue
                seen.add(h.url)
                deduped.append(h)
            result[entity.key] = deduped[: per_entity * 2]
        return result
