"""A bar is FINAL only if it was written after its period closed (validator, gating tj-grna9p.167).

DESIGN: bug tj-grna9p.167, option (A) as the architect recommended it, plus its clarification note; decision
tj-grna9p.8 (the DAILY rule and the 2026-10-20 period addendum) is the context:
    (1) a bar is FINAL when it was written (updated_at) at or after the close of its period: the bar's own end for
        1min..1hour, the session close for 1day, the close of the last session of the week or month for 1week and
        1month, in the calendar's zone. A period holding no session has no close, so its bar is final.
    (2) a DAILY dataset reads FRESH only when its newest bar is of the expected period AND final; a provisional one
        reads LATE with the normal deadline (the next session's open after the expected period), OVERDUE after it.
        A newer, still open period reads as before. Unknown write time is final.
    (3) the refresh's tail starts AT a provisional newest bar (inclusive), whatever the expected period, else at the
        newest bar plus one bar, else at the declared start.
    (4) STATIC, STREAM, retired datasets, and every completed-bar result for 1min..1day, are unchanged.

THE CLOSES ARE WRITTEN BY HAND from the NYSE 2026 schedule (16:00 local, 13:00 on an early-close day; EDT is UTC-4
until Sun 1 Nov, EST UTC-5 after), never derived from the code under test:
    Tue 20 Oct   close 20:00Z          Fri 27 Nov (after Thanksgiving)   early close 18:00Z
    Fri 30 Oct   close 20:00Z (EDT)    Mon 2 Nov (first EST weekday)     close 21:00Z
    Thu 2 Apr    close 20:00Z, the last session of its week (Good Friday 3 Apr is a holiday)
    Fri 29 May   close 20:00Z, the last session of May (31 May is a Sunday)
    Fri 30 Oct   the last session of October (31 Oct is a Saturday)
    Thu 31 Dec   close 21:00Z, the last session of December

Tiers: the pure rules over the real XNYS calendar; the refresh's planning over CatalogRows; the real pass and the
real catalog routes over SQLite (the fixtures of test_daily_refresh.py and test_ui_datasets_route.py). That the
Postgres upsert really advances updated_at on a conflicting row, and that the repaired bar then reads final, is
tests/system/test_daily_refresh_pass.py's.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import daily_refresh as refresh_module
from data.store.app.daily_refresh import DailyRefresher, PassResult, _fetch_start, _request_for
from data.store.app.database.crud.stock.dataset_catalog import (
    CatalogFilter,
    CatalogRow,
    get_catalog_row,
    list_catalog,
    list_pending,
)
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.freshness import (
    DatasetHealth,
    FreshnessStatus,
    NaiveDatetimeError,
    bar_period_close,
    evaluate_dataset,
    last_bar_is_provisional,
    session_of,
)
from data.store.tests import test_daily_refresh, test_ui_datasets_route
from data.store.tests.test_daily_refresh import LAST_BAR, LAST_CLOSE, ScriptedClient, World, _add, _scheduled_run
from data.store.tests.test_freshness_periods import (
    GRID_LAST_BARS,
    GRID_NOWS,
    GRID_RANGES,
    LATER,
    MONTHLY_SUB_START,
    OPEN_ENDED,
    SESSION_GRANULARITIES,
    WEEKLY_SUB_START,
    XNYS,
    _old_daily,
    d,
    m,
    z,
)
from data.store.tests.test_ui_datasets_route import NOW as ROUTE_NOW
from data.store.tests.test_ui_datasets_route import bar, entry, list_all, ny_midnight, status_of, store


pytestmark = pytest.mark.data_store

# The fixtures of the two SQLite tiers, reused as they are: the pass's world, and the route tier's engine and app.
aware_sqlite_datetimes = test_ui_datasets_route.aware_sqlite_datetimes
world = test_daily_refresh.world
engine = test_ui_datasets_route.engine
client = test_ui_datasets_route.client

TICK = timedelta(microseconds=1)
DAY = Granularity.ONE_DAY
WEEK = Granularity.ONE_WEEK
MONTH = Granularity.ONE_MONTH
INTRADAY = [Granularity.ONE_MINUTE, Granularity.FIVE_MINUTES, Granularity.THIRTY_MINUTES, Granularity.ONE_HOUR]

# The seed scenario of the bug: DIA 1day, newest bar Tue 20 Oct (stamped 04:00Z, local midnight), fetched at 16:16Z
# during the session (13:30-20:00Z). The next session opens Wed 21 Oct 13:30Z.
SEED_BAR = m(d('2026-10-20'))
SEED_WRITTEN = z('2026-10-20T16:16:00Z')
OCT20_CLOSE = z('2026-10-20T20:00:00Z')
OCT21_OPEN = z('2026-10-21T13:30:00Z')
SUB_START = m(d('2026-09-01'))


def daily(start, now, last_bar, granularity, written, end=OPEN_ENDED, expiry=None) -> DatasetHealth:
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=granularity,
        expiry=expiry,
        start=start,
        end=end,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
        last_bar_written_at=written,
    )


# ------------------------------------------------------------------------------------------------- bar_period_close

# (bar, granularity, close) with the close written by hand (see the module docstring).
CLOSE_CASES = {
    # 1min..1hour: the bar's own end, timestamp + granularity, whatever the session.
    '1min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.ONE_MINUTE, z('2026-10-20T15:01:00Z')),
    '5min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.FIVE_MINUTES, z('2026-10-20T15:05:00Z')),
    '30min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.THIRTY_MINUTES, z('2026-10-20T15:30:00Z')),
    '1hour mid-session': (z('2026-10-20T15:00:00Z'), Granularity.ONE_HOUR, z('2026-10-20T16:00:00Z')),
    # The last hourly bar of a session ends past the close: its own end still decides.
    '1hour bar straddling the close': (z('2026-10-20T19:30:00Z'), Granularity.ONE_HOUR, z('2026-10-20T20:30:00Z')),
    '1min, last of an early-close day': (z('2026-11-27T17:59:00Z'), Granularity.ONE_MINUTE, z('2026-11-27T18:00:00Z')),
    # 1day: the bar's session close.
    '1day at local midnight': (SEED_BAR, DAY, OCT20_CLOSE),
    '1day stamped mid-session': (z('2026-10-20T15:00:00Z'), DAY, OCT20_CLOSE),
    '1day stamped at UTC midnight (the previous NY evening)': (z('2026-10-21T00:00:00Z'), DAY, OCT20_CLOSE),
    '1day on an early-close day': (m(d('2026-11-27')), DAY, z('2026-11-27T18:00:00Z')),
    '1day on Christmas Eve, early close': (m(d('2026-12-24')), DAY, z('2026-12-24T18:00:00Z')),
    '1day, last EDT Friday': (m(d('2026-10-30')), DAY, z('2026-10-30T20:00:00Z')),
    '1day, first EST Monday after the DST change': (m(d('2026-11-02')), DAY, z('2026-11-02T21:00:00Z')),
    # 1week: the close of the last session of the Monday..Sunday week, whichever day the bar is stamped.
    '1week stamped Monday': (m(d('2026-10-19')), WEEK, z('2026-10-23T20:00:00Z')),
    '1week stamped Sunday': (m(d('2026-10-25')), WEEK, z('2026-10-23T20:00:00Z')),
    '1week with a holiday Friday (Good Friday)': (m(d('2026-03-30')), WEEK, z('2026-04-02T20:00:00Z')),
    '1week ending on an early-close Friday': (m(d('2026-11-23')), WEEK, z('2026-11-27T18:00:00Z')),
    '1week spanning the DST change ends EST': (m(d('2026-10-26')), WEEK, z('2026-10-30T20:00:00Z')),
    '1week after the DST change': (m(d('2026-11-02')), WEEK, z('2026-11-06T21:00:00Z')),
    # 1month: the close of the last session of the calendar month.
    '1month ending on a Saturday (October)': (m(d('2026-10-01')), MONTH, z('2026-10-30T20:00:00Z')),
    '1month ending on a Sunday (May)': (m(d('2026-05-01')), MONTH, z('2026-05-29T20:00:00Z')),
    '1month stamped mid-month': (m(d('2026-10-15')), MONTH, z('2026-10-30T20:00:00Z')),
    '1month ending on a Monday, EST (November)': (m(d('2026-11-01')), MONTH, z('2026-11-30T21:00:00Z')),
    '1month ending on a Thursday (December)': (m(d('2026-12-01')), MONTH, z('2026-12-31T21:00:00Z')),
    '1month, September ends Wednesday': (m(d('2026-09-01')), MONTH, z('2026-09-30T20:00:00Z')),
}


@pytest.mark.parametrize(('bar_at', 'granularity', 'close'), CLOSE_CASES.values(), ids=CLOSE_CASES)
def test_bar_period_close_is_the_close_of_the_bar_s_period(bar_at, granularity, close):
    assert bar_period_close(bar_at, XNYS, granularity) == close


NO_SESSION_CASES = {
    'a 1day bar on a Saturday': (m(d('2026-10-24')), DAY),
    'a 1day bar on a Sunday': (m(d('2026-10-25')), DAY),
    'a 1day bar on Thanksgiving': (m(d('2026-11-26')), DAY),
    'a 1day bar on Good Friday': (m(d('2026-04-03')), DAY),
}


@pytest.mark.parametrize(('bar_at', 'granularity'), NO_SESSION_CASES.values(), ids=NO_SESSION_CASES)
def test_a_bar_whose_period_holds_no_session_has_no_close_and_is_final(bar_at, granularity):
    assert bar_period_close(bar_at, XNYS, granularity) is None
    # Written at any time, even a day before its own timestamp, it is never provisional.
    assert not last_bar_is_provisional(bar_at, bar_at - timedelta(days=1), XNYS, granularity)


@pytest.mark.parametrize('granularity', INTRADAY, ids=str)
def test_an_intraday_bar_on_a_weekend_still_has_its_own_end_as_its_close(granularity):
    """The code's rule, pinned: 1min..1hour never consult the calendar, so a stray weekend bar is not None."""
    saturday = z('2026-10-24T15:00:00Z')
    assert bar_period_close(saturday, XNYS, granularity) == saturday + granularity.offset


