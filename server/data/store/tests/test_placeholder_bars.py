"""A placeholder bar (volume 0 AND trade count 0) is stored raw but counts as no trade (validator, tj-7zjov1.104).

DESIGN: task tj-7zjov1.103 (P40), from ADR tj-26jt9w addendum (7) of 2026-10-24, item 7 ("PLACEHOLDER BARS. Stored
raw. A bar with volume 0 and trade count 0 is no trade: it covers no session in completeness or freshness"), and the
accepted default on tj-7zjov1.90. What is pinned:
    (1) a session whose only bars are placeholders is a GAP: covered_dates leaves it out, so the STATIC status, its
        gap count and the coverage strip all see it missing; the same session with one real bar is covered;
    (2) volume 0 with a trade count above 0, and volume above 0 with trade count 0, are REAL bars (both halves of
        the AND are needed);
    (3) health reads the newest REAL bar: the catalog's last_bar and last_bar_written_at name it when
        placeholders trail it (list_catalog, get_catalog_row, list_pending), and a dataset holding only
        placeholders has last_bar None;
    (4) the rows are stored raw: bar_extents, the bar page, the UI bars route, the page index and the internal read
        still return every placeholder, and the stored rows read back unchanged after every read;
    (5) one predicate, StockMarketActivity.is_placeholder_bar, answers on an instance and as SQL, and both sites
        (covered_dates and the newest-bar subqueries) use it: a mutant at one site reds a test here.

THE REAL CASE. PARA, asof absent, served flat 0.206 bars with volume 0 and trade count 0 on every day of 2019-12 to
2020-12 (tj-7zjov1.90, the architect's 21:02 note: 809 such bars across PARA 2016-01 to 2023-12). The PARA
scenario below rebuilds that year: real bars on either side, a placeholder on every XNYS session from 1 Dec 2019 to
31 Dec 2020 at 0.206. Only the flat year is rebuilt, so the count is that year's session count, not 809.

TIER. SQLite, through test_ui_datasets_route.py's engine, client and covered_dates_as_dates fixtures (that file's
header says what SQLite cannot show; covered_dates' local date is computed by its timezone() stand-in and its TEXT
result parsed, exactly as there). The negation is an ordinary NOT (a AND b) over two NOT NULL integer columns, so
SQLite and Postgres evaluate it alike; the compiled form is pinned so a NULL-sensitive rewrite is noticed.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Granularity, UpdateType
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock.asset_market_activity import read_market_activity_data
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.tests import test_ui_datasets_route
from data.store.tests.test_ui_datasets_route import (
    NOW,
    SEPTEMBER,
    XNYS,
    all_bars,
    bar,
    coverage,
    entry,
    midnight,
    ny_midnight,
    page_index,
    stamp,
    store,
    window,
    z,
)
from schemas.data_store.stock.market_activity_data import StockDataMarketActivityQuery


pytestmark = pytest.mark.data_store

aware_sqlite_datetimes = test_ui_datasets_route.aware_sqlite_datetimes
engine = test_ui_datasets_route.engine
client = test_ui_datasets_route.client
covered_dates_as_dates = test_ui_datasets_route.covered_dates_as_dates

HOUR = Granularity.ONE_HOUR
DAY = timedelta(days=1)
PARA_CLOSE = 0.206


def shaped(of, at: datetime, *, volume: int, trades: int, close: float = 1.0, written: datetime = NOW):
    """A bar of OF at AT with the given volume and trade count, written at WRITTEN."""
    row = bar(of, at, close=close)
    row.volume, row.trade_count, row.updated_at = volume, trades, written
    return row


def placeholder(of, at: datetime, *, close: float = PARA_CLOSE, written: datetime = NOW):
    return shaped(of, at, volume=0, trades=0, close=close, written=written)


def real(of, at: datetime, *, close: float = 1.0, written: datetime = NOW):
    return shaped(of, at, volume=100, trades=10, close=close, written=written)


def at_hour(session: date, hour_utc: int) -> datetime:
    """An intraday stamp on SESSION (Sept 2026, EDT: the session runs 13:30Z to 20:00Z)."""
    return datetime(session.year, session.month, session.day, hour_utc, tzinfo=UTC)


def raw(row: StockMarketActivity) -> tuple:
    """Every stored field a read could alter, in a comparable form."""
    return (
        row.dataset_id,
        row.timestamp,
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.trade_count,
        row.created_at,
        row.updated_at,
    )


async def stored_rows(engine) -> list[tuple]:
    async with AsyncSession(engine) as db:
        rows = (await db.execute(select(StockMarketActivity).order_by(StockMarketActivity.timestamp))).scalars()
        return [raw(row) for row in rows]


async def covered(engine, *datasets) -> dict[uuid.UUID, frozenset[date]]:
    """The real covered_dates statement, its SQLite TEXT dates parsed as test_ui_datasets_route does."""
    async with AsyncSession(engine) as db:
        rows = [await catalog_repo.get_catalog_row(db, dataset.id) for dataset in datasets]
        found = await catalog_repo.covered_dates(db, rows, str(XNYS.tz))
    return {
        key: frozenset(day if isinstance(day, date) else date.fromisoformat(day) for day in days)
        for key, days in found.items()
    }


# ------------------------------------------------------------------------------------------------- the predicate


@pytest.mark.parametrize(
    ('volume', 'trades', 'is_placeholder'),
    [(0, 0, True), (0, 1, False), (1, 0, False), (100, 10, False), (0, 7, False), (12, 0, False)],
    ids=['both-zero', 'volume-zero-only', 'trades-zero-only', 'ordinary', 'volume-zero-many-trades', 'no-trades'],
)
def test_the_predicate_on_an_instance_needs_both_volume_and_trade_count_zero(volume, trades, is_placeholder):
    row = shaped(entry('PRD'), ny_midnight(5), volume=volume, trades=trades)
    assert row.is_placeholder_bar is is_placeholder


def test_the_predicate_as_sql_is_volume_zero_and_trade_count_zero_and_its_negation_is_not_of_that():
    literal = {'compile_kwargs': {'literal_binds': True}}
    predicate = StockMarketActivity.is_placeholder_bar
    assert str(predicate.compile(**literal)) == (
        'stock_market_activity.volume = 0 AND stock_market_activity.trade_count = 0'
    )
    assert str((~predicate).compile(**literal)) == (
        'NOT (stock_market_activity.volume = 0 AND stock_market_activity.trade_count = 0)'
    )


@pytest.mark.asyncio
async def test_the_sql_predicate_selects_exactly_the_rows_the_instance_predicate_names(engine):
    """One definition: over a table of every shape, the database and Python pick the same rows, both ways."""
    dataset = entry('PRD', granularity=HOUR, source=DataSource.IB_API)
    shapes = [(0, 0), (0, 1), (1, 0), (100, 10), (0, 0), (0, 3)]
    rows = [
        shaped(dataset, z('2026-10-05T14:00:00Z') + timedelta(hours=index), volume=v, trades=n)
        for index, (v, n) in enumerate(shapes)
    ]
    await store(engine, dataset, *rows)
    want = {row.timestamp for row in rows if row.is_placeholder_bar}
    assert len(want) == 2
    async with AsyncSession(engine) as db:
        placeholders = set(
            (
                await db.execute(select(StockMarketActivity.timestamp).where(StockMarketActivity.is_placeholder_bar))
            ).scalars()
        )
        real_bars = set(
            (
                await db.execute(select(StockMarketActivity.timestamp).where(~StockMarketActivity.is_placeholder_bar))
            ).scalars()
        )
    assert placeholders == want
    assert real_bars == {row.timestamp for row in rows} - want


# ------------------------------------------------------------------------------------------------- completeness

# Five September sessions, all closed before NOW, with one shape each (1hour, so a session can hold two bars).
WEEK = SEPTEMBER[:5]
ONLY_PLACEHOLDERS, PLACEHOLDER_AND_REAL, VOLUME_ZERO_TRADED, VOLUME_NO_TRADES, ORDINARY = WEEK


def shaped_week(dataset, *, first_session_real: bool = False) -> list[StockMarketActivity]:
    first = real if first_session_real else placeholder
    return [
        first(dataset, at_hour(ONLY_PLACEHOLDERS, 14)),
        first(dataset, at_hour(ONLY_PLACEHOLDERS, 15)),
        placeholder(dataset, at_hour(PLACEHOLDER_AND_REAL, 14)),
        real(dataset, at_hour(PLACEHOLDER_AND_REAL, 15)),
        shaped(dataset, at_hour(VOLUME_ZERO_TRADED, 14), volume=0, trades=1),
        shaped(dataset, at_hour(VOLUME_NO_TRADES, 14), volume=1, trades=0),
        real(dataset, at_hour(ORDINARY, 14)),
    ]


def week_entry(symbol: str):
    return entry(symbol, granularity=HOUR, start=midnight(WEEK[0]), end=midnight(WEEK[-1] + DAY))


@pytest.mark.asyncio
async def test_covered_dates_leaves_out_a_session_of_only_placeholders_and_keeps_every_other_shape(engine):
    gappy, control = week_entry('GAP'), week_entry('CTL')
    await store(engine, gappy, control, *shaped_week(gappy), *shaped_week(control, first_session_real=True))

    found = await covered(engine, gappy, control)

    assert found[gappy.id] == frozenset(WEEK) - {ONLY_PLACEHOLDERS}
    assert found[control.id] == frozenset(WEEK)


@pytest.mark.asyncio
async def test_a_session_of_only_placeholders_is_a_gap_in_the_static_status_and_the_coverage_strip(
    client, engine, covered_dates_as_dates
):
    gappy, control = week_entry('GAP'), week_entry('CTL')
    await store(engine, gappy, control, *shaped_week(gappy), *shaped_week(control, first_session_real=True))

    detail = (await client.get(f'/ui/v1/datasets/{gappy.id}')).json()
    assert (detail['health']['status'], detail['health']['gapCount']) == ('DATASET_HEALTH_STATUS_GAPS', 1)
    strip = await coverage(client, gappy)
    assert strip.gaps == [window(ONLY_PLACEHOLDERS, ONLY_PLACEHOLDERS)] and strip.gap_count == 1
    assert (strip.expected, strip.missing) == (len(WEEK), 1)
    # The stored extent still counts the placeholders: raw is kept, only coverage ignores them.
    assert (detail['barCount'], detail['firstBar']) == ('7', stamp(at_hour(ONLY_PLACEHOLDERS, 14)))

    control_detail = (await client.get(f'/ui/v1/datasets/{control.id}')).json()
    assert control_detail['health']['status'] == 'DATASET_HEALTH_STATUS_COMPLETE'
    control_strip = await coverage(client, control)
    assert (control_strip.gaps, control_strip.gap_count, control_strip.missing) == ([], 0, 0)


# ---------------------------------------------------------------------------------------------------- health

REAL_WRITTEN = z('2026-10-05T21:00:00Z')
PLACEHOLDER_WRITTEN = z('2026-10-06T21:30:00Z')


@pytest.mark.asyncio
async def test_the_catalog_s_last_bar_and_its_write_time_name_the_newest_real_bar(engine):
    """list_catalog, get_catalog_row and list_pending all read the one newest-real-bar subquery pair."""
    expired = z('2026-10-01T00:00:00Z')
    trailing = entry('TRL', update_type=UpdateType.DAILY, expiry=expired)
    only = entry('ONL', update_type=UpdateType.DAILY, expiry=expired)
    odd = entry('ODD', update_type=UpdateType.DAILY, expiry=expired)
    await store(
        engine,
        trailing,
        only,
        odd,
        real(trailing, ny_midnight(5), written=REAL_WRITTEN),
        placeholder(trailing, ny_midnight(6), written=PLACEHOLDER_WRITTEN),
        placeholder(only, ny_midnight(5), written=REAL_WRITTEN),
        placeholder(only, ny_midnight(6), written=PLACEHOLDER_WRITTEN),
        # Neither newest bar here is a placeholder: each half alone is a real (odd) bar.
        shaped(odd, ny_midnight(5), volume=1, trades=0, written=REAL_WRITTEN),
        shaped(odd, ny_midnight(6), volume=0, trades=1, written=PLACEHOLDER_WRITTEN),
    )
    want = {'TRL': (ny_midnight(5), REAL_WRITTEN), 'ONL': (None, None), 'ODD': (ny_midnight(6), PLACEHOLDER_WRITTEN)}
    async with AsyncSession(engine) as db:
        listed = {
            row.asset_symbol: (row.last_bar, row.last_bar_written_at)
            for row in await catalog_repo.list_catalog(db, CatalogFilter())
        }
        details = {}
        for dataset in (trailing, only, odd):
            row = await catalog_repo.get_catalog_row(db, dataset.id)
            details[row.asset_symbol] = (row.last_bar, row.last_bar_written_at)
        pending = {
            row.asset_symbol: (row.last_bar, row.last_bar_written_at)
            for row in await catalog_repo.list_pending(db, NOW)
        }
    assert listed == want
    assert details == want
    assert pending == want


@pytest.mark.asyncio
async def test_a_daily_dataset_whose_newest_bar_is_a_placeholder_is_judged_on_the_real_one(client, engine):
    """NOW is Tue 6 Oct 22:00Z: a real bar of the 6th is FRESH; a placeholder of the 6th leaves Monday's, LATE."""
    trailing = entry('TRL', update_type=UpdateType.DAILY)
    healthy = entry('HLT', update_type=UpdateType.DAILY)
    odd = entry('ODD', update_type=UpdateType.DAILY)
    await store(
        engine,
        trailing,
        healthy,
        odd,
        real(trailing, ny_midnight(5), written=REAL_WRITTEN),
        placeholder(trailing, ny_midnight(6), written=PLACEHOLDER_WRITTEN),
        real(healthy, ny_midnight(5), written=REAL_WRITTEN),
        real(healthy, ny_midnight(6), written=PLACEHOLDER_WRITTEN),
        real(odd, ny_midnight(5), written=REAL_WRITTEN),
        shaped(odd, ny_midnight(6), volume=0, trades=1, written=PLACEHOLDER_WRITTEN),
    )
    statuses = {}
    for dataset in (trailing, healthy, odd):
        body = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()
        statuses[dataset.asset_symbol] = body['health']['status']
        # The extent is the stored one: the placeholder is still the newest stored bar.
        assert (body['barCount'], body['lastBar']) == ('2', stamp(ny_midnight(6))), body
    assert statuses == {
        'TRL': 'DATASET_HEALTH_STATUS_LATE',
        'HLT': 'DATASET_HEALTH_STATUS_FRESH',
        'ODD': 'DATASET_HEALTH_STATUS_FRESH',
    }


