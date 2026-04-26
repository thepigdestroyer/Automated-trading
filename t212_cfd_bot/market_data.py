from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from t212_cfd_bot.config import Settings
from t212_cfd_bot.indicators import build_indicator_snapshot, compute_higher_tf_fields
from t212_cfd_bot.instruments import get_instrument
from t212_cfd_bot.models import Headline, IndicatorSnapshot, ProRealTimeSymbolSnapshot, SymbolContext

INTERVAL_ALIASES = {
    "1h": "60m",
}


class YahooMarketDataClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch_history(self, data_symbol: str) -> pd.DataFrame:
        interval = INTERVAL_ALIASES.get(self.settings.price_interval, self.settings.price_interval)
        history = yf.download(
            tickers=data_symbol,
            period=f"{self.settings.price_lookback_days}d",
            interval=interval,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if history.empty:
            raise ValueError(f"no market data returned for {data_symbol}")
        if isinstance(history.columns, pd.MultiIndex):
            history.columns = history.columns.get_level_values(0)
        history = history.dropna(how="all")
        self._apply_sanity_checks(history, data_symbol)
        return history

    def _apply_sanity_checks(self, history: pd.DataFrame, data_symbol: str) -> None:
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(history.columns)
        if missing:
            raise ValueError(f"{data_symbol}: missing columns {sorted(missing)}")

        close = history["Close"].astype(float)
        returns = close.pct_change().abs().dropna()
        if not returns.empty:
            median_abs = float(returns.median())
            # 15% floor handles futures rollover gaps; 10× median catches truly corrupted ticks
            extreme_threshold = max(median_abs * 10.0, 0.15)
            bad_rows = returns[returns > extreme_threshold].index
            if len(bad_rows) > 0:
                import logging
                logging.getLogger(__name__).warning(
                    "%s: dropping %d outlier bar(s) with gap > %.1f%%",
                    data_symbol, len(bad_rows), extreme_threshold * 100,
                )
                history.drop(index=bad_rows, inplace=True, errors="ignore")

        volume = history["Volume"].astype(float)
        if (volume > 0).sum() == 0:
            raise ValueError(f"{data_symbol}: all rows have zero volume")

        non_nan = history.dropna(how="any").shape[0]
        if non_nan < int(history.shape[0] * 0.8):
            raise ValueError(f"{data_symbol}: >20% NaN rows after fetch")

    def fetch_higher_tf(self, data_symbol: str) -> dict[str, Any]:
        if not self.settings.higher_tf_enabled:
            return {"daily_sma_200": None, "daily_sma_200_distance_bps": None, "weekly_return_5w": None}
        try:
            daily = yf.download(
                tickers=data_symbol,
                period="400d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            weekly = yf.download(
                tickers=data_symbol,
                period="5y",
                interval="1wk",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            if isinstance(weekly.columns, pd.MultiIndex):
                weekly.columns = weekly.columns.get_level_values(0)
            return compute_higher_tf_fields(daily=daily, weekly=weekly)
        except Exception:
            return {"daily_sma_200": None, "daily_sma_200_distance_bps": None, "weekly_return_5w": None}

    def build_symbol_context(
        self,
        symbol: str,
        headlines: list[Headline],
        prorealtime: ProRealTimeSymbolSnapshot | None = None,
    ) -> SymbolContext:
        instrument = get_instrument(symbol)
        history = self.fetch_history(instrument.data_symbol)
        indicators = build_indicator_snapshot(history)
        higher_tf = self.fetch_higher_tf(instrument.data_symbol)
        indicators = indicators.model_copy(update=higher_tf)
        chart_note = (
            f"{len(history)} candles from Yahoo Finance for {instrument.data_symbol} "
            f"using interval {INTERVAL_ALIASES.get(self.settings.price_interval, self.settings.price_interval)}"
        )
        if prorealtime and (prorealtime.last_price or 0.0) > 0:
            indicators = indicators.model_copy(update={"price": float(prorealtime.last_price)})
            chart_note += (
                f"; current price overlaid from ProRealTime sidecar snapshot "
                f"({prorealtime.last_price})"
            )
        return SymbolContext(
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            as_of=datetime.now(timezone.utc).isoformat(),
            indicators=indicators,
            headlines=headlines,
            chart_note=chart_note,
            prorealtime=prorealtime,
        )

    def build_fallback_symbol_context(
        self,
        *,
        symbol: str,
        fallback_price: float,
        headlines: list[Headline],
        note: str,
        prorealtime: ProRealTimeSymbolSnapshot | None = None,
    ) -> SymbolContext:
        instrument = get_instrument(symbol)
        indicators = IndicatorSnapshot(
            price=fallback_price,
            sma_20=fallback_price,
            sma_50=fallback_price,
            ema_12=fallback_price,
            ema_26=fallback_price,
            rsi_14=50.0,
            macd=0.0,
            macd_signal=0.0,
            atr_14=0.0,
            volatility_20d=0.0,
            return_1d=0.0,
            return_5d=0.0,
        )
        return SymbolContext(
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            as_of=datetime.now(timezone.utc).isoformat(),
            indicators=indicators,
            headlines=headlines,
            chart_note=note,
            prorealtime=prorealtime,
        )