def test_bar_period_close_refuses_a_naive_bar():
    with pytest.raises(NaiveDatetimeError):
        bar_period_close(datetime(2026, 10, 20), XNYS, DAY)


# ------------------------------------------------------------------------------------------------- provisional

# (bar, written, granularity, provisional). The rule is written_at < close: written AT the close is final.
PROVISIONAL_CASES = {
    '1day written one tick before the close': (SEED_BAR, OCT20_CLOSE - TICK, DAY, True),
    '1day written exactly at the close is final': (SEED_BAR, OCT20_CLOSE, DAY, False),
    '1day written a tick after the close': (SEED_BAR, OCT20_CLOSE + TICK, DAY, False),
    '1day written mid-session (the seed)': (SEED_BAR, SEED_WRITTEN, DAY, True),
    '1day written before its own session opened': (SEED_BAR, z('2026-10-20T05:00:00Z'), DAY, True),
    '1day early close, a tick before 18:00Z': (m(d('2026-11-27')), z('2026-11-27T18:00:00Z') - TICK, DAY, True),
    '1day early close, at 18:00Z (not 21:00Z) is final': (m(d('2026-11-27')), z('2026-11-27T18:00:00Z'), DAY, False),
    '1day EST close, at 20:00Z is still provisional': (m(d('2026-11-02')), z('2026-11-02T20:00:00Z'), DAY, True),
    '1min, a tick before its own end': (
        z('2026-10-20T15:00:00Z'),
        z('2026-10-20T15:01:00Z') - TICK,
        Granularity.ONE_MINUTE,
        True,
    ),
    '1min, at its own end': (z('2026-10-20T15:00:00Z'), z('2026-10-20T15:01:00Z'), Granularity.ONE_MINUTE, False),
    '1hour, at its own end, before the session close': (
        z('2026-10-20T15:00:00Z'),
        z('2026-10-20T16:00:00Z'),
        Granularity.ONE_HOUR,
        False,
    ),
    '1week, Thursday of a Good Friday week, after its close': (
        m(d('2026-03-30')),
        z('2026-04-02T20:00:00Z'),
        WEEK,
        False,
    ),
    '1week, Thursday of a Good Friday week, before its close': (
        m(d('2026-03-30')),
        z('2026-04-02T19:59:59Z'),
        WEEK,
        True,
    ),
    '1week written on the Wednesday': (m(d('2026-10-19')), z('2026-10-21T21:00:00Z'), WEEK, True),
    '1month, after the last session of May': (m(d('2026-05-01')), z('2026-05-29T20:00:00Z'), MONTH, False),
    '1month, on the 31st of October, a Saturday': (m(d('2026-10-01')), z('2026-10-31T12:00:00Z'), MONTH, False),
    '1month, a tick before the last close of October': (
        m(d('2026-10-01')),
        z('2026-10-30T20:00:00Z') - TICK,
        MONTH,
        True,
    ),
}


