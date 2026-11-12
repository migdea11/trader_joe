"""Dataset health (recency and completeness) over the real XNYS calendar (tj-grna9p.19; decision tj-grna9p.8 item 2 as ruled).

WHY A TABLE OVER THE REAL CALENDAR, NOT A FAKE ONE (validator, gating tj-grna9p.19). The rules are only as
right as the session arithmetic underneath them: which session is the last completed one, when the next one
opens, and which days are not sessions at all. A hand-written fake calendar would pin the rules against the
author's own idea of the exchange. Every case here runs through calendar_for_source(ALPACA_API), the adapter the
service itself uses, over exchange_calendars' XNYS, so a weekend, Thanksgiving, Christmas and the two 13:00 ET
early closes are the exchange's, not the test's.

THE DATES, all 2026, so they are worth reading once:
    Fri 2 Oct, Mon 5 Oct, Tue 6 Oct   EDT: open 13:30Z, close 20:00Z
    Wed 25 Nov                        EST: open 14:30Z, close 21:00Z
    Thu 26 Nov                        Thanksgiving, no session
    Fri 27 Nov                        early close: 14:30Z to 18:00Z
    Thu 24 Dec                        early close: 14:30Z to 18:00Z
    Fri 25 Dec, Sat 26, Sun 27        no session
    Mon 28 Dec                        open 14:30Z
A daily bar is stamped at its session's local midnight, as trading_calendar.session_of reads it.

WHAT THE RULING SAYS, as the acceptance on tj-grna9p.19 words it after the 2026-10-01 amendment: DAILY is FRESH
when the last completed session is stored, LATE (pending, not a failure) from that session's close until the NEXT
SESSION'S OPEN, and OVERDUE (the ruling's STALE, renamed 2026-10-06) from that open. There is no close + grace.

AMENDED 2026-11-10 (owner ruling on decision tj-grna9p.8, built in 8ecbb33a): LATE begins at the close PLUS THE SETTLE
MARGIN (HealthConfig.settle_margin, default 30 minutes), inclusive, because the scheduler's own refresh fires then;
between the close and close plus the margin the session is not owed yet. The deadline (the next session's open) did
not move. Rows that asserted LATE at a close were moved to close plus SETTLE, and each now has a twin inside the
window; the ruling's own table is the "LATE FROM CLOSE PLUS SETTLE" section below.
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas
import pytest

from common.enums.data_stock import DataSource, Granularity, UpdateType
from data.store.app.completeness import GapInterval, covered_sessions, gap_intervals
from data.store.app.health import (
    DEFAULT_STREAM_BAR_MULTIPLE,
    STREAM_BAR_MULTIPLE_ENV,
    DatasetHealth,
    DatasetHealthStatus,
    HealthConfig,
    evaluate_dataset,
)
from data.store.app.trading_calendar import (
    CALENDAR_START,
    CalendarRangeError,
    ExchangeCalendarAdapter,
    NaiveDatetimeError,
    UnknownCalendarError,
    calendar_for_source,
    session_of,
)


pytestmark = pytest.mark.data_store

XNYS = calendar_for_source(DataSource.ALPACA_API)
NEW_YORK = ZoneInfo('America/New_York')
TICK = timedelta(microseconds=1)
# The default settle margin, written by hand (DAILY_REFRESH_SETTLE_MINUTES unset), never read from the module.
SETTLE = timedelta(minutes=30)


def z(text: str) -> datetime:
    """An instant from an ISO string ending in Z."""
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def daily_bar(session: date) -> datetime:
    """A daily bar's timestamp: its session's local midnight."""
    return datetime.combine(session, datetime.min.time(), tzinfo=NEW_YORK)


def daily(now: datetime, last_bar: datetime | None):
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=Granularity.ONE_DAY,
        collect=True,
        start=z('2026-01-02T00:00:00Z'),
        end=now,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
    )


OCT_1, OCT_2, OCT_5 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)
NOV_25, NOV_27 = date(2026, 11, 25), date(2026, 11, 27)
DEC_23, DEC_24 = date(2026, 12, 23), date(2026, 12, 24)

# (now, last stored session or None, expected status, expected_last_session)
DAILY_CASES = {
    # During Monday's session, the last COMPLETED session is Friday's.
    'mon in session, fri stored': (z('2026-10-05T15:00:00Z'), OCT_2, DatasetHealthStatus.FRESH, OCT_2),
    # ...and Friday's bar still missing at that point is already a failure: Monday has opened.
    'mon in session, thu stored': (z('2026-10-05T15:00:00Z'), OCT_1, DatasetHealthStatus.OVERDUE, OCT_2),
    # From the close PLUS SETTLE the expectation moves to Monday; Friday's bar is no longer enough, but it is only
    # LATE. (Before 2026-11-10 this moved at the close itself; inside the margin Monday is not owed yet.)
    'mon one tick before close, fri stored': (
        z('2026-10-05T20:00:00Z') - TICK,
        OCT_2,
        DatasetHealthStatus.FRESH,
        OCT_2,
    ),
    'mon at close, fri stored: not owed yet': (z('2026-10-05T20:00:00Z'), OCT_2, DatasetHealthStatus.FRESH, OCT_2),
    'mon at close plus settle, fri stored': (
        z('2026-10-05T20:00:00Z') + SETTLE,
        OCT_2,
        DatasetHealthStatus.LATE,
        OCT_5,
    ),
    'mon at close plus settle, mon stored': (
        z('2026-10-05T20:00:00Z') + SETTLE,
        OCT_5,
        DatasetHealthStatus.FRESH,
        OCT_5,
    ),
    # The amendment's own case: incomplete at 23:59 the same day is LATE, not a failure. The old close+2h
    # rule would have failed it at 22:00Z.
    'mon 23:59 ET, fri stored': (z('2026-10-06T03:59:00Z'), OCT_2, DatasetHealthStatus.LATE, OCT_5),
    # The deadline is the NEXT SESSION'S OPEN, Tuesday 13:30Z, to the microsecond.
    'tue one tick before open, fri stored': (z('2026-10-06T13:30:00Z') - TICK, OCT_2, DatasetHealthStatus.LATE, OCT_5),
    'tue at open, fri stored': (z('2026-10-06T13:30:00Z'), OCT_2, DatasetHealthStatus.OVERDUE, OCT_5),
    'tue at open, mon stored': (z('2026-10-06T13:30:00Z'), OCT_5, DatasetHealthStatus.FRESH, OCT_5),
    'tue at open, nothing stored': (z('2026-10-06T13:30:00Z'), None, DatasetHealthStatus.OVERDUE, OCT_5),
    # A WEEKEND: Friday's bar is pending all weekend and fails only at Monday's open.
    'sat, thu stored': (z('2026-10-03T12:00:00Z'), OCT_1, DatasetHealthStatus.LATE, OCT_2),
    'sun night, thu stored': (z('2026-10-05T03:00:00Z'), OCT_1, DatasetHealthStatus.LATE, OCT_2),
    'mon one tick before open, thu stored': (z('2026-10-05T13:30:00Z') - TICK, OCT_1, DatasetHealthStatus.LATE, OCT_2),
    'mon at open, thu stored': (z('2026-10-05T13:30:00Z'), OCT_1, DatasetHealthStatus.OVERDUE, OCT_2),
    'sat, fri stored': (z('2026-10-03T12:00:00Z'), OCT_2, DatasetHealthStatus.FRESH, OCT_2),
    # A HOLIDAY: Wednesday's bar may land any time on Thanksgiving; the deadline is Friday's open.
    'thanksgiving, tue stored': (z('2026-11-26T18:00:00Z'), date(2026, 11, 24), DatasetHealthStatus.LATE, NOV_25),
    'fri one tick before open, tue stored': (
        z('2026-11-27T14:30:00Z') - TICK,
        date(2026, 11, 24),
        DatasetHealthStatus.LATE,
        NOV_25,
    ),
    'fri at open, tue stored': (z('2026-11-27T14:30:00Z'), date(2026, 11, 24), DatasetHealthStatus.OVERDUE, NOV_25),
    'thanksgiving, wed stored': (z('2026-11-26T18:00:00Z'), NOV_25, DatasetHealthStatus.FRESH, NOV_25),
    # AN EARLY CLOSE: the half-day's own close (18:00Z), not the usual 21:00Z, moves the expectation.
    'fri half-day one tick before its close, wed stored': (
        z('2026-11-27T18:00:00Z') - TICK,
        NOV_25,
        DatasetHealthStatus.FRESH,
        NOV_25,
    ),
    # ...plus the settle margin, from the half-day's own close (2026-11-10 ruling): 18:30Z, not 18:00Z or 21:30Z.
    'fri half-day at its close, wed stored: not owed yet': (
        z('2026-11-27T18:00:00Z'),
        NOV_25,
        DatasetHealthStatus.FRESH,
        NOV_25,
    ),
    'fri half-day one tick before its close plus settle, wed stored': (
        z('2026-11-27T18:00:00Z') + SETTLE - TICK,
        NOV_25,
        DatasetHealthStatus.FRESH,
        NOV_25,
    ),
    'fri half-day at its close plus settle, wed stored': (
        z('2026-11-27T18:00:00Z') + SETTLE,
        NOV_25,
        DatasetHealthStatus.LATE,
        NOV_27,
    ),
    # A WEEKEND AND A HOLIDAY TOGETHER: Christmas Eve's bar is due by Monday 28 Dec's open, four days on.
    'christmas day, wed stored': (z('2026-12-25T15:00:00Z'), DEC_23, DatasetHealthStatus.LATE, DEC_24),
    'sun 27 dec, wed stored': (z('2026-12-27T15:00:00Z'), DEC_23, DatasetHealthStatus.LATE, DEC_24),
    'mon 28 dec one tick before open, wed stored': (
        z('2026-12-28T14:30:00Z') - TICK,
        DEC_23,
        DatasetHealthStatus.LATE,
        DEC_24,
    ),
    'mon 28 dec at open, wed stored': (z('2026-12-28T14:30:00Z'), DEC_23, DatasetHealthStatus.OVERDUE, DEC_24),
}


@pytest.mark.parametrize(('now', 'stored', 'status', 'expected_session'), DAILY_CASES.values(), ids=DAILY_CASES)
def test_daily_is_late_until_the_next_session_opens_and_overdue_from_it(now, stored, status, expected_session):
    health = daily(now, None if stored is None else daily_bar(stored))
    assert health.status is status
    assert health.expected_last_session == expected_session
    assert health.expected_last_bar == daily_bar(expected_session)
    assert health.gap_count == 0
    assert health.is_failure is (status is DatasetHealthStatus.OVERDUE)


MISSED_SESSION_CASES = {
    # Monday's bar was due by Tuesday's 13:30Z open and is still missing at Tuesday's close and after it.
    'tue at close, fri stored': z('2026-10-06T20:00:00Z'),
    'tue night, fri stored': z('2026-10-07T02:00:00Z'),
    'wed one tick before open, fri stored': z('2026-10-07T13:30:00Z') - TICK,
}


@pytest.mark.parametrize('now', MISSED_SESSION_CASES.values(), ids=MISSED_SESSION_CASES)
def test_a_missed_session_stays_overdue_after_the_following_close(now):
    """Already OVERDUE at Tuesday's open (see 'tue at open, fri stored'), so it must not recover to LATE.

    Found by the validator gating tj-grna9p.19: _daily judged only the LATEST completed session, so once the next
    session closed a dataset that had already missed a deadline read LATE again until the following open, flapping
    OVERDUE, LATE, OVERDUE. Fixed in 15b74aa (the deadline is the first unstored session's).
    """
    assert daily(z('2026-10-06T13:30:00Z'), daily_bar(OCT_2)).status is DatasetHealthStatus.OVERDUE
    assert daily(now, daily_bar(OCT_2)).status is DatasetHealthStatus.OVERDUE


def test_a_bar_newer_than_the_expected_session_is_fresh():
    """An intraday-stamped bar of the session in progress is past the expectation, not short of it."""
    assert daily(z('2026-10-05T15:00:00Z'), z('2026-10-05T14:00:00Z')).status is DatasetHealthStatus.FRESH


def test_late_is_unhealthy_but_not_a_failure():
    """The ruling's distinction: LATE is pending, OVERDUE is a failure, and only OVERDUE says so."""
    late = daily(z('2026-10-05T21:00:00Z'), daily_bar(OCT_2))
    overdue = daily(z('2026-10-06T13:30:00Z'), daily_bar(OCT_2))
    assert (late.status, late.is_failure) == (DatasetHealthStatus.LATE, False)
    assert (overdue.status, overdue.is_failure) == (DatasetHealthStatus.OVERDUE, True)


# ------------------------------------------------------------------------------------------- DAILY in [start, end)
#
# THE RULE (architect addendum on decision tj-grna9p.8, 2026-10-06; work tj-grna9p.100): a DAILY dataset owes only
# the completed sessions inside its [start, end). Nothing owed yet reads FRESH with no expected session. The deadline
# is the open of the session after the FIRST unstored session in range: with no bars, the first session in range.
# Every case below would read otherwise if _daily ignored start or end, which is the defect the bead fixed: a dataset
# created today read LATE and then OVERDUE for sessions it never covered.

OCT_6, OCT_7, OCT_8 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)
OPEN_ENDED = z('2100-01-01T00:00:00Z')


