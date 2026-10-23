"""PATCH /ui/v1/datasets/{id}/collecting, driven over real rows in SQLite (tj-grna9p.175, decision tj-hhy3dk (3)).

THE DESIGN is the bead and the architect's rulings in its notes, which win over its body. The checks, IN ORDER:
  0. the instance secret (401);
  1. a body that is not a SetCollectingRequest is 422 INVALID_REQUEST from ProtoBody; a body that parses but carries
     no collect (HasField false: '{}', or an override alone) is 422 INVALID_REQUEST 'collect is required' (the F1
     ruling on tj-grna9p.174: presence, a missing key is never read as false), before anything is looked up;
  2. an unknown id is 404 NOT_FOUND;
  3. a STATIC dataset is 422 INVALID_REQUEST: it has no collect intent (ruling (2): 404 precedes it, the row is
     needed to know the type);
  4. another owner's dataset without a confirmed override carrying a reason is 409 OVERRIDE_REQUIRED, no owner
     anywhere; the operator's own ignores any override;
  5. the write and, for an override, EXACTLY ONE override-log row (action SET_COLLECTING, reason trimmed) in one
     transaction; DatasetChanged strictly after the commit, RETIRED on a flip to false and RESUMED on a flip to
     true (tj-grna9p.189), none when collect was already the value sent, and none on a refused write;
  6. 200 with the updated DatasetSummary: collecting, state and freshness follow collect; the expiry is untouched.
Unmigrated (no audit tables): an overriding PATCH is 503 DATABASE_UNAVAILABLE before any write; the operator's own
goes through unrecorded.

WHAT TIER THIS IS. test_ui_dataset_writes_route.py's world and client (real app, handlers, repository SQL and mapping
over in-memory SQLite, audit_tables_exist stood in). Postgres, including the native override_action label
SET_COLLECTING (tj-grna9p.183), is tests/system/test_http_ui_dataset_writes.py's.

THE EMPTY BODY. A PATCH with no bytes at all never reaches the presence check: ProtoBody refuses it as not valid
protobuf JSON (an object is required), so it is a 422 INVALID_REQUEST with ProtoBody's detail, not 'collect is
required'. Both are 422 with nothing written, which is what the F1 ruling asks of an empty body; the detail is pinned
as it is, so a change to it is a decision and not an accident.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import event

from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.database.crud.dataset_override import OperatorOverrideInput
from data.store.app.database.crud.stock import store_dataset_entry as repo
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.tests.problem_body import problem
from data.store.tests.test_ui_dataset_writes_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    FOREIGN_OWNER,
    GOOD_OVERRIDE,
    HEADERS,
    INVALID_OVERRIDES,
    UNMIGRATED_DETAIL,
    World,
    _deleted_right_after_the_read,
    aware_sqlite_datetimes,
    dataset_path,
    set_collect,
)
from data.store.tests.test_ui_dataset_writes_route import client as _writes_client  # noqa: F401 (a fixture)
from data.store.tests.test_ui_dataset_writes_route import world as _writes_world  # noqa: F401 (a fixture)
from data.store.tests.test_ui_datasets_route import NOW, z
from routers.common.instance_secret import INSTANCE_SECRET_HEADER


pytestmark = pytest.mark.data_store

STATIC_REFUSAL = 'A STATIC dataset has no collect intent: collect applies to subscriptions only.'
REQUIRED = 'collect is required'
NOT_PROTO_JSON = 'The request body is not valid protobuf JSON for trader_joe.proto.ui.v1.SetCollectingRequest.'


@pytest.fixture
def world(_writes_world: World) -> World:  # noqa: F811 (the imported fixture, under its own name here)
    """test_ui_dataset_writes_route.py's world: the same rows, engine, commit count and event record."""
    return _writes_world


@pytest.fixture
def client(_writes_client: httpx.AsyncClient) -> httpx.AsyncClient:  # noqa: F811 (as above)
    return _writes_client


def collecting_path(dataset_id: Any) -> str:
    return f'/ui/v1/datasets/{dataset_id}/collecting'


async def patch(client: httpx.AsyncClient, dataset_id: Any, body: Any, headers: dict | None = None) -> httpx.Response:
    kwargs: dict = {}
    if isinstance(body, str | bytes):
        kwargs['content'] = body
    elif body is not None:
        kwargs['json'] = body
    return await client.patch(collecting_path(dataset_id), headers=HEADERS if headers is None else headers, **kwargs)


