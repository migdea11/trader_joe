"""A bar is FINAL only if it was written after its period closed AND settled (validator, tj-grna9p.167 and .172).

DESIGN: bug tj-grna9p.167, option (A) as the architect recommended it, plus its clarification note, as amended by
task tj-grna9p.172 (the .167 gate's rulings (1) and (2)); decision tj-grna9p.8 (the DAILY rule and the 2026-10-20
period addendum) is the context:
    (1) a bar is FINAL when it was written (updated_at) at or after the close of its period PLUS the settle margin
        (DAILY_REFRESH_SETTLE_MINUTES, default 30, the same setting the refresh waits by). The period is the SESSION
        for 1min..1day (an intraday bar is judged by its session's close, not its own end: tj-grna9p.172), the last
        session of the week or month for 1week and 1month, in the calendar's zone. A period holding no session has
        no close, so its bar is final. The margin reaches the rule as an argument (FreshnessConfig.settle_margin):
        the evaluation never reads the environment.
    (2) a DAILY dataset reads FRESH only when its newest bar is of the expected period AND final; a provisional one
        reads LATE with the normal deadline (the next session's open after the expected period), OVERDUE after it.
        A newer, still open period reads as before. Unknown write time is final.
    (3) the refresh's tail starts AT a provisional newest bar (inclusive), whatever the expected period, else at the
        newest bar plus one bar, else at the declared start.
    (4) STATIC, STREAM, retired datasets, and every completed-bar result for 1min..1day (a bar written after its
        session's close plus the margin), are unchanged.

THE MARGIN IS WRITTEN BY HAND too: SETTLE (30 minutes, the default the bead names), never read from the module.

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

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.tests.roots import SERVER_ROOT
from data.store.app import daily_refresh as refresh_module
from data.store.app import health
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
from data.store.app.health import (
    SETTLE_ENV,
    DatasetHealth,
    FreshnessConfig,
    FreshnessStatus,
    bar_period_close,
    evaluate_dataset,
    last_bar_is_provisional,
    settle_margin_from_env,
)
from data.store.app.trading_calendar import NaiveDatetimeError, session_of
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
MINUTE = timedelta(minutes=1)
# The default settle margin, by hand (DAILY_REFRESH_SETTLE_MINUTES unset).
SETTLE = timedelta(minutes=30)
DAY = Granularity.ONE_DAY
WEEK = Granularity.ONE_WEEK
MONTH = Granularity.ONE_MONTH
THIRTY = Granularity.THIRTY_MINUTES
INTRADAY = [Granularity.ONE_MINUTE, Granularity.FIVE_MINUTES, Granularity.THIRTY_MINUTES, Granularity.ONE_HOUR]

# The seed scenario of the bug: DIA 1day, newest bar Tue 20 Oct (stamped 04:00Z, local midnight), fetched at 16:16Z
# during the session (13:30-20:00Z). The next session opens Wed 21 Oct 13:30Z.
SEED_BAR = m(d('2026-10-20'))
SEED_WRITTEN = z('2026-10-20T16:16:00Z')
OCT20_CLOSE = z('2026-10-20T20:00:00Z')
OCT20_SETTLED = z('2026-10-20T20:30:00Z')  # the close plus SETTLE: the first instant a bar of the 20th is final
OCT21_OPEN = z('2026-10-21T13:30:00Z')
SUB_START = m(d('2026-09-01'))


@pytest.fixture(autouse=True)
def _no_settle_env(monkeypatch):
    """Every test here starts from the default margin; a test that sets the variable does so itself."""
    monkeypatch.delenv(SETTLE_ENV, raising=False)


def daily(start, now, last_bar, granularity, written, end=OPEN_ENDED, collect=True, config=None) -> DatasetHealth:
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=granularity,
        collect=collect,
        start=start,
        end=end,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
        last_bar_written_at=written,
        config=config,
    )


# ------------------------------------------------------------------------------------------------- bar_period_close

# (bar, granularity, close) with the close written by hand (see the module docstring).
CLOSE_CASES = {
    # 1min..1hour: the close of the bar's SESSION (tj-grna9p.172), never the bar's own end.
    '1min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.ONE_MINUTE, OCT20_CLOSE),
    '5min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.FIVE_MINUTES, OCT20_CLOSE),
    '30min mid-session': (z('2026-10-20T15:00:00Z'), Granularity.THIRTY_MINUTES, OCT20_CLOSE),
    '1hour mid-session': (z('2026-10-20T15:00:00Z'), Granularity.ONE_HOUR, OCT20_CLOSE),
    # The last hourly bar of a session ends past the close: the session's close still decides.
    '1hour bar straddling the close': (z('2026-10-20T19:30:00Z'), Granularity.ONE_HOUR, OCT20_CLOSE),
    '1min, last of an early-close day': (z('2026-11-27T17:59:00Z'), Granularity.ONE_MINUTE, z('2026-11-27T18:00:00Z')),
    '30min, early in an early-close day': (z('2026-11-27T15:00:00Z'), THIRTY, z('2026-11-27T18:00:00Z')),
    '5min pre-market, the same local date': (z('2026-10-20T12:00:00Z'), Granularity.FIVE_MINUTES, OCT20_CLOSE),
    '1hour after hours, the same local date': (z('2026-10-20T21:00:00Z'), Granularity.ONE_HOUR, OCT20_CLOSE),
    '1min in the NY evening, the next UTC day': (z('2026-10-21T00:30:00Z'), Granularity.ONE_MINUTE, OCT20_CLOSE),
    '1hour, first EST Monday after the DST change': (
        z('2026-11-02T15:00:00Z'),
        Granularity.ONE_HOUR,
        z('2026-11-02T21:00:00Z'),
    ),
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
@pytest.mark.parametrize(
    'bar_at',
    [z('2026-10-24T15:00:00Z'), z('2026-10-25T15:00:00Z'), z('2026-11-26T15:00:00Z')],
    ids=['Saturday', 'Sunday', 'Thanksgiving'],
)
def test_an_intraday_bar_outside_any_session_has_no_close_and_is_final(granularity, bar_at):
    """Judged by its session like 1day (tj-grna9p.172): a stray weekend or holiday bar has none, so no close."""
    assert bar_period_close(bar_at, XNYS, granularity) is None
    assert not last_bar_is_provisional(bar_at, bar_at - timedelta(days=1), XNYS, granularity)


def test_bar_period_close_refuses_a_naive_bar():
    with pytest.raises(NaiveDatetimeError):
        bar_period_close(datetime(2026, 10, 20), XNYS, DAY)


# ------------------------------------------------------------------------------------------------- provisional

# (bar, written, granularity, provisional) under the default margin. The rule is written_at < close + SETTLE: written
# AT the close is provisional now, and written AT the close plus the margin is final.
PROVISIONAL_CASES = {
    '1day written one tick before the close': (SEED_BAR, OCT20_CLOSE - TICK, DAY, True),
    '1day written exactly at the close is still provisional': (SEED_BAR, OCT20_CLOSE, DAY, True),
    '1day written 5 minutes after the close': (SEED_BAR, OCT20_CLOSE + 5 * MINUTE, DAY, True),
    '1day written a tick before the close plus the margin': (SEED_BAR, OCT20_SETTLED - TICK, DAY, True),
    '1day written exactly at the close plus the margin is final': (SEED_BAR, OCT20_SETTLED, DAY, False),
    '1day written 31 minutes after the close': (SEED_BAR, OCT20_CLOSE + 31 * MINUTE, DAY, False),
    '1day written mid-session (the seed)': (SEED_BAR, SEED_WRITTEN, DAY, True),
    '1day written before its own session opened': (SEED_BAR, z('2026-10-20T05:00:00Z'), DAY, True),
    '1day early close, at 18:00Z': (m(d('2026-11-27')), z('2026-11-27T18:00:00Z'), DAY, True),
    '1day early close, a tick before 18:30Z': (m(d('2026-11-27')), z('2026-11-27T18:30:00Z') - TICK, DAY, True),
    '1day early close, at 18:30Z (not 21:30Z) is final': (m(d('2026-11-27')), z('2026-11-27T18:30:00Z'), DAY, False),
    '1day EST close, at 20:30Z is still provisional': (m(d('2026-11-02')), z('2026-11-02T20:30:00Z'), DAY, True),
    '1day EST close, at 21:30Z is final': (m(d('2026-11-02')), z('2026-11-02T21:30:00Z'), DAY, False),
    '1min, a tick before its own end': (
        z('2026-10-20T15:00:00Z'),
        z('2026-10-20T15:01:00Z') - TICK,
        Granularity.ONE_MINUTE,
        True,
    ),
    '1min, at its own end, mid-session, is provisional': (
        z('2026-10-20T15:00:00Z'),
        z('2026-10-20T15:01:00Z'),
        Granularity.ONE_MINUTE,
        True,
    ),
    '1min, at the session close plus the margin': (
        z('2026-10-20T15:00:00Z'),
        OCT20_SETTLED,
        Granularity.ONE_MINUTE,
        False,
    ),
    '1hour, at its own end, before the session close': (
        z('2026-10-20T15:00:00Z'),
        z('2026-10-20T16:00:00Z'),
        Granularity.ONE_HOUR,
        True,
    ),
    '1hour straddling the close, a tick before 20:30Z': (
        z('2026-10-20T19:30:00Z'),
        OCT20_SETTLED - TICK,
        Granularity.ONE_HOUR,
        True,
    ),
    '1hour straddling the close, at 20:30Z': (z('2026-10-20T19:30:00Z'), OCT20_SETTLED, Granularity.ONE_HOUR, False),
    '30min, early-close day, at 18:30Z': (z('2026-11-27T17:30:00Z'), z('2026-11-27T18:30:00Z'), THIRTY, False),
    '30min, early-close day, at 18:29Z': (z('2026-11-27T17:30:00Z'), z('2026-11-27T18:29:00Z'), THIRTY, True),
    '1week, Thursday of a Good Friday week, at its close': (m(d('2026-03-30')), z('2026-04-02T20:00:00Z'), WEEK, True),
    '1week, Thursday of a Good Friday week, after its close plus the margin': (
        m(d('2026-03-30')),
        z('2026-04-02T20:30:00Z'),
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
    '1month, at the last close of May': (m(d('2026-05-01')), z('2026-05-29T20:00:00Z'), MONTH, True),
    '1month, after the last close of May plus the margin': (
        m(d('2026-05-01')),
        z('2026-05-29T20:30:00Z'),
        MONTH,
        False,
    ),
    '1month, on the 31st of October, a Saturday': (m(d('2026-10-01')), z('2026-10-31T12:00:00Z'), MONTH, False),
    '1month, a tick before the last close of October': (
        m(d('2026-10-01')),
        z('2026-10-30T20:00:00Z') - TICK,
        MONTH,
        True,
    ),
    '1month, a tick before the last close of October plus the margin': (
        m(d('2026-10-01')),
        z('2026-10-30T20:30:00Z') - TICK,
        MONTH,
        True,
    ),
}


@pytest.mark.parametrize(
    ('bar_at', 'written', 'granularity', 'provisional'), PROVISIONAL_CASES.values(), ids=PROVISIONAL_CASES
)
def test_a_bar_is_provisional_only_when_written_before_its_period_closed_and_settled(
    bar_at, written, granularity, provisional
):
    assert last_bar_is_provisional(bar_at, written, XNYS, granularity) is provisional


# (bar, granularity, close) by hand: the margin is the caller's, added to that close for every granularity.
MARGIN_CLOSES = {
    '1min': (z('2026-10-20T15:00:00Z'), Granularity.ONE_MINUTE, OCT20_CLOSE),
    '30min': (z('2026-10-20T15:30:00Z'), THIRTY, OCT20_CLOSE),
    '1day': (SEED_BAR, DAY, OCT20_CLOSE),
    '1day early close': (m(d('2026-11-27')), DAY, z('2026-11-27T18:00:00Z')),
    '1week, Good Friday': (m(d('2026-03-30')), WEEK, z('2026-04-02T20:00:00Z')),
    '1month, May': (m(d('2026-05-01')), MONTH, z('2026-05-29T20:00:00Z')),
}
MARGINS = [timedelta(0), 5 * MINUTE, SETTLE, timedelta(hours=2)]


@pytest.mark.parametrize('margin', MARGINS, ids=str)
@pytest.mark.parametrize(('bar_at', 'granularity', 'close'), MARGIN_CLOSES.values(), ids=MARGIN_CLOSES)
def test_the_margin_passed_moves_the_final_instant_to_the_close_plus_that_margin(bar_at, granularity, close, margin):
    assert last_bar_is_provisional(bar_at, close + margin - TICK, XNYS, granularity, margin)
    assert not last_bar_is_provisional(bar_at, close + margin, XNYS, granularity, margin)
    # Only the close moves by the margin: a margin of zero is the old rule, written at the close is final.
    if margin == timedelta(0):
        assert not last_bar_is_provisional(bar_at, close, XNYS, granularity, margin)


@pytest.mark.parametrize('granularity', [Granularity.ONE_MINUTE, DAY, WEEK, MONTH], ids=str)
def test_the_default_margin_is_thirty_minutes_and_a_freshness_config_s(granularity):
    assert health.DEFAULT_SETTLE_MINUTES == 30
    assert health.DEFAULT_SETTLE_MARGIN == SETTLE == FreshnessConfig().settle_margin
    bar_at, close = {
        Granularity.ONE_MINUTE: (z('2026-10-20T15:00:00Z'), OCT20_CLOSE),
        DAY: (SEED_BAR, OCT20_CLOSE),
        WEEK: (m(d('2026-03-30')), z('2026-04-02T20:00:00Z')),
        MONTH: (m(d('2026-05-01')), z('2026-05-29T20:00:00Z')),
    }[granularity]
    assert last_bar_is_provisional(bar_at, close + SETTLE - TICK, XNYS, granularity)
    assert not last_bar_is_provisional(bar_at, close + SETTLE, XNYS, granularity)


def test_a_negative_margin_is_refused_by_the_config():
    with pytest.raises(ValueError):
        FreshnessConfig(settle_margin=-MINUTE)
    assert FreshnessConfig(settle_margin=timedelta(0)).settle_margin == timedelta(0)


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
    'written at the close, inside the margin: LATE': (z('2026-10-20T20:45:00Z'), OCT20_CLOSE, FreshnessStatus.LATE),
    'written 5 minutes after the close: LATE': (
        z('2026-10-20T20:45:00Z'),
        OCT20_CLOSE + 5 * MINUTE,
        FreshnessStatus.LATE,
    ),
    'written a tick before the close plus the margin: LATE': (
        z('2026-10-20T20:45:00Z'),
        OCT20_SETTLED - TICK,
        FreshnessStatus.LATE,
    ),
    'written at the close plus the margin: FRESH': (z('2026-10-20T20:45:00Z'), OCT20_SETTLED, FreshnessStatus.FRESH),
    'written 31 minutes after the close: FRESH at once': (
        z('2026-10-20T20:45:00Z'),
        OCT20_CLOSE + 31 * MINUTE,
        FreshnessStatus.FRESH,
    ),
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
def test_an_intraday_subscription_s_session_last_bar_is_final_only_after_the_close_plus_the_margin(granularity):
    """The session's last bar (19:59Z for 1min), complete at its own end, is still provisional until 20:30Z."""
    last = OCT20_CLOSE - granularity.offset
    now = z('2026-10-20T21:00:00Z')
    for written in (OCT20_CLOSE - TICK, OCT20_CLOSE, OCT20_CLOSE + 5 * MINUTE, OCT20_SETTLED - TICK):
        assert daily(SUB_START, now, last, granularity, written) == DatasetHealth(
            FreshnessStatus.LATE, d('2026-10-20'), SEED_BAR
        ), written
    for written in (OCT20_SETTLED, OCT20_CLOSE + 31 * MINUTE):
        assert daily(SUB_START, now, last, granularity, written).status is FreshnessStatus.FRESH, written
    assert daily(SUB_START, OCT21_OPEN, last, granularity, OCT20_CLOSE).status is FreshnessStatus.OVERDUE


