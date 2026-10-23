"""Usage on the /ui/v1 catalog against a real Postgres (validator, gating tj-grna9p.85).

Design: tj-grna9p.85, the architect's 2026-10-14 CONSOLIDATED BUILD BRIEF (it wins over the body):
  0 the usage window functions refuse a naive now and take the window's first day in UTC;
  1 GET /ui/v1/datasets/{id}/bars records ONE read per call WITHOUT a cursor, labelled by X-Client-Id; a call
    WITH a cursor records nothing, and no other route records;
  2 DatasetSummary.usage on list and detail: last_read_at and reads_30d under the client filter, read_by_30d and
    read_by_clients_30d regardless of it, and the echo (exactly one of client_ids / client_kinds, default [SDK]);
  3 the client filter: client_id (repeatable, wins) else client_kind (repeatable) else SDK;
  4 usage = READ_30D / STALE (never-read included) / NEVER_READ with stale_days 1..3650 (default 90), and
    sort=last_read;
  5 Expiring Soon: expiry < expires_before PLUS the reaper's pending set, ignoring the client filter;
  6 reaper_pending on list and detail;
  7 each facet count equals the filtered list's total;
  8 a constant number of statements per request.

WHY THIS TIER. The usage SQL takes UTC days with CAST(timezone('UTC', read_at) AS DATE). On the SQLite tier
(data/store/tests) that cast yields an integer and the to_regclass probe does not exist, so there every catalog
read sees the zero usage view and none of the usage arithmetic runs. Everything that depends on the arithmetic is
pinned here, on Postgres through asyncpg.

HOW. Every test but the last runs the REAL app in process (httpx's ASGI transport; the lifespan never runs) with
its session dependency pointed at a scratch database this test creates, migrates and drops
(test_usage_record.scratch_db, reused), and its clock pinned through the routes' own utc_now dependency. The
statement counts are read off that engine. The last test drives the RUNNING data_store over real HTTP and reads
back only the usage rows keyed by an entry this run created, which it deletes.

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_stock import ExpiryType, UpdateType
from data.store.app import dataset_catalog as catalog_module
from data.store.app import usage_recorder as recorder_module
from data.store.app.client_label import CLIENT_ID_HEADER, OPERATOR_PRINCIPAL, UNATTRIBUTED
from data.store.app.database.crud.dataset_read import read_dataset_usage
from data.store.app.database.database import async_db
from data.store.app.main import app
from data.store.app.usage_recorder import UsageRecorder
from routers.data_store import ui_datasets as ui_datasets_module
from routers.data_store.app_endpoints import UiDatasetsInterface, UiReaperInterface
from routers.data_store.ui_datasets import utc_now
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE


pytestmark = pytest.mark.data_store

# Reused, not copied: a scratch database this test creates and drops, and an asyncpg engine on it.
scratch_db = test_usage_record.scratch_db
scratch_async = test_usage_record.scratch_async
_head = test_usage_record._head
READ_TABLE = test_usage_record.READ_TABLE
DAILY_TABLE = test_usage_record.DAILY_TABLE
PRE_USAGE_REVISION = test_usage_record.PARENT_REVISION

LIST = UiDatasetsInterface.GET_UI_DATASETS.value
FACETS = UiDatasetsInterface.GET_UI_DATASET_FACETS.value
BARS_ROUTE = UiDatasetsInterface.GET_UI_DATASET_BARS.value

# A Tuesday evening, UTC. The 30-day window's first UTC day is therefore 2026-09-06.
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
WINDOW_FIRST_DAY = datetime(2026, 9, 6, tzinfo=UTC)

SDK = 'CLIENT_KIND_SDK'
UI = 'CLIENT_KIND_UI'
CLIENT_A, CLIENT_B, CLIENT_C = 'client-a', 'client-b', 'client-c'


def stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------------------------
# The world. One dataset per usage shape, plus three for expiry. Symbols order the never-read tail of
# sort=last_read: DAYIN < DAYOUT < FAR < NEVER < OLD < PAST < RECENT < ROLLED < SOON < UIONLY.
#
#   RECENT  read by client-a (SDK) at exactly NOW - 1 day, and by the operator (UI) at NOW - 3 days
#   UIONLY  read by the operator (UI) only, NOW - 2 days
#   OLD     read by client-b (SDK) at NOW - 60 days
#   ROLLED  only a rolled-up day: client-a (SDK), 3 reads, 120 days ago
#   DAYIN   client-c (SDK) at 00:00:00Z of the window's first UTC day: IN reads_30d
#   DAYOUT  client-c (SDK) one second earlier: OUT of reads_30d
#   NEVER   no read
#   SOON    expires NOW + 3 days; FAR expires NOW + 30 days; PAST expired a day ago (reaper pending)

NAMES = ('RECENT', 'UIONLY', 'OLD', 'ROLLED', 'DAYIN', 'DAYOUT', 'NEVER', 'SOON', 'FAR', 'PAST')
EXPIRIES = {'SOON': NOW + timedelta(days=3), 'FAR': NOW + timedelta(days=30), 'PAST': NOW - timedelta(days=1)}


class World:
    def __init__(self, ids: dict[str, uuid.UUID]) -> None:
        self.ids = ids
        self.names = {value: key for key, value in ids.items()}

    def named(self, items: list[dict]) -> list[str]:
        return [self.names[uuid.UUID(item['id'])] for item in items]


def _read_row(dataset_id: uuid.UUID, client_id: str, kind: str, at: datetime) -> dict[str, Any]:
    return {
        'dataset_id': dataset_id,
        'client_id': client_id,
        'client_kind': kind,
        'kind': 'READ',
        'route': BARS_ROUTE,
        'bars_returned': 5,
        'read_at': at,
    }


def _seed(engine: Engine, entry_values: Callable[..., dict[str, Any]], prefix: str = '', names=NAMES) -> World:
    ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        for name in names:
            values = entry_values(
                asset_symbol=f'{prefix}{name}',
                expiry=EXPIRIES.get(name),
                update_type=UpdateType.STATIC.value,
                expiry_type=ExpiryType.BULK.value,
            )
            ids[name] = conn.execute(sa.insert(ENTRY_TABLE).values(values).returning(ENTRY_TABLE.c.id)).scalar_one()
        reads = [
            _read_row(ids['RECENT'], CLIENT_A, 'SDK', NOW - timedelta(days=1)),
            _read_row(ids['RECENT'], OPERATOR_PRINCIPAL, 'UI', NOW - timedelta(days=3)),
            _read_row(ids['UIONLY'], OPERATOR_PRINCIPAL, 'UI', NOW - timedelta(days=2)),
            _read_row(ids['OLD'], CLIENT_B, 'SDK', NOW - timedelta(days=60)),
            _read_row(ids['DAYIN'], CLIENT_C, 'SDK', WINDOW_FIRST_DAY),
            _read_row(ids['DAYOUT'], CLIENT_C, 'SDK', WINDOW_FIRST_DAY - timedelta(seconds=1)),
        ]
        conn.execute(sa.insert(READ_TABLE), reads)
        rolled_at = NOW - timedelta(days=120)
        conn.execute(
            sa.insert(DAILY_TABLE).values(
                dataset_id=ids['ROLLED'],
                client_id=CLIENT_A,
                client_kind='SDK',
                day=rolled_at.date(),
                reads=3,
                sessions=0,
                bars_returned=15,
                last_read_at=rolled_at,
            )
        )
    return World(ids)


@pytest_asyncio.fixture
async def client(scratch_async: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The real app on the scratch database, its clock pinned at NOW."""

    async def session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as db:
            yield db

    async def pinned_now() -> datetime:
        return NOW

    app.dependency_overrides[async_db] = session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
            yield http
    finally:
        app.dependency_overrides.pop(async_db, None)
        app.dependency_overrides.pop(utc_now, None)