@pytest.mark.parametrize(
    ('bar_at', 'written', 'granularity', 'provisional'), PROVISIONAL_CASES.values(), ids=PROVISIONAL_CASES
)
def test_a_bar_is_provisional_only_when_written_before_its_period_closed(bar_at, written, granularity, provisional):
    assert last_bar_is_provisional(bar_at, written, XNYS, granularity) is provisional


@pytest.mark.parametrize('granularity', list(Granularity), ids=str)
def test_no_bar_or_no_write_time_is_not_provisional(granularity):
    assert not last_bar_is_provisional(None, SEED_WRITTEN, XNYS, granularity)
    assert not last_bar_is_provisional(SEED_BAR, None, XNYS, granularity)
    assert not last_bar_is_provisional(None, None, XNYS, granularity)


def test_a_naive_write_time_is_refused():
    with pytest.raises(NaiveDatetimeError):
        last_bar_is_provisional(SEED_BAR, datetime(2026, 10, 20, 16, 16), XNYS, DAY)
    with pytest.raises(NaiveDatetimeError):
        daily(SUB_START, OCT20_CLOSE, SEED_BAR, DAY, datetime(2026, 10, 20, 16, 16))


# ------------------------------------------------------------------------------------------------- DAILY freshness

# (now, written, status) for the seed: the expected period is Tue 20 Oct from its close until Wed 21 Oct's close.
SEED_CASES = {
    'at the 20:30Z pass, written mid-session: LATE': (z('2026-10-20T20:30:00Z'), SEED_WRITTEN, FreshnessStatus.LATE),
    'at the close itself, written mid-session: LATE': (OCT20_CLOSE, SEED_WRITTEN, FreshnessStatus.LATE),
    'a tick before the next open: still LATE': (OCT21_OPEN - TICK, SEED_WRITTEN, FreshnessStatus.LATE),
    'at the next open: OVERDUE': (OCT21_OPEN, SEED_WRITTEN, FreshnessStatus.OVERDUE),
    'during the next session: OVERDUE': (z('2026-10-21T17:00:00Z'), SEED_WRITTEN, FreshnessStatus.OVERDUE),
    'a tick before the next close: OVERDUE': (z('2026-10-21T20:00:00Z') - TICK, SEED_WRITTEN, FreshnessStatus.OVERDUE),
    'written after the close: FRESH at once': (
        z('2026-10-20T20:30:00Z'),
        z('2026-10-20T20:10:00Z'),
        FreshnessStatus.FRESH,
    ),
    'written at the close: FRESH': (z('2026-10-20T20:30:00Z'), OCT20_CLOSE, FreshnessStatus.FRESH),
    'unknown write time: FRESH': (z('2026-10-20T20:30:00Z'), None, FreshnessStatus.FRESH),
    'repaired overnight: FRESH': (z('2026-10-21T01:00:00Z'), z('2026-10-21T00:30:00Z'), FreshnessStatus.FRESH),
    'repaired during the next session: FRESH again': (
        z('2026-10-21T17:00:00Z'),
        z('2026-10-21T16:00:00Z'),
        FreshnessStatus.FRESH,
    ),
}


