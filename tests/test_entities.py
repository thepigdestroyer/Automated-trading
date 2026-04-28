from __future__ import annotations

from unittest.mock import patch

from trading_bot.config import Settings
from trading_bot.entities import ENTITIES, EntityNewsClient, entities_for_symbol


def test_entity_catalog_contains_expected_keys():
    keys = {e.key for e in ENTITIES}
    for expected in ("Iran", "Russia", "Fed", "ECB", "OPEC", "China"):
        assert expected in keys


def test_entities_for_symbol_wti_includes_oil_entities():
    impacts = entities_for_symbol("WTI")
    keys = {e.key for e in impacts}
    assert "Iran" in keys
    assert "Russia" in keys
    assert "OPEC" in keys


def test_fetch_returns_dict_keyed_by_entity():
    s = Settings().model_copy(update={"broker": "paper", "entity_tracking_enabled": True})
    client = EntityNewsClient(s)
    with patch("trading_bot.entities.GoogleNewsClient.fetch_query_headlines", return_value=[]):
        result = client.fetch_all()
    assert "Iran" in result
    assert isinstance(result["Iran"], list)
