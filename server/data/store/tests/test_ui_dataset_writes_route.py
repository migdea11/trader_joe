"""PATCH /ui/v1/datasets/{id}/expiry and DELETE /ui/v1/datasets/{id}, driven over real rows in SQLite (tj-grna9p.86).

THE DESIGN is the bead's 2026-10-14 architect ruling, which wins over its body. The rules, in order:
  1. a malformed body or an unknown field (expiryType, updateType included) is 422 INVALID_REQUEST from ProtoBody;
  2. an unknown id is 404 NOT_FOUND;
  3. the owner is not OPERATOR_PRINCIPAL and the override is missing, unconfirmed or blank after trim: 409
     OVERRIDE_REQUIRED, with no metadata and no owner value anywhere in the body;
  4. the owner is the operator: any override sent is ignored and no override row is written;
  5. the write and EXACTLY ONE override-log row (reason stored trimmed) in one transaction; DatasetChanged strictly
     after the commit, and none on a refused write;
  6. PATCH 200 with the updated DatasetSummary; DELETE 204 with no body.
Unmigrated (no audit tables): after rules 1-3, an overriding PATCH and every DELETE are 503 DATABASE_UNAVAILABLE and
change nothing; an operator-owned PATCH still goes through.
Retire/resume (decision tj-hhy3dk, tj-grna9p.175): the expiry is the DELETION date only. Setting or clearing it retires
and resumes nothing and never touches collect; collecting is PATCH .../collecting (test_ui_dataset_collecting_route.py).
The expiry TYPE never changes.

WHAT TIER THIS IS. The real app, handlers, repository SQL and mapping over real tables in an in-memory SQLite
database, as test_ui_datasets_route.py (whose fixtures and row builders are reused). Foreign keys are switched on
so the bars' ON DELETE CASCADE runs. The one thing SQLite cannot run is audit_tables_exist's to_regclass, so it is
replaced per test by a stand-in answering True (migrated) or False (unmigrated); the real probe is exercised by a
fake session below and on Postgres in tests/system/test_http_ui_dataset_writes.py.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_stock import ExpiryType, Granularity, UpdateType
from data.store.app.client_label import CLIENT_ID_HEADER, OPERATOR_PRINCIPAL
from data.store.app.database.crud import dataset_override
from data.store.app.database.crud.dataset_override import OperatorOverrideInput, OverrideRequired, overriding_reason
from data.store.app.database.crud.stock import store_dataset_entry as repo
from data.store.app.database.database import async_db
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.main import app
from data.store.app.usage_recorder import UsageRecorder
from data.store.tests.problem_body import problem
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    NOW,
    _sqlite_timezone,
    aware_sqlite_datetimes,
    bar,
    entry,
    ny_midnight,
    z,
)
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from routers.data_store import ui_dataset_writes
from routers.data_store.ui_datasets import utc_now


pytestmark = pytest.mark.data_store

SECRET = 'ui-writes-instance-secret'
HEADERS = {INSTANCE_SECRET_HEADER: SECRET}
# Distinctive, so its absence from a body or a log line is evidence and not luck.
FOREIGN_OWNER = 'strategy-zq-owner-5521'
GOOD_OVERRIDE = {'reason': '  rebalance cleanup  ', 'confirmed': True}
FUTURE = '2026-10-20T00:00:00Z'


def expiry_path(dataset_id: Any) -> str:
    return f'/ui/v1/datasets/{dataset_id}/expiry'


def dataset_path(dataset_id: Any) -> str:
    return f'/ui/v1/datasets/{dataset_id}'


# ------------------------------------------------------------------------------------------------- fixture


class World:
    """The engine, the commit count of the request sessions, and every event published with that count."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.commits = 0
        self.events: list[tuple[Any, int]] = []
        self.migrated = True
        self.dataset = self.foreign = self.stream = self.static = self.open_static = None

    async def rows(self, model: Any) -> list[Any]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(model))).scalars())

    async def entry(self, dataset_id: uuid.UUID) -> StoreDatasetEntry | None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return (
                await db.execute(select(StoreDatasetEntry).where(StoreDatasetEntry.id == dataset_id))
            ).scalar_one_or_none()

    async def audit_rows(self) -> tuple[list, list]:
        return await self.rows(DatasetOverrideLog), await self.rows(DatasetDeletion)


