from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from trading_bot.config import Settings
from trading_bot.daily_trend import build_daily_trend
from trading_bot.indicators import build_indicator_snapshot, compute_higher_tf_fields
from trading_bot.instruments import get_instrument
from trading_bot.models import (
    DataQuality,
    Headline,
    IndicatorSnapshot,
    ProRealTimeSymbolSnapshot,
    SymbolContext,
)
from trading_bot.signals import build_signal_pack

INTERVAL_ALIASES = {
    "1h": "60m",
}


class YahooMarketDataClient:
    def __init__(self, settings: Settings, *, event_logger: Any | None = None):
        self.settings = settings
        # Optional JsonEventLogger to emit structured data_quality events when
        # outlier bars are dropped (proposal #8). Wired by TradingBot.
        self._event_logger = event_logger
        self._last_quality: DataQuality | None = None

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
        self._last_quality = self._apply_sanity_checks(history, data_symbol)
        return history

    def _apply_sanity_checks(self, history: pd.DataFrame, data_symbol: str) -> DataQuality:
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(history.columns)
        if missing:
            raise ValueError(f"{data_symbol}: missing columns {sorted(missing)}")

        bars_total = int(history.shape[0])
        bars_imputed = 0
        max_gap_pct: float | None = None

        close = history["Close"].astype(float)
        signed_returns = close.pct_change()
        returns = signed_returns.abs().dropna()
        if not returns.empty:
            median_abs = float(returns.median())
            # 15% floor handles futures rollover gaps; 10x median catches truly
            # corrupted ticks. Energy needs a wider floor (large normal gaps).
            floor_pct = 0.30 if data_symbol in {"CL=F", "BZ=F", "NG=F"} else 0.15
            extreme_threshold = max(median_abs * 10.0, floor_pct)
            # An extreme return is a property of a *pair* of bars, not of one
            # bar. A single corrupted tick therefore shows up twice: the jump
            # into it and the reversal back out. Count the corrupted BAR only
            # -- if the next return is also extreme and of the opposite sign,
            # that bar is genuine and merely follows a bad predecessor.
            # Imputing it too would discard good data and double-count.
            extreme_mask = (signed_returns.abs() > extreme_threshold).to_numpy()
            signs = np.sign(signed_returns.to_numpy())
            bad_positions: list[int] = []
            skip_next = False
            for pos in np.flatnonzero(extreme_mask):
                if skip_next:
                    skip_next = False
                    continue
                bad_positions.append(int(pos))
                nxt = int(pos) + 1
                skip_next = (
                    nxt < len(extreme_mask)
                    and bool(extreme_mask[nxt])
                    and signs[nxt] != signs[pos]
                )
            bad_rows = history.index[bad_positions]
            if len(bad_rows) > 0:
                import logging

                bars_imputed = int(len(bad_rows))
                max_gap_pct = round(float(returns.loc[bad_rows].max()) * 100.0, 2)
                logging.getLogger(__name__).warning(
                    "%s: imputing %d outlier bar(s) with gap > %.1f%% "
                    "(index preserved)",
                    data_symbol, bars_imputed, extreme_threshold * 100,
                )
                if self._event_logger is not None:
                    try:
                        last_good_ts = (
                            history.index[-1].isoformat()
                            if len(history.index) > 0 else None
                        )
                        self._event_logger.log(
                            "data_quality",
                            {
                                "symbol": data_symbol,
                                "bars_imputed": bars_imputed,
                                # supervisor.rules.data_feed_degraded keys on
                                # `bars_dropped`; keep emitting it (same count)
                                # so that rule keeps firing. Dropping the key
                                # would silently disable it.
                                "bars_dropped": bars_imputed,
                                "threshold_pct": round(extreme_threshold * 100.0, 2),
                                "gap_pct_max": max_gap_pct,
                                "last_good_ts": last_good_ts,
                            },
                        )
                    except Exception:
                        pass
                # Mark-don't-drop: blank the corrupt OHLC then interpolate on
                # the time axis. This preserves index continuity so rolling /
                # EWM windows stay aligned to real elapsed time.
                ohlc = ["Open", "High", "Low", "Close"]
                history.loc[bad_rows, ohlc] = np.nan
                history[ohlc] = (
                    history[ohlc]
                    .interpolate(method="time", limit_direction="both")
                    .ffill()
                    .bfill()
                )

        # Forex tickers (e.g. JPY=X, EURUSD=X) have no centralised volume.
        is_forex = data_symbol.endswith("=X")
        volume = history["Volume"].astype(float)
        if not is_forex and (volume > 0).sum() == 0:
            raise ValueError(f"{data_symbol}: all rows have zero volume")

        non_nan = history.dropna(how="any").shape[0]
        if non_nan < int(history.shape[0] * 0.8):
            raise ValueError(f"{data_symbol}: >20% NaN rows after fetch")

        degraded_ratio = (bars_imputed / bars_total) if bars_total else 0.0
        return DataQuality(
            bars_total=bars_total,
            bars_imputed=bars_imputed,
            max_gap_pct=max_gap_pct,
            is_degraded=bars_imputed > 0,
            note=(
                f"{bars_imputed}/{bars_total} bars imputed "
                f"({degraded_ratio * 100:.1f}%)"
                if bars_imputed else None
            ),
        )

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
            fields = compute_higher_tf_fields(daily=daily, weekly=weekly)
            # Phase 2 strategy pivot: build the daily-trend classification from
            # the SAME daily frame (no extra download). Stash it for
            # build_symbol_context to attach. Best-effort.
            try:
                self._last_daily_trend = build_daily_trend(daily)
            except Exception:  # pragma: no cover - defensive
                self._last_daily_trend = None
            return fields
        except Exception:
            self._last_daily_trend = None
            return {"daily_sma_200": None, "daily_sma_200_distance_bps": None, "weekly_return_5w": None}

    def build_symbol_context(
        self,
        symbol: str,
        headlines: list[Headline],
        prorealtime: ProRealTimeSymbolSnapshot | None = None,
    ) -> SymbolContext:
        instrument = get_instrument(symbol)
        history = self.fetch_history(instrument.data_symbol)
        data_quality = self._last_quality
        indicators = build_indicator_snapshot(history)
        self._last_daily_trend = None
        higher_tf = self.fetch_higher_tf(instrument.data_symbol)
        indicators = indicators.model_copy(update=higher_tf)
        daily_trend = getattr(self, "_last_daily_trend", None)
        # Phase 1.1/1.2: deterministic pre-classified signal pack, computed
        # from the same frame (after higher-TF fields are merged so trend
        # classification can use the 200d distance). Best-effort: a failure
        # here must not blank the whole symbol context.
        try:
            signals = build_signal_pack(history, indicators)
        except Exception:  # pragma: no cover - defensive
            signals = None
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
            signals=signals,
            daily_trend=daily_trend,
            headlines=headlines,
            chart_note=chart_note,
            prorealtime=prorealtime,
            data_quality=data_quality,
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