@pytest.fixture
def world(scratch_db, entry_values) -> World:
    scratch_db.must('upgrade', _head())
    return _seed(scratch_db.engine, entry_values)


async def list_all(client: httpx.AsyncClient, params: dict[str, Any] | list[tuple[str, Any]] = ()) -> list[dict]:
    """Every item under params, following next_cursor; asserts nothing is served twice."""
    base = list(params.items()) if isinstance(params, dict) else list(params)
    items: list[dict] = []
    cursor = None
    for _ in range(100):
        response = await client.get(LIST, params=[*base, *([('cursor', cursor)] if cursor else [])])
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body.get('items', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids)), f'an item was served twice: {ids}'
    return items


async def facets(client: httpx.AsyncClient, params: dict[str, Any] | list[tuple[str, Any]] = ()) -> dict:
    response = await client.get(FACETS, params=list(params.items()) if isinstance(params, dict) else list(params))
    assert response.status_code == 200, response.text
    return response.json()


ALL = set(NAMES)
NEVER_SDK = {'UIONLY', 'NEVER', 'SOON', 'FAR', 'PAST'}

# (client params, usage, stale_days) -> the datasets the list holds. Comments give the reason for the edge cases.
GROUP_CASES = {
    'SDK default, READ_30D: DAYIN is in the window, DAYOUT a second too early': (
        [],
        'READ_30D',
        None,
        {'RECENT', 'DAYIN'},
    ),
    'SDK default, NEVER_READ: a UI-only dataset is never read by SDK': ([], 'NEVER_READ', None, NEVER_SDK),
    'SDK default, STALE at 90: never-read included, the rolled-up read 120 days ago is stale': (
        [],
        'STALE',
        None,
        NEVER_SDK | {'ROLLED'},
    ),
    # Round 2, day-aligned: DAYIN (dated the window's first day) is read within 30 days, so NOT stale at 30;
    # DAYOUT (one second earlier, the day before) is.
    'SDK, STALE at 30: OLD and DAYOUT join, DAYIN does not': ([], 'STALE', 30, NEVER_SDK | {'ROLLED', 'OLD', 'DAYOUT'}),
    'SDK, STALE at 1: a read yesterday is within 1 day': ([], 'STALE', 1, ALL - {'RECENT'}),
    'SDK, STALE at 3650 (the maximum): only the never-read': ([], 'STALE', 3650, NEVER_SDK),
    'UI kind, READ_30D': ([('client_kind', 'UI')], 'READ_30D', None, {'RECENT', 'UIONLY'}),
    'UI kind, NEVER_READ': ([('client_kind', 'ui')], 'NEVER_READ', None, ALL - {'RECENT', 'UIONLY'}),
    'UI kind, STALE: a UI-only dataset is stale under SDK and not under UI': (
        [('client_kind', 'CLIENT_KIND_UI')],
        'STALE',
        None,
        ALL - {'RECENT', 'UIONLY'},
    ),
    'both kinds, READ_30D': (
        [('client_kind', 'UI'), ('client_kind', 'SDK')],
        'READ_30D',
        None,
        {'RECENT', 'UIONLY', 'DAYIN'},
    ),
    'both kinds, STALE': (
        [('client_kind', 'UI'), ('client_kind', 'SDK')],
        'STALE',
        None,
        {'NEVER', 'SOON', 'FAR', 'PAST', 'ROLLED'},
    ),
    'client_id wins over client_kind, READ_30D': (
        [('client_id', CLIENT_A), ('client_kind', 'UI')],
        'READ_30D',
        None,
        {'RECENT'},
    ),
    'client_id wins over client_kind, NEVER_READ': (
        [('client_id', CLIENT_A), ('client_kind', 'UI')],
        'NEVER_READ',
        None,
        ALL - {'RECENT', 'ROLLED'},
    ),
    'client_id wins over client_kind, STALE': (
        [('client_id', CLIENT_A), ('client_kind', 'UI')],
        'STALE',
        None,
        ALL - {'RECENT'},
    ),
    'two client ids': (
        [('client_id', CLIENT_B), ('client_id', CLIENT_C)],
        'NEVER_READ',
        None,
        ALL - {'OLD', 'DAYIN', 'DAYOUT'},
    ),
    'usage by wire name, lower case': ([], 'usage_filter_read_30d', None, {'RECENT', 'DAYIN'}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('clients', 'usage', 'stale_days', 'expected'), GROUP_CASES.values(), ids=GROUP_CASES)
async def test_the_usage_groups_under_the_client_filter(world, client, clients, usage, stale_days, expected):
    params = [*clients, ('usage', usage), *([('stale_days', stale_days)] if stale_days else []), ('limit', 3)]
    assert set(world.named(await list_all(client, params))) == expected


@pytest.mark.asyncio
async def test_list_and_detail_carry_usage_with_the_echo(world, client):
    """last_read_at and reads_30d follow the filter; read_by_30d and read_by_clients_30d do not; the echo is exact."""
    items = {world.names[uuid.UUID(item['id'])]: item for item in await list_all(client)}
    assert set(items) == ALL
    assert items['RECENT']['usage'] == {
        'lastReadAt': stamp(NOW - timedelta(days=1)),
        'reads30d': '1',
        'readBy30d': [UI, SDK],
        'readByClients30d': [CLIENT_A, OPERATOR_PRINCIPAL],
        'clientKinds': [SDK],
    }
    # Read by UI only: under the default SDK filter it has no last read, yet read_by says who did read it.
    assert items['UIONLY']['usage'] == {
        'readBy30d': [UI],
        'readByClients30d': [OPERATOR_PRINCIPAL],
        'clientKinds': [SDK],
    }
    # Rolled up: the last read survives the fold; 120 days is outside the window, so no count and no readers.
    assert items['ROLLED']['usage'] == {'lastReadAt': stamp(NOW - timedelta(days=120)), 'clientKinds': [SDK]}
    assert items['DAYIN']['usage']['reads30d'] == '1' and 'reads30d' not in items['DAYOUT']['usage']
    assert items['DAYOUT']['usage'] == {
        'lastReadAt': stamp(WINDOW_FIRST_DAY - timedelta(seconds=1)),
        'clientKinds': [SDK],
    }
    # Never read: the zero view with the echo, always present.
    assert items['NEVER']['usage'] == {'clientKinds': [SDK]}

    ui = {world.names[uuid.UUID(item['id'])]: item for item in await list_all(client, {'client_kind': 'UI'})}
    assert ui['RECENT']['usage']['lastReadAt'] == stamp(NOW - timedelta(days=3))
    assert ui['UIONLY']['usage'] == {
        'lastReadAt': stamp(NOW - timedelta(days=2)),
        'reads30d': '1',
        'readBy30d': [UI],
        'readByClients30d': [OPERATOR_PRINCIPAL],
        'clientKinds': [UI],
    }

    both = await list_all(client, [('client_kind', 'SDK'), ('client_kind', 'UI'), ('client_kind', 'sdk')])
    assert {tuple(item['usage']['clientKinds']) for item in both} == {(UI, SDK)}, 'kinds echo once each, enum order'

    # client_id wins: the ids are echoed (sent order, no repeats) and the kinds are NOT.
    by_id = await list_all(
        client, [('client_id', CLIENT_C), ('client_kind', 'UI'), ('client_id', CLIENT_A), ('client_id', CLIENT_C)]
    )
    for item in by_id:
        assert item['usage']['clientIds'] == [CLIENT_C, CLIENT_A] and 'clientKinds' not in item['usage'], item['usage']
    by_id_named = {world.names[uuid.UUID(item['id'])]: item for item in by_id}
    assert by_id_named['RECENT']['usage']['lastReadAt'] == stamp(NOW - timedelta(days=1))
    assert by_id_named['UIONLY']['usage'] == {
        'readBy30d': [UI],
        'readByClients30d': [OPERATOR_PRINCIPAL],
        'clientIds': [CLIENT_C, CLIENT_A],
    }

    detail = (await client.get(f'{LIST}/{world.ids["RECENT"]}')).json()
    assert detail['usage'] == items['RECENT']['usage'], 'detail reads the default SDK filter, like the list'
    assert 'reaperPending' not in detail


@pytest.mark.asyncio
async def test_reaper_pending_is_flagged_on_list_detail_and_the_reaper_page_carries_usage(world, client):
    items = {world.names[uuid.UUID(item['id'])]: item for item in await list_all(client)}
    assert {name for name, item in items.items() if item.get('reaperPending')} == {'PAST'}
    assert (await client.get(f'{LIST}/{world.ids["PAST"]}')).json()['reaperPending'] is True
    assert 'reaperPending' not in (await client.get(f'{LIST}/{world.ids["SOON"]}')).json()

    pending = (await client.get(UiReaperInterface.GET_UI_REAPER_PENDING.value)).json()
    assert world.named(pending['items']) == ['PAST']
    assert pending['items'][0]['usage'] == {'clientKinds': [SDK]}, 'the reaper page uses the default filter'


EXPIRING_CASES = {
    'a bound a week ahead': (NOW + timedelta(days=7), {'SOON', 'PAST'}),
    'a bound two days in the PAST still lists the pending dataset': (NOW - timedelta(days=2), {'PAST'}),
    'a bound two months ahead': (NOW + timedelta(days=60), {'SOON', 'FAR', 'PAST'}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('bound', 'expected'), EXPIRING_CASES.values(), ids=EXPIRING_CASES)
async def test_expiring_soon_is_expiry_before_the_bound_plus_the_pending_set_whatever_the_client(
    world, client, bound, expected
):
    for clients in ([], [('client_kind', 'UI')], [('client_id', CLIENT_B)]):
        params = [*clients, ('expires_before', bound.isoformat())]
        listed = await list_all(client, params)
        assert set(world.named(listed)) == expected, clients
        assert {name for name, item in zip(world.named(listed), listed, strict=True) if item.get('reaperPending')} == (
            {'PAST'}
        )
        body = await facets(client, params)
        assert int(body.get('expiringSoon', '0')) == len(expected), clients
        assert body['expiresBefore'] == stamp(bound)


@pytest.mark.asyncio
async def test_the_facets_default_expires_before_is_now_plus_seven_days(world, client):
    body = await facets(client)
    assert body['expiresBefore'] == stamp(NOW + timedelta(days=catalog_module.EXPIRING_SOON_DAYS))
    assert catalog_module.EXPIRING_SOON_DAYS == 7
    assert int(body['expiringSoon']) == 2
    assert body['staleDays'] == 90
    # The default bound feeds only its own count: the other counts are not narrowed to expiring datasets.
    assert int(body['all']) == len(NAMES)


@pytest.mark.asyncio
async def test_the_clients_picker_counts_datasets_per_client_in_30_days_and_ignores_the_client_filter(world, client):
    expected = [
        {'clientId': OPERATOR_PRINCIPAL, 'clientKind': UI, 'datasets': '2'},
        {'clientId': CLIENT_A, 'clientKind': SDK, 'datasets': '1'},
        {'clientId': CLIENT_C, 'clientKind': SDK, 'datasets': '1'},
    ]
    assert (await facets(client))['clients'] == expected
    assert (await facets(client, {'client_id': CLIENT_B, 'usage': 'STALE'}))['clients'] == expected
    assert (await facets(client, {'expires_before': (NOW - timedelta(days=9)).isoformat()}))['clients'] == expected
    # It honours the structural filters: under the symbol prefix R, RECENT and ROLLED (outside the window) only.
    assert (await facets(client, {'asset_symbol': 'R'}))['clients'] == [
        {'clientId': CLIENT_A, 'clientKind': SDK, 'datasets': '1'},
        {'clientId': OPERATOR_PRINCIPAL, 'clientKind': UI, 'datasets': '1'},
    ]


FACET_PARAMS = {
    'defaults': [],
    'UI kind': [('client_kind', 'UI')],
    'both kinds': [('client_kind', 'UI'), ('client_kind', 'SDK')],
    'client id wins': [('client_id', CLIENT_A), ('client_kind', 'UI')],
    'stale_days 29': [('stale_days', 29)],
    'stale_days 30': [('stale_days', 30)],
    'stale_days 31, both kinds': [('stale_days', 31), ('client_kind', 'UI'), ('client_kind', 'SDK')],
    'stale_days 1, UI': [('stale_days', 1), ('client_kind', 'UI')],
    'usage READ_30D set': [('usage', 'READ_30D')],
    'usage STALE set, stale_days 30': [('usage', 'STALE'), ('stale_days', 30)],
    'expires_before set': [('expires_before', (NOW + timedelta(days=60)).isoformat())],
    'expires_before set with a usage filter': [
        ('expires_before', (NOW + timedelta(days=60)).isoformat()),
        ('usage', 'NEVER_READ'),
    ],
    'symbol prefix': [('asset_symbol', 'D')],
    'symbol prefix and UI': [('asset_symbol', 'R'), ('client_kind', 'UI'), ('usage', 'STALE')],
}


@pytest.mark.asyncio
@pytest.mark.parametrize('params', FACET_PARAMS.values(), ids=FACET_PARAMS)
async def test_every_usage_and_expiry_facet_equals_the_filtered_list_s_total(world, client, params):
    """The acceptance: each count is the list's total with its own filter set and every other filter the same."""
    body = await facets(client, params)
    others = [(key, value) for key, value in params if key not in {'usage'}]
    for field, usage in (('stale', 'STALE'), ('read30d', 'READ_30D'), ('neverRead', 'NEVER_READ')):
        listed = await list_all(client, [*others, ('usage', usage)])
        assert int(body.get(field, '0')) == len(listed), field
    without_expiry = [(key, value) for key, value in params if key != 'expires_before']
    listed = await list_all(client, [*without_expiry, ('expires_before', body['expiresBefore'])])
    assert int(body.get('expiringSoon', '0')) == len(listed), 'expiringSoon'
    assert int(body.get('all', '0')) == len(await list_all(client, params)), 'all'
    sent_days = dict(params).get('stale_days', 90)
    assert body['staleDays'] == sent_days


@pytest.mark.asyncio
async def test_stale_days_changes_the_stale_facet_and_the_list_identically(world, client):
    seen = {}
    for days in (1, 30, 59, 61, 90, 121, 3650):
        listed = await list_all(client, {'usage': 'STALE', 'stale_days': days})
        assert int((await facets(client, {'stale_days': days}))['stale']) == len(listed), days
        seen[days] = len(listed)
    # Whole UTC days: 60 days separates OLD; 120 separates ROLLED; at 30 DAYOUT is stale and DAYIN is not; at 1
    # everything but RECENT (read yesterday).
    assert seen == {1: 9, 30: 8, 59: 7, 61: 6, 90: 6, 121: 5, 3650: 5}, seen


@pytest.mark.asyncio
@pytest.mark.parametrize('clients', [[], [('client_kind', 'UI')], [('client_kind', 'UI'), ('client_kind', 'SDK')]])
async def test_read_30d_and_stale_are_exact_opposites_at_30_and_shift_by_one_day_at_29_and_31(world, client, clients):
    """The reproducer and its neighbours, through the routes: READ_30D is fixed at 30 days, STALE moves.

    At 30 the two lists partition the catalog. At 29 the one dataset read on the window's first day (DAYIN) is in
    both; at 31 the one read the day before (DAYOUT) is in neither. Under UI alone neither edge dataset was read,
    so the partition holds at all three.
    """
    read = set(world.named(await list_all(client, [*clients, ('usage', 'READ_30D')])))
    sdk_counts = ('client_kind', 'SDK') in clients or not clients
    for days, both, neither in ((29, {'DAYIN'}, set()), (30, set(), set()), (31, set(), {'DAYOUT'})):
        stale = set(world.named(await list_all(client, [*clients, ('usage', 'STALE'), ('stale_days', days)])))
        expected_both, expected_neither = (both, neither) if sdk_counts else (set(), set())
        assert (read & stale, ALL - read - stale) == (expected_both, expected_neither), days
        body = await facets(client, [*clients, ('stale_days', days)])
        assert (int(body.get('read30d', '0')), int(body.get('stale', '0'))) == (len(read), len(stale)), days
    if sdk_counts:
        assert 'DAYIN' in read and 'DAYOUT' not in read, 'the reproducer: a read at 00:00Z of day 30 is READ_30D'


@pytest.mark.asyncio
async def test_a_dataset_read_only_by_a_stream_session_is_read_30d_and_not_never_read(
    world, client, scratch_db, entry_values
):
    """A stream session sets last_read_at, so the groups count it as a read; reads_30d still counts reads only."""
    with scratch_db.engine.begin() as conn:
        stream_id = conn.execute(
            sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol='STREAMED')).returning(ENTRY_TABLE.c.id)
        ).scalar_one()
        opened = NOW - timedelta(hours=2)
        conn.execute(
            sa.insert(READ_TABLE).values(
                {
                    **_read_row(stream_id, CLIENT_A, 'SDK', opened),
                    'kind': 'STREAM_SESSION',
                    'session_opened_at': opened,
                    'session_closed_at': opened + timedelta(minutes=30),
                }
            )
        )
    world.ids['STREAMED'] = stream_id
    world.names[stream_id] = 'STREAMED'

    def has(names: list[str]) -> bool:
        return 'STREAMED' in names

    assert has(world.named(await list_all(client, {'usage': 'READ_30D'})))
    assert not has(world.named(await list_all(client, {'usage': 'NEVER_READ'})))
    assert not has(world.named(await list_all(client, {'usage': 'STALE', 'stale_days': 1})))
    detail = (await client.get(f'{LIST}/{stream_id}')).json()
    # last read from the session; no reads in 30 days and no readers, which count ordinary reads only.
    assert detail['usage'] == {'lastReadAt': stamp(opened), 'clientKinds': [SDK]}
    body = await facets(client)
    for field, usage in (('read30d', 'READ_30D'), ('neverRead', 'NEVER_READ'), ('stale', 'STALE')):
        assert int(body.get(field, '0')) == len(await list_all(client, {'usage': usage})), field


