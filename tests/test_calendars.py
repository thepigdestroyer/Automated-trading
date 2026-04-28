from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pandas as pd

from trading_bot.calendars import EconomicCalendarClient, EarningsCalendarClient, HolidayCalendar
from trading_bot.config import Settings


def test_econ_calendar_filters_by_impact_and_window():
    s = Settings().model_copy(update={"broker": "paper"})
    client = EconomicCalendarClient(s)
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    sample = [
        {"title": "FOMC decision", "country": "USD", "date": now + timedelta(hours=6), "impact": "High"},
        {"title": "Building permits", "country": "USD", "date": now + timedelta(hours=2), "impact": "Low"},
        {"title": "ECB rate", "country": "EUR", "date": now + timedelta(hours=20), "impact": "High"},
        {"title": "CPI", "country": "CAD", "date": now - timedelta(hours=1), "impact": "High"},  # past
        {"title": "Distant event", "country": "USD", "date": now + timedelta(hours=48), "impact": "High"},  # too far
    ]
    with patch.object(client, "_fetch_raw", return_value=sample):
        events = client.events_next_hours(hours=24, impact_min="medium", currencies=["USD", "EUR"], now=now)
    titles = {e.title for e in events}
    assert "FOMC decision" in titles
    assert "ECB rate" in titles
    assert "Building permits" not in titles
    assert "Distant event" not in titles
    assert "CPI" not in titles


def test_earnings_calendar_reads_yf_calendar():
    s = Settings().model_copy(update={"broker": "paper"})
    client = EarningsCalendarClient(s)
    fake = MagicMock()
    fake.calendar = pd.DataFrame({"Earnings Date": [pd.Timestamp("2026-05-01")]})
    with patch("trading_bot.calendars.yf.Ticker", return_value=fake):
        event = client.next_earnings_date("AAPL")
    assert event is not None
    assert event.date.startswith("2026-05-01")


def test_holiday_calendar_is_session_open_weekday_us():
    hc = HolidayCalendar()
    # 2026-04-23 is a Thursday — US equities open
    is_open = hc.is_session_open("XNYS", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc))
    assert is_open
