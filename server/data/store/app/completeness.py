"""Dataset completeness over a trading calendar (tj-grna9p.8 item 2, tj-grna9p.19, tj-grna9p.170).

PURE. Nothing in this module reads the wall clock, the database or the environment. The clock (`now`) and the
calendar are arguments, so a table of cases pins every answer without patching anything. It imports only
trading_calendar; health builds on it (trading_calendar -> completeness -> health).

STATIC COMPLETENESS (assess_completeness):
    COMPLETE every expected session in [start, end) has at least one bar.
    GAPS     some do; `gap_count` counts the SESSIONS inside [start, end) of missing periods (not runs).

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

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from common.enums.data_stock import Granularity
from data.store.app.trading_calendar import TradingCalendar, local_midnight, require_aware, session_of


class CompletenessStatus(StrEnum):
    COMPLETE = 'COMPLETE'
    GAPS = 'GAPS'


@dataclass(frozen=True)
class CompletenessResult:
    """What assess_completeness decides: the status and, for GAPS, the count of missing sessions (else 0)."""

    status: CompletenessStatus
    gap_count: int = 0


@dataclass(frozen=True)
class GapInterval:
    """A maximal run of consecutive expected sessions with no bar. Both ends inclusive."""

    first_session: date
    last_session: date
    sessions: int


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


def covered_sessions(timestamps: Iterable[datetime], calendar: TradingCalendar) -> frozenset[date]:
    """The set of sessions holding at least one of these bar timestamps."""
    return frozenset(session_of(ts, calendar) for ts in timestamps)


PERIODIC = frozenset({Granularity.ONE_WEEK, Granularity.ONE_MONTH})


def period_start(day: date, granularity: Granularity) -> date:
    """The first calendar day of the period holding `day`.

    The day itself for 1min..1day, its Monday for 1week, the 1st of its month for 1month.
    """
    if granularity is Granularity.ONE_WEEK:
        return day - timedelta(days=day.weekday())
    if granularity is Granularity.ONE_MONTH:
        return day.replace(day=1)
    return day


def next_period_start(day: date, granularity: Granularity) -> date:
    """The first calendar day of the period after the one holding `day`."""
    if granularity is Granularity.ONE_WEEK:
        return period_start(day, granularity) + timedelta(days=7)
    if granularity is Granularity.ONE_MONTH:
        return (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return day + timedelta(days=1)


def _covered_periods(covered: Collection[date], granularity: Granularity) -> Collection[date]:
    if granularity not in PERIODIC:
        return covered
    return frozenset(period_start(day, granularity) for day in covered)


def judged(
    start: datetime, end: datetime, calendar: TradingCalendar, now: datetime, granularity: Granularity
) -> tuple[list[date], dict[date, date]]:
    """The sessions to walk, and the EXPECTED sessions among them mapped to their period (see the module docstring).

    For 1min..1day: the sessions overlapping [start, end), the completed ones expected, each its own period. For
    1week and 1month: the sessions from `start` through the end of the last period starting before min(end, now), and
    the sessions of the periods that start inside [start, end), have ended and hold a session.
    """
    if granularity not in PERIODIC:
        sessions = calendar.sessions_overlapping(start, end)
        return sessions, {s: s for s in sessions if calendar.session_close(s) <= now}
    # A period that starts before `end` and has ended started before now, so the range can stop at now.
    bound = min(end, now)
    # Refuse a start before the calendar's, as the per-session path does, before anything is derived from it.
    calendar.sessions_overlapping(start, start + timedelta(microseconds=1))
    if bound <= start:
        return [], {}
    window_end = local_midnight(next_period_start(bound.astimezone(calendar.tz).date(), granularity), calendar)
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
    if granularity not in PERIODIC:
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
    _, expected = judged(start, end, calendar, now, granularity)
    expected = _in_range(expected, start, end, calendar, granularity)
    held = _covered_periods(covered, granularity)
    return [s for s, period in expected.items() if period not in held]


def assess_completeness(
    start: datetime,
    end: datetime,
    covered: Collection[date],
    granularity: Granularity,
    calendar: TradingCalendar,
    now: datetime,
) -> CompletenessResult:
    """Whether every expected session of [start, end) holds a bar (the STATIC rule), and how many do not.

    `covered` is the set of sessions holding a bar (see covered_sessions); empty means no bars. COMPLETE when nothing
    is missing, else GAPS with `gap_count` the number of missing SESSIONS inside [start, end).
    """
    missing = _missing_sessions(start, end, covered, calendar, now, granularity)
    if missing:
        return CompletenessResult(CompletenessStatus.GAPS, len(missing))
    return CompletenessResult(CompletenessStatus.COMPLETE)


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
    require_aware(start=start, end=end, now=now)
    sessions, expected = judged(start, end, calendar, now, granularity)
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
    require_aware(start=start, end=end, now=now)
    if buckets < 1:
        raise ValueError('buckets must be >= 1')
    window, judged_sessions = judged(start, end, calendar, now, granularity)
    sessions = window if granularity not in PERIODIC else calendar.sessions_overlapping(start, end)
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
        expected = [s for s in members if s in judged_sessions]
        bucket_start = start if index == 0 else local_midnight(members[0], calendar)
        bucket_end = end if index == groups - 1 else local_midnight(sessions[last], calendar)
        out.append(
            CoverageBucket(bucket_start, bucket_end, len(expected), sum(judged_sessions[s] in held for s in expected))
        )
        first = last
    return out


__all__ = [
    'PERIODIC',
    'CompletenessResult',
    'CompletenessStatus',
    'CoverageBucket',
    'GapInterval',
    'assess_completeness',
    'coverage_buckets',
    'covered_sessions',
    'gap_intervals',
    'judged',
    'next_period_start',
    'period_start',
]