# ------------------------------------------------------------------------------------------------- stored raw


@pytest.mark.asyncio
async def test_every_bar_read_returns_the_placeholders_and_no_read_changes_a_stored_row(client, engine):
    dataset = entry('RAW', granularity=HOUR, source=DataSource.IB_API)
    rows = shaped_week(dataset)
    await store(engine, dataset, *rows)
    before = await stored_rows(engine)
    assert sum(1 for row in rows if row.is_placeholder_bar) == 3
    every_stamp = [stamp(row.timestamp) for row in sorted(rows, key=lambda row: row.timestamp)]

    async with AsyncSession(engine) as db:
        catalog_row = await catalog_repo.get_catalog_row(db, dataset.id)
        extent = (await catalog_repo.bar_extents(db, [catalog_row]))[dataset.id]
        page = await catalog_repo.read_bar_page(db, catalog_row, start=None, end=None, after=None, limit=100)
        internal = await read_market_activity_data(db, StockDataMarketActivityQuery(dataset_id=dataset.id))
    first, last = min(row.timestamp for row in rows), max(row.timestamp for row in rows)
    assert (extent.count, extent.first_bar, extent.last_bar) == (len(rows), first, last)
    assert [raw(row) for row in page] == before
    assert [(item.data.volume, item.data.trade_count) for item in internal] == [(row[6], row[7]) for row in before]

    assert await all_bars(client, dataset.id) == every_stamp
    body = (await client.get(f'/ui/v1/datasets/{dataset.id}/bars', params={'limit': 1})).json()
    (first_bar,) = body['bars']
    assert (first_bar['close'], first_bar.get('volume', 0), first_bar.get('tradeCount', '0')) == (PARA_CLOSE, 0, '0')
    pages = await page_index(client, dataset.id, size=1000)
    assert int(pages.get('barCount', '0')) == len(rows), pages
    (await client.get(f'/ui/v1/datasets/{dataset.id}')).raise_for_status()
    (await client.get('/ui/v1/datasets')).raise_for_status()

    assert await stored_rows(engine) == before