def daily_in(start: datetime, end: datetime, now: datetime, last_bar: datetime | None = None):
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=Granularity.ONE_DAY,
        collect=True,
        start=start,
        end=end,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
    )


def assert_nothing_owed(health) -> None:
    assert health.status is DatasetHealthStatus.FRESH
    assert health.expected_last_session is None
    assert health.expected_last_bar is None
    assert not health.is_failure


def assert_owes(health, status: DatasetHealthStatus, session: date) -> None:
    assert health.status is status
    assert health.expected_last_session == session
    assert health.expected_last_bar == daily_bar(session)
    assert health.is_failure is (status is DatasetHealthStatus.OVERDUE)


# Created on Monday 5 Oct before the open (local midnight), no bars. Friday 2 Oct closed before it began.
CREATED_MONDAY = daily_bar(OCT_5)


@pytest.mark.parametrize(
    'now',
    [
        z('2026-10-05T12:00:00Z'),
        z('2026-10-05T15:00:00Z'),
        z('2026-10-05T20:00:00Z') - TICK,
        z('2026-10-05T20:00:00Z'),
        z('2026-10-05T20:00:00Z') + SETTLE - TICK,
    ],
    ids=[
        'before the open',
        'in session',
        'one tick before the close',
        'at the close',
        'a tick before close plus settle',
    ],
)
def test_daily_created_today_owes_nothing_before_its_first_session_closes(now):
    """Friday is the last completed session, but it is before start: nothing is owed, so FRESH with no expectation."""
    assert_nothing_owed(daily_in(CREATED_MONDAY, OPEN_ENDED, now))


@pytest.mark.parametrize(
    'now',
    [z('2026-10-05T20:00:00Z') + SETTLE, z('2026-10-06T03:59:00Z'), z('2026-10-06T13:30:00Z') - TICK],
    ids=['at the close plus settle', 'that night', 'one tick before the next open'],
)
def test_daily_created_today_is_late_from_its_first_close_until_the_next_open(now):
    """Owed from the first close plus SETTLE (2026-11-10 ruling; the row was at the close itself before)."""
    assert_owes(daily_in(CREATED_MONDAY, OPEN_ENDED, now), DatasetHealthStatus.LATE, OCT_5)


def test_daily_created_today_is_overdue_from_the_next_open_and_stays_so():
    """Overdue at Tuesday's open, and Tuesday's close (a later session owed) must not flip it back to LATE.

    Tuesday is owed from its close plus SETTLE (2026-11-10 ruling), so at 20:00Z the expectation is still Monday.
    """
    assert_owes(daily_in(CREATED_MONDAY, OPEN_ENDED, z('2026-10-06T13:30:00Z')), DatasetHealthStatus.OVERDUE, OCT_5)
    assert_owes(daily_in(CREATED_MONDAY, OPEN_ENDED, z('2026-10-06T20:00:00Z')), DatasetHealthStatus.OVERDUE, OCT_5)
    assert_owes(
        daily_in(CREATED_MONDAY, OPEN_ENDED, z('2026-10-06T20:00:00Z') + SETTLE), DatasetHealthStatus.OVERDUE, OCT_6
    )


def test_daily_no_bars_with_a_past_start_is_overdue_from_the_first_in_range_deadline():
    """Start Thu 1 Oct, nothing ever stored: the backfill failed at Friday's open, Thursday's deadline.

    The latest owed session moves on with every close, but the deadline stays the FIRST in-range session's, so the
    status never recovers to LATE after Monday's close (which, judged only against Monday, would be LATE).
    """
    start = daily_bar(OCT_1)
    # Owed from the close plus SETTLE (2026-11-10 ruling): nothing is owed at Thursday's close itself.
    assert_nothing_owed(daily_in(start, OPEN_ENDED, z('2026-10-01T20:00:00Z')))
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-01T20:00:00Z') + SETTLE), DatasetHealthStatus.LATE, OCT_1)
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-02T13:30:00Z') - TICK), DatasetHealthStatus.LATE, OCT_1)
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-02T13:30:00Z')), DatasetHealthStatus.OVERDUE, OCT_1)
    # Inside Monday's settle window the expectation is still Friday; the deadline already passed either way.
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-05T20:00:00Z')), DatasetHealthStatus.OVERDUE, OCT_2)
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-05T20:00:00Z') + SETTLE), DatasetHealthStatus.OVERDUE, OCT_5)
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-06T03:59:00Z')), DatasetHealthStatus.OVERDUE, OCT_5)