async def assert_untouched(world: World, dataset_id: uuid.UUID, collect: bool | None) -> None:
    """A refused write changed nothing: the entry and its collect stand, no audit row, no event, no commit."""
    stored = await world.entry(dataset_id)
    assert stored is not None, 'a refused write deleted the dataset'
    assert stored.collect is collect, f'a refused write changed collect to {stored.collect}'
    assert await world.audit_rows() == ([], [])
    assert world.events == [], 'a write that did not happen was announced'
    assert world.commits == 0, 'a refused write committed'


def _with_override(body: dict, override: Any) -> dict:
    return body if override is None else {**body, 'override': override}


# ------------------------------------------------------------------------------------------------- 0: the secret


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{}, {INSTANCE_SECRET_HEADER: 'wrong'}], ids=['absent', 'wrong'])
@pytest.mark.parametrize(
    ('name', 'body'),
    [
        pytest.param('foreign', {'collect': False, 'override': GOOD_OVERRIDE}, id='valid'),
        pytest.param('foreign', '{bad', id='malformed'),
        pytest.param('foreign', {}, id='no-collect'),
        pytest.param('static', {'collect': False}, id='static'),
        pytest.param(None, {'collect': False}, id='unknown-id'),
    ],
)
async def test_the_secret_is_checked_before_anything_else(client, world, headers, name, body):
    dataset_id = uuid.uuid4() if name is None else getattr(world, name).id
    assert (await patch(client, dataset_id, body, headers)).status_code == 401
    await assert_untouched(world, world.foreign.id, True)


# ------------------------------------------------------------------------------------------------- 1: the body


@pytest.mark.asyncio
async def test_a_patch_with_no_body_is_422_and_never_stops_collecting(client, world):
    """No bytes at all: refused by ProtoBody (see THE EMPTY BODY), never read as collect false."""
    response = await client.patch(collecting_path(world.dataset.id), headers=HEADERS)
    body = problem(response, status=422, reason='INVALID_REQUEST')
    assert body['detail'] == NOT_PROTO_JSON
    await assert_untouched(world, world.dataset.id, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'body',
    [
        pytest.param({}, id='empty-object'),
        pytest.param({'override': GOOD_OVERRIDE}, id='override-only'),
        pytest.param({'override': {'reason': 'x', 'confirmed': False}}, id='unconfirmed-override-only'),
        pytest.param('{"collect": null}', id='explicit-null'),
    ],
)
@pytest.mark.parametrize('name', ['dataset', 'foreign', 'stream'])
async def test_a_body_without_collect_is_422_collect_is_required_and_writes_nothing(client, world, name, body):
    row = getattr(world, name)
    response = await patch(client, row.id, body)
    refused = problem(response, status=422, reason='INVALID_REQUEST')
    assert refused['detail'] == REQUIRED
    assert FOREIGN_OWNER not in response.text
    await assert_untouched(world, row.id, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'body',
    [
        pytest.param('{not json', id='malformed-json'),
        pytest.param('[]', id='json-array'),
        pytest.param('null', id='json-null'),
        pytest.param({'collect': 'yes'}, id='non-bool'),
        pytest.param({'collect': False, 'expiry': '2026-10-20T00:00:00Z'}, id='unknown-field'),
        pytest.param({'collect': False, 'override': {'reason': 'x', 'confirmed': True, 'owner': 'me'}}, id='nested'),
    ],
)
async def test_a_bad_body_is_422_and_writes_nothing(client, world, body):
    problem(await patch(client, world.foreign.id, body), status=422, reason='INVALID_REQUEST')
    await assert_untouched(world, world.foreign.id, True)


@pytest.mark.asyncio
async def test_the_missing_collect_is_reported_before_the_404_the_static_422_and_the_409(client, world):
    for dataset_id in (uuid.uuid4(), world.static.id, world.open_static.id, world.foreign.id):
        refused = problem(await patch(client, dataset_id, {}), status=422, reason='INVALID_REQUEST')
        assert refused['detail'] == REQUIRED, dataset_id
    assert world.events == [] and world.commits == 0


# ------------------------------------------------------------------------------------------------- 2: 404


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'collect': False}, {'collect': True, 'override': GOOD_OVERRIDE}])
async def test_an_unknown_id_is_404_even_without_an_override(client, world, body):
    problem(await patch(client, uuid.uuid4(), body), status=404, reason='NOT_FOUND')
    assert await world.audit_rows() == ([], [])
    assert world.events == []


