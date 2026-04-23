from __future__ import annotations

from t212_cfd_bot.instruments import get_instrument, list_supported_instruments


def test_instrument_has_hours_and_exchange():
    gold = get_instrument("GOLD")
    assert gold.exchange_code == "CMES"
    assert gold.max_spread_bps > 0
    assert gold.correlation_group == "precious_metals"
    assert gold.tradeable_hours_utc[0] != gold.tradeable_hours_utc[1]


def test_spy_has_us_equity_group():
    spy = get_instrument("SPY")
    assert spy.correlation_group == "us_equity"
    assert spy.exchange_code == "XNYS"


def test_all_instruments_have_required_fields():
    for inst in list_supported_instruments():
        assert inst.exchange_code
        assert inst.correlation_group
        assert inst.max_spread_bps > 0
        assert 0 <= inst.tradeable_hours_utc[0] < 24
        assert 0 < inst.tradeable_hours_utc[1] <= 24