@pytest.mark.asyncio
async def test_the_detail_takes_the_client_filter_with_the_list_s_precedence_and_echo(world, client):
    detail = f'{LIST}/{world.ids["RECENT"]}'
    ui = (await client.get(detail, params={'client_kind': 'UI'})).json()['usage']
    assert ui == {
        'lastReadAt': stamp(NOW - timedelta(days=3)),
        'reads30d': '1',
        'readBy30d': [UI, SDK],
        'readByClients30d': [CLIENT_A, OPERATOR_PRINCIPAL],
        'clientKinds': [UI],
    }
    by_id = (await client.get(detail, params=[('client_kind', 'UI'), ('client_id', CLIENT_A)])).json()['usage']
    assert by_id['lastReadAt'] == stamp(NOW - timedelta(days=1)) and by_id['clientIds'] == [CLIENT_A]
    assert 'clientKinds' not in by_id, 'client_id wins: the kinds are not echoed'
    # The detail agrees with the list's row under the same filter.
    listed = {
        world.names[uuid.UUID(item['id'])]: item
        for item in await list_all(client, [('client_kind', 'UI'), ('asset_symbol', 'RECENT')])
    }
    assert listed['RECENT']['usage'] == ui
    refused = await client.get(detail, params={'owner': 'operator'})
    assert refused.status_code == 422 and 'owner' in refused.text, refused.text


