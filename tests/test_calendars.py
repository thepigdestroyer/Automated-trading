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


def test_holiday_calendar_requests_bounded_window(monkeypatch):
    """A full 1678-2100 schedule is ~119MB per exchange. We must request a
    narrow window instead."""
    from trading_bot import calendars as cal_mod

    captured: dict[str, object] = {}

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    def _fake_get_calendar(code, **kwargs):
        captured["code"] = code
        captured["kwargs"] = kwargs
        return _FakeCal()

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _fake_get_calendar)

    hc = cal_mod.HolidayCalendar(window_days=90)
    hc._get("XLON")

    assert "start" in captured["kwargs"]
    assert "end" in captured["kwargs"]


def test_holiday_calendar_window_brackets_the_queried_day(monkeypatch):
    """The bounds must actually be +/- window_days around the queried day,
    not merely present."""
    from trading_bot import calendars as cal_mod

    captured: dict[str, object] = {}

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    def _fake_get_calendar(code, **kwargs):
        captured.update(kwargs)
        return _FakeCal()

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _fake_get_calendar)

    hc = cal_mod.HolidayCalendar(window_days=90)
    hc.is_session_open("XLON", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc))

    assert captured["start"] == "2026-01-23"
    assert captured["end"] == "2026-07-22"


def test_holiday_calendar_retries_unbounded_when_window_rejected(monkeypatch):
    """Some calendars reject a narrow or out-of-range window. Retry once
    without bounds rather than losing the calendar entirely."""
    from trading_bot import calendars as cal_mod

    calls: list[dict] = []

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    def _fake_get_calendar(code, **kwargs):
        calls.append(dict(kwargs))
        if kwargs:
            raise ValueError("window out of range for this calendar")
        return _FakeCal()

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _fake_get_calendar)

    hc = cal_mod.HolidayCalendar(window_days=90)
    cal = hc._get("XLON")

    assert len(calls) == 2, f"expected bounded attempt then unbounded retry, got {calls}"
    assert "start" in calls[0] and "end" in calls[0]
    assert calls[1] == {}
    assert cal is not None


def test_holiday_calendar_rebuilds_window_once_per_utc_day(monkeypatch):
    """A long-running daemon must not drift outside the window it built on
    day 0 — but must not rebuild more than once per UTC day either."""
    from trading_bot import calendars as cal_mod

    windows: list[tuple[object, object]] = []

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    def _fake_get_calendar(code, **kwargs):
        windows.append((kwargs.get("start"), kwargs.get("end")))
        return _FakeCal()

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _fake_get_calendar)

    hc = cal_mod.HolidayCalendar(window_days=90)
    day0 = datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)

    hc.is_session_open("XLON", day0)
    hc.is_session_open("XLON", day0 + timedelta(hours=1))
    assert len(windows) == 1, "same UTC day must reuse the cached calendar"

    hc.is_session_open("XLON", day0 + timedelta(days=1))
    assert len(windows) == 2, "a new UTC day must rebuild the window"
    assert windows[1] == ("2026-01-24", "2026-07-23")


def test_holiday_calendar_fails_open_when_lookup_raises(monkeypatch):
    """Fail-open safety property: a broken calendar must never stop trading."""
    from trading_bot import calendars as cal_mod

    ts = datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)

    def _boom(code, **kwargs):
        raise RuntimeError("calendar backend exploded")

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _boom)
    hc = cal_mod.HolidayCalendar(window_days=90)
    assert hc.is_session_open("XLON", ts) is True

    class _AngryCal:
        def is_trading_minute(self, ts):
            raise RuntimeError("nope")

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", lambda code, **kw: _AngryCal())
    hc2 = cal_mod.HolidayCalendar(window_days=90)
    assert hc2.is_session_open("XLON", ts) is True


def test_holiday_calendar_fails_open_when_library_missing(monkeypatch):
    """No exchange_calendars installed -> permissive, not crashing."""
    from trading_bot import calendars as cal_mod

    monkeypatch.setattr(cal_mod, "xcals", None)
    hc = cal_mod.HolidayCalendar(window_days=90)
    assert hc.is_session_open("XLON", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)) is True
