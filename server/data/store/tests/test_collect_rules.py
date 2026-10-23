"""The collect rules (decision tj-hhy3dk sections 2-3, tj-grna9p.175): retired is collect false, never the expiry.

THE DESIGN. A DAILY or STREAM subscription's `collect` is the operator's intent: false reads RETIRED (no freshness
owed) and the refresh skips it; the expiry is the DELETION date only and changes no state. STATIC has no intent
(collect NULL) and is never retired. DatasetSummary.collecting is derived: a subscription's collect; for STATIC,
whether its latest run (the summary's last_run, read already: zero extra statements, the architect's ruling (3) on
tj-grna9p.175) is RUNNING.

WHAT TIER THIS IS. The pure rules (is_retired, is_collecting) over every (update type, collect, expiry) combination,
then the real list, detail, status filter and facets over real rows in SQLite, with the fixtures of
test_ui_datasets_route.py. The refresh's skip is test_daily_refresh.py's; the reaper's indifference to collect is
test_reaper.py's; the upsert's binds are test_dataset_entry_identity.py's; the PATCH route is
test_ui_dataset_collecting_route.py's.
"""

import itertools
import uuid
from datetime import datetime, timedelta

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.dataset_catalog import is_collecting, is_retired
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    NOW,
    aware_sqlite_datetimes,
    entry,
    list_all,
    ny_midnight,
    run_of,
    status_of,
    store,
)
from data.store.tests.test_ui_datasets_route import client as _routes_client  # noqa: F401 (a fixture)
from data.store.tests.test_ui_datasets_route import engine as _routes_engine  # noqa: F401 (a fixture)


pytestmark = pytest.mark.data_store


@pytest.fixture
def engine(_routes_engine):  # noqa: F811 (the imported fixture, under its own name here)
    """test_ui_datasets_route.py's in-memory SQLite engine with its tables."""
    return _routes_engine


@pytest.fixture
def client(_routes_client):  # noqa: F811 (as above)
    return _routes_client


SUBSCRIPTIONS = (UpdateType.DAILY, UpdateType.STREAM)
EXPIRIES = {'no expiry': None, 'past expiry': NOW - timedelta(days=2), 'future expiry': NOW + timedelta(days=30)}
COLLECTS = {'collect true': True, 'collect false': False, 'collect null': None}


def _row(update_type: UpdateType, collect: bool | None, expiry: datetime | None) -> CatalogRow:
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='CLR',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=Granularity.ONE_DAY,
        start=ny_midnight(1, 9),
        end=None,
        update_type=update_type,
        expiry_type=ExpiryType.BULK if update_type is UpdateType.STATIC else ExpiryType.ROLLING,
        expiry=expiry,
        owner='operator',
        last_bar=None,
        collect=collect,
    )


GRID = list(itertools.product(UpdateType, COLLECTS.items(), EXPIRIES.items()))


@pytest.mark.parametrize(
    ('update_type', 'collect', 'expiry'),
    [(u, c, e) for u, (_, c), (_, e) in GRID],
    ids=[f'{u.value}-{cn}-{en}' for u, (cn, _), (en, _) in GRID],
)
def test_is_retired_is_exactly_a_subscription_with_collect_false_and_the_expiry_never_changes_it(
    update_type, collect, expiry
):
    want = update_type in SUBSCRIPTIONS and collect is False
    assert is_retired(_row(update_type, collect, expiry)) is want
    # The same row without its expiry, and with the other one: the outcome cannot move.
    for other in EXPIRIES.values():
        assert is_retired(_row(update_type, collect, other)) is want


@pytest.mark.parametrize(
    ('update_type', 'collect', 'expiry'),
    [(u, c, e) for u, (_, c), (_, e) in GRID],
    ids=[f'{u.value}-{cn}-{en}' for u, (cn, _), (en, _) in GRID],
)
@pytest.mark.parametrize('running', [True, False], ids=['running', 'not running'])
def test_is_collecting_is_the_intent_for_a_subscription_and_the_running_run_for_static(
    update_type, collect, expiry, running
):
    row = _row(update_type, collect, expiry)
    want = running if update_type is UpdateType.STATIC else collect is not False
    assert is_collecting(row, running) is want
    # Never the opposite of retired for a subscription: a stopped one is not collecting.
    if update_type in SUBSCRIPTIONS:
        assert is_collecting(row, running) is not is_retired(row)


# ------------------------------------------------------------------------------------------------- the routes