@pytest.mark.asyncio
@pytest.mark.parametrize('days', ['0', '3651', '-1', 'ninety', '1.5'])
async def test_stale_days_outside_1_to_3650_is_a_422_naming_it_on_list_and_facets(world, client, days):
    for path in (LIST, FACETS):
        response = await client.get(path, params={'stale_days': days})
        assert response.status_code == 422, response.text
        assert response.headers['content-type'].startswith('application/problem+json')
        assert 'stale_days' in response.text


@pytest.mark.asyncio
async def test_sort_by_last_read_is_newest_first_never_read_last_and_pages_without_loss(world, client):
    sdk_order = ['RECENT', 'DAYIN', 'DAYOUT', 'OLD', 'ROLLED', 'FAR', 'NEVER', 'PAST', 'SOON', 'UIONLY']
    ui_order = ['UIONLY', 'RECENT', 'DAYIN', 'DAYOUT', 'FAR', 'NEVER', 'OLD', 'PAST', 'ROLLED', 'SOON']
    for limit in (1, 2, 3, 500):
        assert world.named(await list_all(client, {'sort': 'last_read', 'limit': limit})) == sdk_order, limit
        assert (
            world.named(await list_all(client, {'sort': 'last_read', 'limit': limit, 'client_kind': 'UI'})) == ui_order
        )
    # Combined with a usage filter, the order holds over the filtered set.
    assert world.named(
        await list_all(client, {'sort': 'last_read', 'usage': 'STALE', 'stale_days': 30, 'limit': 2})
    ) == ['DAYOUT', 'OLD', 'ROLLED', 'FAR', 'NEVER', 'PAST', 'SOON', 'UIONLY']