def test_daily_starting_mid_session_owes_that_session_once_it_closes():
    """Start Monday 15:00Z: Monday overlaps [start, end), so it is owed at its close plus SETTLE; Friday never is."""
    start = z('2026-10-05T15:00:00Z')
    assert_nothing_owed(daily_in(start, OPEN_ENDED, z('2026-10-05T20:00:00Z') - TICK))
    assert_nothing_owed(daily_in(start, OPEN_ENDED, z('2026-10-05T20:00:00Z') + SETTLE - TICK))
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-05T20:00:00Z') + SETTLE), DatasetHealthStatus.LATE, OCT_5)
    assert_owes(daily_in(start, OPEN_ENDED, z('2026-10-06T13:30:00Z')), DatasetHealthStatus.OVERDUE, OCT_5)


# [Thu 1 Oct 00:00 ET, Tue 6 Oct 00:00 ET): the last in-range session is Monday 5 Oct; judged on Thu 8 Oct, after
# Tuesday and Wednesday have closed outside the range.
ENDED_START, ENDED_END = daily_bar(OCT_1), daily_bar(OCT_6)


def test_daily_whose_end_has_passed_is_fresh_with_its_last_in_range_session_stored():
    """Nothing after end is owed: Monday stored is enough on Thursday, though Tue and Wed have closed since."""
    health = daily_in(ENDED_START, ENDED_END, z('2026-10-08T22:00:00Z'), daily_bar(OCT_5))
    assert_owes(health, DatasetHealthStatus.FRESH, OCT_5)


def test_daily_whose_end_has_passed_is_judged_against_its_last_in_range_session():
    """Friday stored, Monday (the last session before end) not: LATE until Tuesday's open, OVERDUE after.

    The expectation stays Monday rather than moving to a session past end.
    """
    last = daily_bar(OCT_2)
    # Monday is owed from its close plus SETTLE (2026-11-10 ruling): at the close itself Friday is still enough.
    assert_owes(daily_in(ENDED_START, ENDED_END, z('2026-10-05T20:00:00Z'), last), DatasetHealthStatus.FRESH, OCT_2)
    assert_owes(
        daily_in(ENDED_START, ENDED_END, z('2026-10-05T20:00:00Z') + SETTLE, last), DatasetHealthStatus.LATE, OCT_5
    )
    assert_owes(
        daily_in(ENDED_START, ENDED_END, z('2026-10-06T13:30:00Z') - TICK, last), DatasetHealthStatus.LATE, OCT_5
    )
    assert_owes(daily_in(ENDED_START, ENDED_END, z('2026-10-06T13:30:00Z'), last), DatasetHealthStatus.OVERDUE, OCT_5)
    assert_owes(daily_in(ENDED_START, ENDED_END, z('2026-10-08T22:00:00Z'), last), DatasetHealthStatus.OVERDUE, OCT_5)


def test_daily_starting_after_now_owes_nothing():
    """Start Wed 7 Oct, judged Mon 5 Oct after its close: no session in range has happened."""
    assert_nothing_owed(daily_in(daily_bar(OCT_7), OPEN_ENDED, z('2026-10-05T22:00:00Z')))
    assert_nothing_owed(daily_in(daily_bar(OCT_7), daily_bar(OCT_8), z('2026-10-05T22:00:00Z')))


def test_daily_starting_on_a_non_session_day_expects_the_next_session():
    """Saturday 3 Oct owes Monday 5 Oct; Thanksgiving owes the Friday half-day, from its 18:00Z close plus SETTLE."""
    saturday = daily_bar(date(2026, 10, 3))
    assert_nothing_owed(daily_in(saturday, OPEN_ENDED, z('2026-10-05T20:00:00Z') + SETTLE - TICK))
    assert_owes(daily_in(saturday, OPEN_ENDED, z('2026-10-05T20:00:00Z') + SETTLE), DatasetHealthStatus.LATE, OCT_5)
    thanksgiving = daily_bar(date(2026, 11, 26))
    assert_nothing_owed(daily_in(thanksgiving, OPEN_ENDED, z('2026-11-27T18:00:00Z') + SETTLE - TICK))
    assert_owes(
        daily_in(thanksgiving, OPEN_ENDED, z('2026-11-27T18:00:00Z') + SETTLE), DatasetHealthStatus.LATE, NOV_27
    )


@pytest.mark.parametrize(
    'end', [OPEN_ENDED, datetime.max.replace(tzinfo=ZoneInfo('UTC'))], ids=['year 2100', 'datetime.max']
)
def test_daily_with_a_far_future_end_evaluates(end):
    """A declared end far past the calendar's last session (or at the largest datetime) does not break the read."""
    assert_owes(daily_in(CREATED_MONDAY, end, z('2026-10-06T13:30:00Z')), DatasetHealthStatus.OVERDUE, OCT_5)
    assert_owes(
        daily_in(CREATED_MONDAY, end, z('2026-10-06T22:00:00Z'), daily_bar(OCT_6)), DatasetHealthStatus.FRESH, OCT_6
    )


# ------------------------------------------------------------------------------------- LATE FROM CLOSE PLUS SETTLE
#
# THE RULING (owner, 2026-11-10, addendum on decision tj-grna9p.8; built in 8ecbb33a, task tj-7zjov1.366; these rows
# are tj-7zjov1.367's): a DAILY period is owed only once its close PLUS THE SETTLE MARGIN has passed, inclusive,
# because the scheduler's own trigger (AfterTradingEvent CLOSE plus settle_delay()) fires then: "If a dataset hasn't
# been triggered by the scheduler to update, it can't be late." The deadline (OVERDUE at the next session's open) is
# judged against the real now, unchanged. STREAM and STATIC are unchanged.
#
# THE DATES, by hand, a winter (EST) week of 2026: Tue 17 Nov and Wed 18 Nov close 21:00Z; Thu 19 Nov opens 14:30Z.
# The week of Mon 16 Nov ends at Fri 20 Nov's 21:00Z close; Mon 23 Nov opens 14:30Z. Thanksgiving week (Mon 23 Nov)
# ends at Fri 27 Nov's EARLY close, 18:00Z. November's last session is Mon 30 Nov (close 21:00Z); Tue 1 Dec opens
# 14:30Z. The margin is SETTLE (30 minutes) unless a row says otherwise.

NOV_16, NOV_17, NOV_18, NOV_23 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 23)
NOV18_CLOSE = z('2026-11-18T21:00:00Z')
NOV18_SETTLED = z('2026-11-18T21:30:00Z')
NOV19_OPEN = z('2026-11-19T14:30:00Z')
WINTER_START = z('2026-09-01T04:00:00Z')


def winter(
    now: datetime,
    last_bar: datetime | None,
    written: datetime | None = None,
    *,
    start: datetime = WINTER_START,
    granularity: Granularity = Granularity.ONE_DAY,
    margin: timedelta | None = None,
):
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=granularity,
        collect=True,
        start=start,
        end=OPEN_ENDED,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
        config=None if margin is None else HealthConfig(settle_margin=margin),
        last_bar_written_at=written,
    )


def assert_health(health, status: DatasetHealthStatus, period: date | None) -> None:
    assert health.status is status, health
    assert health.expected_last_session == period, health
    assert health.expected_last_bar == (None if period is None else daily_bar(period)), health


# (now, written time of the 18th's bar or None for 'the 18th is not stored', status, expected period)
# The stored bar is Tue 17 Nov's (final, unknown write time) when the 18th is not stored.
SETTLE_RULE_CASES = {
    # (1) The 18th not stored: FRESH (judged against the 17th) before the trigger, LATE from it.
    'not stored, at the close: FRESH': (NOV18_CLOSE, None, DatasetHealthStatus.FRESH, NOV_17),
    'not stored, 21:15Z: FRESH': (z('2026-11-18T21:15:00Z'), None, DatasetHealthStatus.FRESH, NOV_17),
    # (2) The boundary, inclusive.
    'not stored, a tick before 21:30Z: FRESH': (NOV18_SETTLED - TICK, None, DatasetHealthStatus.FRESH, NOV_17),
    'not stored, exactly 21:30Z: LATE': (NOV18_SETTLED, None, DatasetHealthStatus.LATE, NOV_18),
    # (5) The deadline did not move: LATE until the next open, OVERDUE at it.
    'not stored, a tick before the next open: LATE': (NOV19_OPEN - TICK, None, DatasetHealthStatus.LATE, NOV_18),
    'not stored, at the next open: OVERDUE': (NOV19_OPEN, None, DatasetHealthStatus.OVERDUE, NOV_18),
    # (3) The 18th stored, its bar provisional (written mid-session, or after the close but inside the margin): a
    # provisional bar must not read LATE before the trigger. From 21:30Z it is the expected period and provisional.
    'provisional (mid-session), 21:15Z: FRESH': (
        z('2026-11-18T21:15:00Z'),
        z('2026-11-18T17:00:00Z'),
        DatasetHealthStatus.FRESH,
        NOV_17,
    ),
    'provisional (21:05Z), 21:15Z: FRESH': (
        z('2026-11-18T21:15:00Z'),
        z('2026-11-18T21:05:00Z'),
        DatasetHealthStatus.FRESH,
        NOV_17,
    ),
    'provisional (21:05Z), a tick before 21:30Z: FRESH': (
        NOV18_SETTLED - TICK,
        z('2026-11-18T21:05:00Z'),
        DatasetHealthStatus.FRESH,
        NOV_17,
    ),
    'provisional (mid-session), 21:30Z: LATE': (
        NOV18_SETTLED,
        z('2026-11-18T17:00:00Z'),
        DatasetHealthStatus.LATE,
        NOV_18,
    ),
    'provisional (21:05Z), 21:30Z: LATE': (NOV18_SETTLED, z('2026-11-18T21:05:00Z'), DatasetHealthStatus.LATE, NOV_18),
    'provisional, at the next open: OVERDUE': (
        NOV19_OPEN,
        z('2026-11-18T17:00:00Z'),
        DatasetHealthStatus.OVERDUE,
        NOV_18,
    ),
    # The trigger's own fetch, written at 21:30Z, is final: FRESH from then on.
    'final (written 21:30Z), 21:30Z: FRESH': (NOV18_SETTLED, NOV18_SETTLED, DatasetHealthStatus.FRESH, NOV_18),
    'final (written 21:30Z), 22:00Z: FRESH': (
        z('2026-11-18T22:00:00Z'),
        NOV18_SETTLED,
        DatasetHealthStatus.FRESH,
        NOV_18,
    ),
}


