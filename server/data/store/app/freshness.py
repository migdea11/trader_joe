"""Dataset freshness and completeness over a trading calendar (tj-grna9p.8 item 2, tj-grna9p.19).

PURE. Nothing in this module reads the wall clock, the database or the environment (apart from
FreshnessConfig.from_env, which the caller invokes once and passes in). The clock (`now`) and the
calendar are arguments, so a table of cases pins every status without patching anything.

STATUSES AND THE RULING THEY COME FROM (tj-grna9p.8 addendum, final form; tj-grna9p.19 notes):

    DAILY   owes only the completed sessions inside the dataset's [start, end); none closed yet is FRESH.
            FRESH    the last COMPLETED session in range is stored.
            LATE     not stored yet, and the next session has not opened (pending, not a failure;
                     many datasets are collected overnight).
            OVERDUE  not stored by the next session's open. A FAILURE. (The ruling calls this
                     STALE; the owner-approved design renamed it OVERDUE, because STALE is now the
                     UI word for an unread dataset.) There is no close + grace rule.
    STREAM  FRESH    in hours: last bar within `stream_bar_multiple` bar-lengths of now. Outside
                     hours: last bar >= the last session's close minus one bar-length.
            OVERDUE  otherwise. A FAILURE. (STREAM has no source in PR 4; the rule is here so the
                     table is complete.)
    STATIC  COMPLETE every expected session in [start, end) has at least one bar.
            GAPS     some do; `gap_count` is the number of missing SESSIONS (not runs).
    RETIRED expiry is set on a DAILY or STREAM dataset: no freshness is computed.

Any status other than FRESH, COMPLETE and RETIRED is unhealthy, and LATE is the only unhealthy one
that is not yet a failure; `is_failure` says which.

A BAR BELONGS TO THE SESSION WHOSE LOCAL DATE (in the calendar's time zone) ITS TIMESTAMP FALLS ON.
That holds for a daily bar stamped at local midnight and for every intraday bar of the session.

KNOWN LIMIT: completeness is per session ("at least one bar"), which is right for 1min..1day
granularities. A 1week or 1month dataset has bars on fewer days than there are sessions, so it
would read as full of gaps; nothing in PR 4 stores one, and it is a follow-up when it does.
"""

import os
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas

from common.enums.data_stock import DataSource, Granularity, UpdateType


STREAM_BAR_MULTIPLE_ENV = 'FRESHNESS_STREAM_BAR_MULTIPLE'
DEFAULT_STREAM_BAR_MULTIPLE = 3

# Calendar per SOURCE for now (tj-grna9p.8 item 3); per-asset exchange is a follow-up for when
# Questrade/TSX data arrives.
SOURCE_CALENDARS: dict[DataSource, str] = {DataSource.ALPACA_API: 'XNYS'}

# The first day the calendars are built from (tj-grna9p.102). exchange_calendars' default start is twenty years
# before the process starts and moves forward a day per day, so a range before it was silently treated as having
# no sessions (a 2001 STATIC dataset read COMPLETE, a DAILY one FRESH). This sits well before any history a
# supported source serves; an instant before it raises CalendarRangeError instead of being answered as empty.
CALENDAR_START = date(1990, 1, 1)


class FreshnessStatus(StrEnum):
    FRESH = 'FRESH'
    LATE = 'LATE'
    OVERDUE = 'OVERDUE'
    COMPLETE = 'COMPLETE'
    GAPS = 'GAPS'
    RETIRED = 'RETIRED'


_FAILURES = frozenset({FreshnessStatus.OVERDUE, FreshnessStatus.GAPS})


class NaiveDatetimeError(ValueError):
    """A datetime without a time zone was passed where an instant is required."""


class UnknownCalendarError(LookupError):
    """No trading calendar is configured for this source."""


class CalendarRangeError(LookupError):
    """A range or instant begins before the first day the trading calendar covers (CALENDAR_START)."""