@pytest.mark.asyncio
async def test_a_last_read_cursor_is_bound_to_its_sort(world, client):
    by_last_read = (await client.get(LIST, params={'sort': 'last_read', 'limit': 2})).json()['nextCursor']
    by_symbol = (await client.get(LIST, params={'limit': 2})).json()['nextCursor']
    by_expiry = (await client.get(LIST, params={'limit': 2, 'sort': 'expires'})).json()['nextCursor']
    for cursor, sort in (
        (by_last_read, 'symbol'),
        (by_last_read, 'expires'),
        (by_symbol, 'last_read'),
        (by_expiry, 'last_read'),
    ):
        response = await client.get(LIST, params={'sort': sort, 'cursor': cursor})
        assert response.status_code == 422, (sort, response.text)
        assert cursor not in response.text


@pytest.mark.asyncio
async def test_a_bad_client_parameter_is_a_422_that_never_echoes_the_label(world, client):
    label = 'x' * 129
    for path in (LIST, FACETS):
        response = await client.get(path, params={'client_id': label})
        assert response.status_code == 422 and 'client_id' in response.text, response.text
        assert label not in response.text, 'the refused label is echoed back'
        bogus = await client.get(path, params={'client_kind': 'robot-secret-label'})
        assert bogus.status_code == 422 and 'client_kind' in bogus.text, bogus.text
        assert 'robot-secret-label' not in bogus.text, 'the refused kind is echoed back'
        too_many = await client.get(path, params=[('client_id', f'c{index}') for index in range(33)])
        assert too_many.status_code == 422, too_many.text