@pytest_asyncio.fixture
async def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    # Reads timestamptz back UTC-aware, as asyncpg does; imported from test_ui_datasets_route.py.
    request.getfixturevalue('aware_sqlite_datetimes')
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('timezone', 2, _sqlite_timezone))
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA case_sensitive_like = ON'))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA foreign_keys = ON'))

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync,
                tables=[
                    StoreDatasetEntry.__table__,
                    StockMarketActivity.__table__,
                    IngestRun.__table__,
                    DatasetOverrideLog.__table__,
                    DatasetDeletion.__table__,
                ],
            )
        )
    state = World(engine)

    # The operator's own DAILY subscription, another owner's DAILY, another owner's STREAM, a closed STATIC and an
    # open-ended one, each with bars where the case needs them.
    state.dataset = entry('OPA', update_type=UpdateType.DAILY)
    state.foreign = entry('FRN', update_type=UpdateType.DAILY)
    state.foreign.owner = FOREIGN_OWNER
    state.stream = entry('STR', granularity=Granularity.ONE_MINUTE, update_type=UpdateType.STREAM)
    state.stream.owner = FOREIGN_OWNER
    state.static = entry('STA', start=ny_midnight(1), end=ny_midnight(6), expiry=z('2026-09-01T00:00:00Z'))
    state.open_static = entry('OPN', end=None)
    state.open_static.owner = FOREIGN_OWNER
    rows = [state.dataset, state.foreign, state.stream, state.static, state.open_static]
    bars = [
        bar(state.dataset, ny_midnight(5)),
        bar(state.dataset, ny_midnight(6)),
        bar(state.foreign, ny_midnight(2)),
        bar(state.foreign, ny_midnight(5)),
        bar(state.foreign, ny_midnight(6)),
        bar(state.open_static, ny_midnight(6)),
    ]
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add_all(rows)
        await db.flush()
        db.add_all(bars)
        await db.commit()

    class CountingSession(AsyncSession):
        async def commit(self) -> None:
            await super().commit()
            state.commits += 1

    async def session() -> AsyncIterator[AsyncSession]:
        async with CountingSession(engine, expire_on_commit=False) as db:
            yield db

    async def pinned_now():
        return NOW

    async def tables_exist(_db) -> bool:
        return state.migrated

    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, SECRET)
    monkeypatch.setattr(repo, 'audit_tables_exist', tables_exist)
    monkeypatch.setattr(ui_dataset_writes, 'publish', lambda e: state.events.append((e, state.commits)))
    app.dependency_overrides[async_db] = session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest_asyncio.fixture
async def client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
        yield http


async def patch(client: httpx.AsyncClient, dataset_id: Any, body: Any, headers: dict | None = None) -> httpx.Response:
    content = body if isinstance(body, str | bytes) else None
    return await client.patch(
        expiry_path(dataset_id),
        headers=HEADERS if headers is None else headers,
        **({'content': content} if content is not None else {'json': body}),
    )


async def delete(client: httpx.AsyncClient, dataset_id: Any, body: Any = None, headers: dict | None = None):
    kwargs: dict = {}
    if isinstance(body, str | bytes):
        kwargs['content'] = body
    elif body is not None:
        kwargs['json'] = body
    return await client.request(
        'DELETE', dataset_path(dataset_id), headers=HEADERS if headers is None else headers, **kwargs
    )


async def assert_untouched(world: World, dataset_id: uuid.UUID, expiry: Any) -> None:
    """A refused write changed nothing: the entry and its expiry stand, no audit row, no event."""
    stored = await world.entry(dataset_id)
    assert stored is not None, 'a refused write deleted the dataset'
    assert stored.expiry == expiry
    assert await world.audit_rows() == ([], [])
    assert world.events == [], 'a write that did not happen was announced'


# ------------------------------------------------------------------------------------------------- rule 1: 422