class TradingCalendar(Protocol):
    """The calendar surface this module needs; every instant is timezone-aware."""

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

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def session_open(self, session: date) -> datetime:
        return self._cal.session_open(pandas.Timestamp(session)).to_pydatetime()

    def session_close(self, session: date) -> datetime:
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
        labels = self._cal.sessions_in_range(pandas.Timestamp(first), pandas.Timestamp(last))
        sessions = [label.date() for label in labels]
        return [s for s in sessions if self.session_open(s) < end and self.session_close(s) > start]

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


@dataclass(frozen=True)
class FreshnessConfig:
    stream_bar_multiple: int = DEFAULT_STREAM_BAR_MULTIPLE

    def __post_init__(self) -> None:
        if self.stream_bar_multiple < 1:
            raise ValueError('stream_bar_multiple must be >= 1')

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> 'FreshnessConfig':
        env = os.environ if environ is None else environ
        raw = env.get(STREAM_BAR_MULTIPLE_ENV)
        return cls() if raw is None else cls(stream_bar_multiple=int(raw))


@dataclass(frozen=True)
class GapInterval:
    """A maximal run of consecutive expected sessions with no bar. Both ends inclusive."""

    first_session: date
    last_session: date
    sessions: int


@dataclass(frozen=True)
class DatasetHealth:
    status: FreshnessStatus
    # DAILY: the session label that should be stored; STREAM: None.
    expected_last_session: date | None = None
    # DAILY: that session's local midnight; STREAM: the oldest last-bar that would still be FRESH.
    expected_last_bar: datetime | None = None
    # STATIC: missing sessions in [start, end), 0 unless status is GAPS.
    gap_count: int = 0
    # RETIRED: the expiry instant ("Retired - deleted on <date>").
    retired_on: datetime | None = None

    @property
    def is_failure(self) -> bool:
        return self.status in _FAILURES


def _require_aware(**instants: datetime | None) -> None:
    for name, value in instants.items():
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise NaiveDatetimeError(f'{name} must be timezone-aware')


def session_of(timestamp: datetime, calendar: TradingCalendar) -> date:
    """The session date a bar timestamp belongs to (its local date in the calendar's zone)."""
    _require_aware(timestamp=timestamp)
    return timestamp.astimezone(calendar.tz).date()


def covered_sessions(timestamps: Iterable[datetime], calendar: TradingCalendar) -> frozenset[date]:
    """The set of sessions holding at least one of these bar timestamps."""
    return frozenset(session_of(ts, calendar) for ts in timestamps)


def _missing_sessions(
    start: datetime, end: datetime, covered: Collection[date], calendar: TradingCalendar, now: datetime
) -> list[date]:
    # A session still in progress, or not yet started, cannot be a gap: only completed ones count.
    return [
        s for s in calendar.sessions_overlapping(start, end) if calendar.session_close(s) <= now and s not in covered
    ]


def gap_intervals(
    start: datetime, end: datetime, covered: Collection[date], calendar: TradingCalendar, now: datetime
) -> list[GapInterval]:
    """Maximal runs of completed expected sessions in [start, end) with no bar, ascending.

    Runs are consecutive in the CALENDAR's session sequence, so a weekend or holiday between two
    missing sessions does not split them. The coverage strip (tj-grna9p.63) reuses this.
    """
    _require_aware(start=start, end=end, now=now)
    sessions = calendar.sessions_overlapping(start, end)
    missing = set(_missing_sessions(start, end, covered, calendar, now))
    runs: list[GapInterval] = []
    run: list[date] = []
    for session in sessions:
        if session in missing:
            run.append(session)
            continue
        if run:
            runs.append(GapInterval(run[0], run[-1], len(run)))
            run = []
    if run:
        runs.append(GapInterval(run[0], run[-1], len(run)))
    return runs