@pytest.mark.parametrize(('now', 'written', 'status', 'period'), SETTLE_RULE_CASES.values(), ids=SETTLE_RULE_CASES)
def test_a_daily_session_is_owed_from_its_close_plus_the_settle_margin(now, written, status, period):
    last_bar = daily_bar(NOV_17) if written is None else daily_bar(NOV_18)
    health = winter(now, last_bar, written)
    assert_health(health, status, period)
    assert health.is_failure is (status is DatasetHealthStatus.OVERDUE)


def test_the_default_margin_is_the_one_these_rows_assume():
    """The rows above pass no config: they hold only while the default margin is SETTLE."""
    assert HealthConfig().settle_margin == SETTLE


# (4) Created at 21:10Z on the 18th (after the close, inside the margin), declared start the 18th's local midnight, no
# bars: the 18th is in range, so it is owed from 21:30Z, but not before.
CREATED_AT_2110 = {
    'at creation, 21:10Z': (z('2026-11-18T21:10:00Z'), DatasetHealthStatus.FRESH, None),
    '21:15Z': (z('2026-11-18T21:15:00Z'), DatasetHealthStatus.FRESH, None),
    'a tick before 21:30Z': (NOV18_SETTLED - TICK, DatasetHealthStatus.FRESH, None),
    '21:30Z': (NOV18_SETTLED, DatasetHealthStatus.LATE, NOV_18),
    'a tick before the next open': (NOV19_OPEN - TICK, DatasetHealthStatus.LATE, NOV_18),
    'the next open': (NOV19_OPEN, DatasetHealthStatus.OVERDUE, NOV_18),
}


@pytest.mark.parametrize(('now', 'status', 'period'), CREATED_AT_2110.values(), ids=CREATED_AT_2110)
def test_a_dataset_created_inside_the_margin_owes_its_session_only_from_close_plus_settle(now, status, period):
    assert_health(winter(now, None, start=daily_bar(NOV_18)), status, period)


def test_a_dataset_already_overdue_stays_overdue_inside_the_next_settle_window():
    """The deadline is the FIRST missed session's, against the real now: the window never hides an older failure.

    Tue 17 Nov never stored (the 16th is): OVERDUE from Wed 18 Nov's open, and still OVERDUE at the 18th's close and
    inside its margin, where the expectation is still the 17th.
    """
    stored = daily_bar(NOV_16)
    assert_health(winter(z('2026-11-18T14:30:00Z'), stored), DatasetHealthStatus.OVERDUE, NOV_17)
    assert_health(winter(NOV18_CLOSE, stored), DatasetHealthStatus.OVERDUE, NOV_17)
    assert_health(winter(z('2026-11-18T21:15:00Z'), stored), DatasetHealthStatus.OVERDUE, NOV_17)
    assert_health(winter(NOV18_SETTLED, stored), DatasetHealthStatus.OVERDUE, NOV_18)


# (6) The margin comes from the config: zero is the pre-ruling rule (LATE at the close itself), and another margin
# moves the instant with it.
MARGIN_CASES = {
    'zero, a tick before the close: FRESH': (timedelta(0), NOV18_CLOSE - TICK, DatasetHealthStatus.FRESH, NOV_17),
    'zero, at the close: LATE': (timedelta(0), NOV18_CLOSE, DatasetHealthStatus.LATE, NOV_18),
    'zero, at the next open: OVERDUE': (timedelta(0), NOV19_OPEN, DatasetHealthStatus.OVERDUE, NOV_18),
    'two hours, 22:59:59.999999Z: FRESH': (
        timedelta(hours=2),
        z('2026-11-18T23:00:00Z') - TICK,
        DatasetHealthStatus.FRESH,
        NOV_17,
    ),
    'two hours, 23:00Z: LATE': (timedelta(hours=2), z('2026-11-18T23:00:00Z'), DatasetHealthStatus.LATE, NOV_18),
    'two hours, at the next open: OVERDUE': (timedelta(hours=2), NOV19_OPEN, DatasetHealthStatus.OVERDUE, NOV_18),
}


@pytest.mark.parametrize(('margin', 'now', 'status', 'period'), MARGIN_CASES.values(), ids=MARGIN_CASES)
def test_the_owed_instant_follows_the_configured_margin_and_zero_is_the_old_rule(margin, now, status, period):
    assert_health(winter(now, daily_bar(NOV_17), margin=margin), status, period)


AT_CLOSE_PLUS_SETTLE_ROWS = [
    'mon at close plus settle, fri stored',
    'mon at close plus settle, mon stored',
    'fri half-day at its close plus settle, wed stored',
]


@pytest.mark.parametrize('row', AT_CLOSE_PLUS_SETTLE_ROWS)
def test_a_zero_margin_gives_at_the_bare_close_what_the_default_gives_at_close_plus_settle(row):
    """The DAILY_CASES rows moved to close plus SETTLE on 2026-11-10 read the same at the bare close with margin 0."""
    now, stored, status, session = DAILY_CASES[row]
    old = winter(now - SETTLE, daily_bar(stored), start=z('2026-01-02T00:00:00Z'), margin=timedelta(0))
    assert (old.status, old.expected_last_session) == (status, session)