def test_fixed_a_30min_daily_dataset_fetched_mid_session_reads_late_then_overdue_and_is_re_fetched_from_its_bar():
    """THE GAP .167 PINNED, FIXED (tj-grna9p.172 ruling (1)): it read FRESH with 16:00Z..20:00Z never fetched.

    A 30min DAILY dataset fetched at 16:16Z holds bars to 15:30Z, the last one complete by its own end (16:00Z). It
    is judged by its SESSION's close: after the close it reads LATE (the normal deadline), OVERDUE from the next
    open, and the refresh asks again from the 15:30Z bar itself, inclusively.
    """
    last = z('2026-10-20T15:30:00Z')
    written = SEED_WRITTEN
    assert bar_period_close(last, XNYS, THIRTY) == OCT20_CLOSE
    assert last_bar_is_provisional(last, written, XNYS, THIRTY)
    late = DatasetHealth(FreshnessStatus.LATE, d('2026-10-20'), SEED_BAR)
    # While the session is open the expected period is the 19th; the 20th's bars are newer and owe nothing yet.
    assert daily(SUB_START, z('2026-10-20T16:30:00Z'), last, THIRTY, written).status is FreshnessStatus.FRESH
    assert daily(SUB_START, OCT20_CLOSE, last, THIRTY, written) == late
    assert daily(SUB_START, OCT20_SETTLED, last, THIRTY, written) == late
    assert daily(SUB_START, OCT21_OPEN - TICK, last, THIRTY, written) == late
    assert daily(SUB_START, OCT21_OPEN, last, THIRTY, written).status is FreshnessStatus.OVERDUE

    row = _row(last, written, THIRTY)
    assert _fetch_start(row, XNYS) == last
    owed = DailyRefresher._behind([row], frozenset(), XNYS, OCT20_SETTLED)
    assert [r.id for r in owed] == [row.id]
    (productive, again), skipped = DailyRefresher._plan(owed, [], OCT20_CLOSE, XNYS)
    assert (skipped, again) == (0, [])
    assert [planned.fetch_start for planned in productive] == [last]

    # The re-fetch after the close plus the margin brings the session's last bar, final: FRESH, and the tail moves on.
    repaired = _row(z('2026-10-20T19:30:00Z'), z('2026-10-20T20:31:00Z'), THIRTY)
    assert daily(SUB_START, z('2026-10-20T20:45:00Z'), repaired.last_bar, THIRTY, repaired.last_bar_written_at) == (
        DatasetHealth(FreshnessStatus.FRESH, d('2026-10-20'), SEED_BAR)
    )
    assert _fetch_start(repaired, XNYS) == OCT20_CLOSE
    assert DailyRefresher._behind([repaired], frozenset(), XNYS, z('2026-10-20T20:45:00Z')) == []