def evaluate_dataset(
    *,
    update_type: UpdateType,
    granularity: Granularity,
    expiry: datetime | None,
    start: datetime,
    end: datetime,
    last_bar: datetime | None,
    covered: Collection[date] | None,
    calendar: TradingCalendar,
    now: datetime,
    config: FreshnessConfig | None = None,
) -> DatasetHealth:
    """Health of one dataset at `now`.

    `last_bar` is the newest bar timestamp (None when there are none). `covered` is the set of
    sessions holding a bar (see covered_sessions); it is only read for STATIC, where None means
    "no bars". [start, end) is the dataset's half-open range: STATIC checks all of it and DAILY owes
    only the completed sessions inside it (none closed yet reads FRESH with nothing expected; no
    bars at all means the first session in range is the first one missed). STREAM ignores it, since
    it chases the present. Naive datetimes are refused with NaiveDatetimeError.
    """
    _require_aware(expiry=expiry, start=start, end=end, last_bar=last_bar, now=now)
    config = config or FreshnessConfig()

    if expiry is not None and update_type in (UpdateType.DAILY, UpdateType.STREAM):
        return DatasetHealth(FreshnessStatus.RETIRED, retired_on=expiry)
    if update_type == UpdateType.STATIC:
        missing = _missing_sessions(start, end, covered or frozenset(), calendar, now)
        if missing:
            return DatasetHealth(FreshnessStatus.GAPS, gap_count=len(missing))
        return DatasetHealth(FreshnessStatus.COMPLETE)
    if update_type == UpdateType.DAILY:
        return _daily(start, end, last_bar, calendar, now)
    return _stream(last_bar, granularity, calendar, now, config)


def _daily(
    start: datetime, end: datetime, last_bar: datetime | None, calendar: TradingCalendar, now: datetime
) -> DatasetHealth:
    # A DAILY dataset owes only the completed sessions inside [start, end). Capping the range at `now` loses
    # nothing (a session that has closed opened before now) and keeps the calendar query short for a far end.
    owed = [s for s in calendar.sessions_overlapping(start, min(end, now)) if calendar.session_close(s) <= now]
    if not owed:
        # Nothing in range has closed yet (created today, or before its first session): nothing is owed.
        return DatasetHealth(FreshnessStatus.FRESH)
    expected = owed[-1]
    expected_bar = datetime.combine(expected, datetime.min.time(), tzinfo=calendar.tz)
    if last_bar is not None and session_of(last_bar, calendar) >= expected:
        return DatasetHealth(FreshnessStatus.FRESH, expected, expected_bar)
    # Stored late is fine until the NEXT session opens; after that it is a failure. The deadline that counts is
    # the one of the FIRST session not stored: a dataset that missed an earlier session already failed at that
    # session's deadline, and must not read LATE again once a later session closes. With no bars that is the
    # first session in range.
    missed = owed[0]
    if last_bar is not None:
        missed = min(expected, calendar.next_session(session_of(last_bar, calendar)))
    deadline = calendar.session_open(calendar.next_session(missed))
    status = FreshnessStatus.LATE if now < deadline else FreshnessStatus.OVERDUE
    return DatasetHealth(status, expected, expected_bar)


def _stream(
    last_bar: datetime | None,
    granularity: Granularity,
    calendar: TradingCalendar,
    now: datetime,
    config: FreshnessConfig,
) -> DatasetHealth:
    bar_length = granularity.offset
    if calendar.session_in_progress(now) is not None:
        threshold = now - config.stream_bar_multiple * bar_length
    else:
        previous = calendar.last_completed_session(now)
        if previous is None:
            return DatasetHealth(FreshnessStatus.FRESH)
        threshold = calendar.session_close(previous) - bar_length
    fresh = last_bar is not None and last_bar >= threshold
    return DatasetHealth(FreshnessStatus.FRESH if fresh else FreshnessStatus.OVERDUE, expected_last_bar=threshold)


__all__ = [
    'CALENDAR_START',
    'DEFAULT_STREAM_BAR_MULTIPLE',
    'SOURCE_CALENDARS',
    'STREAM_BAR_MULTIPLE_ENV',
    'CalendarRangeError',
    'DatasetHealth',
    'ExchangeCalendarAdapter',
    'FreshnessConfig',
    'FreshnessStatus',
    'GapInterval',
    'NaiveDatetimeError',
    'TradingCalendar',
    'UnknownCalendarError',
    'calendar_for_source',
    'covered_sessions',
    'evaluate_dataset',
    'gap_intervals',
    'session_of',
]