# (7) 1week and 1month: the period's LAST close plus the margin decides when it is owed; its deadline is the open of
# the first session after the period.
PERIODIC_SETTLE_CASES = {
    'week of 16 Nov, Fri 21:15Z: FRESH against the week of 9 Nov': (
        Granularity.ONE_WEEK,
        date(2026, 11, 9),
        None,
        z('2026-11-20T21:15:00Z'),
        DatasetHealthStatus.FRESH,
        date(2026, 11, 9),
    ),
    'week of 16 Nov, a tick before Fri 21:30Z: FRESH': (
        Granularity.ONE_WEEK,
        date(2026, 11, 9),
        None,
        z('2026-11-20T21:30:00Z') - TICK,
        DatasetHealthStatus.FRESH,
        date(2026, 11, 9),
    ),
    'week of 16 Nov, Fri 21:30Z: LATE': (
        Granularity.ONE_WEEK,
        date(2026, 11, 9),
        None,
        z('2026-11-20T21:30:00Z'),
        DatasetHealthStatus.LATE,
        NOV_16,
    ),
    'week of 16 Nov, a tick before Mon 23 Nov open: LATE': (
        Granularity.ONE_WEEK,
        date(2026, 11, 9),
        None,
        z('2026-11-23T14:30:00Z') - TICK,
        DatasetHealthStatus.LATE,
        NOV_16,
    ),
    'week of 16 Nov, Mon 23 Nov open: OVERDUE': (
        Granularity.ONE_WEEK,
        date(2026, 11, 9),
        None,
        z('2026-11-23T14:30:00Z'),
        DatasetHealthStatus.OVERDUE,
        NOV_16,
    ),
    'week of 16 Nov stored provisional (Fri 20:00Z), Fri 21:15Z: FRESH': (
        Granularity.ONE_WEEK,
        NOV_16,
        z('2026-11-20T20:00:00Z'),
        z('2026-11-20T21:15:00Z'),
        DatasetHealthStatus.FRESH,
        date(2026, 11, 9),
    ),
    'week of 16 Nov stored provisional (Fri 20:00Z), Fri 21:30Z: LATE': (
        Granularity.ONE_WEEK,
        NOV_16,
        z('2026-11-20T20:00:00Z'),
        z('2026-11-20T21:30:00Z'),
        DatasetHealthStatus.LATE,
        NOV_16,
    ),
    # Thanksgiving week ends at the Friday EARLY close, 18:00Z: owed from 18:30Z, not 21:30Z.
    'thanksgiving week, Fri 18:15Z: FRESH': (
        Granularity.ONE_WEEK,
        NOV_16,
        None,
        z('2026-11-27T18:15:00Z'),
        DatasetHealthStatus.FRESH,
        NOV_16,
    ),
    'thanksgiving week, Fri 18:30Z: LATE': (
        Granularity.ONE_WEEK,
        NOV_16,
        None,
        z('2026-11-27T18:30:00Z'),
        DatasetHealthStatus.LATE,
        NOV_23,
    ),
    'november, Mon 30 Nov 21:15Z: FRESH against october': (
        Granularity.ONE_MONTH,
        date(2026, 10, 1),
        None,
        z('2026-11-30T21:15:00Z'),
        DatasetHealthStatus.FRESH,
        date(2026, 10, 1),
    ),
    'november, a tick before Mon 30 Nov 21:30Z: FRESH': (
        Granularity.ONE_MONTH,
        date(2026, 10, 1),
        None,
        z('2026-11-30T21:30:00Z') - TICK,
        DatasetHealthStatus.FRESH,
        date(2026, 10, 1),
    ),
    'november, Mon 30 Nov 21:30Z: LATE': (
        Granularity.ONE_MONTH,
        date(2026, 10, 1),
        None,
        z('2026-11-30T21:30:00Z'),
        DatasetHealthStatus.LATE,
        date(2026, 11, 1),
    ),
    'november, Tue 1 Dec open: OVERDUE': (
        Granularity.ONE_MONTH,
        date(2026, 10, 1),
        None,
        z('2026-12-01T14:30:00Z'),
        DatasetHealthStatus.OVERDUE,
        date(2026, 11, 1),
    ),
    'november stored provisional (Mon 30 Nov 21:10Z), 21:15Z: FRESH': (
        Granularity.ONE_MONTH,
        date(2026, 11, 2),
        z('2026-11-30T21:10:00Z'),
        z('2026-11-30T21:15:00Z'),
        DatasetHealthStatus.FRESH,
        date(2026, 10, 1),
    ),
    'november stored provisional (Mon 30 Nov 21:10Z), 21:30Z: LATE': (
        Granularity.ONE_MONTH,
        date(2026, 11, 2),
        z('2026-11-30T21:10:00Z'),
        z('2026-11-30T21:30:00Z'),
        DatasetHealthStatus.LATE,
        date(2026, 11, 1),
    ),
}


@pytest.mark.parametrize(
    ('granularity', 'stored', 'written', 'now', 'status', 'period'),
    PERIODIC_SETTLE_CASES.values(),
    ids=PERIODIC_SETTLE_CASES,
)
def test_a_weekly_or_monthly_period_is_owed_from_its_last_close_plus_the_margin(
    granularity, stored, written, now, status, period
):
    assert_health(winter(now, daily_bar(stored), written, granularity=granularity), status, period)


# (9) STREAM and STATIC do not take the margin: inside the 18th's window they read exactly as at the bare close.


def test_stream_ignores_the_settle_window():
    """After the close a STREAM 1min dataset owes the session's last bar (20:59Z) at once, margin or not."""
    threshold = z('2026-11-18T20:59:00Z')
    for now in (NOV18_CLOSE, z('2026-11-18T21:15:00Z'), NOV18_SETTLED - TICK):
        for margin in (None, timedelta(0), timedelta(hours=2)):
            for last_bar, status in ((threshold, DatasetHealthStatus.FRESH), (z('2026-11-18T20:58:00Z'), None)):
                got = evaluate_dataset(
                    update_type=UpdateType.STREAM,
                    granularity=Granularity.ONE_MINUTE,
                    collect=True,
                    start=WINTER_START,
                    end=OPEN_ENDED,
                    last_bar=last_bar,
                    covered=None,
                    calendar=XNYS,
                    now=now,
                    config=None if margin is None else HealthConfig(settle_margin=margin),
                )
                want = DatasetHealthStatus.OVERDUE if status is None else status
                assert got == DatasetHealth(want, expected_last_bar=threshold), (now, margin, last_bar)


def test_static_ignores_the_settle_window():
    """[Mon 16 Nov, Thu 19 Nov) holding the 16th and 17th: the 18th is a gap from its close, margin or not."""
    for now in (NOV18_CLOSE, z('2026-11-18T21:15:00Z'), NOV18_SETTLED - TICK):
        for margin in (None, timedelta(hours=2)):
            got = evaluate_dataset(
                update_type=UpdateType.STATIC,
                granularity=Granularity.ONE_DAY,
                collect=None,
                start=daily_bar(NOV_16),
                end=daily_bar(date(2026, 11, 19)),
                last_bar=daily_bar(NOV_17),
                covered=frozenset({NOV_16, NOV_17}),
                calendar=XNYS,
                now=now,
                config=None if margin is None else HealthConfig(settle_margin=margin),
            )
            assert got == DatasetHealth(DatasetHealthStatus.GAPS, gap_count=1), (now, margin)


# ------------------------------------------------------------------------------------------------- STREAM


def stream(now: datetime, last_bar: datetime | None, multiple: int | None = None, granularity=Granularity.ONE_MINUTE):
    return evaluate_dataset(
        update_type=UpdateType.STREAM,
        granularity=granularity,
        collect=True,
        start=z('2026-01-02T00:00:00Z'),
        end=now,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
        config=None if multiple is None else HealthConfig(stream_bar_multiple=multiple),
    )


# (now, last bar, multiple or None for the default, expected status, expected threshold)
STREAM_CASES = {
    # IN HOURS: within the default 3 bar-lengths of now, inclusive.
    'in hours, exactly 3 minutes old': (
        z('2026-10-05T15:00:00Z'),
        z('2026-10-05T14:57:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-10-05T14:57:00Z'),
    ),
    'in hours, 3 minutes and a tick old': (
        z('2026-10-05T15:00:00Z'),
        z('2026-10-05T14:57:00Z') - TICK,
        None,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-05T14:57:00Z'),
    ),
    'in hours, multiple 5 widens the window': (
        z('2026-10-05T15:00:00Z'),
        z('2026-10-05T14:55:00Z'),
        5,
        DatasetHealthStatus.FRESH,
        z('2026-10-05T14:55:00Z'),
    ),
    'in hours, multiple 1 narrows it': (
        z('2026-10-05T15:00:00Z'),
        z('2026-10-05T14:58:00Z'),
        1,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-05T14:59:00Z'),
    ),
    'in hours, nothing stored': (
        z('2026-10-05T15:00:00Z'),
        None,
        None,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-05T14:57:00Z'),
    ),
    # The open is in hours: the instant the session opens, the 3-bar window applies.
    'at the open, the last bar is yesterday close': (
        z('2026-10-05T13:30:00Z'),
        z('2026-10-02T19:59:00Z'),
        None,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-05T13:27:00Z'),
    ),
    # OUT OF HOURS: the last bar must reach the last close minus one bar-length.
    'after close, the final bar of the day': (
        z('2026-10-05T22:00:00Z'),
        z('2026-10-05T19:59:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-10-05T19:59:00Z'),
    ),
    'after close, one bar short': (
        z('2026-10-05T22:00:00Z'),
        z('2026-10-05T19:58:00Z'),
        None,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-05T19:59:00Z'),
    ),
    # The close itself is out of hours (open <= now < close).
    'at the close': (
        z('2026-10-05T20:00:00Z'),
        z('2026-10-05T19:59:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-10-05T19:59:00Z'),
    ),
    'before the open, friday close is the reference': (
        z('2026-10-05T12:00:00Z'),
        z('2026-10-02T19:59:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-10-02T19:59:00Z'),
    ),
    'saturday, friday close is the reference': (
        z('2026-10-03T12:00:00Z'),
        z('2026-10-02T19:58:00Z'),
        None,
        DatasetHealthStatus.OVERDUE,
        z('2026-10-02T19:59:00Z'),
    ),
    'thanksgiving, wednesday close is the reference': (
        z('2026-11-26T16:00:00Z'),
        z('2026-11-25T20:59:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-11-25T20:59:00Z'),
    ),
    'after a half-day, its 18:00Z close is the reference': (
        z('2026-11-27T19:00:00Z'),
        z('2026-11-27T17:59:00Z'),
        None,
        DatasetHealthStatus.FRESH,
        z('2026-11-27T17:59:00Z'),
    ),
}


@pytest.mark.parametrize(
    ('now', 'last_bar', 'multiple', 'status', 'threshold'), STREAM_CASES.values(), ids=STREAM_CASES
)
def test_stream_health_in_and_out_of_hours(now, last_bar, multiple, status, threshold):
    health = stream(now, last_bar, multiple)
    assert health.status is status
    assert health.expected_last_bar == threshold
    assert health.expected_last_session is None
    assert health.is_failure is (status is DatasetHealthStatus.OVERDUE)