@pytest.mark.parametrize(('now', 'written', 'status'), SEED_CASES.values(), ids=SEED_CASES)
def test_the_seed_scenario(now, written, status):
    assert daily(SUB_START, now, SEED_BAR, DAY, written) == DatasetHealth(status, d('2026-10-20'), SEED_BAR)


def test_during_the_open_session_the_provisional_bar_of_that_session_reads_as_before():
    """At 16:30Z on the 20th the expected period is the 19th; the 20th's still-open bar is newer and owes nothing."""
    now = z('2026-10-20T16:30:00Z')
    got = daily(SUB_START, now, SEED_BAR, DAY, SEED_WRITTEN)
    assert got == daily(SUB_START, now, SEED_BAR, DAY, None)
    assert got == DatasetHealth(FreshnessStatus.FRESH, d('2026-10-19'), m(d('2026-10-19')))


def test_a_missed_pass_reads_as_before_the_change():
    """The provisional bar's session is already behind the expected one: the old rule judges it, unchanged."""
    now = z('2026-10-22T21:00:00Z')  # after the 22nd's close; the 21st was never stored
    got = daily(SUB_START, now, SEED_BAR, DAY, SEED_WRITTEN)
    assert got == daily(SUB_START, now, SEED_BAR, DAY, None) == _old_daily(SUB_START, OPEN_ENDED, SEED_BAR, XNYS, now)
    assert got.status is FreshnessStatus.OVERDUE


def test_the_provisional_deadline_is_the_next_open_after_an_early_close_and_a_holiday():
    """Fri 27 Nov closes 18:00Z; the next session is Mon 30 Nov, opening 14:30Z (EST)."""
    friday = m(d('2026-11-27'))
    written = z('2026-11-27T17:00:00Z')
    expected = DatasetHealth(FreshnessStatus.LATE, d('2026-11-27'), friday)
    assert daily(SUB_START, z('2026-11-27T18:00:00Z'), friday, DAY, written) == expected
    assert daily(SUB_START, z('2026-11-30T14:30:00Z') - TICK, friday, DAY, written) == expected
    assert daily(SUB_START, z('2026-11-30T14:30:00Z'), friday, DAY, written).status is FreshnessStatus.OVERDUE


@pytest.mark.parametrize('granularity', INTRADAY, ids=str)
def test_an_intraday_subscription_whose_last_bar_was_written_before_its_own_end_reads_late(granularity):
    """The last bar of the session (19:59Z for 1min) fetched a tick before its own end is provisional."""
    last = OCT20_CLOSE - granularity.offset
    now = z('2026-10-20T20:30:00Z')
    assert daily(SUB_START, now, last, granularity, OCT20_CLOSE - TICK).status is FreshnessStatus.LATE
    assert daily(SUB_START, now, last, granularity, OCT20_CLOSE).status is FreshnessStatus.FRESH
    assert daily(SUB_START, OCT21_OPEN, last, granularity, OCT20_CLOSE - TICK).status is FreshnessStatus.OVERDUE