@pytest.mark.asyncio
async def test_a_malformed_id_is_422(client, world):
    problem(await patch(client, 'not-a-uuid', {'collect': False}), status=422)


# ------------------------------------------------------------------------------------------------- 3: STATIC


@pytest.mark.asyncio
@pytest.mark.parametrize('collect', [True, False])
@pytest.mark.parametrize(
    ('name', 'override'),
    [
        pytest.param('static', None, id='operator-static'),
        pytest.param('open_static', None, id='foreign-static-no-override'),
        pytest.param('open_static', GOOD_OVERRIDE, id='foreign-static-with-override'),
    ],
)
async def test_a_static_dataset_is_422_before_the_override_check_and_writes_nothing(
    client, world, name, override, collect
):
    """STATIC has no intent: refused with its text, and before the 409 (another owner's STATIC is a 422, not a 409)."""
    row = getattr(world, name)
    response = await patch(client, row.id, _with_override({'collect': collect}, override))
    refused = problem(response, status=422, reason='INVALID_REQUEST')
    assert refused['detail'] == STATIC_REFUSAL
    assert FOREIGN_OWNER not in response.text
    await assert_untouched(world, row.id, None)


# ------------------------------------------------------------------------------------------------- 4: 409 / operator


@pytest.mark.asyncio
@pytest.mark.parametrize('override', INVALID_OVERRIDES)
@pytest.mark.parametrize('name', ['foreign', 'stream'])
async def test_another_owners_subscription_without_a_valid_override_is_409(client, world, name, override, caplog):
    caplog.set_level(logging.DEBUG)
    row = getattr(world, name)
    response = await patch(client, row.id, _with_override({'collect': False}, override))

    body = problem(response, status=409, reason='OVERRIDE_REQUIRED')
    assert FOREIGN_OWNER not in response.text, f'the 409 names the owner: {response.text}'
    assert FOREIGN_OWNER not in caplog.text, 'the owner reached a log line'
    assert 'metadata' not in body
    await assert_untouched(world, row.id, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'override',
    [None, {'reason': '', 'confirmed': False}, {'reason': 'x', 'confirmed': False}, GOOD_OVERRIDE],
    ids=['none', 'blank-unconfirmed', 'unconfirmed', 'valid'],
)
async def test_the_operators_own_subscription_ignores_any_override_and_writes_no_row(client, world, override):
    assert world.dataset.owner == OPERATOR_PRINCIPAL
    response = await patch(client, world.dataset.id, _with_override({'collect': False}, override))
    assert response.status_code == 200, response.text
    assert await world.rows(DatasetOverrideLog) == [], 'an override row was written for the operator'
    assert (await world.entry(world.dataset.id)).collect is False


# ------------------------------------------------------------------------------------------------- 5: the write


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['foreign', 'stream'])
async def test_an_overriding_patch_writes_exactly_one_trimmed_set_collecting_row(client, world, name):
    row = getattr(world, name)
    response = await patch(client, row.id, {'collect': False, 'override': GOOD_OVERRIDE})

    assert response.status_code == 200, response.text
    overrides, deletions = await world.audit_rows()
    assert deletions == []
    (logged,) = overrides
    assert (logged.actor, logged.dataset_id, logged.owner, logged.action, logged.reason) == (
        OPERATOR_PRINCIPAL,
        row.id,
        FOREIGN_OWNER,
        OverrideAction.SET_COLLECTING,
        'rebalance cleanup',
    )
    assert logged.at is not None
    assert (await world.entry(row.id)).collect is False


@pytest.mark.asyncio
async def test_each_overriding_write_appends_its_own_row_and_a_repeat_is_idempotent_on_the_entry(client, world):
    first = await patch(client, world.foreign.id, {'collect': False, 'override': {'reason': 'one', 'confirmed': True}})
    again = await patch(client, world.foreign.id, {'collect': False, 'override': {'reason': 'two', 'confirmed': True}})

    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json() == again.json(), 'a repeat answered a different summary'
    assert (await world.entry(world.foreign.id)).collect is False
    assert sorted(row.reason for row in await world.rows(DatasetOverrideLog)) == ['one', 'two']
    # The first flips collect and is RETIRED; the repeat changes nothing a client shows, so it publishes nothing
    # (tj-grna9p.189), though it still commits its own override row.
    assert world.events == [(DatasetChanged(world.foreign.id, DatasetChange.RETIRED), 1)]
    assert world.commits == 2


