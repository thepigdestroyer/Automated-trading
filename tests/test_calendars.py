from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

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
    """No exchange_calendars installed -> permissive, not crashing.

    The permissive result alone does not pin the `_available` mechanism:
    with `xcals = None`, `xcals.get_calendar(...)` raises AttributeError,
    which `_get`'s own `except Exception` swallows into `cal = None` ->
    True anyway. So assert the guard *state* too, otherwise deleting the
    entire `_available` mechanism leaves this test green.
    """
    from trading_bot import calendars as cal_mod

    monkeypatch.setattr(cal_mod, "xcals", None)
    hc = cal_mod.HolidayCalendar(window_days=90)
    assert hc.is_session_open("XLON", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)) is True

    assert hc._available is False
    assert hc._get("XLON") is None
    assert hc._cache == {}  # guard returns before caching
    assert hc._window_day is None  # roll never ran


@pytest.mark.parametrize(
    "label, ts",
    [
        ("datetime.max", datetime.max),
        ("datetime.min", datetime.min),
        ("none", None),
        ("plain_date", date(2026, 4, 23)),
    ],
)
def test_holiday_calendar_fails_open_on_hostile_timestamps(label, ts):
    """Hard property: `is_session_open` returns True whenever ANY step of the
    lookup raises -- including `_maybe_roll_window` / `_window_bounds`, which
    run before the calendar is ever touched.

    Each of these raised out of `is_session_open` before the fix:
    datetime.max/min -> OverflowError, None/date -> AttributeError. The
    property is specified on the method, not on its callers, so it must hold
    without any caller-side `except Exception`.
    """
    hc = HolidayCalendar(window_days=90)
    assert hc.is_session_open("XNYS", ts) is True


def test_holiday_calendar_warns_when_falling_back_to_unbounded(monkeypatch, caplog):
    """The unbounded retry restores the full ~77 MB retained footprint and
    re-pays it on every UTC day roll. It must never happen silently."""
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
    with caplog.at_level(logging.WARNING, logger="trading_bot.calendars"):
        cal = hc._get("XLON", now=datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc))

    # fallback still used
    assert cal is not None
    assert len(calls) == 2 and calls[1] == {}

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {caplog.records}"
    msg = warnings[0].getMessage()
    assert "UNBOUNDED" in msg
    assert "XLON" in msg
    assert "2026-01-23" in msg and "2026-07-22" in msg  # the rejected bounds
    assert warnings[0].exc_info is not None  # cause is preserved


def test_holiday_calendar_does_not_warn_when_code_is_not_a_calendar(monkeypatch, caplog):
    """`FX` and friends are real production exchange_codes (instruments.py)
    that exchange_calendars does not know. BOTH lookups fail, so nothing is
    built and no memory is restored -- warning "restores ~77 MB" there would
    be a daily false alarm that devalues the genuine warning."""
    from trading_bot import calendars as cal_mod

    def _always_invalid(code, **kwargs):
        raise ValueError(f"The requested ExchangeCalendar, {code}, does not exist.")

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _always_invalid)

    hc = cal_mod.HolidayCalendar(window_days=90)
    with caplog.at_level(logging.DEBUG, logger="trading_bot.calendars"):
        assert hc.is_session_open("FX", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)) is True

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    # ...but it is not silent: a debug trail still exists.
    assert any("FX" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_holiday_calendar_does_not_warn_on_the_happy_path(monkeypatch, caplog):
    """Guard against the warning becoming background noise on every roll."""
    from trading_bot import calendars as cal_mod

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", lambda code, **kw: _FakeCal())

    hc = cal_mod.HolidayCalendar(window_days=90)
    with caplog.at_level(logging.WARNING, logger="trading_bot.calendars"):
        hc.is_session_open("XLON", datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_holiday_calendar_rolls_on_utc_day_not_local_day(monkeypatch):
    """The docstring promises "once per UTC day". A non-UTC tzinfo whose local
    date differs from the UTC date must not be treated as a new day."""
    from trading_bot import calendars as cal_mod

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", lambda code, **kw: _FakeCal())

    hc = cal_mod.HolidayCalendar(window_days=90)

    # 2026-04-23 23:00 UTC == 2026-04-24 09:00 +10:00 -- same UTC day,
    # different local dates.
    utc_ts = datetime(2026, 4, 23, 23, 0, tzinfo=timezone.utc)
    plus_ten = utc_ts.astimezone(timezone(timedelta(hours=10)))
    assert plus_ten.date() != utc_ts.date()  # sanity: the dates really differ

    hc.is_session_open("XLON", utc_ts)
    assert hc._window_day == "2026-04-23"

    hc.is_session_open("XLON", plus_ten)
    assert hc._window_day == "2026-04-23", "same UTC instant must not roll the window"