def test_known_gap_an_intraday_subscription_fetched_mid_session_reads_fresh_with_the_rest_of_the_session_missing():
    """DOCUMENTS CURRENT BEHAVIOUR, not the design's intent; the architect rules on it (tj-grna9p.167 follow-up).

    A 1min DAILY dataset fetched at 16:16Z holds bars to 16:15Z, the last one complete (its own end, 16:16Z, has
    passed). At the 20:30Z pass the expected period is the 20th and the last bar's period IS the 20th, so it reads
    FRESH, although 16:16Z..20:00Z was never fetched. Judging 1min..1hour by the session close would make it LATE,
    and changes the intraday semantics. If that is ruled, this test flips to LATE.
    """
    last = z('2026-10-20T16:15:00Z')
    written = z('2026-10-20T16:16:00Z')
    assert not last_bar_is_provisional(last, written, XNYS, Granularity.ONE_MINUTE)
    got = daily(SUB_START, z('2026-10-20T20:30:00Z'), last, Granularity.ONE_MINUTE, written)
    assert got.status is FreshnessStatus.FRESH


# Weekly: the week of Mon 12 Oct ends with Fri 16 Oct's close (20:00Z); the next session opens Mon 19 Oct 13:30Z.
WEEK_BAR = m(d('2026-10-12'))
# Good Friday weeks need a subscription started before them (WEEKLY_SUB_START is in August): Mon 2 Mar 2026.
SPRING_START = m(d('2026-03-02'))
# Monthly: September ends with Wed 30 Sep's close (20:00Z); the next session opens Thu 1 Oct 13:30Z.
MONTH_BAR = m(d('2026-09-01'))
PERIODIC_CASES = {
    'week, written Friday mid-session, Saturday: LATE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T15:00:00Z'),
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'week, a tick before Monday open: LATE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T15:00:00Z'),
        z('2026-10-19T13:30:00Z') - TICK,
        FreshnessStatus.LATE,
    ),
    'week, at Monday open: OVERDUE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T15:00:00Z'),
        z('2026-10-19T13:30:00Z'),
        FreshnessStatus.OVERDUE,
    ),
    'week, written Tuesday, Saturday: LATE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-13T15:00:00Z'),
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'week, written at the Friday close: FRESH': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T20:00:00Z'),
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.FRESH,
    ),
    'week, unknown write time: FRESH': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        None,
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.FRESH,
    ),
    'Good Friday week, written Thu 19:00Z, on Good Friday: LATE': (
        WEEK,
        SPRING_START,
        m(d('2026-03-30')),
        z('2026-04-02T19:00:00Z'),
        z('2026-04-03T15:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'Good Friday week, written Thu 20:00Z (its last close): FRESH': (
        WEEK,
        SPRING_START,
        m(d('2026-03-30')),
        z('2026-04-02T20:00:00Z'),
        z('2026-04-03T15:00:00Z'),
        FreshnessStatus.FRESH,
    ),
    'Good Friday week, at Mon 6 Apr open: OVERDUE': (
        WEEK,
        SPRING_START,
        m(d('2026-03-30')),
        z('2026-04-02T19:00:00Z'),
        z('2026-04-06T13:30:00Z'),
        FreshnessStatus.OVERDUE,
    ),
    'month, written a minute before the 30th close: LATE': (
        MONTH,
        MONTHLY_SUB_START,
        MONTH_BAR,
        z('2026-09-30T19:59:00Z'),
        z('2026-10-01T01:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'month, at the 1 Oct open: OVERDUE': (
        MONTH,
        MONTHLY_SUB_START,
        MONTH_BAR,
        z('2026-09-30T19:59:00Z'),
        z('2026-10-01T13:30:00Z'),
        FreshnessStatus.OVERDUE,
    ),
    'month, written at the 30th close: FRESH': (
        MONTH,
        MONTHLY_SUB_START,
        MONTH_BAR,
        z('2026-09-30T20:00:00Z'),
        z('2026-10-01T01:00:00Z'),
        FreshnessStatus.FRESH,
    ),
}


@pytest.mark.parametrize(
    ('granularity', 'start', 'last_bar', 'written', 'now', 'status'), PERIODIC_CASES.values(), ids=PERIODIC_CASES
)
def test_weekly_and_monthly_subscriptions_judge_their_last_bar_by_the_period_s_last_close(
    granularity, start, last_bar, written, now, status
):
    got = daily(start, now, last_bar, granularity, written)
    assert got.status is status
    assert got.expected_last_session == last_bar.date()


def test_a_still_open_week_holding_a_provisional_bar_reads_as_before():
    """Wed 21 Oct: the expected week is the 12th's; the bar of the week of the 19th is newer and owes nothing."""
    now = z('2026-10-21T15:00:00Z')
    bar_at = m(d('2026-10-19'))
    got = daily(WEEKLY_SUB_START, now, bar_at, WEEK, z('2026-10-20T15:00:00Z'))
    assert got == daily(WEEKLY_SUB_START, now, bar_at, WEEK, None)
    assert got.status is FreshnessStatus.FRESH


@pytest.mark.parametrize('update_type', [UpdateType.DAILY, UpdateType.STREAM], ids=str)
def test_a_retired_dataset_is_retired_whatever_its_last_bar_s_write_time(update_type):
    expiry = z('2026-10-25T00:00:00Z')
    for written in (None, SEED_WRITTEN, OCT20_CLOSE):
        got = evaluate_dataset(
            update_type=update_type,
            granularity=DAY,
            expiry=expiry,
            start=SUB_START,
            end=OPEN_ENDED,
            last_bar=SEED_BAR,
            covered=None,
            calendar=XNYS,
            now=z('2026-10-20T20:30:00Z'),
            last_bar_written_at=written,
        )
        assert got == DatasetHealth(FreshnessStatus.RETIRED, retired_on=expiry)


@pytest.mark.parametrize('update_type', [UpdateType.STATIC, UpdateType.STREAM], ids=str)
@pytest.mark.parametrize('granularity', list(Granularity), ids=str)
def test_static_and_stream_ignore_the_write_time(update_type, granularity):
    """A STATIC range ending inside an open session is a separate, request-shape question (clarification note)."""
    sessions = XNYS.sessions_overlapping(SUB_START, OCT20_CLOSE)
    for now in (z('2026-10-20T16:30:00Z'), z('2026-10-20T20:30:00Z'), OCT21_OPEN):
        results = {
            evaluate_dataset(
                update_type=update_type,
                granularity=granularity,
                expiry=None,
                start=SUB_START,
                end=m(d('2026-10-21')),
                last_bar=SEED_BAR,
                covered=frozenset(sessions),
                calendar=XNYS,
                now=now,
                last_bar_written_at=written,
            )
            for written in (None, SEED_WRITTEN, OCT20_CLOSE, LATER)
        }
        assert len(results) == 1, results


# ------------------------------------------------------------------------------------------------- equivalence grid
#
# Over the grid of test_freshness_periods.py (a weekend, the DST change, Thanksgiving and its half-day, boundary ranges,
# every open and close a tick either side): a FINAL or unknown write time gives exactly the pre-change result for
# 1min..1day, and a provisional one changes only a FRESH whose last bar is of the expected period, into LATE before the
# next open and OVERDUE from it, with the same expected period.


@pytest.mark.parametrize('granularity', SESSION_GRANULARITIES, ids=str)
def test_a_completed_bar_gives_exactly_the_pre_change_result(granularity):
    differ = []
    for start, end in GRID_RANGES:
        for now in GRID_NOWS:
            for last_bar in GRID_LAST_BARS:
                old = _old_daily(start, end, last_bar, XNYS, now)
                close = None if last_bar is None else bar_period_close(last_bar, XNYS, granularity)
                for written in {None, LATER, close or LATER}:
                    got = daily(start, now, last_bar, granularity, written, end)
                    if got != old:
                        differ.append((start, end, now, last_bar, written, got, old))
    assert not differ, differ[:5]


@pytest.mark.parametrize('granularity', SESSION_GRANULARITIES, ids=str)
def test_a_provisional_bar_only_ever_withholds_fresh_for_its_own_expected_period(granularity):
    differ, flipped = [], 0
    for start, end in GRID_RANGES:
        for now in GRID_NOWS:
            for last_bar in GRID_LAST_BARS:
                if last_bar is None:
                    continue
                old = _old_daily(start, end, last_bar, XNYS, now)
                written = last_bar - timedelta(days=1)  # before any close of its period
                got = daily(start, now, last_bar, granularity, written, end)
                own_period = old.expected_last_session == session_of(last_bar, XNYS)
                provisional = last_bar_is_provisional(last_bar, written, XNYS, granularity)
                if old.status is FreshnessStatus.FRESH and own_period and provisional:
                    flipped += 1
                    deadline = XNYS.session_open(XNYS.next_session(old.expected_last_session))
                    want = FreshnessStatus.LATE if now < deadline else FreshnessStatus.OVERDUE
                    if got != DatasetHealth(want, old.expected_last_session, old.expected_last_bar):
                        differ.append(('flip', start, end, now, last_bar, got, old))
                elif got != old:
                    differ.append(('same', start, end, now, last_bar, got, old))
    assert not differ, differ[:5]
    assert flipped, 'the grid never reached a provisional bar of the expected period'


# ------------------------------------------------------------------------------------------------- the fetch start


def _row(
    last_bar: datetime | None,
    written: datetime | None,
    granularity: Granularity = DAY,
    start: datetime = SUB_START,
    expiry: datetime | None = None,
) -> CatalogRow:
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='DIA',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=granularity,
        start=start,
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=expiry,
        owner='operator',
        last_bar=last_bar,
        last_bar_written_at=written,
    )