BAD_BODIES = [
    pytest.param('{not json', id='malformed-json'),
    pytest.param('[]', id='json-array'),
    pytest.param('"x"', id='json-string'),
    pytest.param('null', id='json-null'),
    pytest.param({'expiryType': 'EXPIRY_TYPE_BULK'}, id='expiryType'),
    pytest.param({'updateType': 'UPDATE_TYPE_STATIC'}, id='updateType'),
    pytest.param({'expiry': FUTURE, 'expiryType': 'EXPIRY_TYPE_ROLLING', 'override': GOOD_OVERRIDE}, id='type+valid'),
    pytest.param({'expiry': 'not a time'}, id='bad-timestamp'),
    pytest.param({'override': {'reason': 'x', 'confirmed': True, 'owner': 'me'}}, id='unknown-nested'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('body', BAD_BODIES)
async def test_a_bad_patch_body_is_422_and_changes_nothing(client, world, body):
    """The expiry TYPE can never change: the body has no field for it, and sending one is refused end to end."""
    response = await patch(client, world.foreign.id, body)

    problem(response, status=422, reason='INVALID_REQUEST')
    await assert_untouched(world, world.foreign.id, None)
    assert (await world.entry(world.foreign.id)).expiry_type == ExpiryType.ROLLING.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'body',
    [
        pytest.param('{not json', id='malformed-json'),
        pytest.param('null', id='json-null'),
        pytest.param({'expiry': FUTURE}, id='expiry-on-delete'),
        pytest.param({'override': GOOD_OVERRIDE, 'updateType': 'UPDATE_TYPE_STATIC'}, id='updateType'),
    ],
)
async def test_a_bad_delete_body_is_422_and_deletes_nothing(client, world, body):
    response = await delete(client, world.foreign.id, body)

    problem(response, status=422, reason='INVALID_REQUEST')
    await assert_untouched(world, world.foreign.id, None)


@pytest.mark.asyncio
async def test_a_patch_with_no_body_is_422_not_a_clear(client, world):
    """Clearing is an explicit '{}': an absent body is malformed, never a silent RESUME."""
    response = await client.patch(expiry_path(world.static.id), headers=HEADERS)
    problem(response, status=422, reason='INVALID_REQUEST')
    await assert_untouched(world, world.static.id, world.static.expiry)


@pytest.mark.asyncio
async def test_422_comes_before_404_and_before_409(client, world):
    problem(await patch(client, uuid.uuid4(), {'expiryType': 'X'}), status=422, reason='INVALID_REQUEST')
    problem(await delete(client, uuid.uuid4(), {'expiryType': 'X'}), status=422, reason='INVALID_REQUEST')
    # Another owner's dataset and no override: the body's fault is reported first.
    problem(await patch(client, world.foreign.id, {'updateType': 'X'}), status=422, reason='INVALID_REQUEST')
    problem(await delete(client, world.foreign.id, '[]'), status=422, reason='INVALID_REQUEST')


# ------------------------------------------------------------------------------------------------- rule 2: 404


@pytest.mark.asyncio
async def test_an_unknown_id_is_404_on_both_routes_even_without_an_override(client, world):
    missing = uuid.uuid4()
    problem(await patch(client, missing, {'expiry': FUTURE}), status=404, reason='NOT_FOUND')
    problem(await delete(client, missing), status=404, reason='NOT_FOUND')
    problem(await patch(client, missing, {'expiry': FUTURE, 'override': GOOD_OVERRIDE}), status=404)
    problem(await delete(client, missing, {'override': GOOD_OVERRIDE}), status=404)
    assert await world.audit_rows() == ([], [])
    assert world.events == []


@pytest.mark.asyncio
async def test_a_malformed_id_is_422(client, world):
    problem(await patch(client, 'not-a-uuid', {}), status=422)
    problem(await delete(client, 'not-a-uuid'), status=422)


# ------------------------------------------------------------------------------------------------- rule 3: 409


INVALID_OVERRIDES = [
    pytest.param(None, id='absent'),
    pytest.param({'reason': 'because', 'confirmed': False}, id='unconfirmed'),
    pytest.param({'reason': 'because'}, id='confirmed-omitted'),
    pytest.param({'reason': '', 'confirmed': True}, id='empty-reason'),
    pytest.param({'reason': ' \t\n ', 'confirmed': True}, id='blank-after-trim'),
    pytest.param({'confirmed': True}, id='reason-omitted'),
    pytest.param({}, id='empty-override'),
]


def _with_override(body: dict, override: Any) -> dict:
    return body if override is None else {**body, 'override': override}


@pytest.mark.asyncio
@pytest.mark.parametrize('override', INVALID_OVERRIDES)
async def test_another_owners_dataset_without_a_valid_override_is_409_on_patch(client, world, override, caplog):
    caplog.set_level(logging.DEBUG)
    response = await patch(client, world.foreign.id, _with_override({'expiry': FUTURE}, override))

    body = problem(response, status=409, reason='OVERRIDE_REQUIRED')
    assert FOREIGN_OWNER not in response.text, f'the 409 names the owner: {response.text}'
    assert FOREIGN_OWNER not in caplog.text, 'the owner reached a log line'
    assert 'metadata' not in body
    await assert_untouched(world, world.foreign.id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('override', INVALID_OVERRIDES)
async def test_another_owners_dataset_without_a_valid_override_is_409_on_delete(client, world, override, caplog):
    caplog.set_level(logging.DEBUG)
    response = await delete(client, world.foreign.id, _with_override({}, override))

    problem(response, status=409, reason='OVERRIDE_REQUIRED')
    assert FOREIGN_OWNER not in response.text
    assert FOREIGN_OWNER not in caplog.text
    await assert_untouched(world, world.foreign.id, None)
    assert len(await world.rows(StockMarketActivity)) == 6, 'a refused delete removed bars'


@pytest.mark.asyncio
async def test_an_empty_delete_body_on_another_owners_dataset_is_409(client, world):
    problem(await delete(client, world.foreign.id), status=409, reason='OVERRIDE_REQUIRED')
    problem(await delete(client, world.foreign.id, '  '), status=409, reason='OVERRIDE_REQUIRED')
    await assert_untouched(world, world.foreign.id, None)


@pytest.mark.asyncio
async def test_the_409_carries_no_member_a_404_does_not(client, world):
    """No metadata: the 409 has exactly the members of a plain refusal from the same route, and no owner value."""
    refused = problem(await patch(client, world.foreign.id, {}), status=409, reason='OVERRIDE_REQUIRED')
    missing = problem(await patch(client, uuid.uuid4(), {}), status=404, reason='NOT_FOUND')
    assert set(refused) == set(missing), (set(refused), set(missing))
    assert FOREIGN_OWNER not in str(refused.values())


# ------------------------------------------------------------------------------------------------- rule 4: operator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'override',
    [None, {'reason': '', 'confirmed': False}, {'reason': 'x', 'confirmed': False}, GOOD_OVERRIDE],
    ids=['none', 'blank-unconfirmed', 'unconfirmed', 'valid'],
)
async def test_an_operator_owned_dataset_ignores_any_override(client, world, override):
    assert world.dataset.owner == OPERATOR_PRINCIPAL

    response = await patch(client, world.dataset.id, _with_override({'expiry': FUTURE}, override))

    assert response.status_code == 200, response.text
    assert await world.rows(DatasetOverrideLog) == [], 'an override row was written for the operator'
    assert (await world.entry(world.dataset.id)).expiry == z(FUTURE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'body', [None, '', {}, {'override': {'reason': '', 'confirmed': False}}, {'override': GOOD_OVERRIDE}]
)
async def test_an_operator_delete_writes_a_deletion_row_and_no_override_row(client, world, body):
    response = await delete(client, world.dataset.id, body)

    assert response.status_code == 204, response.text
    assert response.content == b''
    overrides, deletions = await world.audit_rows()
    assert overrides == []
    assert len(deletions) == 1
    assert await world.entry(world.dataset.id) is None


# ------------------------------------------------------------------------------------------------- rule 5: the write


@pytest.mark.asyncio
async def test_an_overriding_patch_writes_exactly_one_trimmed_override_row(client, world):
    response = await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': GOOD_OVERRIDE})

    assert response.status_code == 200, response.text
    overrides, deletions = await world.audit_rows()
    assert deletions == []
    (row,) = overrides
    assert (row.actor, row.dataset_id, row.owner, row.action, row.reason) == (
        OPERATOR_PRINCIPAL,
        world.foreign.id,
        FOREIGN_OWNER,
        OverrideAction.SET_EXPIRY,
        'rebalance cleanup',
    )
    assert row.at is not None


@pytest.mark.asyncio
async def test_a_write_route_ignores_the_client_id_header(client, world, monkeypatch, caplog):
    """OPTION R (tj-grna9p.131): X-Client-Id is a read label, never an authority, an actor or a recorded read."""
    recorded: list[Any] = []
    monkeypatch.setattr(UsageRecorder, 'record_read', lambda self, *a, **k: recorded.append(a))
    monkeypatch.setattr(UsageRecorder, '_append', lambda self, *a, **k: recorded.append(a))
    caplog.set_level(logging.DEBUG)
    as_operator = {**HEADERS, CLIENT_ID_HEADER: OPERATOR_PRINCIPAL}
    as_owner = {**HEADERS, CLIENT_ID_HEADER: FOREIGN_OWNER}

    # Claiming to be the operator, or the dataset's owner, does not stand in for the override.
    assert (await patch(client, world.foreign.id, {'expiry': FUTURE}, as_operator)).status_code == 409
    assert (await delete(client, world.foreign.id, None, as_owner)).status_code == 409
    await assert_untouched(world, world.foreign.id, world.foreign.expiry)

    label = 'write-route-label-7f3a'
    with_label = {**HEADERS, CLIENT_ID_HEADER: label}
    assert (
        await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': GOOD_OVERRIDE}, with_label)
    ).status_code == 200
    assert (await delete(client, world.foreign.id, {'override': GOOD_OVERRIDE}, with_label)).status_code == 204
    overrides, deletions = await world.audit_rows()
    assert [row.actor for row in [*overrides, *deletions]] == [OPERATOR_PRINCIPAL] * 3, 'the header became the actor'
    assert recorded == [], 'a write recorded a read'
    assert label not in caplog.text


@pytest.mark.asyncio
async def test_each_overriding_write_appends_its_own_row(client, world):
    """Append-only: a second override adds a row and never rewrites the first."""
    await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': {'reason': 'one', 'confirmed': True}})
    await patch(client, world.foreign.id, {'override': {'reason': 'two', 'confirmed': True}})

    assert sorted(row.reason for row in await world.rows(DatasetOverrideLog)) == ['one', 'two']


@pytest.mark.asyncio
async def test_an_overriding_delete_writes_one_deletion_and_one_override_row_and_the_bars_go(client, world, caplog):
    caplog.set_level(logging.DEBUG)
    response = await delete(client, world.foreign.id, {'override': GOOD_OVERRIDE})

    assert response.status_code == 204, response.text
    assert await world.entry(world.foreign.id) is None
    remaining = {b.dataset_id for b in await world.rows(StockMarketActivity)}
    assert world.foreign.id not in remaining, 'the cascade did not remove the bars'
    assert world.dataset.id in remaining, 'another dataset lost its bars'
    (override,) = await world.rows(DatasetOverrideLog)
    assert (override.action, override.reason, override.owner) == (
        OverrideAction.DELETE,
        'rebalance cleanup',
        FOREIGN_OWNER,
    )
    (deletion,) = await world.rows(DatasetDeletion)
    assert (
        deletion.dataset_id,
        deletion.owner,
        deletion.asset_symbol,
        deletion.source,
        deletion.granularity,
        deletion.update_type,
        deletion.range_start,
        deletion.bars_deleted,
        deletion.cause,
        deletion.actor,
    ) == (
        world.foreign.id,
        FOREIGN_OWNER,
        'FRN',
        world.foreign.source.name,
        'ONE_DAY',
        'DAILY',
        world.foreign.start,
        3,
        DatasetDeletion.CAUSE_OPERATOR,
        OPERATOR_PRINCIPAL,
    )
    assert FOREIGN_OWNER not in caplog.text


@pytest.mark.asyncio
async def test_the_deletion_row_records_a_closed_range_end_and_none_for_an_open_one(client, world):
    assert (await delete(client, world.static.id)).status_code == 204
    assert (await delete(client, world.open_static.id, {'override': GOOD_OVERRIDE})).status_code == 204

    ends = {row.asset_symbol: (row.range_end, row.bars_deleted) for row in await world.rows(DatasetDeletion)}
    assert ends == {'STA': (ny_midnight(6), 0), 'OPN': (None, 1)}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['override', 'deletion'])