async def _seeded(engine_):
    """One of each: subscriptions collecting or stopped, with and without a deletion date; STATIC by its runs."""
    rows = {
        'daily-on': entry('CDA', update_type=UpdateType.DAILY),
        'daily-on-dated': entry('CDB', update_type=UpdateType.DAILY, expiry=NOW + timedelta(days=3)),
        'daily-on-past': entry('CDC', update_type=UpdateType.DAILY, expiry=NOW - timedelta(days=3)),
        'daily-off': entry('CDD', update_type=UpdateType.DAILY, collect=False),
        'daily-off-dated': entry('CDE', update_type=UpdateType.DAILY, expiry=NOW + timedelta(days=3), collect=False),
        'stream-on-dated': entry(
            'CSA', granularity=Granularity.ONE_MINUTE, update_type=UpdateType.STREAM, expiry=NOW + timedelta(days=3)
        ),
        'stream-off': entry('CSB', granularity=Granularity.ONE_MINUTE, update_type=UpdateType.STREAM, collect=False),
        'static-running': entry('CTA', start=ny_midnight(1), end=ny_midnight(6)),
        'static-finished': entry('CTB', start=ny_midnight(1), end=ny_midnight(6)),
        'static-none': entry('CTC', start=ny_midnight(1), end=ny_midnight(6)),
        # An OLDER RUNNING run under a newer finished one: the latest decides (the startup sweep bounds the stuck one).
        'static-older-running': entry('CTD', start=ny_midnight(1), end=ny_midnight(6)),
    }
    runs = [
        run_of(rows['static-running'], minutes_ago=2, state=IngestRunState.RUNNING),
        run_of(rows['static-running'], minutes_ago=60),
        run_of(rows['static-finished'], minutes_ago=5),
        run_of(rows['static-older-running'], minutes_ago=60, state=IngestRunState.RUNNING),
        run_of(rows['static-older-running'], minutes_ago=5),
    ]
    await store(engine_, *rows.values(), *runs)
    return rows


COLLECTING = {
    'daily-on': True,
    'daily-on-dated': True,
    'daily-on-past': True,
    'daily-off': False,
    'daily-off-dated': False,
    'stream-on-dated': True,
    'stream-off': False,
    'static-running': True,
    'static-finished': False,
    'static-none': False,
    'static-older-running': False,
}
RETIRED = {'daily-off', 'daily-off-dated', 'stream-off'}


@pytest.mark.asyncio
async def test_the_list_and_the_detail_derive_collecting_state_and_freshness_from_collect_alone(client, engine):
    rows = await _seeded(engine)
    by_id = {item['id']: item for item in await list_all(client)}
    for name, row in rows.items():
        item = by_id[str(row.id)]
        detail = (await client.get(f'/ui/v1/datasets/{row.id}')).json()
        for summary in (item, detail):
            assert summary.get('collecting', False) is COLLECTING[name], (name, summary.get('collecting'))
            assert 'collecting' in summary or not COLLECTING[name], f'{name}: collecting is not set on the wire'
            retired = name in RETIRED
            assert summary['state'] == ('DATASET_STATE_RETIRED' if retired else 'DATASET_STATE_ACTIVE'), name
            assert (status_of(summary) == 'FRESHNESS_STATUS_RETIRED') is retired, (name, status_of(summary))
        # The deletion date is carried as it is, whatever the state.
        assert item.get('expiry') is not None or row.expiry is None, name


@pytest.mark.asyncio
async def test_the_retired_filter_and_facet_are_the_stopped_subscriptions_not_the_dated_ones(client, engine):
    rows = await _seeded(engine)
    listed = {item['id'] for item in await list_all(client, status='retired')}
    assert listed == {str(rows[name].id) for name in RETIRED}
    facets = (await client.get('/ui/v1/datasets/facets')).json()
    counts = {facet['status']: int(facet.get('count', '0')) for facet in facets.get('statuses', [])}
    assert counts['FRESHNESS_STATUS_RETIRED'] == len(RETIRED), facets.get('statuses')


@pytest.mark.asyncio
async def test_the_summary_reads_collecting_with_no_extra_statement(client, engine):
    """Ruling (3): STATIC collecting comes from the latest run the summary already reads, not a query of its own."""
    from sqlalchemy import event

    rows = await _seeded(engine)
    counts = []
    for dataset_id in (rows['static-running'].id, rows['static-finished'].id, rows['static-none'].id):
        statements: list[str] = []
        listener = lambda *a, seen=statements: seen.append(a[2])  # noqa: E731
        event.listen(engine.sync_engine, 'before_cursor_execute', listener)
        try:
            assert (await client.get(f'/ui/v1/datasets/{dataset_id}')).status_code == 200
        finally:
            event.remove(engine.sync_engine, 'before_cursor_execute', listener)
        counts.append(len(statements))
    assert len(set(counts)) == 1, f'a STATIC detail with a RUNNING latest run read more than one without: {counts}'
    assert not any('ingest_run' in s and 'RUNNING' in s and 'count' in s.lower() for s in statements)