FETCH_CASES = {
    'provisional 1day: the last bar itself': (_row(SEED_BAR, SEED_WRITTEN), SEED_BAR),
    'a tick before the close: the last bar itself': (_row(SEED_BAR, OCT20_CLOSE - TICK), SEED_BAR),
    'final 1day: one bar on': (_row(SEED_BAR, OCT20_CLOSE), SEED_BAR + DAY.offset),
    'unknown write time: one bar on': (_row(SEED_BAR, None), SEED_BAR + DAY.offset),
    'no bars: the declared start': (_row(None, None), SUB_START),
    'provisional 1min: the last bar itself': (
        _row(z('2026-10-20T19:59:00Z'), z('2026-10-20T19:59:30Z'), Granularity.ONE_MINUTE),
        z('2026-10-20T19:59:00Z'),
    ),
    'final 1min: one minute on': (
        _row(z('2026-10-20T16:15:00Z'), z('2026-10-20T16:16:00Z'), Granularity.ONE_MINUTE),
        z('2026-10-20T16:16:00Z'),
    ),
    'provisional 1week: the last bar itself': (
        _row(WEEK_BAR, z('2026-10-16T15:00:00Z'), WEEK, WEEKLY_SUB_START),
        WEEK_BAR,
    ),
    'provisional 1month: the last bar itself': (
        _row(MONTH_BAR, z('2026-09-30T19:59:00Z'), MONTH, MONTHLY_SUB_START),
        MONTH_BAR,
    ),
    'a bar on a non-session day: one bar on': (_row(m(d('2026-10-24')), z('2026-10-23T12:00:00Z')), m(d('2026-10-25'))),
}