def test_the_stream_window_is_measured_in_the_dataset_s_own_bar_length():
    """At 5min the default three bar-lengths are fifteen minutes in hours, and one bar-length is five after close."""
    now = z('2026-10-05T15:00:00Z')
    assert (
        stream(now, z('2026-10-05T14:45:00Z'), granularity=Granularity.FIVE_MINUTES).status is DatasetHealthStatus.FRESH
    )
    assert stream(now, z('2026-10-05T14:45:00Z') - TICK, granularity=Granularity.FIVE_MINUTES).status is (
        DatasetHealthStatus.OVERDUE
    )
    after = stream(z('2026-10-05T22:00:00Z'), z('2026-10-05T19:55:00Z'), granularity=Granularity.FIVE_MINUTES)
    assert (after.status, after.expected_last_bar) == (DatasetHealthStatus.FRESH, z('2026-10-05T19:55:00Z'))


# ------------------------------------------------------------------------------------------------- STATIC


YEAR_END = z('2026-12-31T00:00:00Z')


def static(start: datetime, end: datetime, covered, now: datetime = YEAR_END):
    return evaluate_dataset(
        update_type=UpdateType.STATIC,
        granularity=Granularity.ONE_DAY,
        collect=None,
        start=start,
        end=end,
        last_bar=None,
        covered=covered,
        calendar=XNYS,
        now=now,
    )


# [Thu 1 Oct 00:00 ET, Tue 6 Oct 00:00 ET) holds exactly three sessions: Thu 1, Fri 2 and Mon 5.
WEEK_START, WEEK_END = daily_bar(OCT_1), daily_bar(date(2026, 10, 6))

# (start, end, covered sessions, status, gap count)
STATIC_CASES = {
    'every session covered': (WEEK_START, WEEK_END, {OCT_1, OCT_2, OCT_5}, DatasetHealthStatus.COMPLETE, 0),
    'friday missing': (WEEK_START, WEEK_END, {OCT_1, OCT_5}, DatasetHealthStatus.GAPS, 1),
    'nothing stored (None)': (WEEK_START, WEEK_END, None, DatasetHealthStatus.GAPS, 3),
    'nothing stored (empty)': (WEEK_START, WEEK_END, frozenset(), DatasetHealthStatus.GAPS, 3),
    # A weekend is not a gap: nothing is expected on Saturday or Sunday.
    'the weekend uncovered is not a gap': (
        daily_bar(OCT_2),
        daily_bar(date(2026, 10, 5)) + timedelta(hours=1),
        {OCT_2},
        DatasetHealthStatus.COMPLETE,
        0,
    ),
    # A holiday is not a gap: Thanksgiving holds no session.
    'thanksgiving uncovered is not a gap': (
        daily_bar(NOV_25),
        daily_bar(date(2026, 11, 28)),
        {NOV_25, NOV_27},
        DatasetHealthStatus.COMPLETE,
        0,
    ),
    # HALF-OPEN AT THE END: a session that opens exactly at end is outside the range...
    'end exactly at monday open excludes monday': (
        WEEK_START,
        z('2026-10-05T13:30:00Z'),
        {OCT_1, OCT_2},
        DatasetHealthStatus.COMPLETE,
        0,
    ),
    # ...and one microsecond later it is inside, and missing.
    'end a tick after monday open includes monday': (
        WEEK_START,
        z('2026-10-05T13:30:00Z') + TICK,
        {OCT_1, OCT_2},
        DatasetHealthStatus.GAPS,
        1,
    ),
    # HALF-OPEN AT THE START, mirrored: a range starting at friday's close holds no friday...
    'start exactly at friday close excludes friday': (
        z('2026-10-02T20:00:00Z'),
        WEEK_END,
        {OCT_5},
        DatasetHealthStatus.COMPLETE,
        0,
    ),
    # ...and one starting a tick before it does.
    'start a tick before friday close includes friday': (
        z('2026-10-02T20:00:00Z') - TICK,
        WEEK_END,
        {OCT_5},
        DatasetHealthStatus.GAPS,
        1,
    ),
}


@pytest.mark.parametrize(('start', 'end', 'covered', 'status', 'gaps'), STATIC_CASES.values(), ids=STATIC_CASES)
def test_static_completeness_counts_missing_sessions_in_the_half_open_range(start, end, covered, status, gaps):
    health = static(start, end, covered)
    assert health.status is status
    assert health.gap_count == gaps
    assert health.is_failure is (status is DatasetHealthStatus.GAPS)
    assert health.expected_last_bar is None


def test_a_session_still_in_progress_is_not_a_gap():
    """Monday is inside the range but has not closed at `now`, so it is not missing yet."""
    in_session = z('2026-10-05T15:00:00Z')
    assert static(WEEK_START, WEEK_END, {OCT_1, OCT_2}, now=in_session).status is DatasetHealthStatus.COMPLETE
    after_close = z('2026-10-05T20:00:00Z')
    assert static(WEEK_START, WEEK_END, {OCT_1, OCT_2}, now=after_close).gap_count == 1


def test_gap_intervals_join_across_a_weekend_and_split_on_a_covered_session():
    """Runs are consecutive in SESSION order, so Fri and Mon missing is one run of two."""
    now = z('2026-12-31T00:00:00Z')
    assert gap_intervals(WEEK_START, WEEK_END, {OCT_1}, XNYS, now) == [GapInterval(OCT_2, OCT_5, 2)]
    assert gap_intervals(WEEK_START, WEEK_END, {OCT_2}, XNYS, now) == [
        GapInterval(OCT_1, OCT_1, 1),
        GapInterval(OCT_5, OCT_5, 1),
    ]
    assert gap_intervals(WEEK_START, WEEK_END, {OCT_1, OCT_2, OCT_5}, XNYS, now) == []
    # Agrees with the status's count: the interval lengths sum to gap_count.
    for covered in ({OCT_1}, {OCT_2}, set(), {OCT_5}):
        runs = gap_intervals(WEEK_START, WEEK_END, covered, XNYS, now)
        assert sum(run.sessions for run in runs) == static(WEEK_START, WEEK_END, covered).gap_count


def test_covered_sessions_reads_each_bar_on_its_new_york_date():
    """A bar at 00:30Z belongs to the PREVIOUS New York day; the UTC date would put it a session late."""
    assert session_of(z('2026-10-06T00:30:00Z'), XNYS) == OCT_5
    assert covered_sessions([z('2026-10-02T13:30:00Z'), z('2026-10-06T03:59:00Z')], XNYS) == {OCT_2, OCT_5}


# ------------------------------------------------------------------------------------------------- RETIRED


#
# Decision tj-hhy3dk: RETIRED is the collect intent (false on a DAILY or STREAM subscription), never the expiry, which
# is the deletion date only. evaluate_dataset takes no expiry at all, so it cannot read one.


def _subscription(update_type: UpdateType, collect: bool | None):
    return evaluate_dataset(
        update_type=update_type,
        granularity=Granularity.ONE_MINUTE,
        collect=collect,
        start=WEEK_START,
        end=WEEK_END,
        last_bar=None,  # would be OVERDUE if judged
        covered=None,
        calendar=XNYS,
        now=z('2026-12-01T15:00:00Z'),
    )


@pytest.mark.parametrize('update_type', [UpdateType.DAILY, UpdateType.STREAM])
def test_collect_false_on_a_subscription_retires_it_and_computes_nothing(update_type):
    health = _subscription(update_type, False)
    assert health == DatasetHealth(DatasetHealthStatus.RETIRED)
    assert health.expected_last_bar is None
    assert not health.is_failure


@pytest.mark.parametrize('collect', [True, None], ids=['collect true', 'collect null'])
@pytest.mark.parametrize('update_type', [UpdateType.DAILY, UpdateType.STREAM])
def test_a_collecting_subscription_is_judged_not_retired(update_type, collect):
    """Only an explicit false retires: true, and a NULL that the CHECK never lets a subscription hold, are judged."""
    health = _subscription(update_type, collect)
    assert health.status is DatasetHealthStatus.OVERDUE
    assert health.is_failure


def test_the_evaluation_takes_no_expiry_and_no_retired_on():
    """An expiry cannot retire anything here: it is no argument, and the health no longer carries a retire date."""
    assert 'expiry' not in inspect.signature(evaluate_dataset).parameters
    assert 'collect' in inspect.signature(evaluate_dataset).parameters
    assert 'retired_on' not in {field.name for field in dataclasses.fields(DatasetHealth)}


@pytest.mark.parametrize('collect', [None, False, True], ids=['collect null', 'collect false', 'collect true'])
def test_a_static_dataset_is_never_retired_whatever_collect_says(collect):
    """A STATIC dataset is judged for completeness: retirement is a subscription's state, and STATIC has no intent."""
    health = evaluate_dataset(
        update_type=UpdateType.STATIC,
        granularity=Granularity.ONE_DAY,
        collect=collect,
        start=WEEK_START,
        end=WEEK_END,
        last_bar=None,
        covered={OCT_1},
        calendar=XNYS,
        now=z('2026-12-01T15:00:00Z'),
    )
    assert (health.status, health.gap_count) == (DatasetHealthStatus.GAPS, 2)


