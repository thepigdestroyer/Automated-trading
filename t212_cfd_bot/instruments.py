from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class InstrumentDefinition:
    symbol: str
    display_name: str
    asset_class: str
    data_symbol: str
    news_query: str
    discovery_hint: str
    broker_search_terms: tuple[str, ...] = ()

    def discovery_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "display_name": self.display_name,
            "asset_class": self.asset_class,
            "data_symbol": self.data_symbol,
            "discovery_hint": self.discovery_hint,
        }


_SUPPORTED_INSTRUMENTS: tuple[InstrumentDefinition, ...] = (
    InstrumentDefinition(
        symbol="AAPL",
        display_name="Apple",
        asset_class="equity",
        data_symbol="AAPL",
        news_query="AAPL OR Apple stock OR iPhone",
        discovery_hint="liquid mega-cap tech equity",
        broker_search_terms=("AAPL", "Apple"),
    ),
    InstrumentDefinition(
        symbol="MSFT",
        display_name="Microsoft",
        asset_class="equity",
        data_symbol="MSFT",
        news_query="MSFT OR Microsoft stock OR Azure",
        discovery_hint="liquid mega-cap software equity",
        broker_search_terms=("MSFT", "Microsoft"),
    ),
    InstrumentDefinition(
        symbol="NVDA",
        display_name="NVIDIA",
        asset_class="equity",
        data_symbol="NVDA",
        news_query="NVDA OR NVIDIA stock OR AI chips",
        discovery_hint="high-beta AI semiconductor equity",
        broker_search_terms=("NVDA", "NVIDIA"),
    ),
    InstrumentDefinition(
        symbol="AMD",
        display_name="AMD",
        asset_class="equity",
        data_symbol="AMD",
        news_query="AMD OR Advanced Micro Devices stock OR semiconductors",
        discovery_hint="liquid semiconductor equity",
        broker_search_terms=("AMD", "Advanced Micro Devices"),
    ),
    InstrumentDefinition(
        symbol="TSLA",
        display_name="Tesla",
        asset_class="equity",
        data_symbol="TSLA",
        news_query="TSLA OR Tesla stock OR EV market",
        discovery_hint="high-volatility mega-cap equity",
        broker_search_terms=("TSLA", "Tesla"),
    ),
    InstrumentDefinition(
        symbol="AMZN",
        display_name="Amazon",
        asset_class="equity",
        data_symbol="AMZN",
        news_query="AMZN OR Amazon stock OR AWS",
        discovery_hint="liquid mega-cap consumer and cloud equity",
        broker_search_terms=("AMZN", "Amazon"),
    ),
    InstrumentDefinition(
        symbol="META",
        display_name="Meta",
        asset_class="equity",
        data_symbol="META",
        news_query="META OR Meta stock OR social media advertising",
        discovery_hint="liquid mega-cap internet equity",
        broker_search_terms=("META", "Meta Platforms"),
    ),
    InstrumentDefinition(
        symbol="GOOGL",
        display_name="Alphabet",
        asset_class="equity",
        data_symbol="GOOGL",
        news_query="GOOGL OR Alphabet stock OR Google advertising",
        discovery_hint="liquid mega-cap internet equity",
        broker_search_terms=("GOOGL", "Alphabet", "Google"),
    ),
    InstrumentDefinition(
        symbol="SPY",
        display_name="SPDR S&P 500 ETF",
        asset_class="etf",
        data_symbol="SPY",
        news_query="SPY OR S&P 500 ETF OR US equities",
        discovery_hint="broad US equity ETF proxy",
        broker_search_terms=("SPY", "SPDR S&P 500 ETF"),
    ),
    InstrumentDefinition(
        symbol="QQQ",
        display_name="Invesco QQQ",
        asset_class="etf",
        data_symbol="QQQ",
        news_query="QQQ OR Nasdaq ETF OR US tech stocks",
        discovery_hint="large-cap US tech ETF proxy",
        broker_search_terms=("QQQ", "Invesco QQQ"),
    ),
    InstrumentDefinition(
        symbol="IWM",
        display_name="iShares Russell 2000 ETF",
        asset_class="etf",
        data_symbol="IWM",
        news_query="IWM OR Russell 2000 ETF OR US small caps",
        discovery_hint="small-cap US equity ETF proxy",
        broker_search_terms=("IWM", "iShares Russell 2000 ETF"),
    ),
    InstrumentDefinition(
        symbol="SPX",
        display_name="S&P 500",
        asset_class="index",
        data_symbol="^GSPC",
        news_query="S&P 500 OR SPX OR US equities",
        discovery_hint="flagship US equity index",
        broker_search_terms=("US 500", "S&P 500", "SPX"),
    ),
    InstrumentDefinition(
        symbol="NAS100",
        display_name="Nasdaq 100",
        asset_class="index",
        data_symbol="^NDX",
        news_query="Nasdaq 100 OR NAS100 OR US tech stocks",
        discovery_hint="major US technology index",
        broker_search_terms=("US Tech 100", "Nasdaq 100", "NAS100"),
    ),
    InstrumentDefinition(
        symbol="US30",
        display_name="Dow Jones Industrial Average",
        asset_class="index",
        data_symbol="^DJI",
        news_query="Dow Jones OR US30 OR Wall Street",
        discovery_hint="major blue-chip US equity index",
        broker_search_terms=("Wall Street", "Dow Jones", "US 30"),
    ),
    InstrumentDefinition(
        symbol="UK100",
        display_name="FTSE 100",
        asset_class="index",
        data_symbol="^FTSE",
        news_query="FTSE 100 OR UK100 OR London stocks",
        discovery_hint="major UK equity index",
        broker_search_terms=("UK 100", "FTSE 100", "UK100"),
    ),
    InstrumentDefinition(
        symbol="GER40",
        display_name="DAX",
        asset_class="index",
        data_symbol="^GDAXI",
        news_query="DAX OR GER40 OR Germany stocks",
        discovery_hint="major German equity index",
        broker_search_terms=("Germany 40", "DAX", "GER40"),
    ),
    InstrumentDefinition(
        symbol="EURUSD",
        display_name="EUR/USD",
        asset_class="forex",
        data_symbol="EURUSD=X",
        news_query="EURUSD OR euro dollar OR forex",
        discovery_hint="major FX pair driven by rates and macro data",
        broker_search_terms=("EUR/USD", "EURUSD"),
    ),
    InstrumentDefinition(
        symbol="GBPUSD",
        display_name="GBP/USD",
        asset_class="forex",
        data_symbol="GBPUSD=X",
        news_query="GBPUSD OR pound dollar OR forex",
        discovery_hint="major FX pair driven by rates and UK data",
        broker_search_terms=("GBP/USD", "GBPUSD", "Cable"),
    ),
    InstrumentDefinition(
        symbol="USDJPY",
        display_name="USD/JPY",
        asset_class="forex",
        data_symbol="JPY=X",
        news_query="USDJPY OR dollar yen OR forex",
        discovery_hint="major FX pair sensitive to yields and risk sentiment",
        broker_search_terms=("USD/JPY", "USDJPY"),
    ),
    InstrumentDefinition(
        symbol="AUDUSD",
        display_name="AUD/USD",
        asset_class="forex",
        data_symbol="AUDUSD=X",
        news_query="AUDUSD OR aussie dollar OR forex",
        discovery_hint="commodity-linked major FX pair",
        broker_search_terms=("AUD/USD", "AUDUSD"),
    ),
    InstrumentDefinition(
        symbol="USDCHF",
        display_name="USD/CHF",
        asset_class="forex",
        data_symbol="CHF=X",
        news_query="USDCHF OR dollar swiss franc OR forex",
        discovery_hint="major FX pair with safe-haven sensitivity",
        broker_search_terms=("USD/CHF", "USDCHF"),
    ),
    InstrumentDefinition(
        symbol="GOLD",
        display_name="Gold",
        asset_class="metal",
        data_symbol="GC=F",
        news_query="gold price OR XAUUSD OR bullion OR COMEX gold",
        discovery_hint="liquid precious metal driven by inflation and yields",
        broker_search_terms=("XAUUSD", "Spot Gold", "Gold"),
    ),
    InstrumentDefinition(
        symbol="SILVER",
        display_name="Silver",
        asset_class="metal",
        data_symbol="SI=F",
        news_query="silver price OR XAGUSD OR bullion OR COMEX silver",
        discovery_hint="precious and industrial metal with higher beta than gold",
        broker_search_terms=("XAGUSD", "Spot Silver", "Silver"),
    ),
    InstrumentDefinition(
        symbol="COPPER",
        display_name="Copper",
        asset_class="metal",
        data_symbol="HG=F",
        news_query="copper price OR industrial metals OR COMEX copper",
        discovery_hint="industrial metal tied to growth and China demand",
        broker_search_terms=("Copper", "COMEX Copper"),
    ),
    InstrumentDefinition(
        symbol="PLATINUM",
        display_name="Platinum",
        asset_class="metal",
        data_symbol="PL=F",
        news_query="platinum price OR precious metals OR NYMEX platinum",
        discovery_hint="precious metal with industrial demand exposure",
        broker_search_terms=("Platinum", "Spot Platinum"),
    ),
    InstrumentDefinition(
        symbol="WTI",
        display_name="WTI Crude Oil",
        asset_class="energy",
        data_symbol="CL=F",
        news_query="WTI crude oil price OR US crude OR oil market",
        discovery_hint="benchmark US crude oil market",
        broker_search_terms=("US Crude", "WTI Crude Oil", "WTI"),
    ),
    InstrumentDefinition(
        symbol="BRENT",
        display_name="Brent Crude Oil",
        asset_class="energy",
        data_symbol="BZ=F",
        news_query="Brent crude oil price OR Brent oil OR oil market",
        discovery_hint="benchmark global crude oil market",
        broker_search_terms=("Brent Crude", "UK Oil", "Brent"),
    ),
    InstrumentDefinition(
        symbol="NATGAS",
        display_name="Natural Gas",
        asset_class="energy",
        data_symbol="NG=F",
        news_query="natural gas price OR nat gas OR Henry Hub energy",
        discovery_hint="volatile energy market tied to weather and storage data",
        broker_search_terms=("Natural Gas", "Nat Gas", "Henry Hub"),
    ),
)