async def test_a_failing_audit_insert_rolls_the_write_back_and_announces_nothing(client, world, monkeypatch, stage):
    """One transaction: the write never commits without its audit rows."""

    async def broken(*_args, **_kwargs):
        raise RuntimeError(f'{stage} insert failed')

    monkeypatch.setattr(repo, f'append_{stage}', broken)

    if stage == 'override':
        with pytest.raises(RuntimeError):
            await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': GOOD_OVERRIDE})
        await assert_untouched(world, world.foreign.id, None)
    with pytest.raises(RuntimeError):
        await delete(client, world.foreign.id, {'override': GOOD_OVERRIDE})
    await assert_untouched(world, world.foreign.id, None)
    assert len(await world.rows(StockMarketActivity)) == 6
    assert world.commits == 0


# ------------------------------------------------------------------------------------------------- events


@pytest.mark.asyncio
async def test_patch_publishes_updated_strictly_after_its_commit(client, world):
    assert (await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': GOOD_OVERRIDE})).status_code == 200
    assert world.events == [(DatasetChanged(world.foreign.id, DatasetChange.UPDATED), 1)]


@pytest.mark.asyncio
async def test_delete_publishes_deleted_strictly_after_its_commit(client, world):
    assert (await delete(client, world.foreign.id, {'override': GOOD_OVERRIDE})).status_code == 204
    assert world.events == [(DatasetChanged(world.foreign.id, DatasetChange.DELETED), 1)]


# ------------------------------------------------------------------------------------------------- a 0-row UPDATE
# tj-grna9p.23, the architect's round 2 gate (item B), fixed in 138c009: a PATCH whose dataset is deleted between its
# read and its UPDATE (on Postgres: it waited on the reaper's row lock) must raise EntryNotFound on the UPDATE's row
# count, BEFORE the override row, so the transaction rolls back, no append-only row is left for a dataset that no
# longer exists, and the route answers 404 without publishing. Here the delete is committed by another session right
# after operator_set_expiry's read; the two-session version on Postgres is tests/system/test_reaper_race.py.


def _deleted_right_after_the_read(world: World, monkeypatch: pytest.MonkeyPatch, dataset_id: uuid.UUID) -> None:
    real = repo._get_entry_or_raise

    async def then_deleted(db, id):
        existing = await real(db, id)
        async with AsyncSession(world.engine) as other:
            await other.execute(StoreDatasetEntry.__table__.delete().where(StoreDatasetEntry.id == dataset_id))
            await other.commit()
        return existing

    monkeypatch.setattr(repo, '_get_entry_or_raise', then_deleted)


@pytest.mark.asyncio
async def test_an_update_that_matches_no_row_raises_not_found_and_writes_no_override_row(world, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _deleted_right_after_the_read(world, monkeypatch, world.foreign.id)

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        with pytest.raises(repo.EntryNotFound):
            await repo.operator_set_expiry(
                db, world.foreign.id, z(FUTURE), OperatorOverrideInput(reason='keep', confirmed=True)
            )

    assert await world.entry(world.foreign.id) is None
    assert await world.audit_rows() == ([], []), 'an override row was left for a dataset that no longer exists'
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    ('name', 'body'),
    [
        pytest.param('foreign', {'expiry': FUTURE, 'override': GOOD_OVERRIDE}, id='overriding'),
        pytest.param('dataset', {'expiry': FUTURE}, id='operator-owned'),
    ],
)
@pytest.mark.asyncio
async def test_a_patch_whose_dataset_goes_before_its_update_is_404_with_no_override_row_and_no_event(
    client, world, monkeypatch, caplog, name, body
):
    caplog.set_level(logging.DEBUG)
    dataset_id = getattr(world, name).id
    _deleted_right_after_the_read(world, monkeypatch, dataset_id)

    response = await patch(client, dataset_id, body)

    problem(response, status=404, reason='NOT_FOUND')
    overrides, _ = await world.audit_rows()
    assert (overrides, world.events) == ([], []), 'a PATCH that updated nothing left an override row or an event'
    assert world.commits == 0, 'the PATCH committed'
    assert FOREIGN_OWNER not in response.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [r.getMessage() for r in caplog.records]


# ------------------------------------------------------------------------------------------------- rule 6 and semantics


@pytest.mark.asyncio
@pytest.mark.parametrize('collect', [True, False], ids=['collecting', 'stopped'])
@pytest.mark.parametrize('name', ['dataset', 'foreign', 'stream'])
async def test_setting_or_clearing_an_expiry_never_retires_or_resumes_a_subscription(client, world, name, collect):
    """The expiry is the deletion date only: the state, the collect intent and collecting follow collect, not it."""
    row = getattr(world, name)
    await set_collect(world, row.id, collect)
    override = {} if row.owner == OPERATOR_PRINCIPAL else {'override': GOOD_OVERRIDE}
    state = 'DATASET_STATE_ACTIVE' if collect else 'DATASET_STATE_RETIRED'

    dated = await patch(client, row.id, {'expiry': FUTURE, **override})
    assert dated.status_code == 200, dated.text
    summary = dated.json()
    assert (summary['id'], summary['expiry'], summary['state']) == (str(row.id), FUTURE, state)
    assert summary.get('collecting', False) is collect, summary
    assert (summary['freshness']['status'] == 'FRESHNESS_STATUS_RETIRED') is not collect, summary['freshness']
    assert (await world.entry(row.id)).collect is collect, 'setting an expiry changed collect'

    cleared = await patch(client, row.id, override)
    assert cleared.status_code == 200, cleared.text
    assert 'expiry' not in cleared.json()
    assert cleared.json()['state'] == state, 'clearing an expiry changed the state'
    assert cleared.json().get('collecting', False) is collect
    stored = await world.entry(row.id)
    assert (stored.expiry, stored.collect) == (None, collect), 'clearing an expiry changed collect'
    assert (stored.expiry_type, stored.update_type) == (ExpiryType.ROLLING.value, row.update_type.value), (
        'the expiry write changed the expiry type or the update type'
    )


async def set_collect(world: World, dataset_id: uuid.UUID, collect: bool | None) -> None:
    async with AsyncSession(world.engine) as db:
        await db.execute(
            StoreDatasetEntry.__table__.update().where(StoreDatasetEntry.id == dataset_id).values(collect=collect)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_the_patch_answer_is_the_same_summary_a_detail_read_returns(client, world):
    response = await patch(client, world.foreign.id, {'expiry': FUTURE, 'override': GOOD_OVERRIDE})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/json')
    assert response.json() == (await client.get(dataset_path(world.foreign.id))).json()


@pytest.mark.asyncio
async def test_a_static_dataset_takes_the_time_and_stays_active(client, world):
    response = await patch(client, world.static.id, {'expiry': '2027-01-01T00:00:00Z'})
    assert response.status_code == 200, response.text
    assert (response.json()['expiry'], response.json()['state']) == ('2027-01-01T00:00:00Z', 'DATASET_STATE_ACTIVE')
    assert (await world.entry(world.static.id)).expiry_type == ExpiryType.BULK.value


@pytest.mark.asyncio
async def test_a_past_expiry_can_be_cleared_and_a_past_one_set(client, world):
    """The STATIC row's expiry is already in the past at NOW: clearing it works, and a past time is stored as sent."""
    assert world.static.expiry < NOW
    assert (await patch(client, world.static.id, {})).status_code == 200
    assert (await world.entry(world.static.id)).expiry is None

    past = NOW - timedelta(days=3)
    response = await patch(client, world.dataset.id, {'expiry': past.isoformat().replace('+00:00', 'Z')})
    assert response.status_code == 200
    assert (await world.entry(world.dataset.id)).expiry == past
    # A past deletion date retires nothing (decision tj-hhy3dk): the reaper deletes it, the state stays ACTIVE.
    assert (response.json()['state'], response.json()['collecting']) == ('DATASET_STATE_ACTIVE', True)
    assert (await world.entry(world.dataset.id)).collect is True


@pytest.mark.asyncio
async def test_an_expiry_with_an_offset_is_stored_as_the_same_instant(client, world):
    response = await patch(client, world.dataset.id, {'expiry': '2026-10-20T02:00:00+02:00'})
    assert response.status_code == 200, response.text
    assert (await world.entry(world.dataset.id)).expiry == z(FUTURE)


# ------------------------------------------------------------------------------------------------- the instance secret


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{}, {INSTANCE_SECRET_HEADER: 'wrong'}], ids=['absent', 'wrong'])
async def test_both_routes_require_the_instance_secret_before_anything_else(client, world, headers):
    """401 precedes even the 422 of a bad body, and nothing is written."""
    for body in ({'expiry': FUTURE, 'override': GOOD_OVERRIDE}, '{bad'):
        assert (await patch(client, world.foreign.id, body, headers)).status_code == 401
        assert (await delete(client, world.foreign.id, body, headers)).status_code == 401
    assert (await delete(client, world.dataset.id, headers=headers)).status_code == 401
    await assert_untouched(world, world.foreign.id, None)
    assert await world.entry(world.dataset.id) is not None


