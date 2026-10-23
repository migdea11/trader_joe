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
            GAPS     some do; `gap_count` counts the SESSIONS inside [start, end) of missing periods
                     (not runs).
    RETIRED expiry is set on a DAILY or STREAM dataset: no freshness is computed.

Any status other than FRESH, COMPLETE and RETIRED is unhealthy, and LATE is the only unhealthy one
that is not yet a failure; `is_failure` says which.

A BAR BELONGS TO THE SESSION WHOSE LOCAL DATE (in the calendar's time zone) ITS TIMESTAMP FALLS ON.
That holds for a daily bar stamped at local midnight and for every intraday bar of the session.

COVERAGE IS BY PERIOD, THE UNIT STAYS THE SESSION (tj-grna9p.8 addendum of 2026-10-20). Every granularity has
a PERIOD: the session itself for 1min..1day; the calendar week Monday..Sunday for 1week; the calendar month
for 1month, both in the calendar's time zone. A bar covers the WHOLE period holding its local date, so every
session of that period counts as covered, whichever day of the period the vendor stamps. A period is EXPECTED
when its first day's local midnight lies in [start, end), it has ended (its last session has closed) and it
holds a session; the sessions of a partial first period are not expected, since its bar is stamped before the
range. Gap counts, gap windows and coverage buckets stay in SESSIONS, only those inside [start, end): a missing
week is its sessions inside [start, end), in one run. For 1min..1day the period is the session, so every result is the per-session one.
"""

import os
from bisect import bisect_left, bisect_right
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
    # DAILY: the label of the period that should be stored (a session for 1min..1day; for 1week and 1month the
    # period's first day, which may be no session); STREAM: None.
    expected_last_session: date | None = None
    # DAILY: that label's local midnight; STREAM: the oldest last-bar that would still be FRESH.
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


_DAY = timedelta(days=1)
_PERIODIC = frozenset({Granularity.ONE_WEEK, Granularity.ONE_MONTH})


def period_start(day: date, granularity: Granularity) -> date:
    """The first calendar day of the period holding `day`.

    The day itself for 1min..1day, its Monday for 1week, the 1st of its month for 1month.
    """
    if granularity is Granularity.ONE_WEEK:
        return day - timedelta(days=day.weekday())
    if granularity is Granularity.ONE_MONTH:
        return day.replace(day=1)
    return day


def _next_period_start(day: date, granularity: Granularity) -> date:
    """The first calendar day of the period after the one holding `day`."""
    if granularity is Granularity.ONE_WEEK:
        return period_start(day, granularity) + timedelta(days=7)
    if granularity is Granularity.ONE_MONTH:
        return (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return day + timedelta(days=1)


def _covered_periods(covered: Collection[date], granularity: Granularity) -> Collection[date]:
    if granularity not in _PERIODIC:
        return covered
    return frozenset(period_start(day, granularity) for day in covered)


def _judged(
    start: datetime, end: datetime, calendar: TradingCalendar, now: datetime, granularity: Granularity
) -> tuple[list[date], dict[date, date]]:
    """The sessions to walk, and the EXPECTED sessions among them mapped to their period (see the module docstring).

    For 1min..1day: the sessions overlapping [start, end), the completed ones expected, each its own period. For
    1week and 1month: the sessions from `start` through the end of the last period starting before min(end, now), and
    the sessions of the periods that start inside [start, end), have ended and hold a session.
    """
    if granularity not in _PERIODIC:
        sessions = calendar.sessions_overlapping(start, end)
        return sessions, {s: s for s in sessions if calendar.session_close(s) <= now}
    # A period that starts before `end` and has ended started before now, so the range can stop at now.
    bound = min(end, now)
    # Refuse a start before the calendar's, as the per-session path does, before anything is derived from it.
    calendar.sessions_overlapping(start, start + timedelta(microseconds=1))
    if bound <= start:
        return [], {}
    window_end = local_midnight(_next_period_start(bound.astimezone(calendar.tz).date(), granularity), calendar)
    sessions = calendar.sessions_overlapping(start, window_end)
    by_period: dict[date, list[date]] = {}
    for session in sessions:
        by_period.setdefault(period_start(session, granularity), []).append(session)
    expected: dict[date, date] = {}
    for period, members in by_period.items():
        if start <= local_midnight(period, calendar) < end and calendar.session_close(members[-1]) <= now:
            expected.update((s, period) for s in members)
    return sessions, expected


def _in_range(
    expected: dict[date, date], start: datetime, end: datetime, calendar: TradingCalendar, granularity: Granularity
) -> dict[date, date]:
    """The expected sessions inside [start, end): a 1week/1month period straddling `end` reports only those.

    The period still decides covered or not; only the REPORTED sessions are clipped, the contract being the sessions in
    [start, end) with no bar. 1min..1day sessions never straddle `end`, so they pass through unchanged.
    """
    if granularity not in _PERIODIC:
        return expected
    inside = frozenset(calendar.sessions_overlapping(start, end))
    return {s: period for s, period in expected.items() if s in inside}


def _missing_sessions(
    start: datetime,
    end: datetime,
    covered: Collection[date],
    calendar: TradingCalendar,
    now: datetime,
    granularity: Granularity = Granularity.ONE_DAY,
) -> list[date]:
    # Only completed sessions can be a gap; for 1week and 1month, only those of an ended, expected period.
    _, expected = _judged(start, end, calendar, now, granularity)
    expected = _in_range(expected, start, end, calendar, granularity)
    held = _covered_periods(covered, granularity)
    return [s for s, period in expected.items() if period not in held]


def gap_intervals(
    start: datetime,
    end: datetime,
    covered: Collection[date],
    calendar: TradingCalendar,
    now: datetime,
    granularity: Granularity = Granularity.ONE_DAY,
) -> list[GapInterval]:
    """Maximal runs of expected sessions in [start, end) with no bar, ascending.

    Runs are consecutive in the CALENDAR's session sequence, so a weekend or holiday between two
    missing sessions does not split them. For 1week and 1month a session is missing when its whole period
    holds no bar, so a missing week is one run of its sessions, clipped to those inside [start, end). The coverage strip
    (tj-grna9p.63) reuses this.
    """
    _require_aware(start=start, end=end, now=now)
    sessions, expected = _judged(start, end, calendar, now, granularity)
    # The one clip: gap windows hold only sessions inside [start, end); the walk below only meets missing ones.
    expected = _in_range(expected, start, end, calendar, granularity)
    held = _covered_periods(covered, granularity)
    missing = {s for s, period in expected.items() if period not in held}
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


@dataclass(frozen=True)
class CoverageBucket:
    """A stretch of sessions of the coverage strip, half-open [start, end).

    Attributes:
        start: The first instant of the bucket (the strip's start for the first bucket, else the local midnight of
            the bucket's first session).
        end: The first instant of the next bucket, or the strip's end for the last.
        expected_sessions: The COMPLETED sessions in the bucket.
        sessions_with_bars: How many of those hold at least one bar.
    """

    start: datetime
    end: datetime
    expected_sessions: int
    sessions_with_bars: int


def local_midnight(session: date, calendar: TradingCalendar) -> datetime:
    """The first instant of a session's local date in the calendar's zone (where a daily bar is stamped)."""
    return datetime.combine(session, datetime.min.time(), tzinfo=calendar.tz)


def coverage_buckets(
    start: datetime,
    end: datetime,
    covered: Collection[date],
    calendar: TradingCalendar,
    now: datetime,
    buckets: int,
    granularity: Granularity = Granularity.ONE_DAY,
) -> list[CoverageBucket]:
    """Partition the sessions of [start, end) into at most `buckets` contiguous, near-equal groups (tj-grna9p.63).

    Every session overlapping the range is partitioned, expected or not, into min(buckets, sessions) groups whose
    sizes differ by at most one (the earlier groups take the remainder). The buckets tile [start, end) with no hole
    or overlap. A session counts as EXPECTED exactly as gap_intervals judges it (completed; for 1week and 1month, of
    an ended period starting inside the range), so an in-progress or future session is counted in no bucket's
    expected_sessions, and a session holds a bar when its whole period does. Buckets count sessions whatever the
    granularity. No sessions give no buckets.

    Raises:
        ValueError: If `buckets` is below 1.
    """
    _require_aware(start=start, end=end, now=now)
    if buckets < 1:
        raise ValueError('buckets must be >= 1')
    window, judged = _judged(start, end, calendar, now, granularity)
    sessions = window if granularity not in _PERIODIC else calendar.sessions_overlapping(start, end)
    if not sessions:
        return []
    held = _covered_periods(covered, granularity)
    groups = min(buckets, len(sessions))
    size, remainder = divmod(len(sessions), groups)
    out: list[CoverageBucket] = []
    first = 0
    for index in range(groups):
        last = first + size + (1 if index < remainder else 0)
        members = sessions[first:last]
        expected = [s for s in members if s in judged]
        bucket_start = start if index == 0 else local_midnight(members[0], calendar)
        bucket_end = end if index == groups - 1 else local_midnight(sessions[last], calendar)
        out.append(CoverageBucket(bucket_start, bucket_end, len(expected), sum(judged[s] in held for s in expected)))
        first = last
    return out


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
    last_bar_written_at: datetime | None = None,
) -> DatasetHealth:
    """Health of one dataset at `now`.

    `last_bar` is the newest bar timestamp (None when there are none). `covered` is the set of
    sessions holding a bar (see covered_sessions); it is only read for STATIC, where None means
    "no bars". [start, end) is the dataset's half-open range: STATIC checks all of it and DAILY owes
    only the completed sessions inside it (none closed yet reads FRESH with nothing expected; no
    bars at all means the first session in range is the first one missed). STREAM ignores it, since
    it chases the present. Naive datetimes are refused with NaiveDatetimeError.

    `last_bar_written_at` is when the newest bar was last written (None: unknown, taken as final). Only DAILY reads
    it: a newest bar of the expected period written before that period closed is provisional, and the dataset reads
    LATE (see last_bar_is_provisional).
    """
    _require_aware(
        expiry=expiry, start=start, end=end, last_bar=last_bar, now=now, last_bar_written_at=last_bar_written_at
    )
    config = config or FreshnessConfig()

    if expiry is not None and update_type in (UpdateType.DAILY, UpdateType.STREAM):
        return DatasetHealth(FreshnessStatus.RETIRED, retired_on=expiry)
    if update_type == UpdateType.STATIC:
        missing = _missing_sessions(start, end, covered or frozenset(), calendar, now, granularity)
        if missing:
            return DatasetHealth(FreshnessStatus.GAPS, gap_count=len(missing))
        return DatasetHealth(FreshnessStatus.COMPLETE)
    if update_type == UpdateType.DAILY:
        return _daily(start, end, last_bar, calendar, now, granularity, last_bar_written_at)
    return _stream(last_bar, granularity, calendar, now, config)


def bar_period_close(bar: datetime, calendar: TradingCalendar, granularity: Granularity) -> datetime | None:
    """The instant the period holding `bar` closes: a bar written at or after it can no longer be partial.

    1min..1hour: the bar's own period end (timestamp + granularity). 1day: the close of the bar's session. 1week and
    1month: the close of the last session of the bar's week or month, in the calendar's zone. None when the period
    holds no session (a stray bar on a weekend or holiday), which nothing can have been partial of.
    """
    _require_aware(bar=bar)
    if granularity not in _PERIODIC and granularity is not Granularity.ONE_DAY:
        return bar + granularity.offset
    period = period_start(session_of(bar, calendar), granularity)
    sessions = calendar.sessions_overlapping(
        local_midnight(period, calendar), local_midnight(_next_period_start(period, granularity), calendar)
    )
    return calendar.session_close(sessions[-1]) if sessions else None


def last_bar_is_provisional(
    last_bar: datetime | None, last_bar_written_at: datetime | None, calendar: TradingCalendar, granularity: Granularity
) -> bool:
    """Whether the newest bar was written before its period closed, so the vendor may have sent it part-way.

    A bar is FINAL only if it was written at or after the close of its period (see bar_period_close), judged against
    the calendar and never the wall clock. Unknown (no bar, or no write time) is not provisional. A bar re-fetched
    after the close is rewritten by the upsert with a later write time and becomes final.
    """
    if last_bar is None or last_bar_written_at is None:
        return False
    _require_aware(last_bar_written_at=last_bar_written_at)
    closes = bar_period_close(last_bar, calendar, granularity)
    return closes is not None and last_bar_written_at < closes


def _daily(
    start: datetime,
    end: datetime,
    last_bar: datetime | None,
    calendar: TradingCalendar,
    now: datetime,
    granularity: Granularity = Granularity.ONE_DAY,
    last_bar_written_at: datetime | None = None,
) -> DatasetHealth:
    # A DAILY dataset owes only the completed periods inside [start, end) (a period is a session for 1min..1day).
    # Capping the range at `now` loses nothing (a session that has closed opened before now) and keeps the calendar
    # query short for a far end.
    _, owed_sessions = _judged(start, min(end, now), calendar, now, granularity)
    if not owed_sessions:
        # Nothing in range has closed yet (created today, or before its first session): nothing is owed.
        return DatasetHealth(FreshnessStatus.FRESH)
    owed = list(dict.fromkeys(owed_sessions.values()))
    expected = owed[-1]
    expected_bar = local_midnight(expected, calendar)
    last_period = None if last_bar is None else period_start(session_of(last_bar, calendar), granularity)
    # The expected period's bar counts only once FINAL (written after its period closed); a provisional one is pending,
    # LATE with the normal deadline, until a re-fetch after the close rewrites it. A newer, still open period holds
    # nothing owed, and the period that closes later is judged then.
    provisional = last_period == expected and last_bar_is_provisional(
        last_bar, last_bar_written_at, calendar, granularity
    )
    if last_period is not None and last_period >= expected and not provisional:
        return DatasetHealth(FreshnessStatus.FRESH, expected, expected_bar)
    # Stored late is fine until the first session AFTER the missed period opens; after that it is a failure. The
    # deadline that counts is the one of the FIRST period not stored: a dataset that missed an earlier period already
    # failed at that deadline, and must not read LATE again once a later period ends. With no bars that is the
    # first period in range.
    missed = expected if provisional else owed[0]
    if last_period is not None and not provisional:
        after_last = calendar.next_session(_next_period_start(last_period, granularity) - _DAY)
        missed = min(expected, period_start(after_last, granularity))
    deadline = calendar.session_open(calendar.next_session(_next_period_start(missed, granularity) - _DAY))
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
    'CoverageBucket',
    'DatasetHealth',
    'ExchangeCalendarAdapter',
    'FreshnessConfig',
    'FreshnessStatus',
    'GapInterval',
    'NaiveDatetimeError',
    'TradingCalendar',
    'UnknownCalendarError',
    'bar_period_close',
    'calendar_for_source',
    'coverage_buckets',
    'covered_sessions',
    'evaluate_dataset',
    'gap_intervals',
    'last_bar_is_provisional',
    'local_midnight',
    'period_start',
    'session_of',
]
