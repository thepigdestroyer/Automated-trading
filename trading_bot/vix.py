from __future__ import annotations

import logging
from datetime import datetime, timezone

import yfinance as yf

from trading_bot.config import Settings
from trading_bot.models import VolRegimeContext

LOGGER = logging.getLogger(__name__)


class VixContextClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self) -> VolRegimeContext:
        as_of = datetime.now(timezone.utc).isoformat()
        if not self.settings.vix_context_enabled:
            return VolRegimeContext(as_of=as_of)

        spot = self._last_close("^VIX")
        vix_9d = self._last_close("^VIX9D")
        vix_3m = self._last_close("^VIX3M")
        return VolRegimeContext(
            as_of=as_of,
            vix_spot=spot,
            vix_9d=vix_9d,
            vix_3m=vix_3m,
            term_structure_flag=self._term_structure(spot, vix_3m),
        )

    def _last_close(self, ticker: str) -> float | None:
        try:
            frame = yf.download(
                tickers=ticker,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if frame.empty:
                return None
            close_col = frame["Close"]
            if hasattr(close_col, "columns"):
                close_col = close_col.iloc[:, 0]
            closes = close_col.dropna()
            if closes.empty:
                return None
            val = closes.iloc[-1]
            return float(val.iloc[0] if hasattr(val, "iloc") else val)
        except Exception as exc:
            LOGGER.warning("VIX fetch failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _term_structure(spot: float | None, three_month: float | None) -> str:
        if spot is None or three_month is None:
            return "unknown"
        if three_month > spot * 1.02:
            return "contango"
        if three_month < spot * 0.98:
            return "backwardation"
        return "flat"