# ------------------------------------------------------------------------------------------------- unmigrated


# THE ARCHITECT'S 2026-10-14 RULING on this bead: the override log and the deletion record are the audit of an act,
# not telemetry, so a write that needs a row is REFUSED when the tables are missing -- an overriding PATCH and every
# DELETE, the operator's own included (it writes a deletion row). The refusal is the existing DATABASE_UNAVAILABLE
# (503, no new reason) with a fixed detail naming `make migrate`, no owner and no metadata. It comes after the 422,
# 404 and 409 checks and before any write, so nothing changes and nothing is announced. An operator-owned PATCH
# needs no row and still goes through.

UNMIGRATED_DETAIL = 'The database has not been migrated: the operator audit tables are missing. Run make migrate.'

AUDITED_WRITES = [
    pytest.param('patch', 'foreign', {'expiry': FUTURE, 'override': GOOD_OVERRIDE}, id='overriding-patch'),
    pytest.param('delete', 'foreign', {'override': GOOD_OVERRIDE}, id='overriding-delete'),
    pytest.param('delete', 'dataset', None, id='operator-own-delete'),
    pytest.param('delete', 'dataset', {'override': GOOD_OVERRIDE}, id='operator-own-delete-ignored-override'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('verb', 'name', 'body'), AUDITED_WRITES)
async def test_an_unmigrated_database_refuses_every_audited_write_and_changes_nothing(
    client, world, caplog, verb, name, body
):
    caplog.set_level(logging.DEBUG)
    world.migrated = False
    row = getattr(world, name)
    send = patch if verb == 'patch' else delete

    response = await send(client, row.id, body)

    refused = problem(response, status=503, reason='DATABASE_UNAVAILABLE')
    assert refused['detail'] == UNMIGRATED_DETAIL
    assert 'metadata' not in refused
    assert FOREIGN_OWNER not in response.text, f'the 503 names the owner: {response.text}'
    assert FOREIGN_OWNER not in caplog.text, 'the owner reached a log line'
    # The same members a plain 404 from the same route carries: the platform renderer's error_id, nothing more.
    missing = problem(await send(client, uuid.uuid4(), body), status=404, reason='NOT_FOUND')
    assert set(refused) == set(missing), (set(refused), set(missing))
    await assert_untouched(world, row.id, row.expiry)
    assert len(await world.rows(StockMarketActivity)) == 6, 'a refused write removed bars'
    assert world.commits == 0, 'a refused write committed'


@pytest.mark.asyncio
@pytest.mark.parametrize(('verb', 'name', 'body'), AUDITED_WRITES)
async def test_an_unmigrated_refusal_issues_no_write_statement_at_all(client, world, verb, name, body):
    """BEFORE any write, not undone by the rollback: the refused call sends no UPDATE, DELETE or INSERT.

    The rollback would hide a write issued and then undone (the test above stays green either way); on Postgres
    that write still takes the row lock and runs for a call that is refused. Found at 6b96fa4, where the overriding
    PATCH issued its UPDATE before raising.
    """
    world.migrated = False
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement)

    event.listen(world.engine.sync_engine, 'before_cursor_execute', record)
    try:
        send = patch if verb == 'patch' else delete
        problem(await send(client, getattr(world, name).id, body), status=503, reason='DATABASE_UNAVAILABLE')
    finally:
        event.remove(world.engine.sync_engine, 'before_cursor_execute', record)

    assert statements, 'the listener saw nothing, so the assertion below would hold vacuously'
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {'UPDATE', 'DELETE', 'INSERT'}]
    assert writes == [], f'a refused write issued {writes}'


