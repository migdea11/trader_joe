"""Dataset health: freshness of DAILY and STREAM datasets, completeness of STATIC ones (tj-grna9p.8 item 2, tj-grna9p.19, tj-grna9p.170).

PURE. Nothing in this module reads the wall clock, the database or the environment (apart from
FreshnessConfig.from_env, which the caller invokes once and passes in). The clock (`now`) and the
calendar are arguments, so a table of cases pins every status without patching anything. It sits on top of
completeness and trading_calendar (trading_calendar -> completeness -> health).

STATUSES AND THE RULING THEY COME FROM (tj-grna9p.8 addendum, final form; tj-grna9p.19 notes):

    DAILY   owes only the completed sessions inside the dataset's [start, end); none closed yet is FRESH.
            FRESH    the last COMPLETED session in range is stored.
            LATE     not stored yet, and the next session has not opened (pending, not a failure;
                     many datasets are collected overnight).
            OVERDUE  not stored by the next session's open. A FAILURE. (The ruling calls this
                     STALE; the owner-approved design renamed it OVERDUE, because STALE is now the
                     UI word for an unread dataset.) There is no close + grace rule.
    STREAM  FRESH    in hours: last bar within `stream_bar_multiple` bar-lengths of now. Outside hours:
                     last bar >= the last session's close minus one bar-length.
            OVERDUE  otherwise. A FAILURE. (STREAM has no source in PR 4; the rule is here so the table
                     is complete.)
    STATIC  COMPLETE every expected session in [start, end) has at least one bar.
            GAPS     some do; `gap_count` counts the SESSIONS inside [start, end) of missing periods
                     (not runs). Decided by completeness.assess_completeness; the status is mapped from its
                     CompletenessStatus, not redefined here.
    RETIRED expiry is set on a DAILY or STREAM dataset: no freshness is computed.

A DAILY bar counts only once FINAL: written at or after the close of its period (the session for 1min..1day, the
last session of the week or month for 1week and 1month) PLUS the settle margin (`FreshnessConfig.settle_margin`,
DAILY_REFRESH_SETTLE_MINUTES, default 30), because a vendor's bar can still change after the close (closing auction,
late corrections) and clocks skew. A newest bar of the expected period written earlier is provisional: the dataset
reads LATE until the next session after the expected period opens, then OVERDUE, and the refresh re-fetches from that
bar inclusively. A STATIC dataset has no such rule.

Any status other than FRESH, COMPLETE and RETIRED is unhealthy, and LATE is the only unhealthy one
that is not yet a failure; `is_failure` says which.

FreshnessStatus keeps its name and its string values: it mirrors the proto enum FreshnessStatus.
"""

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from common.enums.data_stock import Granularity, UpdateType
from data.store.app.completeness import CompletenessStatus, assess_completeness, judged, next_period_start, period_start
from data.store.app.trading_calendar import TradingCalendar, local_midnight, require_aware, session_of


STREAM_BAR_MULTIPLE_ENV = 'FRESHNESS_STREAM_BAR_MULTIPLE'
DEFAULT_STREAM_BAR_MULTIPLE = 3
# The refresh's own setting, shared: it decides when the pass runs after a close and when a bar is final.
SETTLE_ENV = 'DAILY_REFRESH_SETTLE_MINUTES'
DEFAULT_SETTLE_MINUTES = 30
DEFAULT_SETTLE_MARGIN = timedelta(minutes=DEFAULT_SETTLE_MINUTES)

_DAY = timedelta(days=1)


class FreshnessStatus(StrEnum):
    FRESH = 'FRESH'
    LATE = 'LATE'
    OVERDUE = 'OVERDUE'
    # The STATIC pair takes its value from the member completeness owns, so the string is written once.
    COMPLETE = CompletenessStatus.COMPLETE.value
    GAPS = CompletenessStatus.GAPS.value
    RETIRED = 'RETIRED'


_FAILURES = frozenset({FreshnessStatus.OVERDUE, FreshnessStatus.GAPS})