_INSTRUMENT_BY_SYMBOL = {instrument.symbol: instrument for instrument in _SUPPORTED_INSTRUMENTS}
DEFAULT_ALLOWED_ASSET_CLASSES = ("equity", "etf", "index", "forex", "metal", "energy")


def list_supported_instruments(asset_classes: Iterable[str] | None = None) -> list[InstrumentDefinition]:
    if asset_classes is None:
        return list(_SUPPORTED_INSTRUMENTS)

    allowed = {str(value).strip().lower() for value in asset_classes if str(value).strip()}
    if not allowed:
        return list(_SUPPORTED_INSTRUMENTS)
    return [
        instrument
        for instrument in _SUPPORTED_INSTRUMENTS
        if instrument.asset_class.lower() in allowed
    ]


def get_instrument(symbol: str) -> InstrumentDefinition:
    normalized = symbol.strip().upper()
    instrument = _INSTRUMENT_BY_SYMBOL.get(normalized)
    if instrument is not None:
        return instrument

    return InstrumentDefinition(
        symbol=normalized,
        display_name=normalized,
        asset_class="unknown",
        data_symbol=normalized,
        news_query=f"{normalized} market price",
        discovery_hint="user-specified instrument outside the built-in universe",
        broker_search_terms=(normalized,),
    )
