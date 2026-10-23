"""The /ui/v1 operator writes on real Postgres, over real HTTP: what the SQLite route suite cannot prove (tj-grna9p.86).

The design is tj-grna9p.86's 2026-10-14 architect ruling (rule order 422, 404, 409 OVERRIDE_REQUIRED, the operator's
own dataset ignoring any override, the write and exactly one override-log row in one transaction, PATCH 200 with the
DatasetSummary, DELETE 204). The SQLite tier, data/store/tests/test_ui_dataset_writes_route.py, pins the rules; this
file proves the parts only Postgres and the running image can:
  * the probe audit_tables_exist answers True on a migrated database, so the audit rows really are written;
  * the override_action enum, SensitiveString owner and gen_random_uuid()/now() defaults round-trip;
  * the bars go by ON DELETE CASCADE and the deletion record counts them;
  * the 409 body names no owner (the owner value is distinctive, so its absence is evidence).
That the owner reaches no data_store LOG line is checked from outside this suite, on the container log, because
test_client cannot read it (see the bead's verdict note).

Audit rows are append-only for the SERVICE. This suite deletes the rows it caused, by its own dataset ids, in
teardown, as conftest's entry_registry deletes its entries: test infrastructure, not a code path of the store.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from common.enums.data_stock import ExpiryType, Granularity, UpdateType
from common.errors.vocabulary import Reason
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.database.crud.dataset_override import audit_tables_exist
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE, WriteAuth
from tests.system.problem_json import assert_problem


# Its scratch database fixture, reused rather than copied: a database this test creates, migrates and drops.
scratch_db = test_usage_record.scratch_db

REVISION = 'd7e2a9c35b18'
PARENT_REVISION = 'b3d9e1a47c62'


OVERRIDE = DatasetOverrideLog.__table__
DELETION = DatasetDeletion.__table__
GOOD = {'reason': '  system override  ', 'confirmed': True}
EXPIRY = '2031-01-02T00:00:00Z'


def _expiry_path(dataset_id: UUID) -> str:
    return f'/ui/v1/datasets/{dataset_id}/expiry'


def _path(dataset_id: UUID) -> str:
    return f'/ui/v1/datasets/{dataset_id}'


@pytest.fixture
def foreign_owner(run_identity) -> str:
    return run_identity.alt_owner('zq-distinct-owner')


@pytest.fixture
def audited(pg_engine: Engine) -> Iterator[list[UUID]]:
    """The dataset ids whose audit rows this test causes; deleted in teardown so the stack stays clean."""
    ids: list[UUID] = []
    yield ids
    if ids:
        with pg_engine.begin() as conn:
            conn.execute(sa.delete(OVERRIDE).where(OVERRIDE.c.dataset_id.in_(ids)))
            conn.execute(sa.delete(DELETION).where(DELETION.c.dataset_id.in_(ids)))


@pytest.fixture
def subscription(
    insert_entry: Callable[..., sa.Row], bar_values, pg_engine: Engine, own_symbol: str, foreign_owner: str, audited
) -> Callable[..., sa.Row]:
    """A DAILY subscription with three bars, owned by `owner` (another owner by default)."""

    def _make(suffix: str = '', owner: str | None = None) -> sa.Row:
        row = insert_entry(
            asset_symbol=f'{own_symbol}{suffix}',
            owner=foreign_owner if owner is None else owner,
            granularity=Granularity.ONE_DAY,
            update_type=UpdateType.DAILY.value,
            expiry_type=ExpiryType.ROLLING.value,
        )
        with pg_engine.begin() as conn:
            for day in range(3):
                conn.execute(sa.insert(BAR_TABLE).values(bar_values(row, timestamp=row.start + timedelta(days=day))))
        audited.append(row.id)
        return row

    return _make


def _rows(pg_engine: Engine, table: sa.Table, dataset_id: UUID) -> list[Any]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(table).where(table.c.dataset_id == dataset_id)))


def _stored(pg_engine: Engine, entry: sa.Row) -> sa.Row | None:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(ENTRY_TABLE).where(ENTRY_TABLE.c.id == entry.id)).one_or_none()


def _expiry(pg_engine: Engine, entry: sa.Row) -> Any:
    stored = _stored(pg_engine, entry)
    assert stored is not None, f'dataset {entry.id} is gone'
    return stored.expiry


def test_an_overriding_patch_sets_the_deletion_date_only_and_writes_one_trimmed_override_row(
    subscription, pg_engine, data_store
):
    """Decision tj-hhy3dk: the expiry is the deletion date; the subscription stays ACTIVE and collecting."""
    row = subscription()

    response = data_store.request(
        'PATCH', _expiry_path(row.id), json={'expiry': EXPIRY, 'override': GOOD}, auth=WriteAuth.RIGHT
    )

    assert response.status_code == 200, data_store.describe(response)
    body = response.json()
    assert (body['id'], body['expiry'], body['state'], body['collecting']) == (
        str(row.id),
        EXPIRY,
        'DATASET_STATE_ACTIVE',
        True,
    )
    assert _stored(pg_engine, row).collect is True, 'the expiry write changed collect'
    assert _expiry(pg_engine, row) == datetime(2031, 1, 2, tzinfo=UTC)
    (logged,) = _rows(pg_engine, OVERRIDE, row.id)
    assert (logged.actor, logged.owner, logged.action, logged.reason) == (
        OPERATOR_PRINCIPAL,
        row.owner,
        'SET_EXPIRY',
        'system override',
    )
    assert logged.at is not None and logged.id is not None

    cleared = data_store.request('PATCH', _expiry_path(row.id), json={'override': GOOD}, auth=WriteAuth.RIGHT)
    assert cleared.status_code == 200, data_store.describe(cleared)
    assert cleared.json()['state'] == 'DATASET_STATE_ACTIVE'
    assert _expiry(pg_engine, row) is None
    assert len(_rows(pg_engine, OVERRIDE, row.id)) == 2


@pytest.mark.parametrize('override', [None, {'reason': 'x', 'confirmed': False}, {'reason': '   ', 'confirmed': True}])
def test_another_owners_dataset_without_a_valid_override_is_409_naming_no_owner(
    subscription, pg_engine, data_store, override, foreign_owner
):
    row = subscription()
    body = {'expiry': EXPIRY} if override is None else {'expiry': EXPIRY, 'override': override}

    for response in (
        data_store.request('PATCH', _expiry_path(row.id), json=body, auth=WriteAuth.RIGHT),
        data_store.request(
            'DELETE', _path(row.id), json=None if override is None else {'override': override}, auth=WriteAuth.RIGHT
        ),
    ):
        problem = assert_problem(
            response, status=409, reason=Reason.OVERRIDE_REQUIRED.value, title='Conflict', describe=data_store.describe
        )
        assert foreign_owner not in response.text
        assert 'metadata' not in problem
    assert _expiry(pg_engine, row) is None
    assert _rows(pg_engine, OVERRIDE, row.id) == [] and _rows(pg_engine, DELETION, row.id) == []
    assert len(_rows(pg_engine, BAR_TABLE, row.id)) == 3


def test_an_overriding_delete_cascades_and_writes_one_deletion_and_one_override_row(
    subscription, pg_engine, data_store
):
    row = subscription()

    response = data_store.request('DELETE', _path(row.id), json={'override': GOOD}, auth=WriteAuth.RIGHT)

    assert response.status_code == 204, data_store.describe(response)
    assert response.content == b''
    assert _stored(pg_engine, row) is None and _rows(pg_engine, BAR_TABLE, row.id) == []
    (logged,) = _rows(pg_engine, OVERRIDE, row.id)
    assert (logged.action, logged.reason, logged.owner) == ('DELETE', 'system override', row.owner)
    (deletion,) = _rows(pg_engine, DELETION, row.id)
    assert (
        deletion.owner,
        deletion.asset_symbol,
        deletion.granularity,
        deletion.update_type,
        deletion.range_start,
        deletion.range_end,
        deletion.bars_deleted,
        deletion.cause,
        deletion.actor,
    ) == (
        row.owner,
        row.asset_symbol,
        'ONE_DAY',
        'DAILY',
        row.start,
        row.end,
        3,
        DatasetDeletion.CAUSE_OPERATOR,
        OPERATOR_PRINCIPAL,
    )
    assert data_store.get(_path(row.id)).status_code == 404


def test_the_operators_own_dataset_ignores_the_override_and_an_empty_delete_body_is_accepted(
    subscription, pg_engine, data_store
):
    row = subscription('OP', owner=OPERATOR_PRINCIPAL)

    patched = data_store.request(
        'PATCH',
        _expiry_path(row.id),
        json={'expiry': EXPIRY, 'override': {'reason': '', 'confirmed': False}},
        auth=WriteAuth.RIGHT,
    )
    assert patched.status_code == 200, data_store.describe(patched)
    deleted = data_store.request('DELETE', _path(row.id), auth=WriteAuth.RIGHT)
    assert deleted.status_code == 204, data_store.describe(deleted)

    assert _rows(pg_engine, OVERRIDE, row.id) == []
    (deletion,) = _rows(pg_engine, DELETION, row.id)
    assert (deletion.owner, deletion.bars_deleted) == (OPERATOR_PRINCIPAL, 3)


def test_unknown_id_is_404_and_a_type_change_is_422_and_neither_writes(subscription, pg_engine, data_store):
    row = subscription()
    unknown = UUID('00000000-0000-4000-8000-0000000086a1')
    for response in (
        data_store.request('PATCH', _expiry_path(unknown), json={'expiry': EXPIRY}, auth=WriteAuth.RIGHT),
        data_store.request('DELETE', _path(unknown), auth=WriteAuth.RIGHT),
    ):
        assert_problem(
            response, status=404, reason=Reason.NOT_FOUND.value, title='Not Found', describe=data_store.describe
        )
    for body in ({'expiryType': 'EXPIRY_TYPE_BULK', 'override': GOOD}, {'updateType': 'UPDATE_TYPE_STATIC'}):
        assert_problem(
            data_store.request('PATCH', _expiry_path(row.id), json=body, auth=WriteAuth.RIGHT),
            status=422,
            reason=Reason.INVALID_REQUEST.value,
            title='Unprocessable Entity',
            describe=data_store.describe,
        )
    assert _rows(pg_engine, OVERRIDE, row.id) == [] and _rows(pg_engine, DELETION, unknown) == []


# ---------------------------------------------------------------------------------------------
# The revision and the probe, on a scratch database (the tj-vhboky.62 exception, as test_usage_record.py).


def test_upgrade_downgrade_upgrade_adds_then_drops_exactly_the_two_tables_and_the_type(scratch_db):
    scratch_db.must('upgrade', PARENT_REVISION)
    parent_schema = scratch_db.schema()
    assert not scratch_db.enum_exists('override_action')

    scratch_db.must('upgrade', REVISION)
    head_schema = scratch_db.schema()
    assert {row[0] for row in head_schema['columns']} >= {OVERRIDE.name, DELETION.name}
    assert scratch_db.enum_exists('override_action')
    for kind in ('columns', 'constraints', 'indexes', 'enums'):
        assert set(parent_schema[kind]) <= set(head_schema[kind]), f'{kind} of the parent changed'

    scratch_db.must('downgrade', PARENT_REVISION)
    assert scratch_db.schema() == parent_schema
    assert not scratch_db.enum_exists('override_action')

    scratch_db.must('upgrade', REVISION)
    assert scratch_db.schema() == head_schema


@pytest.mark.asyncio
async def test_the_probe_is_false_before_the_revision_and_true_after_it(scratch_db):
    """audit_tables_exist's to_regclass on Postgres, through asyncpg: the unmigrated path's switch, both ways."""
    engine = create_async_engine(scratch_db.url.set(drivername='postgresql+asyncpg'), poolclass=NullPool)
    try:
        scratch_db.must('upgrade', PARENT_REVISION)
        async with AsyncSession(engine) as db:
            assert await audit_tables_exist(db) is False
        scratch_db.must('upgrade', REVISION)
        async with AsyncSession(engine) as db:
            assert await audit_tables_exist(db) is True
    finally:
        await engine.dispose()


@pytest.mark.parametrize('auth', [WriteAuth.ABSENT, WriteAuth.WRONG])
def test_both_writes_refuse_without_the_instance_secret(subscription, pg_engine, data_store, auth):
    row = subscription()
    assert (
        data_store.request(
            'PATCH', _expiry_path(row.id), json={'expiry': EXPIRY, 'override': GOOD}, auth=auth
        ).status_code
        == 401
    )
    assert data_store.request('DELETE', _path(row.id), json={'override': GOOD}, auth=auth).status_code == 401
    assert len(_rows(pg_engine, BAR_TABLE, row.id)) == 3 and _rows(pg_engine, OVERRIDE, row.id) == []


# ---------------------------------------------------------------------------------------------
# PATCH /ui/v1/datasets/{id}/collecting on Postgres (tj-grna9p.175, the native label tj-grna9p.183): the override-log
# row's action is the NATIVE override_action label SET_COLLECTING, which no SQLite test can store or read back.


def _collecting_path(dataset_id: UUID) -> str:
    return f'/ui/v1/datasets/{dataset_id}/collecting'


def test_an_overriding_stop_then_start_writes_two_set_collecting_rows_and_keeps_the_expiry(
    subscription, pg_engine, data_store
):
    row = subscription()
    dated = data_store.request(
        'PATCH', _expiry_path(row.id), json={'expiry': EXPIRY, 'override': GOOD}, auth=WriteAuth.RIGHT
    )
    assert dated.status_code == 200, data_store.describe(dated)

    stopped = data_store.request(
        'PATCH', _collecting_path(row.id), json={'collect': False, 'override': GOOD}, auth=WriteAuth.RIGHT
    )
    assert stopped.status_code == 200, data_store.describe(stopped)
    body = stopped.json()
    assert (body['state'], body['collecting'], body['expiry']) == ('DATASET_STATE_RETIRED', False, EXPIRY)
    assert body['freshness']['status'] == 'FRESHNESS_STATUS_RETIRED'
    assert _stored(pg_engine, row).collect is False

    started = data_store.request(
        'PATCH', _collecting_path(row.id), json={'collect': True, 'override': GOOD}, auth=WriteAuth.RIGHT
    )
    assert started.status_code == 200, data_store.describe(started)
    assert (started.json()['state'], started.json()['collecting']) == ('DATASET_STATE_ACTIVE', True)
    assert (_stored(pg_engine, row).collect, _expiry(pg_engine, row)) == (True, datetime(2031, 1, 2, tzinfo=UTC))

    logged_rows = _rows(pg_engine, OVERRIDE, row.id)
    assert sorted(logged.action for logged in logged_rows) == ['SET_COLLECTING', 'SET_COLLECTING', 'SET_EXPIRY']
    for logged in logged_rows:
        assert (logged.actor, logged.owner, logged.reason) == (OPERATOR_PRINCIPAL, row.owner, 'system override')


def test_the_collecting_refusals_on_postgres_write_nothing(
    subscription, insert_entry, own_symbol, pg_engine, data_store, foreign_owner
):
    row = subscription()
    static = insert_entry(asset_symbol=f'{own_symbol}ST', owner=foreign_owner)
    unknown = UUID('00000000-0000-4000-8000-0000000175a1')
    cases = [
        (row.id, {}, 422, Reason.INVALID_REQUEST, 'Unprocessable Entity'),
        (row.id, {'override': GOOD}, 422, Reason.INVALID_REQUEST, 'Unprocessable Entity'),
        (unknown, {'collect': False, 'override': GOOD}, 404, Reason.NOT_FOUND, 'Not Found'),
        (static.id, {'collect': False, 'override': GOOD}, 422, Reason.INVALID_REQUEST, 'Unprocessable Entity'),
        (row.id, {'collect': False}, 409, Reason.OVERRIDE_REQUIRED, 'Conflict'),
    ]
    for dataset_id, body, status, reason, title in cases:
        response = data_store.request('PATCH', _collecting_path(dataset_id), json=body, auth=WriteAuth.RIGHT)
        assert_problem(response, status=status, reason=reason.value, title=title, describe=data_store.describe)
        assert foreign_owner not in response.text
    assert _stored(pg_engine, row).collect is True
    assert _rows(pg_engine, OVERRIDE, row.id) == [] and _rows(pg_engine, OVERRIDE, static.id) == []


def test_the_operators_own_stop_writes_no_override_row(subscription, pg_engine, data_store):
    row = subscription('OC', owner=OPERATOR_PRINCIPAL)
    response = data_store.request(
        'PATCH',
        _collecting_path(row.id),
        json={'collect': False, 'override': {'reason': '', 'confirmed': False}},
        auth=WriteAuth.RIGHT,
    )
    assert response.status_code == 200, data_store.describe(response)
    assert _stored(pg_engine, row).collect is False
    assert _rows(pg_engine, OVERRIDE, row.id) == []