# A completeness verdict as a health status.
_FROM_COMPLETENESS: dict[CompletenessStatus, FreshnessStatus] = {
    CompletenessStatus.COMPLETE: FreshnessStatus.COMPLETE,
    CompletenessStatus.GAPS: FreshnessStatus.GAPS,
}


def settle_margin_from_env(environ: Mapping[str, str] | None = None) -> timedelta:
    """The wait after a session's close before its bars count as final: DAILY_REFRESH_SETTLE_MINUTES.

    The one setting the DAILY refresh (when its pass runs after a close) and the freshness rule (when a bar is final)
    share. Zero is allowed; a negative or malformed value is the default.
    """
    env = os.environ if environ is None else environ
    try:
        minutes = int(env.get(SETTLE_ENV, DEFAULT_SETTLE_MINUTES))
    except ValueError:
        return DEFAULT_SETTLE_MARGIN
    return timedelta(minutes=minutes) if minutes >= 0 else DEFAULT_SETTLE_MARGIN


@dataclass(frozen=True)
class FreshnessConfig:
    stream_bar_multiple: int = DEFAULT_STREAM_BAR_MULTIPLE
    # How long after its period's close a DAILY bar may still change; a bar written sooner is provisional.
    settle_margin: timedelta = DEFAULT_SETTLE_MARGIN

    def __post_init__(self) -> None:
        if self.stream_bar_multiple < 1:
            raise ValueError('stream_bar_multiple must be >= 1')
        if self.settle_margin < timedelta(0):
            raise ValueError('settle_margin must be >= 0')

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> 'FreshnessConfig':
        env = os.environ if environ is None else environ
        raw = env.get(STREAM_BAR_MULTIPLE_ENV)
        multiple = DEFAULT_STREAM_BAR_MULTIPLE if raw is None else int(raw)
        return cls(stream_bar_multiple=multiple, settle_margin=settle_margin_from_env(env))


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
    sessions holding a bar (see completeness.covered_sessions); it is only read for STATIC, where None means
    "no bars". [start, end) is the dataset's half-open range: STATIC checks all of it and DAILY owes
    only the completed sessions inside it (none closed yet reads FRESH with nothing expected; no
    bars at all means the first session in range is the first one missed). STREAM ignores it, since
    it chases the present. Naive datetimes are refused with NaiveDatetimeError.

    `last_bar_written_at` is when the newest bar was last written (None: unknown, taken as final). Only DAILY reads
    it: a newest bar of the expected period written before that period closed plus `config.settle_margin` is
    provisional, and the dataset reads LATE (see last_bar_is_provisional).
    """
    require_aware(
        expiry=expiry, start=start, end=end, last_bar=last_bar, now=now, last_bar_written_at=last_bar_written_at
    )
    config = config or FreshnessConfig()

    if expiry is not None and update_type in (UpdateType.DAILY, UpdateType.STREAM):
        return DatasetHealth(FreshnessStatus.RETIRED, retired_on=expiry)
    if update_type == UpdateType.STATIC:
        result = assess_completeness(start, end, covered or frozenset(), granularity, calendar, now)
        return DatasetHealth(_FROM_COMPLETENESS[result.status], gap_count=result.gap_count)
    if update_type == UpdateType.DAILY:
        return _daily(start, end, last_bar, calendar, now, granularity, last_bar_written_at, config.settle_margin)
    return _stream(last_bar, granularity, calendar, now, config)


def bar_period_close(bar: datetime, calendar: TradingCalendar, granularity: Granularity) -> datetime | None:
    """The instant the period holding `bar` closes: a bar written before it is part-way.

    1min..1day: the close of the bar's SESSION, not the bar's own period end (a 30-minute bar fetched mid-session
    leaves the rest of the session missing, so it stays partial until the session closes). 1week and 1month: the close
    of the last session of the bar's week or month, in the calendar's zone. None when the period holds no session (a
    stray bar on a weekend or holiday), which nothing can have been partial of.
    """
    require_aware(bar=bar)
    period = period_start(session_of(bar, calendar), granularity)
    sessions = calendar.sessions_overlapping(
        local_midnight(period, calendar), local_midnight(next_period_start(period, granularity), calendar)
    )
    return calendar.session_close(sessions[-1]) if sessions else None


def last_bar_is_provisional(
    last_bar: datetime | None,
    last_bar_written_at: datetime | None,
    calendar: TradingCalendar,
    granularity: Granularity,
    settle_margin: timedelta = DEFAULT_SETTLE_MARGIN,
) -> bool:
    """Whether the newest bar was written before its period closed and settled, so it may still change.

    A bar is FINAL only if it was written at or after the close of its period (see bar_period_close) PLUS
    `settle_margin` (the closing auction and late corrections can still change it, and clocks skew), judged against
    the calendar and never the wall clock. The caller passes the margin; the default is a FreshnessConfig's. Unknown (no bar, or no write time) is not provisional. A bar re-fetched
    after the close is rewritten by the upsert with a later write time and becomes final.
    """
    if last_bar is None or last_bar_written_at is None:
        return False
    require_aware(last_bar_written_at=last_bar_written_at)
    closes = bar_period_close(last_bar, calendar, granularity)
    return closes is not None and last_bar_written_at < closes + settle_margin


def _daily(
    start: datetime,
    end: datetime,
    last_bar: datetime | None,
    calendar: TradingCalendar,
    now: datetime,
    granularity: Granularity = Granularity.ONE_DAY,
    last_bar_written_at: datetime | None = None,
    settle_margin: timedelta = DEFAULT_SETTLE_MARGIN,
) -> DatasetHealth:
    # A DAILY dataset owes only the completed periods inside [start, end) (a period is a session for 1min..1day).
    # Capping the range at `now` loses nothing (a session that has closed opened before now) and keeps the calendar
    # query short for a far end.
    _, owed_sessions = judged(start, min(end, now), calendar, now, granularity)
    if not owed_sessions:
        # Nothing in range has closed yet (created today, or before its first session): nothing is owed.
        return DatasetHealth(FreshnessStatus.FRESH)
    owed = list(dict.fromkeys(owed_sessions.values()))
    expected = owed[-1]
    expected_bar = local_midnight(expected, calendar)
    last_period = None if last_bar is None else period_start(session_of(last_bar, calendar), granularity)
    # The expected period's bar counts only once FINAL (written after its period closed and settled); a provisional one is pending,
    # LATE with the normal deadline, until a re-fetch after the close rewrites it. A newer, still open period holds
    # nothing owed, and the period that closes later is judged then.
    provisional = last_period == expected and last_bar_is_provisional(
        last_bar, last_bar_written_at, calendar, granularity, settle_margin
    )
    if last_period is not None and last_period >= expected and not provisional:
        return DatasetHealth(FreshnessStatus.FRESH, expected, expected_bar)
    # Stored late is fine until the first session AFTER the missed period opens; after that it is a failure. The
    # deadline that counts is the one of the FIRST period not stored: a dataset that missed an earlier period already
    # failed at that deadline, and must not read LATE again once a later period ends. With no bars that is the
    # first period in range.
    missed = expected if provisional else owed[0]
    if last_period is not None and not provisional:
        after_last = calendar.next_session(next_period_start(last_period, granularity) - _DAY)
        missed = min(expected, period_start(after_last, granularity))
    deadline = calendar.session_open(calendar.next_session(next_period_start(missed, granularity) - _DAY))
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
    'DEFAULT_SETTLE_MARGIN',
    'DEFAULT_SETTLE_MINUTES',
    'DEFAULT_STREAM_BAR_MULTIPLE',
    'SETTLE_ENV',
    'STREAM_BAR_MULTIPLE_ENV',
    'DatasetHealth',
    'FreshnessConfig',
    'FreshnessStatus',
    'bar_period_close',
    'evaluate_dataset',
    'last_bar_is_provisional',
    'settle_margin_from_env',
]