@pytest.mark.asyncio
async def test_an_unmigrated_database_still_answers_422_404_and_409_first(client, world):
    """The refusal is the LAST rule: a bad body, an unknown id and a missing override each keep their own answer."""
    world.migrated = False

    problem(await patch(client, world.foreign.id, {'expiryType': 'EXPIRY_TYPE_BULK'}), status=422)
    problem(await delete(client, world.dataset.id, '{bad'), status=422)
    problem(await patch(client, uuid.uuid4(), {'expiry': FUTURE, 'override': GOOD_OVERRIDE}), status=404)
    problem(await delete(client, uuid.uuid4()), status=404, reason='NOT_FOUND')
    problem(await patch(client, world.foreign.id, {'expiry': FUTURE}), status=409, reason='OVERRIDE_REQUIRED')
    problem(await delete(client, world.foreign.id), status=409, reason='OVERRIDE_REQUIRED')
    await assert_untouched(world, world.foreign.id, None)
    assert await world.entry(world.dataset.id) is not None


@pytest.mark.asyncio
async def test_an_unmigrated_database_lets_an_operator_owned_patch_through_unrecorded(client, world):
    """No audit row is needed for the operator's own dataset, so its expiry write proceeds, set and clear alike."""
    world.migrated = False

    dated = await patch(client, world.dataset.id, {'expiry': FUTURE})
    assert dated.status_code == 200, dated.text
    assert dated.json()['state'] == 'DATASET_STATE_ACTIVE', 'an expiry retired the subscription'
    assert (await world.entry(world.dataset.id)).expiry == z(FUTURE)

    cleared = await patch(client, world.dataset.id, {'override': GOOD_OVERRIDE})
    assert cleared.status_code == 200, cleared.text
    assert (await world.entry(world.dataset.id)).expiry is None

    assert await world.audit_rows() == ([], [])
    assert world.events == [
        (DatasetChanged(world.dataset.id, DatasetChange.UPDATED), 1),
        (DatasetChanged(world.dataset.id, DatasetChange.UPDATED), 2),
    ]