@pytest.mark.parametrize(('row', 'start'), FETCH_CASES.values(), ids=FETCH_CASES)
def test_the_fetch_start(row, start):
    assert _fetch_start(row, XNYS) == start
    # And it is the start the request carries; the body keeps the declared start as the entry's identity.
    _path, body, fetch_start = _request_for(row, XNYS)
    assert fetch_start == start
    assert body.start == row.start


def test_the_missed_pass_case_is_owed_and_re_fetched_from_the_provisional_bar():
    """The 20:30Z pass never ran; on the 22nd after the close the expected period is the 22nd, the bar is the 20th's."""
    now = z('2026-10-22T21:00:00Z')
    behind = _row(SEED_BAR, SEED_WRITTEN)
    owed = DailyRefresher._behind([behind], frozenset(), XNYS, now)
    assert [row.id for row in owed] == [behind.id]
    (productive, again), skipped = DailyRefresher._plan(owed, [], XNYS.session_close(d('2026-10-22')), XNYS)
    assert (skipped, again) == (0, [])
    assert [planned.fetch_start for planned in productive] == [SEED_BAR]


def test_the_refresh_owes_a_provisional_seed_and_not_a_final_one():
    now = z('2026-10-20T20:30:00Z')
    provisional = _row(SEED_BAR, SEED_WRITTEN)
    final = _row(SEED_BAR, z('2026-10-20T20:10:00Z'))
    unknown = _row(SEED_BAR, None)
    retired = _row(SEED_BAR, SEED_WRITTEN, expiry=z('2026-10-25T00:00:00Z'))
    owed = DailyRefresher._behind([provisional, final, unknown, retired], frozenset(), XNYS, now)
    assert [row.id for row in owed] == [provisional.id]


# ------------------------------------------------------------------------------------------------- the pass (SQLite)
#
# test_daily_refresh.py's world: NOW is Tue 6 Oct 22:00Z, the 6th closed at 20:00Z. Its `fresh` dataset's newest bar
# is the 6th's, written at NOW (final). Rewriting that bar's updated_at to 16:16Z makes it the seed scenario.

PROVISIONAL_WRITE = datetime(2026, 10, 6, 16, 16, tzinfo=UTC)