@pytest.mark.asyncio
async def test_the_window_is_the_same_for_the_same_instant_in_any_zone_and_a_naive_now_is_refused(world, scratch_async):
    """Step 0: in +14:00 NOW is already 7 October, and the window's first day must still be the UTC one."""
    kiritimati = NOW.astimezone(ZoneInfo('Pacific/Kiritimati'))
    new_york = NOW.astimezone(ZoneInfo('America/New_York'))
    ids = [world.ids['DAYIN'], world.ids['DAYOUT']]
    async with AsyncSession(scratch_async) as session:
        answers = [
            {
                key: value.reads_in_window
                for key, value in (await read_dataset_usage(session, ids, now=at, window_days=30)).items()
            }
            for at in (NOW, kiritimati, new_york)
        ]
        with pytest.raises(ValueError, match='timezone-aware'):
            await read_dataset_usage(session, ids, now=NOW.replace(tzinfo=None), window_days=30)
    assert answers[0] == {world.ids['DAYIN']: 1, world.ids['DAYOUT']: 0}
    assert answers[1] == answers[0] == answers[2]


# ---------------------------------------------------------------------------------------------
# The statement budget, with the usage tables present and read.


class Statements:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sent: list[str] = []
        event.listen(engine.sync_engine, 'before_cursor_execute', self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.sent.append(statement)


BUDGET_REQUESTS = {
    'list': (LIST, {'limit': 500}),
    'list usage STALE': (LIST, {'limit': 500, 'usage': 'STALE'}),
    'list usage READ_30D by client id': (LIST, {'limit': 500, 'usage': 'READ_30D', 'client_id': CLIENT_A}),
    'list sort last_read': (LIST, {'limit': 500, 'sort': 'last_read'}),
    'list expires_before': (LIST, {'limit': 500, 'expires_before': (NOW + timedelta(days=60)).isoformat()}),
    'list everything': (
        LIST,
        {'limit': 500, 'usage': 'STALE', 'sort': 'last_read', 'expires_before': (NOW + timedelta(days=60)).isoformat()},
    ),
    'facets': (FACETS, {}),
    'facets everything': (FACETS, {'usage': 'NEVER_READ', 'client_kind': 'UI', 'expires_before': NOW.isoformat()}),
    'reaper pending': (UiReaperInterface.GET_UI_REAPER_PENDING.value, {'limit': 500}),
}


@pytest.mark.asyncio
async def test_a_request_runs_a_constant_number_of_statements_with_usage_read(
    world, client, scratch_db, scratch_async, entry_values, caplog
):
    caplog.set_level(logging.WARNING, logger='httpx')  # keep a failure's counts readable
    statements = Statements(scratch_async)

    async def counts() -> dict[str, int]:
        found = {}
        for name, (path, params) in BUDGET_REQUESTS.items():
            statements.sent.clear()
            response = await client.get(path, params=params)
            assert response.status_code == 200, (name, response.text)
            found[name] = len(statements.sent)
        statements.sent.clear()
        assert (await client.get(f'{LIST}/{world.ids["RECENT"]}')).status_code == 200
        found['detail'] = len(statements.sent)
        return found

    await counts()  # warm: the engine's first connection runs the dialect's own setup statements
    small = await counts()
    # Forty more datasets, every one read by SDK, UI and a rolled-up day, and every one pending.
    for index in range(4):
        _seed(scratch_db.engine, entry_values, prefix=f'X{index}', names=NAMES)
    with scratch_db.engine.begin() as conn:
        conn.execute(
            sa.update(ENTRY_TABLE).where(ENTRY_TABLE.c.asset_symbol.like('X%')).values(expiry=NOW - timedelta(hours=1))
        )
    large = await counts()
    assert small == large, f'the statement count grew with the datasets: {small} -> {large}'
    # Pinned exactly, so a new statement is a deliberate change. Over the SQLite tier's figures (where the usage
    # reads never run, see the module docstring): the usage read (all datasets in one statement when a filter or the
    # sort needs it, else the page's) and the readers read on list, detail and reaper pending; the usage read and the
    # clients picker on facets. The to_regclass probe runs once per request whatever asks for usage.
    assert large == {
        'list': 9,
        'list usage STALE': 9,
        'list usage READ_30D by client id': 9,
        'list sort last_read': 9,
        'list expires_before': 9,
        'list everything': 9,
        'facets': 7,
        'facets everything': 7,
        'reaper pending': 8,
        'detail': 9,
    }, large


# ---------------------------------------------------------------------------------------------
# An unmigrated database reads as the zero view.


@pytest.mark.asyncio
async def test_an_unmigrated_database_reads_as_zero_usage_with_one_warning(
    scratch_db, client, entry_values, monkeypatch, caplog
):
    scratch_db.must('upgrade', PRE_USAGE_REVISION)
    with scratch_db.engine.begin() as conn:
        conn.execute(sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol='UNREAD')))
    monkeypatch.setattr(catalog_module, '_warned_usage_missing', False)
    caplog.set_level(logging.DEBUG, logger=catalog_module.log.name)

    items = await list_all(client)
    assert [item['usage'] for item in items] == [{'clientKinds': [SDK]}]
    assert len(await list_all(client, {'usage': 'NEVER_READ'})) == 1
    assert await list_all(client, {'usage': 'READ_30D'}) == []
    body = await facets(client)
    assert (body['stale'], body.get('read30d', '0'), body['neverRead']) == ('1', '0', '1')
    assert 'clients' not in body

    warnings = [r for r in caplog.records if r.name == catalog_module.log.name and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and 'make migrate' in warnings[0].getMessage(), [r.getMessage() for r in warnings]


# ---------------------------------------------------------------------------------------------
# Bar reads are recorded: in process, through a real flush to the scratch database.


@pytest.mark.asyncio
async def test_a_bars_call_without_a_cursor_is_one_read_and_with_one_is_none(
    scratch_db, client, entry_values, bar_values, scratch_async, monkeypatch
):
    scratch_db.must('upgrade', _head())
    with scratch_db.engine.begin() as conn:
        entry = conn.execute(
            sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol='BARS')).returning(ENTRY_TABLE)
        ).one()
        conn.execute(
            sa.insert(BAR_TABLE), [bar_values(entry, timestamp=entry.start + timedelta(minutes=m)) for m in range(5)]
        )
    recorder = UsageRecorder(flush_batch_size=1000)
    monkeypatch.setattr(ui_datasets_module, 'usage_recorder', recorder)

    monkeypatch.setattr(recorder_module, 'own_session', asynccontextmanager(_session_on(scratch_async)))
    bars = f'{LIST}/{entry.id}/bars'

    first = await client.get(bars, params={'limit': 2}, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL})
    assert first.status_code == 200 and first.json()['nextCursor']
    assert recorder.buffered == 1
    more = await client.get(
        bars, params={'limit': 2, 'cursor': first.json()['nextCursor']}, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL}
    )
    assert more.status_code == 200 and len(more.json()['bars']) == 2
    assert recorder.buffered == 1, 'a continuation with a cursor was recorded'
    assert (await client.get(bars)).status_code == 200  # no header: _unattributed, 5 bars
    assert (
        await client.get(bars, params={'cursor': first.json()['nextCursor']}, headers={CLIENT_ID_HEADER: 'robot'})
    ).status_code == 200
    assert (await client.get(f'{LIST}/{uuid.uuid4()}/bars')).status_code == 404
    for path in (
        LIST,
        FACETS,
        f'{LIST}/{entry.id}',
        f'{LIST}/{entry.id}/bar-pages',
        f'{LIST}/{entry.id}/coverage',
        f'{LIST}/{entry.id}/runs',
        UiReaperInterface.GET_UI_REAPER_PENDING.value,
    ):
        response = await client.get(path, headers={CLIENT_ID_HEADER: 'robot'})
        assert response.status_code < 500, (path, response.text)
    assert recorder.buffered == 2, 'a route other than a cursorless /bars recorded a read'

    assert await recorder.flush() == 2
    with scratch_db.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                READ_TABLE.c.dataset_id,
                READ_TABLE.c.client_id,
                READ_TABLE.c.client_kind,
                READ_TABLE.c.kind,
                READ_TABLE.c.route,
                READ_TABLE.c.bars_returned,
            )
        ).all()
    assert sorted(tuple(row) for row in rows) == sorted(
        [
            (entry.id, OPERATOR_PRINCIPAL, 'UI', 'READ', BARS_ROUTE, 2),
            (entry.id, UNATTRIBUTED, 'SDK', 'READ', BARS_ROUTE, 5),
        ]
    )


