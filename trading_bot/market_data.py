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

# An extreme bar is only treated as corrupt if price comes back to the
# pre-spike level within this many following bars. A move that holds longer is
# a real move (earnings gap, futures rollover) and is left intact.
SPIKE_REVERT_WINDOW_BARS = 3

# Only repairs inside this many trailing bars set `DataQuality.is_degraded`.
# 50 is the longest rolling window in the indicator snapshot (sma_50); a bar
# further back than that feeds no current indicator value.
DEGRADATION_LOOKBACK_BARS = 50


class YahooMarketDataClient:
    def __init__(self, settings: Settings, *, event_logger: Any | None = None):
        self.settings = settings
        # Optional JsonEventLogger to emit structured data_quality events when
        # outlier bars are imputed -- neutralised in place, never dropped, see
        # ADR 0006 (proposal #8). Wired by TradingBot.
        self._event_logger = event_logger
        self._last_quality: DataQuality | None = None

    def fetch_history(self, data_symbol: str) -> pd.DataFrame:
        # Clear first: a raising fetch must not leave the PREVIOUS symbol's
        # report visible to callers, which would mis-attribute degradation
        # across symbols in a field the risk gate consumes.
        self._last_quality = None
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
        # Wholly-empty bars (Yahoo emits these for halted / missing intraday
        # sessions) carry no data, so they are still removed -- but that DOES
        # break index contiguity, which is the exact failure mode ADR 0006
        # exists to eliminate. Count them and surface the count as a TYPED
        # field (DataQuality.bars_dropped_empty), folded into `is_degraded`;
        # the note keeps a human-readable copy but is not the contract.
        # `bars_total` is measured after this drop. See ADR 0006.
        rows_before_empty_drop = int(history.shape[0])
        empty_rows = history.isna().all(axis=1).to_numpy()
        history = history.dropna(how="all")
        bars_dropped_empty = rows_before_empty_drop - int(history.shape[0])
        # A dropped row only gaps the indicator window when fewer than
        # DEGRADATION_LOOKBACK_BARS kept bars follow it.
        kept_after = np.cumsum((~empty_rows)[::-1])[::-1]
        bars_dropped_empty_stale = int(
            (empty_rows & (kept_after >= DEGRADATION_LOOKBACK_BARS)).sum()
        )
        self._last_quality = self._apply_sanity_checks(
            history,
            data_symbol,
            bars_dropped_empty=bars_dropped_empty,
            bars_dropped_empty_stale=bars_dropped_empty_stale,
        )
        return history

    def _apply_sanity_checks(
        self,
        history: pd.DataFrame,
        data_symbol: str,
        *,
        bars_dropped_empty: int = 0,
        bars_dropped_empty_stale: int = 0,
    ) -> DataQuality:
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(history.columns)
        if missing:
            raise ValueError(f"{data_symbol}: missing columns {sorted(missing)}")

        ohlc = ["Open", "High", "Low", "Close"]
        bars_total = int(history.shape[0])

        # --- Guards run BEFORE any imputation. -----------------------------
        # The interpolation below fills *every* NaN in the OHLC block, not just
        # the outlier rows. Evaluating the >20%-NaN guard afterwards therefore
        # let a mostly-empty frame sail through whenever an outlier happened to
        # be present, because the imputation had already erased the evidence.
        # Order between the two raises is preserved from the previous version.
        #
        # Forex tickers (e.g. JPY=X, EURUSD=X) have no centralised volume.
        is_forex = data_symbol.endswith("=X")
        volume = history["Volume"].astype(float)
        if not is_forex and (volume > 0).sum() == 0:
            raise ValueError(f"{data_symbol}: all rows have zero volume")

        non_nan = history.dropna(how="any").shape[0]
        if non_nan < int(history.shape[0] * 0.8):
            raise ValueError(f"{data_symbol}: >20% NaN rows after fetch")

        # Pre-existing NaN bars get fabricated by the same interpolation pass
        # as detected outliers, so they count towards `bars_imputed` too --
        # otherwise the report understates how much of the frame is invented.
        pre_nan_mask = history[ohlc].isna().any(axis=1).to_numpy()
        # `pre_nan_mask` deliberately covers OHLC only, but the >20%-NaN guard
        # above covers EVERY column, so a NaN-Volume row that stays under the
        # ceiling used to pass with bars_imputed=0, is_degraded=False,
        # note=None -- and the NaN was never filled, so it flowed straight into
        # the volume indicators. Decision (ADR 0006): fill Volume by the same
        # interpolation pass and count it in its OWN field. Folding it into
        # `bars_imputed` would falsify that field's documented meaning
        # ("fabricated OHLC", see DataQuality.imputed_ratio).
        pre_nan_volume = int(history["Volume"].isna().sum())

        max_gap_pct: float | None = None
        extreme_threshold: float | None = None
        bad_positions: list[int] = []
        unconfirmed_positions: list[int] = []
        level_shift_positions: list[int] = []

        close = history["Close"].astype(float)
        returns = close.pct_change().abs()
        known_returns = returns.dropna()
        if not known_returns.empty:
            median_abs = float(known_returns.median())
            # 15% floor handles futures rollover gaps; 10x median catches truly
            # corrupted ticks. Energy needs a wider floor (large normal gaps).
            floor_pct = 0.30 if data_symbol in {"CL=F", "BZ=F", "NG=F"} else 0.15
            extreme_threshold = max(median_abs * 10.0, floor_pct)
            # An extreme return only says that two adjacent bars disagree. What
            # separates a corrupt tick from a real move is what happens NEXT:
            #
            #  * spike: price comes back to the pre-spike level within
            #    SPIKE_REVERT_WINDOW_BARS. Every bar of the excursion is
            #    imputed (this covers consecutive and same-direction corrupt
            #    bursts); the bar that reverts is real and left alone.
            #  * level shift: the new level holds for the whole window. That is
            #    a real move -- measured: AMD's +15.7% earnings gap was imputed
            #    and flagged the symbol degraded for ~90 days -- so it is left
            #    intact and not counted.
            #  * unconfirmed: the move is in the newest bars and the window has
            #    not elapsed. Left intact (fabricating the live edge would show
            #    a stale pre-gap price after a real gap) but counted towards
            #    `is_degraded`, so new entries need the higher confidence bar.
            #
            # "Reverts" means back within the threshold of the pre-spike close
            # AND at least half of the jump retraced. The second clause keeps a
            # real gap just over the floor (e.g. +15.7%) that drifts back a
            # little (+14.9%) from reading as a reversion.
            extreme_mask = returns.to_numpy() > extreme_threshold
            closes = close.to_numpy()
            n_bars = len(closes)
            next_allowed = 0
            for raw_pos in np.flatnonzero(extreme_mask):
                pos = int(raw_pos)
                if pos < next_allowed:
                    continue
                ref = float(closes[pos - 1]) if pos >= 1 else float("nan")
                if not (np.isfinite(ref) and ref > 0):
                    # No usable pre-spike level (a zero or negative close):
                    # reversion cannot be measured, so keep the conservative
                    # behaviour and impute the flagged bar alone.
                    bad_positions.append(pos)
                    next_allowed = pos + 1
                    continue
                jump = abs(float(closes[pos]) - ref)
                window_end = min(n_bars, pos + 1 + SPIKE_REVERT_WINDOW_BARS)
                recovery: int | None = None
                for k in range(pos + 1, window_end):
                    candidate = float(closes[k])
                    if (
                        np.isfinite(candidate)
                        and abs(candidate / ref - 1.0) <= extreme_threshold
                        and abs(candidate - ref) <= 0.5 * jump
                    ):
                        recovery = k
                        break
                if recovery is not None:
                    bad_positions.extend(range(pos, recovery))
                    # The recovery bar's own return is measured off the corrupt
                    # bar before it, so it is extreme by construction.
                    next_allowed = recovery + 1
                elif window_end - (pos + 1) < SPIKE_REVERT_WINDOW_BARS:
                    unconfirmed_positions.append(pos)
                    # Everything after this bar is inside the same open window.
                    next_allowed = n_bars
                else:
                    level_shift_positions.append(pos)
                    next_allowed = pos + 1
            if level_shift_positions:
                import logging

                logging.getLogger(__name__).debug(
                    "%s: %d extreme move(s) held for %d bars; treated as real "
                    "level shifts and left intact",
                    data_symbol, len(level_shift_positions), SPIKE_REVERT_WINDOW_BARS,
                )
            flagged_positions = bad_positions + unconfirmed_positions
            if flagged_positions:
                # A zero previous close makes pct_change() infinite. `inf` is
                # not valid JSON: pydantic's model_dump_json() renders it as
                # `null` (losing the signal) and json.dumps writes the
                # non-standard literal `Infinity` into events.jsonl.
                #
                # Drop the non-finite gaps BEFORE taking the max, not after:
                # np.nanmax([1.0, inf]) is inf, so a single zero-close bar used
                # to blank the whole field and discard a co-occurring real
                # spike (measured: a genuine 791.97% gap reported as None).
                # None now means "every flagged gap was non-finite".
                gaps = returns.to_numpy()[flagged_positions]
                finite = gaps[np.isfinite(gaps)]
                max_gap_pct = (
                    round(float(finite.max()) * 100.0, 2) if finite.size else None
                )

        outlier_mask = np.zeros(bars_total, dtype=bool)
        if bad_positions:
            outlier_mask[bad_positions] = True
        # Two possibly-overlapping sets; report them disjointly so the parts
        # sum to the whole.
        bars_outlier = int(outlier_mask.sum())
        bars_nan_prefilled = int((pre_nan_mask & ~outlier_mask).sum())
        bars_imputed = bars_outlier + bars_nan_prefilled
        # Every NaN volume is filled by the pass below, so the pre-count is the
        # filled count. Kept OUT of `bars_imputed`: that field means OHLC.
        bars_volume_filled = pre_nan_volume
        bars_unconfirmed = len(unconfirmed_positions)

        # Degradation must be recent to matter: a repair older than the
        # indicator lookback feeds no current indicator, so it is reported in a
        # *_stale count but does not set is_degraded. Without this one imputed
        # bar kept a symbol on the higher entry-confidence floor for as long as
        # it stayed in the ~630-bar window (~90 days).
        stale_mask = np.zeros(bars_total, dtype=bool)
        stale_mask[: max(0, bars_total - DEGRADATION_LOOKBACK_BARS)] = True
        bars_imputed_stale = int(((outlier_mask | pre_nan_mask) & stale_mask).sum())
        volume_nan_mask = history["Volume"].isna().to_numpy()
        bars_volume_filled_stale = int((volume_nan_mask & stale_mask).sum())

        if bars_imputed or bars_volume_filled:
            import logging

            threshold_pct = (
                round(extreme_threshold * 100.0, 2)
                if extreme_threshold is not None else None
            )
            logging.getLogger(__name__).warning(
                "%s: imputing %d bar(s) in place -- %d outlier (gap > %s%%), "
                "%d pre-existing NaN, %d NaN volume (index preserved)",
                data_symbol, bars_imputed, bars_outlier, threshold_pct,
                bars_nan_prefilled, bars_volume_filled,
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
                            # `bars_dropped` is the LEGACY key and carries the
                            # OUTLIER count only -- deliberately NOT
                            # `bars_imputed`. supervisor.rules.data_feed_degraded
                            # reads int(data.get("bars_dropped", 0)) and breaks
                            # its streak on <= 0, so this key is the trigger for
                            # a live alerting rule whose documented meaning is
                            # "outlier bars". Feeding it the wider
                            # outlier+pre-existing-NaN total silently widened
                            # that rule: a frame with 3 NaN-OHLC bars and NO
                            # outlier emitted no event before and now emitted
                            # bars_dropped=3. Keep it narrow; the richer signals
                            # are the other keys. Do NOT "simplify" the two to
                            # one value -- pinned by tests/test_data_quality.py::
                            # test_event_bars_dropped_counts_outliers_only
                            "bars_dropped": bars_outlier,
                            "bars_nan_prefilled": bars_nan_prefilled,
                            "bars_volume_filled": bars_volume_filled,
                            "bars_dropped_empty": bars_dropped_empty,
                            "bars_imputed_stale": bars_imputed_stale,
                            "bars_unconfirmed": bars_unconfirmed,
                            "threshold_pct": threshold_pct,
                            "gap_pct_max": max_gap_pct,
                            "last_good_ts": last_good_ts,
                        },
                    )
                except Exception:
                    pass
            # Mark-don't-drop: blank the corrupt OHLC then interpolate on
            # the time axis. This preserves index continuity so rolling /
            # EWM windows stay aligned to real elapsed time.
            if bad_positions:
                history.iloc[
                    bad_positions,
                    [history.columns.get_loc(col) for col in ohlc],
                ] = np.nan
            # Volume is filled by the same pass (see `pre_nan_volume` above).
            # Only NaN is filled -- no Volume value is ever overwritten, and the
            # outlier blanking above touches OHLC only, so a corrupt-price bar
            # keeps its real traded volume.
            fill_cols = (ohlc + ["Volume"]) if bars_volume_filled else list(ohlc)
            history[fill_cols] = (
                history[fill_cols]
                .interpolate(method="time", limit_direction="both")
                .ffill()
                .bfill()
            )

        degraded_ratio = (bars_imputed / bars_total) if bars_total else 0.0
        note_parts: list[str] = []
        if bars_imputed:
            note_parts.append(
                f"{bars_imputed}/{bars_total} bars imputed "
                f"({degraded_ratio * 100:.1f}%)"
            )
            if bars_nan_prefilled:
                note_parts.append(f"{bars_nan_prefilled} already NaN on arrival")
        if bars_volume_filled:
            note_parts.append(f"{bars_volume_filled} NaN volume bar(s) interpolated")
        if bars_dropped_empty:
            note_parts.append(
                f"{bars_dropped_empty} wholly-empty bar(s) dropped before checks"
            )
        stale_total = bars_imputed_stale + bars_volume_filled_stale + bars_dropped_empty_stale
        if stale_total:
            note_parts.append(
                f"{stale_total} of these older than the last "
                f"{DEGRADATION_LOOKBACK_BARS} bars (not counted as degraded)"
            )
        if bars_unconfirmed:
            note_parts.append(
                f"{bars_unconfirmed} extreme move(s) in the newest bars not yet "
                f"confirmed as spike or level shift"
            )
        return DataQuality(
            bars_total=bars_total,
            bars_imputed=bars_imputed,
            # Empty-bar drops gap the index, so they ARE degradation even though
            # they fabricate nothing. Folded into the flag, not just the note.
            bars_dropped_empty=bars_dropped_empty,
            bars_volume_filled=bars_volume_filled,
            bars_imputed_stale=bars_imputed_stale,
            bars_dropped_empty_stale=bars_dropped_empty_stale,
            bars_volume_filled_stale=bars_volume_filled_stale,
            bars_unconfirmed=bars_unconfirmed,
            max_gap_pct=max_gap_pct,
            is_degraded=(
                bars_imputed > bars_imputed_stale
                or bars_dropped_empty > bars_dropped_empty_stale
                or bars_volume_filled > bars_volume_filled_stale
                or bars_unconfirmed > 0
            ),
            note="; ".join(note_parts) if note_parts else None,
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
            # This context has NO price history at all -- every indicator above
            # is a flat placeholder derived from a single fallback price. Left
            # as None it would read as "quality unknown", which a risk gate
            # would treat as not-degraded. It is maximally degraded.
            data_quality=DataQuality(bars_total=0, is_degraded=True, note=note),
        )