@pytest.mark.asyncio
async def test_an_unmigrated_database_warns_once_through_the_routes(client, world, monkeypatch, caplog):
    """The real probe, behind the routes: one process-wide warning naming `make migrate`, however many writes."""
    monkeypatch.setattr(dataset_override, '_warned_missing', False)

    async def unmigrated(_db) -> bool:
        return await dataset_override.audit_tables_exist(_ProbeSession((False, False)))

    monkeypatch.setattr(repo, 'audit_tables_exist', unmigrated)
    caplog.set_level(logging.WARNING, logger=dataset_override.log.name)

    problem(await delete(client, world.dataset.id), status=503, reason='DATABASE_UNAVAILABLE')
    problem(await patch(client, world.foreign.id, {'override': GOOD_OVERRIDE}), status=503)
    problem(await delete(client, world.foreign.id, {'override': GOOD_OVERRIDE}), status=503)
    assert (await patch(client, world.dataset.id, {'expiry': FUTURE})).status_code == 200

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == dataset_override.log.name and r.levelno == logging.WARNING and 'make migrate' in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert FOREIGN_OWNER not in warnings[0]


class _ProbeResult:
    def __init__(self, answer: tuple[bool, bool]) -> None:
        self._answer = answer

    def one(self) -> tuple[bool, bool]:
        return self._answer