# ------------------------------------------------------------------------------------------------- PARA

PARA_START = datetime(2019, 11, 1, tzinfo=XNYS.tz).astimezone(UTC)
PARA_FLAT_FROM = datetime(2019, 12, 1, tzinfo=XNYS.tz).astimezone(UTC)
PARA_FLAT_TO = datetime(2021, 1, 1, tzinfo=XNYS.tz).astimezone(UTC)
PARA_END = datetime(2021, 2, 1, tzinfo=XNYS.tz).astimezone(UTC)


def para_bars(dataset) -> tuple[list[StockMarketActivity], list[date]]:
    """Paramount-like real bars in Nov 2019 and Jan 2021, the flat 0.206 placeholder year between."""
    before = XNYS.sessions_overlapping(PARA_START, PARA_FLAT_FROM)
    flat = XNYS.sessions_overlapping(PARA_FLAT_FROM, PARA_FLAT_TO)
    after = XNYS.sessions_overlapping(PARA_FLAT_TO, PARA_END)
    rows = (
        [real(dataset, midnight(session), close=40.5) for session in before]
        + [placeholder(dataset, midnight(session)) for session in flat]
        + [real(dataset, midnight(session), close=41.2) for session in after]
    )
    return rows, list(flat)


@pytest.mark.asyncio
async def test_para_s_flat_placeholder_year_is_one_gap_of_every_session_in_it(client, engine, covered_dates_as_dates):
    dataset = entry('PARA', start=PARA_START, end=PARA_END)
    rows, flat = para_bars(dataset)
    await store(engine, dataset, *rows)
    before = await stored_rows(engine)
    assert len(flat) > 250 and all(row.close == PARA_CLOSE for row in rows if row.is_placeholder_bar)

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()
    assert (detail['health']['status'], detail['health']['gapCount']) == ('DATASET_HEALTH_STATUS_GAPS', len(flat))
    assert detail['barCount'] == str(len(rows))
    strip = await coverage(client, dataset)
    assert strip.gaps == [window(flat[0], flat[-1])] and strip.gap_count == 1
    assert strip.missing == len(flat)

    served = await all_bars(client, dataset.id, limit=50000)
    assert served == [stamp(row.timestamp) for row in rows]
    assert await stored_rows(engine) == before


@pytest.mark.asyncio
async def test_a_para_subscription_whose_series_went_flat_reads_its_last_real_bar(engine):
    """A DAILY PARA whose stored tail is the flat year: health reads the last real bar, 29 Nov 2019."""
    dataset = entry('PARA', update_type=UpdateType.DAILY, start=PARA_START)
    rows, _ = para_bars(dataset)
    rows = [row for row in rows if row.timestamp < PARA_FLAT_TO]
    await store(engine, dataset, *rows)
    last_real = max(row.timestamp for row in rows if not row.is_placeholder_bar)
    assert last_real == midnight(date(2019, 11, 29))

    async with AsyncSession(engine) as db:
        catalog_row = await catalog_repo.get_catalog_row(db, dataset.id)
        extent = (await catalog_repo.bar_extents(db, [catalog_row]))[dataset.id]
    assert catalog_row.last_bar == last_real
    assert extent.last_bar == max(row.timestamp for row in rows) > last_real
