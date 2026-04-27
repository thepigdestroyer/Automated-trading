from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

import pandas as pd
import requests
import yfinance as yf

try:
    import exchange_calendars as xcals
except Exception:  # pragma: no cover
    xcals = None

from t212_cfd_bot.config import Settings
from t212_cfd_bot.instruments import get_instrument
from t212_cfd_bot.models import EarningsEvent, EconomicEvent

EQUITY_ASSET_CLASSES = {"equity"}

LOGGER = logging.getLogger(__name__)

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}


class EconomicCalendarClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: tuple[float, list[dict]] | None = None
        self._cache_ttl_seconds = 900.0

    def events_next_hours(
        self,
        *,
        hours: int,
        impact_min: Literal["low", "medium", "high"] = "medium",
        currencies: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> list[EconomicEvent]:
        if not self.settings.econ_calendar_enabled:
            return []
        raw = self._fetch_raw()
        now = now or datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        currency_set = {c.upper() for c in (currencies or [])} or None
        out: list[EconomicEvent] = []
        for item in raw:
            impact = str(item.get("impact", "")).strip().lower()
            if _IMPACT_RANK.get(impact, -1) < _IMPACT_RANK.get(impact_min, 1):
                continue
            when = self._parse_dt(item.get("date"))
            if when is None or when < now or when > horizon:
                continue
            currency = str(item.get("country", "")).strip().upper()
            if currency_set and currency not in currency_set:
                continue
            out.append(
                EconomicEvent(
                    title=str(item.get("title", "")).strip() or "untitled",
                    when=when.isoformat(),
                    impact=impact if impact in {"low", "medium", "high"} else "medium",
                    currency=currency,
                    country=currency or None,
                    source="forexfactory",
                )
            )
        return sorted(out, key=lambda e: e.when)

    def _fetch_raw(self) -> list[dict]:
        now_ts = time.monotonic()
        if self._cache and now_ts - self._cache[0] < self._cache_ttl_seconds:
            return self._cache[1]
        try:
            response = requests.get(FF_CALENDAR_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                self._cache = (now_ts, data)
                return data
        except Exception as exc:
            LOGGER.warning("Economic calendar fetch failed: %s", exc)
        return self._cache[1] if self._cache else []

    @staticmethod
    def _parse_dt(raw) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        text = str(raw).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


class EarningsCalendarClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, EarningsEvent | None]] = {}
        self._cache_ttl_seconds = 6 * 3600.0

    def next_earnings_date(self, symbol: str) -> EarningsEvent | None:
        symbol = symbol.strip().upper()
        cached = self._cache.get(symbol)
        if cached and (time.monotonic() - cached[0] < self._cache_ttl_seconds):
            return cached[1]
        try:
            instrument = get_instrument(symbol)
            if instrument.asset_class not in EQUITY_ASSET_CLASSES:
                self._cache[symbol] = (time.monotonic(), None)
                return None
            ticker = yf.Ticker(instrument.data_symbol)
            calendar = getattr(ticker, "calendar", None)
            when: datetime | None = None
            if isinstance(calendar, pd.DataFrame) and not calendar.empty:
                col = next((c for c in calendar.columns if "Earnings Date" in str(c)), None)
                if col is not None:
                    value = calendar[col].iloc[0]
                    when = self._to_datetime(value)
            elif isinstance(calendar, dict):
                value = calendar.get("Earnings Date")
                if isinstance(value, (list, tuple)) and value:
                    value = value[0]
                when = self._to_datetime(value)
            event: EarningsEvent | None = None
            if when is not None:
                event = EarningsEvent(symbol=symbol, date=when.isoformat())
        except Exception as exc:
            LOGGER.info("Earnings fetch failed for %s: %s", symbol, exc)
            event = None
        self._cache[symbol] = (time.monotonic(), event)
        return event

    @staticmethod
    def _to_datetime(value) -> datetime | None:
        if value is None:
            return None
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None


class HolidayCalendar:
    def __init__(self):
        if xcals is None:
            self._available = False
            return
        self._available = True
        self._cache: dict[str, object] = {}

    def is_session_open(self, exchange_code: str, ts: datetime) -> bool:
        if not self._available:
            return True
        cal = self._get(exchange_code)
        if cal is None:
            return True
        try:
            return bool(cal.is_trading_minute(pd.Timestamp(ts)))
        except Exception:
            return True

    def is_half_day(self, exchange_code: str, ts: datetime) -> bool:
        # conservative default: never report half-day unless a real-calendar signal clearly shows it
        return False

    def _get(self, exchange_code: str):
        if exchange_code in self._cache:
            return self._cache[exchange_code]
        try:
            cal = xcals.get_calendar(exchange_code)
        except Exception:
            cal = None
        self._cache[exchange_code] = cal
        return cal