# ------------------------------------------------------------------------------------------------- refusals


NAIVE = datetime(2026, 10, 5, 15, 0)


@pytest.mark.parametrize('argument', ['start', 'end', 'last_bar', 'now', 'last_bar_written_at'])
def test_a_naive_datetime_is_refused_by_name(argument):
    """Every instant argument is checked, and the refusal names which one was naive."""
    arguments = {
        'collect': True,
        'last_bar_written_at': None,
        'start': WEEK_START,
        'end': WEEK_END,
        'last_bar': z('2026-10-05T14:00:00Z'),
        'now': z('2026-10-05T15:00:00Z'),
    }
    arguments[argument] = NAIVE
    with pytest.raises(NaiveDatetimeError, match=argument):
        evaluate_dataset(
            update_type=UpdateType.DAILY, granularity=Granularity.ONE_DAY, covered=None, calendar=XNYS, **arguments
        )


def test_gap_intervals_and_session_of_refuse_naive_datetimes():
    with pytest.raises(NaiveDatetimeError):
        gap_intervals(NAIVE, WEEK_END, set(), XNYS, z('2026-12-01T00:00:00Z'))
    with pytest.raises(NaiveDatetimeError):
        session_of(NAIVE, XNYS)


@pytest.mark.parametrize('multiple', [0, -1])
def test_a_stream_bar_multiple_below_one_is_refused(multiple):
    with pytest.raises(ValueError, match='stream_bar_multiple'):
        HealthConfig(stream_bar_multiple=multiple)
    with pytest.raises(ValueError, match='stream_bar_multiple'):
        HealthConfig.from_env({STREAM_BAR_MULTIPLE_ENV: str(multiple)})


def test_the_stream_bar_multiple_comes_from_the_environment_with_the_ruled_default():
    assert HealthConfig.from_env({}).stream_bar_multiple == DEFAULT_STREAM_BAR_MULTIPLE == 3
    assert HealthConfig.from_env({STREAM_BAR_MULTIPLE_ENV: '7'}).stream_bar_multiple == 7


@pytest.mark.parametrize('source', [source for source in DataSource if source is not DataSource.ALPACA_API])
def test_a_source_with_no_calendar_is_refused(source):
    with pytest.raises(UnknownCalendarError):
        calendar_for_source(source)


def test_alpaca_trades_on_xnys():
    assert str(XNYS.tz) == 'America/New_York'
    assert XNYS.session_open(OCT_5) == z('2026-10-05T13:30:00Z')
    assert calendar_for_source(DataSource.ALPACA_API) is XNYS, 'the calendar is built once and cached'


# ---------------------------------------------------------------------------------------- the calendar's first day
#
# THE RULING (architect DECISION on tj-grna9p.102, option A): build the calendar from an EXPLICIT early start
# (CALENDAR_START, 1990-01-01) and raise a typed CalendarRangeError for a range or instant before it, instead of
# answering as if no sessions existed. Option B (a no-status state for out-of-range datasets) was rejected.
#
# THE DEFECT: exchange_calendars' default first session trails the process by twenty years (2006-10-06 on the day
# this was found), and the adapter clamped to it, so a 2001 STATIC range had no expected sessions and read COMPLETE
# with no bars, and a 2001 DAILY dataset read FRESH however far behind it was. Both cases below read otherwise
# under the default bound.
#
# The counts are the exchange's, written out by hand, not read back from the calendar: the week of Mon 5 Feb 2001
# holds five sessions (Presidents' Day is 19 Feb); [Mon 10 Sep, Mon 24 Sep 2001) holds six, since the NYSE stayed
# shut from 11 to 14 September; conftest's BASE_START..BASE_END [Mon 5 Feb 14:30Z, Tue 6 Feb 21:00Z) holds two.

FEB_5_2001, FEB_6_2001 = date(2001, 2, 5), date(2001, 2, 6)

# (start, end, sessions in range)
EARLY_STATIC_RANGES = {
    'week of 5 feb 2001': (daily_bar(FEB_5_2001), daily_bar(date(2001, 2, 12)), 5),
    'september 2001 closure': (daily_bar(date(2001, 9, 10)), daily_bar(date(2001, 9, 24)), 6),
    'system conftest BASE_START..BASE_END': (z('2001-02-05T14:30:00Z'), z('2001-02-06T21:00:00Z'), 2),
}


@pytest.mark.parametrize(('start', 'end', 'sessions'), EARLY_STATIC_RANGES.values(), ids=EARLY_STATIC_RANGES)
def test_a_2001_static_range_with_no_bars_has_a_gap_for_every_session(start, end, sessions):
    health = static(start, end, None)
    assert (health.status, health.gap_count) == (DatasetHealthStatus.GAPS, sessions)
    assert health.is_failure


def test_a_2001_static_range_with_every_session_covered_is_complete():
    """The other side of the same range: GAPS above is the calendar's sessions, not a refusal to judge 2001."""
    feb_week = {date(2001, 2, day) for day in (5, 6, 7, 8, 9)}
    assert static(daily_bar(FEB_5_2001), daily_bar(date(2001, 2, 12)), feb_week).status is DatasetHealthStatus.COMPLETE


def test_a_2001_daily_dataset_with_old_bars_is_overdue():
    """[Mon 5 Feb, Wed 7 Feb 2001) owes Tuesday 6 Feb; Monday's bar alone failed at Wednesday's open, 25 years ago."""
    start, end, now = daily_bar(FEB_5_2001), daily_bar(date(2001, 2, 7)), z('2026-10-06T15:00:00Z')
    assert_owes(daily_in(start, end, now, daily_bar(FEB_5_2001)), DatasetHealthStatus.OVERDUE, FEB_6_2001)
    assert_owes(daily_in(start, end, now), DatasetHealthStatus.OVERDUE, FEB_6_2001)
    assert_owes(daily_in(start, end, now, daily_bar(FEB_6_2001)), DatasetHealthStatus.FRESH, FEB_6_2001)


def test_the_calendar_starts_on_the_ruled_day():
    assert date(1990, 1, 1) == CALENDAR_START


# 1990-01-01 00:00 UTC is 19:00 on 31 Dec 1989 in New York: before the start, though its UTC date is not.
NEW_YEAR_1990_UTC = z('1990-01-01T00:00:00Z')
NEW_YEAR_1990_LOCAL = datetime(1990, 1, 1, tzinfo=NEW_YORK)
BEFORE_START = {
    'utc midnight 1990 (31 dec 1989 in new york)': NEW_YEAR_1990_UTC,
    'one tick before local midnight 1990': NEW_YEAR_1990_LOCAL - TICK,
    '1985': z('1985-06-03T15:00:00Z'),
}
AFTER_START = z('1990-01-10T00:00:00Z')


@pytest.mark.parametrize('instant', BEFORE_START.values(), ids=BEFORE_START)
def test_a_range_starting_before_the_calendar_start_raises(instant):
    with pytest.raises(CalendarRangeError):
        XNYS.sessions_overlapping(instant, AFTER_START)


@pytest.mark.parametrize('instant', BEFORE_START.values(), ids=BEFORE_START)
def test_last_completed_session_before_the_calendar_start_raises(instant):
    with pytest.raises(CalendarRangeError):
        XNYS.last_completed_session(instant)


@pytest.mark.parametrize('instant', BEFORE_START.values(), ids=BEFORE_START)
def test_session_in_progress_before_the_calendar_start_raises(instant):
    with pytest.raises(CalendarRangeError):
        XNYS.session_in_progress(instant)


@pytest.mark.parametrize('session', [date(1989, 12, 31), date(1989, 12, 29), date(1985, 6, 3)])
def test_next_session_after_a_day_before_the_calendar_start_raises(session):
    with pytest.raises(CalendarRangeError):
        XNYS.next_session(session)


@pytest.mark.parametrize('update_type', [UpdateType.STATIC, UpdateType.DAILY])
def test_a_dataset_starting_before_the_calendar_start_fails_loudly(update_type):
    """Through the public entry point: an error, never a COMPLETE or FRESH answered over no sessions."""
    with pytest.raises(CalendarRangeError):
        evaluate_dataset(
            update_type=update_type,
            granularity=Granularity.ONE_DAY,
            collect=None if update_type is UpdateType.STATIC else True,
            start=NEW_YEAR_1990_UTC,
            end=daily_bar(date(1990, 2, 1)),
            last_bar=None,
            covered=None,
            calendar=XNYS,
            now=z('2026-10-06T15:00:00Z'),
        )


