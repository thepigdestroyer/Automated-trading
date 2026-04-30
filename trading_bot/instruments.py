from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class NewsTopic:
    label: str
    query: str
    weight: float = 1.0
    max_age_hours: int = 24


@dataclass(frozen=True)
class InstrumentDefinition:
    symbol: str
    display_name: str
    asset_class: str
    data_symbol: str
    news_query: str
    discovery_hint: str
    broker_search_terms: tuple[str, ...] = ()
    exchange_code: str = "FX"
    tradeable_hours_utc: tuple[int, int] = (0, 24)
    max_spread_bps: int = 50
    correlation_group: str = "misc"
    news_topics: tuple[NewsTopic, ...] = ()
    # Multiplier mapping bot price-units (Yahoo data feed) to IG point-units.
    # On WTI Yahoo quotes ~$65.42 while IG quotes ~6542 (1 IG pt = 1 cent), so
    # ig_price_scale=100. For FX (EURUSD: Yahoo 1.083 vs IG 10830, 1 pt =
    # 1/10000) the scale is 10000. Equity/ETF symbols on IG are normally
    # quoted in price-cents too. Default 1.0 = no rescale; set explicitly
    # per instrument where the executor compares plan distances against
    # IG dealing-rule minimums (see review 2026-04-29-adhoc-1 #1).
    ig_price_scale: float = 1.0

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
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Apple OR AAPL stock", 1.0, 24),
            NewsTopic("earnings", "Apple earnings OR AAPL guidance", 1.2, 48),
            NewsTopic("iphone_demand", "iPhone sales OR iPhone demand OR China iPhone", 1.0, 48),
            NewsTopic("services", "Apple services OR App Store revenue", 0.8, 72),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("regulation", "Apple antitrust OR App Store regulation OR DOJ Apple", 1.0, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="MSFT",
        display_name="Microsoft",
        asset_class="equity",
        data_symbol="MSFT",
        news_query="MSFT OR Microsoft stock OR Azure",
        discovery_hint="liquid mega-cap software equity",
        broker_search_terms=("MSFT", "Microsoft"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Microsoft OR MSFT stock", 1.0, 24),
            NewsTopic("earnings", "Microsoft earnings OR MSFT guidance", 1.2, 48),
            NewsTopic("azure_cloud", "Azure revenue OR Microsoft cloud growth", 1.1, 48),
            NewsTopic("ai_copilot", "Microsoft AI OR Copilot OR OpenAI partnership", 1.0, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("regulation", "Microsoft antitrust OR Activision regulator", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="NVDA",
        display_name="NVIDIA",
        asset_class="equity",
        data_symbol="NVDA",
        news_query="NVDA OR NVIDIA stock OR AI chips",
        discovery_hint="high-beta AI semiconductor equity",
        broker_search_terms=("NVDA", "NVIDIA"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "NVIDIA OR NVDA stock", 1.0, 24),
            NewsTopic("earnings", "NVIDIA earnings OR NVDA guidance", 1.3, 48),
            NewsTopic("ai_demand", "AI chip demand OR GPU demand OR data center capex", 1.2, 48),
            NewsTopic("china_export", "US chip export controls OR NVIDIA China", 1.2, 72),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("competitors", "AMD MI300 OR Intel Gaudi OR AI chip competition", 0.9, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="AMD",
        display_name="AMD",
        asset_class="equity",
        data_symbol="AMD",
        news_query="AMD OR Advanced Micro Devices stock OR semiconductors",
        discovery_hint="liquid semiconductor equity",
        broker_search_terms=("AMD", "Advanced Micro Devices"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "AMD OR Advanced Micro Devices stock", 1.0, 24),
            NewsTopic("earnings", "AMD earnings OR AMD guidance", 1.2, 48),
            NewsTopic("ai_gpu", "AMD MI300 OR AMD AI accelerator", 1.1, 48),
            NewsTopic("data_center", "data center chip demand OR server CPU", 1.0, 72),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("competitors", "NVIDIA competition OR Intel datacenter", 0.9, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="TSLA",
        display_name="Tesla",
        asset_class="equity",
        data_symbol="TSLA",
        news_query="TSLA OR Tesla stock OR EV market",
        discovery_hint="high-volatility mega-cap equity",
        broker_search_terms=("TSLA", "Tesla"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Tesla OR TSLA stock", 1.0, 24),
            NewsTopic("deliveries", "Tesla deliveries OR Tesla production numbers", 1.2, 48),
            NewsTopic("earnings", "Tesla earnings OR TSLA guidance", 1.2, 48),
            NewsTopic("china_ev", "China EV demand OR BYD competition", 1.0, 72),
            NewsTopic("musk", "Elon Musk OR Tesla CEO news", 0.9, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US consumer demand", 0.8, 24),
        ),
    ),
    InstrumentDefinition(
        symbol="AMZN",
        display_name="Amazon",
        asset_class="equity",
        data_symbol="AMZN",
        news_query="AMZN OR Amazon stock OR AWS",
        discovery_hint="liquid mega-cap consumer and cloud equity",
        broker_search_terms=("AMZN", "Amazon"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Amazon OR AMZN stock", 1.0, 24),
            NewsTopic("earnings", "Amazon earnings OR AMZN guidance", 1.2, 48),
            NewsTopic("aws", "AWS revenue OR Amazon cloud growth", 1.1, 48),
            NewsTopic("consumer", "US consumer spending OR retail sales", 0.9, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("regulation", "Amazon antitrust OR FTC Amazon", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="META",
        display_name="Meta",
        asset_class="equity",
        data_symbol="META",
        news_query="META OR Meta stock OR social media advertising",
        discovery_hint="liquid mega-cap internet equity",
        broker_search_terms=("META", "Meta Platforms"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Meta OR Meta Platforms stock", 1.0, 24),
            NewsTopic("earnings", "Meta earnings OR META guidance", 1.2, 48),
            NewsTopic("ad_market", "digital advertising market OR ad spend", 1.1, 48),
            NewsTopic("ai_reality_labs", "Meta AI OR Reality Labs spending", 0.9, 72),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("regulation", "Meta EU regulation OR DMA DSA", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="GOOGL",
        display_name="Alphabet",
        asset_class="equity",
        data_symbol="GOOGL",
        news_query="GOOGL OR Alphabet stock OR Google advertising",
        discovery_hint="liquid mega-cap internet equity",
        broker_search_terms=("GOOGL", "Alphabet", "Google"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=15,
        correlation_group="us_equity_mega",
        news_topics=(
            NewsTopic("symbol_base", "Alphabet OR Google OR GOOGL stock", 1.0, 24),
            NewsTopic("earnings", "Alphabet earnings OR GOOGL guidance", 1.2, 48),
            NewsTopic("ad_market", "search ad market OR Google Ads revenue", 1.1, 48),
            NewsTopic("ai_gemini", "Google Gemini OR Google AI", 1.0, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 0.8, 24),
            NewsTopic("regulation", "Google antitrust OR DOJ Google search", 1.0, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="SPY",
        display_name="SPDR S&P 500 ETF",
        asset_class="etf",
        data_symbol="SPY",
        news_query="SPY OR S&P 500 ETF OR US equities",
        discovery_hint="broad US equity ETF proxy",
        broker_search_terms=("SPY", "SPDR S&P 500 ETF"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=10,
        correlation_group="us_equity",
        news_topics=(
            NewsTopic("symbol_base", "S&P 500 OR SPY ETF", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR FOMC minutes OR US inflation CPI", 1.2, 24),
            NewsTopic("yields", "US Treasury yields OR 10 year yield", 1.0, 24),
            NewsTopic("earnings_season", "S&P 500 earnings OR Q earnings season", 1.0, 72),
            NewsTopic("geopolitics", "Middle East conflict OR Ukraine Russia war OR Taiwan tension", 0.8, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="QQQ",
        display_name="Invesco QQQ",
        asset_class="etf",
        data_symbol="QQQ",
        news_query="QQQ OR Nasdaq ETF OR US tech stocks",
        discovery_hint="large-cap US tech ETF proxy",
        broker_search_terms=("QQQ", "Invesco QQQ"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=10,
        correlation_group="us_equity",
        news_topics=(
            NewsTopic("symbol_base", "Nasdaq 100 OR QQQ ETF", 1.0, 24),
            NewsTopic("mega_tech", "mega cap tech earnings OR big tech", 1.2, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.2, 24),
            NewsTopic("yields", "10 year Treasury yield OR bond yields", 1.0, 24),
            NewsTopic("ai_theme", "AI stocks OR semiconductor demand", 1.0, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="IWM",
        display_name="iShares Russell 2000 ETF",
        asset_class="etf",
        data_symbol="IWM",
        news_query="IWM OR Russell 2000 ETF OR US small caps",
        discovery_hint="small-cap US equity ETF proxy",
        broker_search_terms=("IWM", "iShares Russell 2000 ETF"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=20,
        correlation_group="us_equity_small",
        news_topics=(
            NewsTopic("symbol_base", "Russell 2000 OR small caps OR IWM", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.2, 24),
            NewsTopic("regional_banks", "regional banks OR KRE ETF OR bank stress", 1.0, 48),
            NewsTopic("credit_conditions", "credit conditions OR lending standards OR SLOOS", 1.0, 72),
            NewsTopic("small_biz", "small business confidence OR NFIB survey", 0.9, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="SPX",
        display_name="S&P 500",
        asset_class="index",
        data_symbol="^GSPC",
        news_query="S&P 500 OR SPX OR US equities",
        discovery_hint="flagship US equity index",
        broker_search_terms=("US 500", "S&P 500", "SPX"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=5,
        correlation_group="us_equity",
        news_topics=(
            NewsTopic("symbol_base", "S&P 500 OR SPX index", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR FOMC OR US inflation CPI", 1.2, 24),
            NewsTopic("yields", "US Treasury yields OR 10 year yield", 1.0, 24),
            NewsTopic("earnings_season", "S&P 500 earnings OR US earnings season", 1.0, 72),
            NewsTopic("vix_risk", "VIX OR market volatility OR risk off", 0.9, 24),
        ),
    ),
    InstrumentDefinition(
        symbol="NAS100",
        display_name="Nasdaq 100",
        asset_class="index",
        data_symbol="^NDX",
        news_query="Nasdaq 100 OR NAS100 OR US tech stocks",
        discovery_hint="major US technology index",
        broker_search_terms=("US Tech 100", "Nasdaq 100", "NAS100"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=5,
        correlation_group="us_equity",
        news_topics=(
            NewsTopic("symbol_base", "Nasdaq 100 OR NDX index", 1.0, 24),
            NewsTopic("mega_tech", "mega cap tech OR FAANG OR Magnificent Seven", 1.2, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.2, 24),
            NewsTopic("yields", "10 year Treasury yield OR real yields", 1.0, 24),
            NewsTopic("ai_theme", "AI demand OR NVIDIA earnings OR semiconductors", 1.0, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="US30",
        display_name="Dow Jones Industrial Average",
        asset_class="index",
        data_symbol="^DJI",
        news_query="Dow Jones OR US30 OR Wall Street",
        discovery_hint="major blue-chip US equity index",
        broker_search_terms=("Wall Street", "Dow Jones", "US 30"),
        exchange_code="XNYS",
        tradeable_hours_utc=(13, 20),
        max_spread_bps=5,
        correlation_group="us_equity",
        news_topics=(
            NewsTopic("symbol_base", "Dow Jones OR Dow 30 OR DJIA", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.2, 24),
            NewsTopic("industrials", "industrial production OR manufacturing PMI", 1.0, 48),
            NewsTopic("earnings_season", "Dow component earnings", 0.9, 72),
            NewsTopic("trade_policy", "US tariffs OR trade policy OR China tariffs", 1.0, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="UK100",
        display_name="FTSE 100",
        asset_class="index",
        data_symbol="^FTSE",
        news_query="FTSE 100 OR UK100 OR London stocks",
        discovery_hint="major UK equity index",
        broker_search_terms=("UK 100", "FTSE 100", "UK100"),
        exchange_code="XLON",
        tradeable_hours_utc=(7, 16),
        max_spread_bps=6,
        correlation_group="uk_equity",
        news_topics=(
            NewsTopic("symbol_base", "FTSE 100 OR UK equities", 1.0, 24),
            NewsTopic("boe_macro", "Bank of England OR BoE rate decision OR UK CPI", 1.2, 24),
            NewsTopic("uk_politics", "UK government OR UK fiscal policy OR budget", 0.9, 72),
            NewsTopic("gbp_moves", "GBP weakness OR sterling rally OR pound dollar", 0.9, 48),
            NewsTopic("commodities", "oil price OR miners OR commodity prices", 0.9, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="GER40",
        display_name="DAX",
        asset_class="index",
        data_symbol="^GDAXI",
        news_query="DAX OR GER40 OR Germany stocks",
        discovery_hint="major German equity index",
        broker_search_terms=("Germany 40", "DAX", "GER40"),
        exchange_code="XFRA",
        tradeable_hours_utc=(7, 16),
        max_spread_bps=6,
        correlation_group="eu_equity",
        news_topics=(
            NewsTopic("symbol_base", "DAX OR German equities", 1.0, 24),
            NewsTopic("ecb_macro", "ECB rate decision OR eurozone inflation OR HICP", 1.2, 24),
            NewsTopic("germany_data", "German IFO OR German PMI OR industrial production", 1.0, 48),
            NewsTopic("energy_europe", "European energy crisis OR natural gas Europe", 0.9, 72),
            NewsTopic("china_export", "Germany China trade OR auto exports", 0.9, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="EURUSD",
        display_name="EUR/USD",
        asset_class="forex",
        data_symbol="EURUSD=X",
        news_query="EURUSD OR euro dollar OR forex",
        discovery_hint="major FX pair driven by rates and macro data",
        broker_search_terms=("EUR/USD", "EURUSD"),
        exchange_code="FX",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=2,
        correlation_group="fx_eur",
        # IG quotes EURUSD in 1/10000 units (1pt = 1 pip = 0.0001).
        # Yahoo quotes 1.0830, IG quotes 10830. Per review 2026-04-30-adhoc-1 #2.
        ig_price_scale=10000.0,
        news_topics=(
            NewsTopic("symbol_base", "EURUSD OR euro dollar", 1.0, 24),
            NewsTopic("ecb_macro", "ECB rate decision OR eurozone inflation OR HICP", 1.2, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI OR NFP jobs", 1.2, 24),
            NewsTopic("rate_diff", "US EU rate differential OR Bund Treasury spread", 1.0, 48),
            NewsTopic("eu_politics", "EU elections OR Italy spread OR French politics", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="GBPUSD",
        display_name="GBP/USD",
        asset_class="forex",
        data_symbol="GBPUSD=X",
        news_query="GBPUSD OR pound dollar OR forex",
        discovery_hint="major FX pair driven by rates and UK data",
        broker_search_terms=("GBP/USD", "GBPUSD", "Cable"),
        exchange_code="FX",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=2,
        correlation_group="fx_gbp",
        # 1pt = 1 pip = 0.0001. Per review 2026-04-30-adhoc-1 #2.
        ig_price_scale=10000.0,
        news_topics=(
            NewsTopic("symbol_base", "GBPUSD OR cable OR pound dollar", 1.0, 24),
            NewsTopic("boe_macro", "Bank of England OR BoE rate decision OR UK CPI", 1.2, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI OR NFP jobs", 1.2, 24),
            NewsTopic("uk_data", "UK GDP OR UK retail sales OR UK wages", 1.0, 48),
            NewsTopic("uk_politics", "UK budget OR UK fiscal OR gilt market", 0.9, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="USDJPY",
        display_name="USD/JPY",
        asset_class="forex",
        data_symbol="JPY=X",
        news_query="USDJPY OR dollar yen OR forex",
        discovery_hint="major FX pair sensitive to yields and risk sentiment",
        broker_search_terms=("USD/JPY", "USDJPY"),
        exchange_code="FX",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=2,
        correlation_group="fx_jpy",
        # USD/JPY is the FX exception: 1pt = 1 pip = 0.01 (not 0.0001) because
        # JPY is the quote currency and pip convention shifts. Yahoo quotes
        # 160.45, IG quotes 16045. Per review 2026-04-30-adhoc-1 #2.
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "USDJPY OR dollar yen", 1.0, 24),
            NewsTopic("boj_macro", "Bank of Japan OR BOJ policy OR YCC yield curve control", 1.3, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.2, 24),
            NewsTopic("yields", "US Treasury 10 year yield OR JGB yield", 1.0, 24),
            NewsTopic("mof_intervention", "Japan MOF intervention OR yen weakness", 1.2, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="AUDUSD",
        display_name="AUD/USD",
        asset_class="forex",
        data_symbol="AUDUSD=X",
        news_query="AUDUSD OR aussie dollar OR forex",
        discovery_hint="commodity-linked major FX pair",
        broker_search_terms=("AUD/USD", "AUDUSD"),
        exchange_code="FX",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=2,
        correlation_group="fx_aud",
        # 1pt = 1 pip = 0.0001. Per review 2026-04-30-adhoc-1 #2.
        ig_price_scale=10000.0,
        news_topics=(
            NewsTopic("symbol_base", "AUDUSD OR aussie dollar", 1.0, 24),
            NewsTopic("rba_macro", "RBA rate decision OR Australia CPI", 1.2, 24),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.1, 24),
            NewsTopic("china_demand", "China PMI OR China stimulus OR iron ore demand", 1.1, 48),
            NewsTopic("commodities", "iron ore price OR copper price", 0.9, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="USDCHF",
        display_name="USD/CHF",
        asset_class="forex",
        data_symbol="CHF=X",
        news_query="USDCHF OR dollar swiss franc OR forex",
        discovery_hint="major FX pair with safe-haven sensitivity",
        broker_search_terms=("USD/CHF", "USDCHF"),
        exchange_code="FX",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=2,
        correlation_group="fx_eur",
        # 1pt = 1 pip = 0.0001. Per review 2026-04-30-adhoc-1 #2.
        ig_price_scale=10000.0,
        news_topics=(
            NewsTopic("symbol_base", "USDCHF OR dollar swiss franc", 1.0, 24),
            NewsTopic("snb_macro", "SNB rate decision OR Swiss inflation", 1.2, 48),
            NewsTopic("fed_macro", "Fed rate decision OR US inflation CPI", 1.1, 24),
            NewsTopic("safe_haven", "safe haven flows OR risk off sentiment", 1.0, 24),
            NewsTopic("geopolitics", "Middle East conflict OR Ukraine war", 0.9, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="GOLD",
        display_name="Gold",
        asset_class="metal",
        data_symbol="GC=F",
        news_query="gold price OR XAUUSD OR bullion OR COMEX gold",
        discovery_hint="liquid precious metal driven by inflation and yields",
        broker_search_terms=("XAUUSD", "Spot Gold", "Gold"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=15,
        correlation_group="precious_metals",
        # IG quotes Gold in 1/10 dollars (1pt = $0.10). Yahoo quotes
        # GC=F at ~$4700, IG `MT.D.GC.FWM2.IP` quotes ~47000 → ×10.
        # Per review 2026-04-30-adhoc-1 #2 (audit deferred from #1 of
        # the 2026-04-29 review). TODO: verify against live IG dealing
        # rules `markets/MT.D.GC.FWM2.IP` on first paper-trade.
        ig_price_scale=10.0,
        news_topics=(
            NewsTopic("symbol_base", "gold price OR XAUUSD OR spot gold", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR real yields OR US CPI", 1.2, 24),
            NewsTopic("dxy", "US dollar index OR DXY strength", 1.0, 24),
            NewsTopic("central_bank_buying", "central bank gold buying OR PBOC gold reserves", 1.0, 72),
            NewsTopic("geopolitics", "Middle East war OR Ukraine conflict OR safe haven demand", 1.1, 48),
            NewsTopic("etf_flows", "GLD ETF flows OR gold ETF holdings", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="SILVER",
        display_name="Silver",
        asset_class="metal",
        data_symbol="SI=F",
        news_query="silver price OR XAGUSD OR bullion OR COMEX silver",
        discovery_hint="precious and industrial metal with higher beta than gold",
        broker_search_terms=("XAGUSD", "Spot Silver", "Silver"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=15,
        correlation_group="precious_metals",
        # IG quotes Silver in cents (1pt = 1¢). Yahoo SI=F ~$24, IG ~2400.
        # Per review 2026-04-30-adhoc-1 #2. TODO: verify on first paper-trade.
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "silver price OR XAGUSD OR spot silver", 1.0, 24),
            NewsTopic("fed_macro", "Fed rate decision OR real yields OR US CPI", 1.2, 24),
            NewsTopic("industrial_demand", "solar panel demand OR silver industrial use", 1.0, 72),
            NewsTopic("gold_beta", "gold silver ratio OR precious metals rally", 1.0, 48),
            NewsTopic("geopolitics", "Middle East war OR safe haven demand", 0.9, 48),
            NewsTopic("china_demand", "China silver imports OR China solar installations", 0.8, 72),
        ),
    ),
    InstrumentDefinition(
        symbol="COPPER",
        display_name="Copper",
        asset_class="metal",
        data_symbol="HG=F",
        news_query="copper price OR industrial metals OR COMEX copper",
        discovery_hint="industrial metal tied to growth and China demand",
        broker_search_terms=("Copper", "COMEX Copper"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=25,
        correlation_group="industrial_metals",
        # IG quotes Copper in cents (1pt = 1¢). Yahoo HG=F ~$4.50, IG ~450.
        # Per review 2026-04-30-adhoc-1 #2. TODO: verify on first paper-trade.
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "copper price OR COMEX copper OR LME copper", 1.0, 24),
            NewsTopic("china_demand", "China property OR China stimulus OR China PMI", 1.2, 48),
            NewsTopic("supply", "Chile copper mine OR Peru copper strike OR BHP copper", 1.0, 72),
            NewsTopic("ev_transition", "EV demand OR energy transition copper", 1.0, 72),
            NewsTopic("fed_macro", "Fed rate decision OR global growth PMI", 0.9, 24),
            NewsTopic("dxy", "US dollar index OR DXY", 0.8, 24),
        ),
    ),
    InstrumentDefinition(
        symbol="PLATINUM",
        display_name="Platinum",
        asset_class="metal",
        data_symbol="PL=F",
        news_query="platinum price OR precious metals OR NYMEX platinum",
        discovery_hint="precious metal with industrial demand exposure",
        broker_search_terms=("Platinum", "Spot Platinum"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=15,
        correlation_group="precious_metals",
        # IG quotes Platinum in 1/10 dollars (1pt = $0.10), like Gold.
        # Yahoo PL=F ~$1000, IG ~10000. Per review 2026-04-30-adhoc-1 #2.
        # TODO: verify on first paper-trade.
        ig_price_scale=10.0,
        news_topics=(
            NewsTopic("symbol_base", "platinum price OR XPTUSD OR spot platinum", 1.0, 24),
            NewsTopic("auto_demand", "auto catalyst demand OR vehicle sales", 1.1, 72),
            NewsTopic("south_africa", "South Africa platinum mine OR Amplats OR Sibanye", 1.0, 72),
            NewsTopic("fed_macro", "Fed rate decision OR real yields OR US CPI", 0.9, 24),
            NewsTopic("hydrogen", "hydrogen economy OR fuel cell demand", 0.8, 96),
            NewsTopic("gold_beta", "precious metals rally OR gold price", 0.8, 48),
        ),
    ),
    InstrumentDefinition(
        symbol="WTI",
        display_name="WTI Crude Oil",
        asset_class="energy",
        data_symbol="CL=F",
        news_query="WTI crude oil price OR US crude OR oil market",
        discovery_hint="benchmark US crude oil market",
        broker_search_terms=("US Crude", "WTI Crude Oil", "WTI"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=30,
        correlation_group="energy_oil",
        # IG quotes WTI in cents (e.g. 6542 = $65.42); Yahoo quotes $65.42.
        # Plan stop_distance is computed in dollars, IG min in points → ×100.
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "WTI crude oil price OR US crude", 1.0, 24),
            NewsTopic("opec", "OPEC production OR OPEC+ quota", 1.0, 48),
            NewsTopic("iran", "Iran oil OR Strait of Hormuz OR Iran sanctions", 1.2, 12),
            NewsTopic("russia", "Russia oil OR Ukraine energy OR oil price cap", 1.2, 12),
            NewsTopic("inventories", "EIA crude inventory OR API crude stocks", 0.8, 72),
            NewsTopic("demand_china", "China oil demand OR Chinese refinery", 0.8, 48),
            NewsTopic("shipping", "tanker attack OR oil tanker OR Red Sea", 1.0, 24),
        ),
    ),
    InstrumentDefinition(
        symbol="BRENT",
        display_name="Brent Crude Oil",
        asset_class="energy",
        data_symbol="BZ=F",
        news_query="Brent crude oil price OR Brent oil OR oil market",
        discovery_hint="benchmark global crude oil market",
        broker_search_terms=("Brent Crude", "UK Oil", "Brent"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=30,
        correlation_group="energy_oil",
        # IG quotes Brent in cents; Yahoo in dollars (×100).
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "Brent crude oil price OR Brent oil", 1.0, 24),
            NewsTopic("opec", "OPEC production OR OPEC+ quota OR Saudi Arabia oil", 1.0, 48),
            NewsTopic("middle_east", "Middle East conflict OR Iran Israel OR Red Sea shipping", 1.3, 12),
            NewsTopic("russia", "Russia oil OR Ukraine energy OR oil price cap", 1.2, 12),
            NewsTopic("inventories", "EIA crude inventory OR oil stocks", 0.8, 72),
            NewsTopic("demand_europe", "European oil demand OR refinery runs", 0.8, 48),
            NewsTopic("shipping", "oil tanker OR Red Sea OR Strait of Hormuz", 1.0, 24),
        ),
    ),
    InstrumentDefinition(
        symbol="NATGAS",
        display_name="Natural Gas",
        asset_class="energy",
        data_symbol="NG=F",
        news_query="natural gas price OR nat gas OR Henry Hub energy",
        discovery_hint="volatile energy market tied to weather and storage data",
        broker_search_terms=("Natural Gas", "Nat Gas", "Henry Hub"),
        exchange_code="CMES",
        tradeable_hours_utc=(0, 24),
        max_spread_bps=40,
        correlation_group="energy_gas",
        # IG quotes NATGAS in cents (e.g. 268 = $2.68); Yahoo in dollars.
        # 2026-04-29 incident: ATTACHED_ORDER_LEVEL_ERROR with stop_distance
        # 0.0134 (~$0.013) vs IG 1pt min — same unit mismatch class as WTI.
        ig_price_scale=100.0,
        news_topics=(
            NewsTopic("symbol_base", "Henry Hub natural gas OR nat gas price", 1.0, 24),
            NewsTopic("weather", "US weather forecast OR heating cooling demand OR polar vortex", 1.3, 24),
            NewsTopic("storage", "EIA natural gas storage OR gas inventories", 1.1, 72),
            NewsTopic("lng_exports", "US LNG exports OR LNG terminal OR Freeport LNG", 1.0, 72),
            NewsTopic("europe_gas", "European gas OR TTF gas price OR EU storage", 0.9, 48),
            NewsTopic("production", "US shale gas production OR Appalachia gas", 0.8, 96),
        ),
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
