"""Tests for Phase 4 free macro-data sources.

All three clients (FRED/ALFRED, Finnhub, CFTC COT) take an injectable JSON
fetcher so tests run fully offline. Like vix.py, every client is defensive:
a network/parse failure degrades to a note and never raises into the cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.config import Settings
from trading_bot.macro_data import (
    CftcClient,
    EiaClient,
    FinnhubClient,
    FredClient,
    MacroContextClient,
    _staleness_days,
)


def _settings(**kw) -> Settings:
    base = dict(
        broker="paper",
        macro_context_enabled=True,
        fred_api_key="FRED_KEY",
        finnhub_api_key="FINN_KEY",
        eia_api_key="EIA_KEY",
        fred_series_ids=["DGS10", "T10Y2Y"],
        cftc_markets=["GOLD", "CRUDE OIL, LIGHT SWEET"],
        eia_series_ids=["WCESTUS1"],
    )
    base.update(kw)
    return Settings().model_copy(update=base)


# --- staleness ----------------------------------------------------------


def test_staleness_days():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    assert _staleness_days("2026-06-08", now=now) == 2
    assert _staleness_days(None, now=now) is None
    assert _staleness_days("garbage", now=now) is None


# --- FRED / ALFRED ------------------------------------------------------


def test_fred_parses_latest_observation_with_vintage():
    payload = {
        "observations": [
            {"date": "2026-06-05", "value": "4.31", "realtime_start": "2026-06-06"}
        ]
    }
    captured = {}

    def fetch(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return payload

    client = FredClient(_settings(), json_fetcher=fetch)
    obs = client.fetch_series("DGS10", now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert obs is not None
    assert obs.series_id == "DGS10"
    assert obs.value == 4.31
    assert obs.date == "2026-06-05"
    assert obs.vintage_date == "2026-06-06"
    assert obs.staleness_days == 5
    # vintage correctness: realtime_start/end must be pinned to "now"
    assert captured["params"]["realtime_start"] == "2026-06-10"
    assert captured["params"]["realtime_end"] == "2026-06-10"
    assert captured["params"]["api_key"] == "FRED_KEY"


def test_fred_missing_value_dot_is_none():
    payload = {"observations": [{"date": "2026-06-05", "value": ".", "realtime_start": "2026-06-06"}]}
    client = FredClient(_settings(), json_fetcher=lambda u, p=None: payload)
    obs = client.fetch_series("DGS10")
    assert obs is not None and obs.value is None


def test_fred_empty_returns_none():
    client = FredClient(_settings(), json_fetcher=lambda u, p=None: {"observations": []})
    assert client.fetch_series("DGS10") is None


def test_fred_fetch_error_returns_none():
    def boom(u, p=None):
        raise RuntimeError("network down")

    client = FredClient(_settings(), json_fetcher=boom)
    assert client.fetch_series("DGS10") is None


def test_fred_no_key_returns_none():
    client = FredClient(_settings(fred_api_key=None), json_fetcher=lambda u, p=None: {})
    assert client.fetch_series("DGS10") is None


# --- Finnhub ------------------------------------------------------------


def test_finnhub_general_news_summary():
    payload = [
        {"headline": "Fed holds rates", "category": "general", "datetime": 1800000000},
        {"headline": "Gold rallies", "category": "general", "datetime": 1800000100},
    ]
    captured = {}

    def fetch(url, params=None):
        captured["params"] = params
        return payload

    client = FinnhubClient(_settings(), json_fetcher=fetch)
    res = client.fetch_market_news()
    assert res["count"] == 2
    assert "Fed holds rates" in res["summary"]
    assert captured["params"]["token"] == "FINN_KEY"


def test_finnhub_premium_403_degrades_to_note():
    def forbidden(url, params=None):
        raise PermissionError("403 premium endpoint")

    client = FinnhubClient(_settings(), json_fetcher=forbidden)
    res = client.fetch_market_news()
    assert res["count"] == 0
    assert "unavailable" in res["note"].lower() or "premium" in res["note"].lower()


def test_finnhub_no_key_returns_note():
    client = FinnhubClient(_settings(finnhub_api_key=None), json_fetcher=lambda u, p=None: [])
    res = client.fetch_market_news()
    assert res["count"] == 0
    assert res["note"]


# --- CFTC COT -----------------------------------------------------------


def test_cftc_parses_net_positioning_and_lag():
    rows = [
        {
            "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
            "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
            "noncomm_positions_long_all": "250000",
            "noncomm_positions_short_all": "100000",
            "change_in_noncomm_long_all": "5000",
            "change_in_noncomm_short_all": "-2000",
        }
    ]
    client = CftcClient(_settings(), json_fetcher=lambda u, p=None: rows)
    cot = client.fetch_market("GOLD", now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert cot is not None
    assert cot.noncommercial_long == 250000
    assert cot.noncommercial_short == 100000
    assert cot.net_noncommercial == 150000
    assert cot.net_change_1w == 7000  # +5000 long minus -2000 short = +7000
    assert cot.report_date == "2026-06-02"
    assert cot.publication_lag_days == 8


def test_cftc_empty_returns_none():
    client = CftcClient(_settings(), json_fetcher=lambda u, p=None: [])
    assert client.fetch_market("GOLD") is None


def test_cftc_error_returns_none():
    def boom(u, p=None):
        raise RuntimeError("timeout")

    client = CftcClient(_settings(), json_fetcher=boom)
    assert client.fetch_market("GOLD") is None


# --- aggregator ---------------------------------------------------------


def test_macro_context_disabled_is_empty():
    s = _settings(macro_context_enabled=False)
    client = MacroContextClient(s)
    ctx = client.fetch()
    assert ctx.fred_series == []
    assert ctx.cot == []
    assert "disabled" in " ".join(ctx.notes).lower()


def test_macro_context_aggregates_all_sources():
    fred_payload = {"observations": [{"date": "2026-06-05", "value": "4.31", "realtime_start": "2026-06-06"}]}
    finn_payload = [{"headline": "Fed holds", "datetime": 1800000000}]
    cot_rows = [{
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
        "noncomm_positions_long_all": "250000", "noncomm_positions_short_all": "100000",
        "change_in_noncomm_long_all": "5000", "change_in_noncomm_short_all": "-2000",
    }]

    eia_payload = {"response": {"data": [
        {"period": "2026-06-20", "value": "445000", "units": "Mbbl",
         "series-description": "Weekly U.S. Ending Stocks of Crude Oil"},
        {"period": "2026-06-13", "value": "450000", "units": "Mbbl",
         "series-description": "Weekly U.S. Ending Stocks of Crude Oil"},
    ]}}

    def router(url, params=None):
        if "stlouisfed" in url:
            return fred_payload
        if "finnhub" in url:
            return finn_payload
        if "cftc" in url:
            return cot_rows
        if "eia.gov" in url:
            return eia_payload
        return None

    s = _settings(cftc_markets=["GOLD"], eia_series_ids=["WCESTUS1"])
    client = MacroContextClient(s, json_fetcher=router)
    ctx = client.fetch()
    assert len(ctx.fred_series) == 2  # DGS10 + T10Y2Y
    assert ctx.finnhub_news_count == 1
    assert len(ctx.cot) == 1
    assert ctx.cot[0].net_noncommercial == 150000
    assert len(ctx.eia_series) == 1
    assert ctx.eia_series[0].change_1w == -5000.0  # crude DRAW


def test_macro_context_never_raises_on_total_failure():
    def boom(url, params=None):
        raise RuntimeError("everything is down")

    client = MacroContextClient(_settings(), json_fetcher=boom)
    ctx = client.fetch()  # must not raise
    assert ctx.fred_series == []
    assert ctx.cot == []


def test_macro_context_json_serialisable():
    import json

    client = MacroContextClient(_settings(macro_context_enabled=False))
    json.dumps(client.fetch().model_dump(mode="json"))


# --- EIA energy inventories --------------------------------------------


def _eia_payload(latest=445000, prior=450000):
    return {"response": {"data": [
        {"period": "2026-06-20", "value": str(latest), "units": "Mbbl",
         "series-description": "Weekly U.S. Ending Stocks of Crude Oil"},
        {"period": "2026-06-13", "value": str(prior), "units": "Mbbl",
         "series-description": "Weekly U.S. Ending Stocks of Crude Oil"},
    ]}}


def test_eia_parses_latest_and_wow_draw():
    captured = {}

    def fetch(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return _eia_payload(latest=445000, prior=450000)

    client = EiaClient(_settings(), json_fetcher=fetch)
    obs = client.fetch_series("WCESTUS1", now=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert obs is not None
    assert obs.series_id == "WCESTUS1"
    assert obs.value == 445000.0
    assert obs.period == "2026-06-20"
    assert obs.change_1w == -5000.0  # draw (bullish oil)
    assert obs.units == "Mbbl"
    assert obs.staleness_days == 4
    assert "WCESTUS1" in captured["url"]  # series id in the path
    assert captured["params"]["api_key"] == "EIA_KEY"


def test_eia_build_is_positive_change():
    client = EiaClient(_settings(), json_fetcher=lambda u, p=None: _eia_payload(latest=452000, prior=450000))
    obs = client.fetch_series("WCESTUS1")
    assert obs.change_1w == 2000.0  # build (bearish oil)


def test_eia_single_observation_no_change():
    payload = {"response": {"data": [
        {"period": "2026-06-20", "value": "445000", "units": "Mbbl"}
    ]}}
    client = EiaClient(_settings(), json_fetcher=lambda u, p=None: payload)
    obs = client.fetch_series("WCESTUS1")
    assert obs is not None and obs.value == 445000.0 and obs.change_1w is None


def test_eia_empty_returns_none():
    client = EiaClient(_settings(), json_fetcher=lambda u, p=None: {"response": {"data": []}})
    assert client.fetch_series("WCESTUS1") is None


def test_eia_no_key_returns_none():
    client = EiaClient(_settings(eia_api_key=None), json_fetcher=lambda u, p=None: _eia_payload())
    assert client.fetch_series("WCESTUS1") is None


def test_eia_error_returns_none():
    def boom(u, p=None):
        raise RuntimeError("eia timeout")

    client = EiaClient(_settings(), json_fetcher=boom)
    assert client.fetch_series("WCESTUS1") is None


def test_eia_unsorted_data_picks_most_recent():
    # Data arriving oldest-first must still resolve the latest period.
    payload = {"response": {"data": [
        {"period": "2026-06-13", "value": "450000", "units": "Mbbl"},
        {"period": "2026-06-20", "value": "445000", "units": "Mbbl"},
    ]}}
    client = EiaClient(_settings(), json_fetcher=lambda u, p=None: payload)
    obs = client.fetch_series("WCESTUS1")
    assert obs.period == "2026-06-20"
    assert obs.value == 445000.0
    assert obs.change_1w == -5000.0