def _session_on(engine: AsyncEngine):
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    return own_session


# ---------------------------------------------------------------------------------------------
# The RUNNING data_store over real HTTP.

FLUSH_WAIT_SECONDS = 30.0


def test_the_running_store_records_one_read_per_cursorless_bars_call(
    pg_engine: Engine, insert_entry, bar_values, data_store_url: str, run_identity, own_symbol: str
):
    entry = insert_entry(asset_symbol=own_symbol)
    try:
        with pg_engine.begin() as conn:
            conn.execute(
                sa.insert(BAR_TABLE),
                [bar_values(entry, timestamp=entry.start + timedelta(minutes=m)) for m in range(5)],
            )
        bars = UiDatasetsInterface.GET_UI_DATASET_BARS.format(dataset_id=entry.id)
        with httpx.Client(base_url=data_store_url, timeout=30.0) as http:
            first = http.get(bars, params={'limit': 2}, headers={CLIENT_ID_HEADER: run_identity.owner})
            assert first.status_code == 200, first.text[:500]
            cursor = first.json()['nextCursor']
            for _ in range(2):
                follow = http.get(
                    bars, params={'limit': 2, 'cursor': cursor}, headers={CLIENT_ID_HEADER: run_identity.owner}
                )
                assert follow.status_code == 200, follow.text[:500]
            assert http.get(bars, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL}).status_code == 200
            for path in (
                UiDatasetsInterface.GET_UI_DATASET.format(dataset_id=entry.id),
                UiDatasetsInterface.GET_UI_DATASET_BAR_PAGES.format(dataset_id=entry.id),
                UiDatasetsInterface.GET_UI_DATASET_COVERAGE.format(dataset_id=entry.id),
                UiDatasetsInterface.GET_UI_DATASET_RUNS.format(dataset_id=entry.id),
            ):
                http.get(path, headers={CLIENT_ID_HEADER: run_identity.owner})
            http.get(LIST, params={'asset_symbol': entry.asset_symbol}, headers={CLIENT_ID_HEADER: run_identity.owner})

        deadline = time.monotonic() + FLUSH_WAIT_SECONDS
        rows = test_usage_record._usage_rows(pg_engine, entry.id)
        while len(rows) < 2 and time.monotonic() < deadline:
            time.sleep(0.5)
            rows = test_usage_record._usage_rows(pg_engine, entry.id)
        time.sleep(6)  # one more flush interval: nothing else may arrive
        rows = test_usage_record._usage_rows(pg_engine, entry.id)
        assert sorted(tuple(row) for row in rows) == sorted(
            [(run_identity.owner, 'SDK', BARS_ROUTE, 2), (OPERATOR_PRINCIPAL, 'UI', BARS_ROUTE, 5)]
        )
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.delete(READ_TABLE).where(READ_TABLE.c.dataset_id == entry.id))
            conn.execute(sa.delete(DAILY_TABLE).where(DAILY_TABLE.c.dataset_id == entry.id))