def test_the_calendar_start_itself_is_covered():
    """1 Jan 1990 (a holiday) holds no session; the first is Tue 2 Jan. Nothing at the start raises."""
    assert XNYS.last_completed_session(NEW_YEAR_1990_LOCAL) is None
    assert XNYS.session_in_progress(NEW_YEAR_1990_LOCAL) is None
    assert XNYS.next_session(date(1990, 1, 1)) == date(1990, 1, 2)
    assert XNYS.sessions_overlapping(NEW_YEAR_1990_LOCAL, datetime(1990, 1, 3, tzinfo=NEW_YORK)) == [date(1990, 1, 2)]


# ------------------------------------------------------------------------------ the session table against the calendar
#
# a8a3046 (tj-sww0b1 item 7) stopped asking pandas for each session's open and close and reads them once into a
# list and two dicts, bisecting the list for a range. It is a pure speed change, so the pin is EQUIVALENCE: over
# every day below, the adapter answers exactly what the exchange_calendars calendar it wraps answers, asked
# directly. The oracle is the calendar's own data and calls (opens, closes, session_open, date_to_session), never
# the adapter's arithmetic; the boolean masks over opens and closes are the Protocol's own definitions.
#
# THE DAYS: a DST change (Sun 1 Nov 2026), Thanksgiving and its half-day, Christmas Eve's half-day, Christmas, New
# Year's Day 2027, the weekends between; the September 2001 closure; the calendar's first session (Tue 2 Jan 1990,
# after the 1 Jan holiday) and its last, the two ends where the range is clamped and the bisect sits on the edge.
# Non-session days are in every span, so session_open and session_close take their fallback branch there.

ORACLE = exchange_calendars.get_calendar('XNYS', start=pandas.Timestamp(CALENDAR_START))
TABLE = ExchangeCalendarAdapter(ORACLE, CALENDAR_START)
ORACLE_LAST = ORACLE.last_session.date()


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


EQUIVALENCE_DAYS = sorted(
    set(_span(date(2026, 10, 28), date(2027, 1, 6)))
    | set(_span(date(2001, 9, 7), date(2001, 9, 19)))
    | set(_span(CALENDAR_START, date(1990, 1, 5)))
    | set(_span(ORACLE_LAST - timedelta(days=4), ORACLE_LAST))
)


def _is_session(day: date) -> bool:
    return pandas.Timestamp(day) in ORACLE.sessions


def _oracle_open(day: date) -> datetime:
    return ORACLE.session_open(pandas.Timestamp(day)).to_pydatetime()


def _oracle_close(day: date) -> datetime:
    return ORACLE.session_close(pandas.Timestamp(day)).to_pydatetime()


def _instants() -> list[datetime]:
    """Each day's local and UTC midnight, and every session's open and close with a tick either side."""
    found = set()
    for day in EQUIVALENCE_DAYS:
        found.add(datetime.combine(day, datetime.min.time(), tzinfo=NEW_YORK))
        found.add(datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo('UTC')))
        found.add(datetime.combine(day, datetime.min.time().replace(hour=12), tzinfo=NEW_YORK))
        if _is_session(day):
            for edge in (_oracle_open(day), _oracle_close(day)):
                found.update({edge - TICK, edge, edge + TICK})
    # Only instants on or after the calendar start: before it is a CalendarRangeError, pinned above.
    return sorted(i for i in found if i.astimezone(NEW_YORK).date() >= CALENDAR_START)


INSTANTS = _instants()


def _dates(index) -> list[date]:
    return [label.date() for label in index]


def _oracle_overlapping(start: datetime, end: datetime) -> list[date]:
    """The Protocol's definition, over the whole table: [open, close) meets [start, end)."""
    mask = (ORACLE.opens < pandas.Timestamp(end)) & (ORACLE.closes > pandas.Timestamp(start))
    return _dates(ORACLE.opens.index[mask])


def _oracle_last_completed(now: datetime) -> date | None:
    done = ORACLE.closes.index[ORACLE.closes <= pandas.Timestamp(now)]
    return done[-1].date() if len(done) else None


def _oracle_in_progress(now: datetime) -> date | None:
    stamp = pandas.Timestamp(now)
    found = _dates(ORACLE.opens.index[(ORACLE.opens <= stamp) & (ORACLE.closes > stamp)])
    assert len(found) <= 1, found
    return found[0] if found else None


def _windows() -> list[tuple[datetime, datetime]]:
    """From every instant: a tick, each of the next eight instants, three days and ten days."""
    windows = []
    for index, start in enumerate(INSTANTS):
        ends = [start + TICK, start + timedelta(days=3), start + timedelta(days=10)]
        ends += INSTANTS[index + 1 : index + 9]
        windows.extend((start, end) for end in ends)
    return windows


def test_the_equivalence_span_holds_what_it_claims():
    """Guard the guard: half-days, a holiday, weekends, the 2001 closure and both ends of the table are in it."""
    sessions = [day for day in EQUIVALENCE_DAYS if _is_session(day)]
    non_sessions = [day for day in EQUIVALENCE_DAYS if not _is_session(day)]
    assert {date(2026, 11, 27), date(2026, 12, 24)} <= set(sessions)
    assert {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1), date(2001, 9, 11), date(1990, 1, 1)} <= set(
        non_sessions
    )
    assert any(day.weekday() >= 5 for day in non_sessions)
    assert _oracle_close(date(2026, 11, 27)) == z('2026-11-27T18:00:00Z'), 'the half-day is not a half-day'
    assert ORACLE.first_session.date() in sessions and ORACLE_LAST in sessions
    assert calendar_for_source(DataSource.ALPACA_API).__class__ is ExchangeCalendarAdapter


def test_session_open_and_close_equal_the_calendars_on_every_session():
    for day in EQUIVALENCE_DAYS:
        if _is_session(day):
            for got, want in (
                (TABLE.session_open(day), _oracle_open(day)),
                (TABLE.session_close(day), _oracle_close(day)),
            ):
                assert (got, got.tzinfo) == (want, want.tzinfo), (day, got, want)


@pytest.mark.parametrize('method', ['session_open', 'session_close'])
def test_a_non_session_day_gets_the_calendars_own_refusal(method: str):
    """The fallback branch: not in the table, so the calendar is asked, and its NotSessionError comes back as is."""
    refused = 0
    for day in EQUIVALENCE_DAYS:
        if _is_session(day):
            continue
        with pytest.raises(exchange_calendars.errors.NotSessionError) as table_error:
            getattr(TABLE, method)(day)
        with pytest.raises(exchange_calendars.errors.NotSessionError) as oracle_error:
            getattr(ORACLE, method)(pandas.Timestamp(day))
        assert str(table_error.value) == str(oracle_error.value), day
        refused += 1
    assert refused > 20, refused


def test_sessions_overlapping_equals_the_calendar_over_every_boundary_window():
    windows = _windows()
    assert len(windows) > 5000, len(windows)
    differ = [
        (start, end, got, want)
        for start, end in windows
        if (got := TABLE.sessions_overlapping(start, end)) != (want := _oracle_overlapping(start, end))
    ]
    assert not differ, differ[:5]


def test_sessions_overlapping_reaches_both_ends_of_the_table():
    """The clamped edges, where the bisect bounds are the table's first and last sessions themselves."""
    first, last = ORACLE.first_session.date(), ORACLE_LAST
    assert TABLE.sessions_overlapping(
        datetime(1990, 1, 1, tzinfo=NEW_YORK), datetime(1990, 1, 2, 12, tzinfo=NEW_YORK)
    ) == [first]
    tail = TABLE.sessions_overlapping(_oracle_open(last) - timedelta(days=1), _oracle_close(last) + timedelta(days=30))
    assert tail[-1] == last and tail == _oracle_overlapping(_oracle_open(last) - timedelta(days=1), _oracle_close(last))


def test_last_completed_session_and_session_in_progress_equal_the_calendar_at_every_instant():
    differ = [
        (now, TABLE.last_completed_session(now), _oracle_last_completed(now))
        for now in INSTANTS
        if TABLE.last_completed_session(now) != _oracle_last_completed(now)
    ]
    differ += [
        (now, TABLE.session_in_progress(now), _oracle_in_progress(now))
        for now in INSTANTS
        if TABLE.session_in_progress(now) != _oracle_in_progress(now)
    ]
    assert not differ, differ[:5]


def test_next_session_equals_the_calendar_on_every_day_before_the_last():
    for day in EQUIVALENCE_DAYS:
        if day < ORACLE_LAST:
            want = ORACLE.date_to_session(pandas.Timestamp(day + timedelta(days=1)), direction='next').date()
            assert TABLE.next_session(day) == want, day


def test_the_table_still_refuses_before_the_calendar_start():
    """The session table changed nothing at the lower bound: every entry point still raises CalendarRangeError."""
    before = datetime(1989, 12, 31, 12, tzinfo=NEW_YORK)
    for call in (
        lambda: TABLE.sessions_overlapping(before, z('1990-01-10T00:00:00Z')),
        lambda: TABLE.last_completed_session(before),
        lambda: TABLE.session_in_progress(before),
        lambda: TABLE.next_session(date(1989, 12, 29)),
    ):
        with pytest.raises(CalendarRangeError):
            call()
