"""The trading calendar the store judges datasets by (tj-grna9p.8 item 3, tj-grna9p.19, tj-grna9p.170).

PURE. Nothing in this module reads the wall clock, the database or the environment. The calendar is built once per
source (calendar_for_source, cached) and every instant is an argument, so a table of cases pins every answer without
patching anything. It imports neither completeness nor health: it is the bottom of the chain
trading_calendar -> completeness -> health.

A BAR BELONGS TO THE SESSION WHOSE LOCAL DATE (in the calendar's time zone) ITS TIMESTAMP FALLS ON (session_of).
That holds for a daily bar stamped at local midnight and for every intraday bar of the session.
"""

from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from functools import cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas

from common.enums.data_stock import DataSource


# Calendar per SOURCE for now (tj-grna9p.8 item 3); per-asset exchange is a follow-up for when
# Questrade/TSX data arrives.
SOURCE_CALENDARS: dict[DataSource, str] = {DataSource.ALPACA_API: 'XNYS'}

# The first day the calendars are built from (tj-grna9p.102). exchange_calendars' default start is twenty years
# before the process starts and moves forward a day per day, so a range before it was silently treated as having
# no sessions (a 2001 STATIC dataset read COMPLETE, a DAILY one FRESH). This sits well before any history a
# supported source serves; an instant before it raises CalendarRangeError instead of being answered as empty.
CALENDAR_START = date(1990, 1, 1)


class NaiveDatetimeError(ValueError):
    """A datetime without a time zone was passed where an instant is required."""


class UnknownCalendarError(LookupError):
    """No trading calendar is configured for this source."""


class CalendarRangeError(LookupError):
    """A range or instant begins before the first day the trading calendar covers (CALENDAR_START)."""


class TradingCalendar(Protocol):
    """The calendar surface the store's health rules need; every instant is timezone-aware."""

    @property
    def tz(self) -> ZoneInfo: ...

    def session_open(self, session: date) -> datetime: ...

    def session_close(self, session: date) -> datetime: ...

    def sessions_overlapping(self, start: datetime, end: datetime) -> list[date]:
        """Sessions whose [open, close) overlaps [start, end), ascending."""
        ...

    def last_completed_session(self, now: datetime) -> date | None:
        """The latest session whose close is <= now, or None if the calendar has none."""
        ...

    def session_in_progress(self, now: datetime) -> date | None:
        """The session open at `now` (open <= now < close), else None."""
        ...

    def next_session(self, session: date) -> date: ...


class ExchangeCalendarAdapter:
    """TradingCalendar over an exchange_calendars calendar (offline; holidays and early closes)."""

    def __init__(self, calendar: exchange_calendars.ExchangeCalendar, start: date | None = None) -> None:
        self._cal = calendar
        self._tz = ZoneInfo(str(calendar.tz))
        self._first = calendar.first_session.date()
        self._last = calendar.last_session.date()
        # What the calendar was built to cover. Days between this and the first session are known to hold none.
        self._start = start if start is not None else self._first
        # The session table, read out of pandas once: a session's open and close are looked up by date and a range
        # is a bisect over the sorted dates. A pandas scalar lookup costs tens of microseconds, and a STATIC dataset
        # over nine years asked for thousands of them per request (tj-sww0b1 item 7).
        self._sessions: list[date] = list(calendar.sessions.date)
        self._opens: dict[date, datetime] = dict(
            zip(self._sessions, (ts.to_pydatetime() for ts in calendar.opens), strict=True)
        )
        self._closes: dict[date, datetime] = dict(
            zip(self._sessions, (ts.to_pydatetime() for ts in calendar.closes), strict=True)
        )

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def session_open(self, session: date) -> datetime:
        known = self._opens.get(session)
        if known is not None:
            return known
        # Not a session: the calendar's own refusal, unchanged.
        return self._cal.session_open(pandas.Timestamp(session)).to_pydatetime()

    def session_close(self, session: date) -> datetime:
        known = self._closes.get(session)
        if known is not None:
            return known
        return self._cal.session_close(pandas.Timestamp(session)).to_pydatetime()

    def _require_covered(self, day: date, what: str) -> None:
        if day < self._start:
            raise CalendarRangeError(f'{what} {day} is before the calendar start {self._start}')

    def sessions_overlapping(self, start: datetime, end: datetime) -> list[date]:
        self._require_covered(start.astimezone(self._tz).date(), 'range start')
        # Pad a day each way: a session's local date can sit either side of a UTC date.
        first = max(start.astimezone(self._tz).date() - timedelta(days=1), self._first)
        last = min(end.astimezone(self._tz).date() + timedelta(days=1), self._last)
        if first > last:
            return []
        window = self._sessions[bisect_left(self._sessions, first) : bisect_right(self._sessions, last)]
        return [s for s in window if self._opens[s] < end and self._closes[s] > start]

    def last_completed_session(self, now: datetime) -> date | None:
        local = now.astimezone(self._tz).date()
        self._require_covered(local, 'instant')
        if local < self._first:
            return None
        candidate = self._cal.date_to_session(pandas.Timestamp(min(local, self._last)), direction='previous')
        if self.session_close(candidate.date()) > now:
            if candidate.date() == self._first:
                return None
            candidate = self._cal.previous_session(candidate)
        return candidate.date()

    def session_in_progress(self, now: datetime) -> date | None:
        local = now.astimezone(self._tz).date()
        self._require_covered(local, 'instant')
        if not self._first <= local <= self._last:
            return None
        session = self._cal.date_to_session(pandas.Timestamp(local), direction='previous').date()
        if self.session_open(session) <= now < self.session_close(session):
            return session
        return None

    def next_session(self, session: date) -> date:
        # The first session strictly after `session`, which need not itself be a session (a stray weekend bar).
        self._require_covered(session, 'session')
        if session < self._first:
            return self._first
        label = pandas.Timestamp(session)
        if self._cal.is_session(label):
            return self._cal.next_session(label).date()
        return self._cal.date_to_session(label, direction='next').date()


@cache
def calendar_for_source(source: DataSource) -> TradingCalendar:
    """The calendar a source's data trades on (alpaca -> XNYS), from CALENDAR_START; cached, since building one is slow."""
    code = SOURCE_CALENDARS.get(source)
    if code is None:
        raise UnknownCalendarError(f'no trading calendar configured for source {source!s}')
    return ExchangeCalendarAdapter(
        exchange_calendars.get_calendar(code, start=pandas.Timestamp(CALENDAR_START)), CALENDAR_START
    )


def require_aware(**instants: datetime | None) -> None:
    """Refuse a naive datetime among `instants` (None passes), naming the argument, with NaiveDatetimeError."""
    for name, value in instants.items():
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise NaiveDatetimeError(f'{name} must be timezone-aware')


def session_of(timestamp: datetime, calendar: TradingCalendar) -> date:
    """The session date a bar timestamp belongs to (its local date in the calendar's zone)."""
    require_aware(timestamp=timestamp)
    return timestamp.astimezone(calendar.tz).date()


def local_midnight(session: date, calendar: TradingCalendar) -> datetime:
    """The first instant of a session's local date in the calendar's zone (where a daily bar is stamped)."""
    return datetime.combine(session, datetime.min.time(), tzinfo=calendar.tz)


__all__ = [
    'CALENDAR_START',
    'SOURCE_CALENDARS',
    'CalendarRangeError',
    'ExchangeCalendarAdapter',
    'NaiveDatetimeError',
    'TradingCalendar',
    'UnknownCalendarError',
    'calendar_for_source',
    'local_midnight',
    'require_aware',
    'session_of',
]