@pytest.mark.asyncio
async def test_a_failing_override_insert_rolls_the_write_back_and_announces_nothing(client, world, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError('override insert failed')

    monkeypatch.setattr(repo, 'append_override', broken)
    with pytest.raises(RuntimeError):
        await patch(client, world.foreign.id, {'collect': False, 'override': GOOD_OVERRIDE})
    await assert_untouched(world, world.foreign.id, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('name', 'body'),
    [
        pytest.param('foreign', {'collect': False, 'override': GOOD_OVERRIDE}, id='overriding'),
        pytest.param('dataset', {'collect': False}, id='operator-owned'),
    ],
)
async def test_a_patch_whose_dataset_goes_before_its_update_is_404_with_no_row_and_no_event(
    client, world, monkeypatch, name, body
):
    dataset_id = getattr(world, name).id
    _deleted_right_after_the_read(world, monkeypatch, dataset_id)

    response = await patch(client, dataset_id, body)

    problem(response, status=404, reason='NOT_FOUND')
    overrides, _ = await world.audit_rows()
    assert (overrides, world.events, world.commits) == ([], [], 0)
    assert FOREIGN_OWNER not in response.text


@pytest.mark.asyncio
async def test_the_crud_touches_only_collect_and_updated_at(world):
    """Nothing else on the entry moves, the expiry included (the expiry is the deletion date, decision tj-hhy3dk)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    statements: list[str] = []
    event.listen(world.engine.sync_engine, 'before_cursor_execute', lambda *a: statements.append(a[2]))
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        await repo.operator_set_collecting(
            db, world.foreign.id, False, OperatorOverrideInput(reason='why', confirmed=True)
        )
    (update_sql,) = [s for s in statements if s.lstrip().upper().startswith('UPDATE')]
    set_clause = update_sql.split(' SET ', 1)[1].split(' WHERE ', 1)[0]
    assert sorted(part.split('=')[0].strip() for part in set_clause.split(',')) == ['collect', 'updated_at']


# ------------------------------------------------------------------------------------------------- events


# tj-grna9p.189 (the tj-hhy3dk addendum): a flip to false is RETIRED, a flip to true is RESUMED, the vocabulary's own
# meaning (events.proto); a PATCH that leaves collect as it was publishes NOTHING; every refusal publishes nothing
# (assert_untouched, in each refusal case above). world.events pairs each event with the commit count when it was
# published, so 1 is "after the one commit", never before it.


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['dataset', 'foreign', 'stream'])
async def test_stopping_a_collecting_subscription_publishes_retired_strictly_after_its_commit(client, world, name):
    row = getattr(world, name)
    override = {} if row.owner == OPERATOR_PRINCIPAL else {'override': GOOD_OVERRIDE}
    assert (await patch(client, row.id, {'collect': False, **override})).status_code == 200
    assert world.events == [(DatasetChanged(row.id, DatasetChange.RETIRED), 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['dataset', 'foreign', 'stream'])
async def test_starting_a_stopped_subscription_publishes_resumed_strictly_after_its_commit(client, world, name):
    row = getattr(world, name)
    await set_collect(world, row.id, False)
    override = {} if row.owner == OPERATOR_PRINCIPAL else {'override': GOOD_OVERRIDE}
    assert (await patch(client, row.id, {'collect': True, **override})).status_code == 200
    assert world.events == [(DatasetChanged(row.id, DatasetChange.RESUMED), 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize('collect', [True, False], ids=['start-a-collecting-one', 'stop-a-stopped-one'])
@pytest.mark.parametrize('name', ['dataset', 'foreign'])
async def test_a_patch_that_changes_nothing_publishes_nothing_and_still_answers_200(client, world, name, collect):
    row = getattr(world, name)
    if not collect:
        await set_collect(world, row.id, False)
    override = {} if row.owner == OPERATOR_PRINCIPAL else {'override': GOOD_OVERRIDE}

    response = await patch(client, row.id, {'collect': collect, **override})

    assert response.status_code == 200, response.text
    assert response.json()['collecting'] is collect
    assert world.events == [], f'a PATCH that left collect {collect} published {world.events}'
    assert (await world.entry(row.id)).collect is collect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('stored', 'sent', 'returned'),
    [(True, False, False), (False, True, True), (True, True, None), (False, False, None)],
    ids=['stop', 'start', 'no-op-collecting', 'no-op-stopped'],
)
async def test_the_crud_returns_the_new_collect_only_when_it_flipped(world, stored, sent, returned):
    """operator_set_collecting's return is what the route publishes from: the new value on a flip, else None."""
    from sqlalchemy.ext.asyncio import AsyncSession

    await set_collect(world, world.dataset.id, stored)
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await repo.operator_set_collecting(db, world.dataset.id, sent, None) is returned
    assert (await world.entry(world.dataset.id)).collect is sent


# The re-gate of tj-grna9p.189 (fix a16d21a). The original defect read existing.collect AFTER the ORM UPDATE, whose
# session synchronisation had already written the new value onto the loaded object, so no flip was ever reported. The
# fix reads the stored collect from the DATABASE, under a row lock, BEFORE the UPDATE, in the same transaction. Two
# things the cases above do not tell apart: the read being FOR UPDATE (SQLite drops the clause from its SQL, so only
# the statement object shows it), and the read coming from the row rather than from the session's identity map (a
# fix that captured existing.collect before the UPDATE passes every case above and reports a stale value whenever
# the session already held the entry).


@pytest.mark.asyncio
async def test_the_crud_reads_collect_for_update_by_id_before_its_update_in_one_transaction(world):
    from sqlalchemy import Select, Update
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import AsyncSession

    statements: list[Any] = []
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        event.listen(db.sync_session, 'do_orm_execute', lambda state: statements.append(state.statement))
        transactions: list[Any] = []
        event.listen(db.sync_session, 'after_begin', lambda _s, transaction, _c: transactions.append(transaction))
        assert await repo.operator_set_collecting(db, world.dataset.id, False, None) is False

    locks = [i for i, s in enumerate(statements) if isinstance(s, Select) and s._for_update_arg is not None]
    updates = [i for i, s in enumerate(statements) if isinstance(s, Update)]
    assert len(locks) == 1, f'expected one locked read, saw {[str(s) for s in statements]}'
    assert len(updates) == 1, f'expected one UPDATE, saw {[str(s) for s in statements]}'
    assert locks[0] < updates[0], 'the stored collect is read AFTER the UPDATE: the flip is computed from the new value'
    compiled = statements[locks[0]].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith('SELECT store_dataset_entry.collect \nFROM'), (
        f'the locked read reads more than collect: {sql}'
    )
    assert sql.rstrip().endswith('FOR UPDATE'), f'the locked read is not FOR UPDATE: {sql}'
    assert 'NOWAIT' not in sql and 'SKIP LOCKED' not in sql, f'a racing stop must wait, never skip: {sql}'
    assert list(compiled.params.values()) == [world.dataset.id], f'the lock is not keyed on the id: {compiled.params}'
    assert len(transactions) == 1, 'the locked read and the UPDATE are not one transaction'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('held', 'stored', 'sent', 'returned'),
    [(True, False, True, True), (False, True, False, False), (True, False, False, None), (False, True, True, None)],
    ids=[
        'held-collecting-stored-stopped-start',
        'held-stopped-stored-collecting-stop',
        'stale-no-op-stop',
        'stale-no-op-start',
    ],
)
async def test_the_flip_is_judged_from_the_stored_row_not_the_sessions_loaded_object(
    world, held, stored, sent, returned
):
    """The session already holds the entry with a value the row no longer has: the row decides, never the object.

    The UPDATE below is Core on the table, so it moves the row and leaves the loaded object's collect as it was, the
    state a session is in when its entry was loaded before another write committed. _get_entry_or_raise returns that
    object as it stands (no populate_existing), so its collect is the stale one.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry

    await set_collect(world, world.dataset.id, held)
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        loaded = (
            await db.execute(select(StoreDatasetEntry).where(StoreDatasetEntry.id == world.dataset.id))
        ).scalar_one()
        await db.execute(
            StoreDatasetEntry.__table__.update().where(StoreDatasetEntry.id == world.dataset.id).values(collect=stored)
        )
        assert loaded.collect is held, 'the precondition failed: the loaded object was refreshed'

        assert await repo.operator_set_collecting(db, world.dataset.id, sent, None) is returned
    assert (await world.entry(world.dataset.id)).collect is sent


# ------------------------------------------------------------------------------------------------- 6: the answer


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['dataset', 'foreign', 'stream'])
async def test_stop_then_start_round_trips_state_collecting_and_freshness_and_keeps_the_expiry(client, world, name):
    row = getattr(world, name)
    override = {} if row.owner == OPERATOR_PRINCIPAL else {'override': GOOD_OVERRIDE}
    expiry = NOW + timedelta(days=9)
    await _set_expiry(world, row.id, expiry)

    stopped = await patch(client, row.id, {'collect': False, **override})
    assert stopped.status_code == 200, stopped.text
    summary = stopped.json()
    assert (summary['id'], summary['state'], summary.get('collecting', False)) == (
        str(row.id),
        'DATASET_STATE_RETIRED',
        False,
    )
    assert 'collecting' in summary, 'collecting false is not set on the wire: the field has presence'
    assert summary['freshness']['status'] == 'FRESHNESS_STATUS_RETIRED'
    assert z(summary['expiry']) == expiry, 'stopping changed the deletion date'
    assert summary == (await client.get(dataset_path(row.id))).json(), 'the answer is not the detail read'

    started = await patch(client, row.id, {'collect': True, **override})
    assert started.status_code == 200, started.text
    summary = started.json()
    assert (summary['state'], summary['collecting']) == ('DATASET_STATE_ACTIVE', True)
    assert summary['freshness']['status'] != 'FRESHNESS_STATUS_RETIRED'
    assert z(summary['expiry']) == expiry, 'starting changed the deletion date'
    stored = await world.entry(row.id)
    assert (stored.collect, stored.expiry, stored.update_type) == (True, expiry, row.update_type.value)
    assert world.events == [
        (DatasetChanged(row.id, DatasetChange.RETIRED), 1),
        (DatasetChanged(row.id, DatasetChange.RESUMED), 2),
    ], 'a stop and a start are RETIRED then RESUMED, each after its own commit'


async def _set_expiry(world: World, dataset_id: uuid.UUID, expiry: Any) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry

    async with AsyncSession(world.engine) as db:
        await db.execute(
            StoreDatasetEntry.__table__.update().where(StoreDatasetEntry.id == dataset_id).values(expiry=expiry)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_starting_a_subscription_that_was_stopped_elsewhere_resumes_it(client, world):
    await set_collect(world, world.dataset.id, False)
    response = await patch(client, world.dataset.id, {'collect': True})
    assert response.status_code == 200, response.text
    assert (response.json()['state'], response.json()['collecting']) == ('DATASET_STATE_ACTIVE', True)


# ------------------------------------------------------------------------------------------------- unmigrated


@pytest.mark.asyncio
async def test_an_unmigrated_database_refuses_an_overriding_patch_before_any_write(client, world):
    world.migrated = False
    statements: list[str] = []
    listener = lambda _c, _cur, statement, *_rest: statements.append(statement)  # noqa: E731
    event.listen(world.engine.sync_engine, 'before_cursor_execute', listener)
    try:
        response = await patch(client, world.foreign.id, {'collect': False, 'override': GOOD_OVERRIDE})
    finally:
        event.remove(world.engine.sync_engine, 'before_cursor_execute', listener)

    refused = problem(response, status=503, reason='DATABASE_UNAVAILABLE')
    assert refused['detail'] == UNMIGRATED_DETAIL
    assert FOREIGN_OWNER not in response.text
    assert statements, 'the listener saw nothing'
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {'UPDATE', 'DELETE', 'INSERT'}]
    assert writes == [], f'a refused write issued {writes}'
    await assert_untouched(world, world.foreign.id, True)


@pytest.mark.asyncio
async def test_an_unmigrated_database_still_answers_the_earlier_checks_first(client, world):
    world.migrated = False
    assert problem(await patch(client, world.foreign.id, {}), status=422)['detail'] == REQUIRED
    problem(await patch(client, uuid.uuid4(), {'collect': False, 'override': GOOD_OVERRIDE}), status=404)
    problem(await patch(client, world.open_static.id, {'collect': False, 'override': GOOD_OVERRIDE}), status=422)
    problem(await patch(client, world.foreign.id, {'collect': False}), status=409, reason='OVERRIDE_REQUIRED')
    await assert_untouched(world, world.foreign.id, True)


@pytest.mark.asyncio
async def test_an_unmigrated_database_lets_an_operator_owned_patch_through_unrecorded(client, world):
    world.migrated = False
    response = await patch(client, world.dataset.id, {'collect': False})
    assert response.status_code == 200, response.text
    assert (await world.entry(world.dataset.id)).collect is False
    assert await world.audit_rows() == ([], [])
    assert len(await world.rows(StockMarketActivity)) == 6