class _ProbeSession:
    def __init__(self, answer: tuple[bool, bool]) -> None:
        self.answer = answer
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ProbeResult(self.answer)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [(False, False), (True, False), (False, True)])
async def test_the_probe_answers_false_unless_both_tables_exist_and_warns_once(monkeypatch, caplog, answer):
    monkeypatch.setattr(dataset_override, '_warned_missing', False)
    caplog.set_level(logging.WARNING, logger=dataset_override.log.name)

    assert await dataset_override.audit_tables_exist(_ProbeSession(answer)) is False
    assert await dataset_override.audit_tables_exist(_ProbeSession(answer)) is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and 'make migrate' in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_the_probe_answers_true_without_a_warning_and_reads_both_tables(monkeypatch, caplog):
    monkeypatch.setattr(dataset_override, '_warned_missing', False)
    caplog.set_level(logging.WARNING)
    session = _ProbeSession((True, True))

    assert await dataset_override.audit_tables_exist(session) is True
    assert 'make migrate' not in caplog.text
    sql = str(session.statements[0])
    assert 'to_regclass' in sql
    (statement,) = session.statements
    names = {str(v) for v in statement.compile().params.values()}
    assert names == {DatasetOverrideLog.TABLE_NAME, DatasetDeletion.TABLE_NAME}


# ------------------------------------------------------------------------------------------------- the decision


@pytest.mark.parametrize(
    ('owner', 'override', 'expected'),
    [
        (OPERATOR_PRINCIPAL, None, None),
        (OPERATOR_PRINCIPAL, OperatorOverrideInput(reason='', confirmed=False), None),
        (OPERATOR_PRINCIPAL, OperatorOverrideInput(reason='why', confirmed=True), None),
        ('other', OperatorOverrideInput(reason='  why  ', confirmed=True), 'why'),
        ('other', OperatorOverrideInput(reason='a\tb', confirmed=True), 'a\tb'),
    ],
)
def test_overriding_reason_returns_the_trimmed_reason_or_none_for_the_operator(owner, override, expected):
    assert overriding_reason(owner, OPERATOR_PRINCIPAL, override) == expected


@pytest.mark.parametrize(
    'override',
    [
        None,
        OperatorOverrideInput(reason='why', confirmed=False),
        OperatorOverrideInput(reason='', confirmed=True),
        OperatorOverrideInput(reason=' \n', confirmed=True),
    ],
)
@pytest.mark.parametrize('owner', ['other', 'unassigned', '', 'Operator', ' operator'])
def test_overriding_reason_refuses_every_other_owner_without_a_valid_override(owner, override):
    with pytest.raises(OverrideRequired) as raised:
        overriding_reason(owner, OPERATOR_PRINCIPAL, override)
    assert raised.value.reason.value == 'OVERRIDE_REQUIRED'
    assert not getattr(raised.value, 'metadata', None)


def test_the_override_input_repr_hides_the_reason():
    assert 'secret-why' not in repr(OperatorOverrideInput(reason='secret-why', confirmed=True))