@pytest.mark.parametrize('granularity', INTRADAY, ids=str)
def test_every_intraday_granularity_fetched_mid_session_reads_late_after_the_close(granularity):
    """The last bar complete by 16:00Z, written at 16:16Z: provisional by the session for 1min..1hour alike."""
    last = z('2026-10-20T16:00:00Z') - granularity.offset
    assert last_bar_is_provisional(last, SEED_WRITTEN, XNYS, granularity)
    got = daily(SUB_START, OCT20_SETTLED, last, granularity, SEED_WRITTEN)
    assert got == DatasetHealth(FreshnessStatus.LATE, d('2026-10-20'), SEED_BAR)
    assert _fetch_start(_row(last, SEED_WRITTEN, granularity), XNYS) == last


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
    'week, written at the Friday close, inside the margin: LATE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T20:00:00Z'),
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'week, written Friday 20:29Z: LATE': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T20:29:00Z'),
        z('2026-10-17T12:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'week, written at the Friday close plus the margin: FRESH': (
        WEEK,
        WEEKLY_SUB_START,
        WEEK_BAR,
        z('2026-10-16T20:30:00Z'),
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
    'Good Friday week, written Thu 20:00Z (its last close): LATE': (
        WEEK,
        SPRING_START,
        m(d('2026-03-30')),
        z('2026-04-02T20:00:00Z'),
        z('2026-04-03T15:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'Good Friday week, written Thu 20:30Z (its last close plus the margin): FRESH': (
        WEEK,
        SPRING_START,
        m(d('2026-03-30')),
        z('2026-04-02T20:30:00Z'),
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
    'month, written at the 30th close, inside the margin: LATE': (
        MONTH,
        MONTHLY_SUB_START,
        MONTH_BAR,
        z('2026-09-30T20:00:00Z'),
        z('2026-10-01T01:00:00Z'),
        FreshnessStatus.LATE,
    ),
    'month, written at the 30th close plus the margin: FRESH': (
        MONTH,
        MONTHLY_SUB_START,
        MONTH_BAR,
        z('2026-09-30T20:30:00Z'),
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


# (granularity, start, last bar, the period's last close) by hand; a configured margin of two hours.
PERIODIC_MARGIN_CASES = {
    'week of the 12th': (WEEK, WEEKLY_SUB_START, WEEK_BAR, z('2026-10-16T20:00:00Z')),
    'Good Friday week': (WEEK, SPRING_START, m(d('2026-03-30')), z('2026-04-02T20:00:00Z')),
    'September': (MONTH, MONTHLY_SUB_START, MONTH_BAR, z('2026-09-30T20:00:00Z')),
}


@pytest.mark.parametrize(
    ('granularity', 'start', 'last_bar', 'close'), PERIODIC_MARGIN_CASES.values(), ids=PERIODIC_MARGIN_CASES
)
def test_weekly_and_monthly_take_the_configured_margin_on_the_period_s_last_close(granularity, start, last_bar, close):
    config = FreshnessConfig(settle_margin=timedelta(hours=2))
    now = close + timedelta(hours=3)
    assert daily(start, now, last_bar, granularity, close + SETTLE, config=config).status is FreshnessStatus.LATE
    assert daily(start, now, last_bar, granularity, close + timedelta(hours=2) - TICK, config=config).status is (
        FreshnessStatus.LATE
    )
    assert daily(start, now, last_bar, granularity, close + timedelta(hours=2), config=config).status is (
        FreshnessStatus.FRESH
    )
    # And the same write time under the default margin is already final.
    assert daily(start, now, last_bar, granularity, close + SETTLE).status is FreshnessStatus.FRESH


def test_a_still_open_week_holding_a_provisional_bar_reads_as_before():
    """Wed 21 Oct: the expected week is the 12th's; the bar of the week of the 19th is newer and owes nothing."""
    now = z('2026-10-21T15:00:00Z')
    bar_at = m(d('2026-10-19'))
    got = daily(WEEKLY_SUB_START, now, bar_at, WEEK, z('2026-10-20T15:00:00Z'))
    assert got == daily(WEEKLY_SUB_START, now, bar_at, WEEK, None)
    assert got.status is FreshnessStatus.FRESH


@pytest.mark.parametrize('update_type', [UpdateType.DAILY, UpdateType.STREAM], ids=str)
def test_a_retired_dataset_is_retired_whatever_its_last_bar_s_write_time(update_type):
    """Retired is collect false (decision tj-hhy3dk); the write time never brings a stopped subscription back."""
    for written in (None, SEED_WRITTEN, OCT20_CLOSE):
        got = evaluate_dataset(
            update_type=update_type,
            granularity=DAY,
            collect=False,
            start=SUB_START,
            end=OPEN_ENDED,
            last_bar=SEED_BAR,
            covered=None,
            calendar=XNYS,
            now=z('2026-10-20T20:30:00Z'),
            last_bar_written_at=written,
        )
        assert got == DatasetHealth(FreshnessStatus.RETIRED)


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
                collect=None if update_type is UpdateType.STATIC else True,
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
# next open and OVERDUE from it, with the same expected period. FINAL is now written at or after the SESSION's close
# plus the margin (tj-grna9p.172): written there, or long after, a completed bar still gives the pre-change result.


@pytest.mark.parametrize('granularity', SESSION_GRANULARITIES, ids=str)
def test_a_completed_bar_gives_exactly_the_pre_change_result(granularity):
    differ, judged_at_settle = [], 0
    for start, end in GRID_RANGES:
        for now in GRID_NOWS:
            for last_bar in GRID_LAST_BARS:
                old = _old_daily(start, end, last_bar, XNYS, now)
                close = None if last_bar is None else bar_period_close(last_bar, XNYS, granularity)
                settled = None if close is None else close + SETTLE
                long_after = None if close is None else close + timedelta(days=3)
                for written in {None, LATER, settled or LATER, long_after or LATER}:
                    got = daily(start, now, last_bar, granularity, written, end)
                    judged_at_settle += written is not None and written == settled
                    if got != old:
                        differ.append((start, end, now, last_bar, written, got, old))
    assert not differ, differ[:5]
    assert judged_at_settle, 'the grid never wrote a bar exactly at its close plus the margin'


WITHHOLDING_WRITES = {
    'a day before its own timestamp': lambda last_bar, close: last_bar - timedelta(days=1),
    'at its session close': lambda last_bar, close: close,
    'inside the margin': lambda last_bar, close: close + 5 * MINUTE,
    'a tick before the margin ends': lambda last_bar, close: close + SETTLE - TICK,
}


@pytest.mark.parametrize('write', WITHHOLDING_WRITES.values(), ids=WITHHOLDING_WRITES)
@pytest.mark.parametrize('granularity', SESSION_GRANULARITIES, ids=str)
def test_a_provisional_bar_only_ever_withholds_fresh_for_its_own_expected_period(granularity, write):
    differ, flipped = [], 0
    for start, end in GRID_RANGES:
        for now in GRID_NOWS:
            for last_bar in GRID_LAST_BARS:
                if last_bar is None:
                    continue
                old = _old_daily(start, end, last_bar, XNYS, now)
                written = write(last_bar, bar_period_close(last_bar, XNYS, granularity))
                assert last_bar_is_provisional(last_bar, written, XNYS, granularity), (last_bar, written)
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
    collect: bool = True,
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
        collect=collect,
    )


FETCH_CASES = {
    'provisional 1day: the last bar itself': (_row(SEED_BAR, SEED_WRITTEN), SEED_BAR),
    'a tick before the close: the last bar itself': (_row(SEED_BAR, OCT20_CLOSE - TICK), SEED_BAR),
    'at the close, inside the margin: the last bar itself': (_row(SEED_BAR, OCT20_CLOSE), SEED_BAR),
    'close + 5 min: the last bar itself': (_row(SEED_BAR, OCT20_CLOSE + 5 * MINUTE), SEED_BAR),
    'final 1day at the close plus the margin: one bar on': (_row(SEED_BAR, OCT20_SETTLED), SEED_BAR + DAY.offset),
    'final 1day, close + 31 min: one bar on': (_row(SEED_BAR, OCT20_CLOSE + 31 * MINUTE), SEED_BAR + DAY.offset),
    'unknown write time: one bar on': (_row(SEED_BAR, None), SEED_BAR + DAY.offset),
    'no bars: the declared start': (_row(None, None), SUB_START),
    'provisional 1min: the last bar itself': (
        _row(z('2026-10-20T19:59:00Z'), z('2026-10-20T19:59:30Z'), Granularity.ONE_MINUTE),
        z('2026-10-20T19:59:00Z'),
    ),
    '1min complete by its own end, mid-session: the last bar itself': (
        _row(z('2026-10-20T16:15:00Z'), z('2026-10-20T16:16:00Z'), Granularity.ONE_MINUTE),
        z('2026-10-20T16:15:00Z'),
    ),
    '30min fetched at 16:16Z: the 15:30Z bar itself': (
        _row(z('2026-10-20T15:30:00Z'), SEED_WRITTEN, THIRTY),
        z('2026-10-20T15:30:00Z'),
    ),
    'final 1min, written at the close plus the margin: one minute on': (
        _row(z('2026-10-20T19:59:00Z'), OCT20_SETTLED, Granularity.ONE_MINUTE),
        OCT20_CLOSE,
    ),
    'provisional 1week: the last bar itself': (
        _row(WEEK_BAR, z('2026-10-16T15:00:00Z'), WEEK, WEEKLY_SUB_START),
        WEEK_BAR,
    ),
    '1week at its last close, inside the margin: the last bar itself': (
        _row(WEEK_BAR, z('2026-10-16T20:10:00Z'), WEEK, WEEKLY_SUB_START),
        WEEK_BAR,
    ),
    'final 1week at its last close plus the margin: one week on': (
        _row(WEEK_BAR, z('2026-10-16T20:30:00Z'), WEEK, WEEKLY_SUB_START),
        m(d('2026-10-19')),
    ),
    'provisional 1month: the last bar itself': (
        _row(MONTH_BAR, z('2026-09-30T19:59:00Z'), MONTH, MONTHLY_SUB_START),
        MONTH_BAR,
    ),
    '1month at its last close, inside the margin: the last bar itself': (
        _row(MONTH_BAR, z('2026-09-30T20:10:00Z'), MONTH, MONTHLY_SUB_START),
        MONTH_BAR,
    ),
    'final 1month at its last close plus the margin: one month on': (
        _row(MONTH_BAR, z('2026-09-30T20:30:00Z'), MONTH, MONTHLY_SUB_START),
        MONTH_BAR + MONTH.offset,
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
    now = z('2026-10-20T20:45:00Z')
    provisional = _row(SEED_BAR, SEED_WRITTEN)
    inside_margin = _row(SEED_BAR, z('2026-10-20T20:10:00Z'))
    final = _row(SEED_BAR, OCT20_SETTLED)
    unknown = _row(SEED_BAR, None)
    # Decision tj-hhy3dk: a stopped subscription is skipped; a provisional one with only a deletion date is still owed.
    retired = _row(SEED_BAR, SEED_WRITTEN, collect=False)
    deletion_dated = _row(SEED_BAR, SEED_WRITTEN, expiry=z('2026-10-25T00:00:00Z'))
    owed = DailyRefresher._behind(
        [provisional, inside_margin, final, unknown, retired, deletion_dated], frozenset(), XNYS, now
    )
    assert {row.id for row in owed} == {provisional.id, inside_margin.id, deletion_dated.id}


# ------------------------------------------------------------------------------------------------- the one setting
#
# DAILY_REFRESH_SETTLE_MINUTES decides both when the refresh pass runs after a close and when a bar is final. It is
# parsed in ONE place (health.settle_margin_from_env); the refresh reads it through that, the catalog through
# FreshnessConfig.from_env, and the evaluation itself only ever sees the margin it is passed.

# (raw value or None for unset, minutes)
SETTLE_PARSE_CASES = {
    'unset: the default': (None, 30),
    'zero is allowed': ('0', 0),
    'five': ('5', 5),
    'forty-five': ('45', 45),
    'a day': ('1440', 1440),
    'very large': ('100000', 100000),
    'negative: the default': ('-1', 30),
    'very negative: the default': ('-600', 30),
    'malformed: the default': ('soon', 30),
    'empty: the default': ('', 30),
    'a fraction: the default': ('1.5', 30),
    'a unit suffix: the default': ('30m', 30),
}


@pytest.mark.parametrize(('raw', 'minutes'), SETTLE_PARSE_CASES.values(), ids=SETTLE_PARSE_CASES)
def test_the_settle_margin_parser(raw, minutes):
    environ = {} if raw is None else {SETTLE_ENV: raw}
    assert settle_margin_from_env(environ) == timedelta(minutes=minutes)
    assert FreshnessConfig.from_env(environ).settle_margin == timedelta(minutes=minutes)


@pytest.mark.parametrize(('raw', 'minutes'), SETTLE_PARSE_CASES.values(), ids=SETTLE_PARSE_CASES)
def test_the_refresh_s_settle_delay_and_the_freshness_margin_are_one_setting(monkeypatch, raw, minutes):
    if raw is not None:
        monkeypatch.setenv(SETTLE_ENV, raw)
    want = timedelta(minutes=minutes)
    assert refresh_module.settle_delay() == want
    assert settle_margin_from_env() == want
    assert FreshnessConfig.from_env().settle_margin == want


def test_the_settle_env_name_is_unchanged_and_re_exported_by_the_refresh():
    """Renaming the variable would silently drop an operator's setting; the refresh's names are health's objects."""
    assert health.SETTLE_ENV == 'DAILY_REFRESH_SETTLE_MINUTES'
    assert refresh_module.SETTLE_ENV is health.SETTLE_ENV
    assert refresh_module.DEFAULT_SETTLE_MINUTES is health.DEFAULT_SETTLE_MINUTES
    assert refresh_module.settle_margin_from_env is health.settle_margin_from_env
    assert refresh_module.FreshnessConfig is health.FreshnessConfig


def _server_sources():
    for path in sorted(SERVER_ROOT.rglob('*.py')):
        if 'tests' in path.relative_to(SERVER_ROOT).parts:
            continue
        yield path, ast.parse(path.read_text(encoding='utf-8'))


def test_the_settle_setting_is_named_defined_and_read_in_health_only():
    """One parser: the env name is a literal once, the names are bound once, and only health reads SETTLE_ENV."""
    literal = [
        path.name
        for path, tree in _server_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == 'DAILY_REFRESH_SETTLE_MINUTES'
    ]
    assert literal == ['health.py']
    owned = {'SETTLE_ENV', 'DEFAULT_SETTLE_MINUTES', 'DEFAULT_SETTLE_MARGIN', 'settle_margin_from_env'}
    elsewhere = []
    for path, tree in _server_sources():
        if path == SERVER_ROOT / 'data' / 'store' / 'app' / 'health.py':
            continue
        for node in ast.walk(tree):
            # Bound (assigned or defined) outside health, or read by name: a second definition or a second parser.
            if isinstance(node, ast.Name) and node.id in owned and node.id != 'settle_margin_from_env':
                elsewhere.append((path.name, node.id, node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in owned:
                elsewhere.append((path.name, node.name, node.lineno))
    assert not elsewhere, elsewhere


def test_the_evaluation_never_reads_the_environment(monkeypatch):
    """The margin reaches the rule only through the config or the argument: the variable alone changes nothing."""
    now = z('2026-10-20T21:00:00Z')
    at_close, settled = OCT20_CLOSE, OCT20_SETTLED
    monkeypatch.setenv(SETTLE_ENV, '0')
    assert last_bar_is_provisional(SEED_BAR, at_close, XNYS, DAY)
    assert daily(SUB_START, now, SEED_BAR, DAY, at_close).status is FreshnessStatus.LATE
    assert daily(SUB_START, now, z('2026-10-20T15:30:00Z'), THIRTY, at_close).status is FreshnessStatus.LATE
    # Through the config the caller builds, it does.
    assert daily(SUB_START, now, SEED_BAR, DAY, at_close, config=FreshnessConfig.from_env()).status is (
        FreshnessStatus.FRESH
    )
    monkeypatch.setenv(SETTLE_ENV, '120')
    assert not last_bar_is_provisional(SEED_BAR, settled, XNYS, DAY)
    assert daily(SUB_START, now, SEED_BAR, DAY, settled).status is FreshnessStatus.FRESH
    assert daily(WEEKLY_SUB_START, z('2026-10-17T12:00:00Z'), WEEK_BAR, WEEK, z('2026-10-16T20:30:00Z')).status is (
        FreshnessStatus.FRESH
    )
    assert daily(SUB_START, now, SEED_BAR, DAY, settled, config=FreshnessConfig.from_env()).status is (
        FreshnessStatus.LATE
    )


def test_the_evaluation_calls_no_env_reader(monkeypatch):
    """Belt and braces: with the parser and os.environ access made to fail, every DAILY evaluation still runs."""

    def refuse(*_args, **_kwargs):
        raise AssertionError('the evaluation read the environment')

    monkeypatch.setattr(health, 'settle_margin_from_env', refuse)
    monkeypatch.setattr(health.FreshnessConfig, 'from_env', classmethod(refuse))
    # health's only route to the environment is its `os` import: without one, any access is an AttributeError.
    monkeypatch.setattr(health, 'os', SimpleNamespace())
    for granularity, start, last_bar in (
        (DAY, SUB_START, SEED_BAR),
        (THIRTY, SUB_START, z('2026-10-20T15:30:00Z')),
        (WEEK, WEEKLY_SUB_START, WEEK_BAR),
        (MONTH, MONTHLY_SUB_START, MONTH_BAR),
    ):
        daily(start, z('2026-10-21T12:00:00Z'), last_bar, granularity, SEED_WRITTEN)
        last_bar_is_provisional(last_bar, SEED_WRITTEN, XNYS, granularity)


# The refresh takes the margin from the same setting, for the tail and for what it owes: a 1day bar written at
# close + 10 min, with the variable at (raw, provisional).
REFRESH_MARGIN_CASES = {
    'unset (30)': (None, True),
    'zero': ('0', False),
    'five': ('5', False),
    'ten: written exactly at close + margin is final': ('10', False),
    'eleven': ('11', True),
    'malformed (30)': ('soon', True),
}


@pytest.mark.parametrize(('raw', 'provisional'), REFRESH_MARGIN_CASES.values(), ids=REFRESH_MARGIN_CASES)
def test_the_refresh_s_tail_and_owed_set_follow_the_settle_setting(monkeypatch, raw, provisional):
    if raw is not None:
        monkeypatch.setenv(SETTLE_ENV, raw)
    row = _row(SEED_BAR, OCT20_CLOSE + 10 * MINUTE)
    assert _fetch_start(row, XNYS) == (SEED_BAR if provisional else SEED_BAR + DAY.offset)
    owed = DailyRefresher._behind([row], frozenset(), XNYS, z('2026-10-20T23:00:00Z'))
    assert [r.id for r in owed] == ([row.id] if provisional else [])


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
@pytest.mark.parametrize(
    'written',
    [LAST_CLOSE + timedelta(minutes=30), LAST_CLOSE + timedelta(minutes=31)],
    ids=['close + 30', 'close + 31'],
)
async def test_a_final_newest_bar_is_not_re_fetched(world: World, written):
    await _rewrite_written_at(world, 'fresh', written)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert 'fresh' not in world.requested(client)
    assert result.behind == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'written', [LAST_CLOSE, LAST_CLOSE + timedelta(minutes=5)], ids=['at the close', 'close + 5, inside the margin']
)
async def test_a_newest_bar_written_inside_the_margin_is_re_fetched_inclusively(world: World, written):
    await _rewrite_written_at(world, 'fresh', written)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    (request,) = [r for r in client.requests if world.name_of(r.asset_symbol) == 'fresh']
    assert request.start == LAST_BAR['fresh']
    assert result.behind == 5


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


async def _catalog_statuses(client) -> dict[str, str]:
    return {item['id']: status_of(item) for item in await list_all(client)}


@pytest.mark.asyncio
async def test_list_detail_filter_and_facets_agree_on_a_provisional_daily_dataset(client, engine):
    provisional = entry('PRV', update_type=UpdateType.DAILY)
    inside_margin = entry('MRG', update_type=UpdateType.DAILY)
    at_close = entry('CLS', update_type=UpdateType.DAILY)
    settled = entry('SET', update_type=UpdateType.DAILY)
    final = entry('FIN', update_type=UpdateType.DAILY)
    await store(
        engine,
        provisional,
        inside_margin,
        at_close,
        settled,
        final,
        *_two_bars(provisional, PROVISIONAL_WRITE, ROUTE_NOW),
        *_two_bars(inside_margin, z('2026-10-06T20:10:00Z'), ROUTE_NOW),
        *_two_bars(at_close, LAST_CLOSE, ROUTE_NOW),
        *_two_bars(settled, z('2026-10-06T20:30:00Z'), ROUTE_NOW),
        *_two_bars(final, z('2026-10-06T20:31:00Z'), ROUTE_NOW),
    )
    late = {str(provisional.id), str(inside_margin.id), str(at_close.id)}
    fresh = {str(settled.id), str(final.id)}
    want = {**dict.fromkeys(late, 'FRESHNESS_STATUS_LATE'), **dict.fromkeys(fresh, 'FRESHNESS_STATUS_FRESH')}

    assert await _catalog_statuses(client) == want
    for dataset_id, status in want.items():
        body = (await client.get(f'/ui/v1/datasets/{dataset_id}')).json()
        assert body['freshness']['status'] == status, dataset_id
    assert {item['id'] for item in await list_all(client, status='late')} == late
    assert {item['id'] for item in await list_all(client, status='healthy')} == fresh
    facets = (await client.get('/ui/v1/datasets/facets')).json()
    counts = {facet['status']: int(facet.get('count', '0')) for facet in facets['statuses']}
    assert (counts['FRESHNESS_STATUS_LATE'], counts['FRESHNESS_STATUS_FRESH']) == (3, 2)
    assert int(facets['needsAttention']) == 3
    # The detail's expected period is the 6th, the provisional bar's own session.
    body = (await client.get(f'/ui/v1/datasets/{provisional.id}')).json()
    assert body['freshness']['expectedLastBar'] == '2026-10-06T04:00:00Z'


@pytest.mark.asyncio
async def test_the_catalog_routes_take_the_margin_from_the_settle_setting(client, engine, monkeypatch):
    """dataset_catalog builds its FreshnessConfig from the environment per read: the routes follow the variable."""
    at_close = entry('CLS', update_type=UpdateType.DAILY)
    inside = entry('MRG', update_type=UpdateType.DAILY)
    await store(
        engine,
        at_close,
        inside,
        *_two_bars(at_close, LAST_CLOSE, ROUTE_NOW),
        *_two_bars(inside, z('2026-10-06T20:10:00Z'), ROUTE_NOW),
    )
    ids = (str(at_close.id), str(inside.id))
    for raw, statuses in (
        (None, ('LATE', 'LATE')),
        ('0', ('FRESH', 'FRESH')),
        ('10', ('LATE', 'FRESH')),  # written exactly at close + margin: final
        ('5', ('LATE', 'FRESH')),
        ('11', ('LATE', 'LATE')),
        ('soon', ('LATE', 'LATE')),
    ):
        if raw is None:
            monkeypatch.delenv(SETTLE_ENV, raising=False)
        else:
            monkeypatch.setenv(SETTLE_ENV, raw)
        want = {i: f'FRESHNESS_STATUS_{s}' for i, s in zip(ids, statuses, strict=True)}
        assert await _catalog_statuses(client) == want, raw
        for dataset_id, status in want.items():
            body = (await client.get(f'/ui/v1/datasets/{dataset_id}')).json()
            assert body['freshness']['status'] == status, (raw, dataset_id)


def test_the_fixture_clock_is_the_one_this_section_reasons_about():
    assert datetime(2026, 10, 6, 22, 0, tzinfo=UTC) == ROUTE_NOW
    assert XNYS.session_close(date(2026, 10, 6)) == LAST_CLOSE