async def _rewrite_written_at(world: World, name: str, written: datetime) -> None:
    async with AsyncSession(world.engine) as db:
        await db.execute(
            update(StockMarketActivity)
            .where(StockMarketActivity.dataset_id == world.rows[name].id)
            .values(updated_at=written)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_a_pass_re_fetches_a_provisional_newest_bar_inclusively_and_once(world: World):
    await _rewrite_written_at(world, 'fresh', PROVISIONAL_WRITE)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    requested = world.requested(client)
    assert requested.count('fresh') == 1
    assert len(requested) == len(set(requested))
    (request,) = [r for r in client.requests if world.name_of(r.asset_symbol) == 'fresh']
    assert request.start == LAST_BAR['fresh'], 'the provisional bar itself is not in the tail'
    assert result == PassResult(behind=5, refreshed=5)
    (run,) = [run for run in await world.runs() if run.dataset_id == world.rows['fresh'].id]
    assert run.requested_start == LAST_BAR['fresh']


@pytest.mark.asyncio
async def test_a_final_newest_bar_is_not_re_fetched(world: World):
    await _rewrite_written_at(world, 'fresh', LAST_CLOSE)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert 'fresh' not in world.requested(client)
    assert result.behind == 4


@pytest.mark.asyncio
async def test_the_per_pass_cap_counts_the_re_fetch_as_one_dataset(world: World, monkeypatch):
    monkeypatch.setenv(refresh_module.MAX_PER_PASS_ENV, '5')
    await _rewrite_written_at(world, 'fresh', PROVISIONAL_WRITE)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert len(client.requests) == 5
    assert result == PassResult(behind=5, refreshed=5, deferred=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('tail', 'skipped'),
    [(LAST_BAR['fresh'], True), (LAST_BAR['fresh'] + DAY.offset, False)],
    ids=['the same inclusive tail: skipped', 'the old exclusive tail: a different one, not skipped'],
)
async def test_the_tried_this_tail_already_skip_still_applies_to_the_inclusive_tail(world: World, tail, skipped):
    await _rewrite_written_at(world, 'fresh', PROVISIONAL_WRITE)
    await _add(
        world,
        _scheduled_run(
            world,
            'fresh',
            state=IngestRunState.EMPTY,
            finished_at=LAST_CLOSE + timedelta(minutes=1),
            requested_start=tail,
        ),
    )
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert ('fresh' not in world.requested(client)) is skipped
    assert result.skipped == (1 if skipped else 0)


# ------------------------------------------------------------------------------------------------- the catalog (SQLite)
#
# test_ui_datasets_route.py's app and clock (Tue 6 Oct 22:00Z). The write time reaches every catalog read through the
# crud's newest-bar subquery: the list, the detail, the status filter and the facets must agree.


def _two_bars(of, newest_written: datetime, older_written: datetime) -> list[StockMarketActivity]:
    older, newest = bar(of, ny_midnight(5)), bar(of, ny_midnight(6))
    older.updated_at, newest.updated_at = older_written, newest_written
    return [older, newest]


@pytest.mark.asyncio
async def test_the_crud_reads_the_newest_bar_s_write_time_not_the_latest_write(engine):
    """The older bar was rewritten LATER than the newest one: the newest bar's own updated_at is what counts."""
    subscription = entry('PRV', update_type=UpdateType.DAILY)
    expired = entry('EXP', expiry=z('2026-10-01T00:00:00Z'))
    empty = entry('EMP', update_type=UpdateType.DAILY)
    rewritten_later = z('2026-10-06T23:00:00Z')
    await store(
        engine,
        subscription,
        expired,
        empty,
        *_two_bars(subscription, PROVISIONAL_WRITE, rewritten_later),
        *_two_bars(expired, PROVISIONAL_WRITE, rewritten_later),
    )
    async with AsyncSession(engine) as db:
        listed = {row.asset_symbol: row for row in await list_catalog(db, CatalogFilter())}
        detail = await get_catalog_row(db, subscription.id)
        pending = await list_pending(db, ROUTE_NOW)
    assert listed['PRV'].last_bar_written_at == PROVISIONAL_WRITE
    assert listed['EXP'].last_bar_written_at == PROVISIONAL_WRITE
    assert listed['EMP'].last_bar_written_at is None
    assert detail is not None and detail.last_bar_written_at == PROVISIONAL_WRITE
    assert [(row.asset_symbol, row.last_bar_written_at) for row in pending] == [('EXP', PROVISIONAL_WRITE)]


@pytest.mark.asyncio
async def test_list_detail_filter_and_facets_agree_on_a_provisional_daily_dataset(client, engine):
    provisional = entry('PRV', update_type=UpdateType.DAILY)
    final = entry('FIN', update_type=UpdateType.DAILY)
    at_close = entry('CLS', update_type=UpdateType.DAILY)
    await store(
        engine,
        provisional,
        final,
        at_close,
        *_two_bars(provisional, PROVISIONAL_WRITE, ROUTE_NOW),
        *_two_bars(final, z('2026-10-06T20:10:00Z'), ROUTE_NOW),
        *_two_bars(at_close, LAST_CLOSE, ROUTE_NOW),
    )
    want = {
        str(provisional.id): 'FRESHNESS_STATUS_LATE',
        str(final.id): 'FRESHNESS_STATUS_FRESH',
        str(at_close.id): 'FRESHNESS_STATUS_FRESH',
    }

    assert {item['id']: status_of(item) for item in await list_all(client)} == want
    for dataset_id, status in want.items():
        body = (await client.get(f'/ui/v1/datasets/{dataset_id}')).json()
        assert body['freshness']['status'] == status, dataset_id
    assert {item['id'] for item in await list_all(client, status='late')} == {str(provisional.id)}
    assert {item['id'] for item in await list_all(client, status='healthy')} == {str(final.id), str(at_close.id)}
    facets = (await client.get('/ui/v1/datasets/facets')).json()
    counts = {facet['status']: int(facet.get('count', '0')) for facet in facets['statuses']}
    assert (counts['FRESHNESS_STATUS_LATE'], counts['FRESHNESS_STATUS_FRESH']) == (1, 2)
    assert int(facets['needsAttention']) == 1
    # The detail's expected period is the 6th, the provisional bar's own session.
    body = (await client.get(f'/ui/v1/datasets/{provisional.id}')).json()
    assert body['freshness']['expectedLastBar'] == '2026-10-06T04:00:00Z'


def test_the_fixture_clock_is_the_one_this_section_reasons_about():
    assert datetime(2026, 10, 6, 22, 0, tzinfo=UTC) == ROUTE_NOW
    assert XNYS.session_close(date(2026, 10, 6)) == LAST_CLOSE
