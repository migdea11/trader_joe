"""dataset_override_log.new_value at its three writers (tj-7zjov1.176, P20b), over the real routes in SQLite.

THE DESIGN: ADR tj-26jt9w addendum 20 (parked default: nullable new_value, written for SET_COLLECTING (true or false)
and SET_EXPIRY (the new expiry), NULL for DELETE and for every earlier row) and the P20b bead (6): 'true' or 'false'
for SET_COLLECTING, the new expiry as ISO 8601 UTC for SET_EXPIRY, NULL for DELETE. A CLEARED expiry is written NULL:
the builder's reading, which the ruling does not name (the bead's 'the new expiry' has no value to write); it is
pinned here as built and put to the architect in the P20bv verdict note, because a NULL there cannot be told from a
row written before the column existed. append_override takes new_value as a REQUIRED keyword, so every caller states
it. A panel-owned dataset is reserved like any other owner's: an operator write needs the override (pin (7)).

TIER. test_ui_dataset_writes_route.py's world (the real app, handlers and crud over SQLite rows), reused as
test_ui_dataset_collecting_route.py reuses it. The rows read back are the log table's own.
"""

import inspect
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.dataset_override import OperatorOverrideInput, append_override
from data.store.app.database.crud.stock import store_dataset_entry as repo
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.tests.problem_body import problem
from data.store.tests.test_ui_dataset_writes_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    FOREIGN_OWNER,
    GOOD_OVERRIDE,
    HEADERS,
    World,
    aware_sqlite_datetimes,
    dataset_path,
    expiry_path,
)
from data.store.tests.test_ui_dataset_writes_route import client as _writes_client  # noqa: F401 (a fixture)
from data.store.tests.test_ui_dataset_writes_route import world as _writes_world  # noqa: F401 (a fixture)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]


@pytest.fixture
def world(_writes_world: World) -> World:  # noqa: F811 (the imported fixture, under its own name here)
    return _writes_world


@pytest.fixture
def client(_writes_client: httpx.AsyncClient) -> httpx.AsyncClient:  # noqa: F811 (as above)
    return _writes_client


def collecting_path(dataset_id: Any) -> str:
    return f'/ui/v1/datasets/{dataset_id}/collecting'


async def _only_row(world: World) -> DatasetOverrideLog:
    (row,) = await world.rows(DatasetOverrideLog)
    return row


# --------------------------------------------------------------------------------------------- SET_COLLECTING


@pytest.mark.parametrize(('collect', 'written'), [(False, 'false'), (True, 'true')])
async def test_a_collecting_override_writes_true_or_false(client, world, collect, written):
    response = await client.patch(
        collecting_path(world.foreign.id), headers=HEADERS, json={'collect': collect, 'override': GOOD_OVERRIDE}
    )

    assert response.status_code == 200, response.text
    row = await _only_row(world)
    assert (row.action, row.owner, row.new_value) == (OverrideAction.SET_COLLECTING, FOREIGN_OWNER, written)


async def test_stop_then_resume_writes_false_then_true(client, world):
    for collect in (False, True, True):
        response = await client.patch(
            collecting_path(world.foreign.id), headers=HEADERS, json={'collect': collect, 'override': GOOD_OVERRIDE}
        )
        assert response.status_code == 200, response.text

    rows = await world.rows(DatasetOverrideLog)
    assert sorted(row.new_value for row in rows) == ['false', 'true', 'true'], 'a repeat still records what it set'
    assert {row.action for row in rows} == {OverrideAction.SET_COLLECTING}


# --------------------------------------------------------------------------------------------- SET_EXPIRY


@pytest.mark.parametrize(
    ('sent', 'instant'),
    [
        ('2026-10-20T00:00:00Z', datetime(2026, 10, 20, tzinfo=UTC)),
        ('2026-10-20T05:30:00+05:30', datetime(2026, 10, 20, tzinfo=UTC)),
        ('2026-12-31T19:00:00-05:00', datetime(2027, 1, 1, tzinfo=UTC)),
        ('2026-10-20T00:00:00.250Z', datetime(2026, 10, 20, 0, 0, 0, 250000, tzinfo=UTC)),
    ],
    ids=['z', 'east-offset', 'west-offset-crosses-the-year', 'fraction'],
)
async def test_an_expiry_override_writes_the_new_expiry_as_iso_8601_utc(client, world, sent, instant):
    response = await client.patch(
        expiry_path(world.foreign.id), headers=HEADERS, json={'expiry': sent, 'override': GOOD_OVERRIDE}
    )

    assert response.status_code == 200, response.text
    row = await _only_row(world)
    assert row.action is OverrideAction.SET_EXPIRY
    assert row.new_value == instant.isoformat()
    parsed = datetime.fromisoformat(row.new_value)
    assert parsed == instant and parsed.utcoffset().total_seconds() == 0, 'not written in UTC'


async def test_the_crud_writes_an_offset_expiry_converted_to_utc(world):
    """The route hands the crud a UTC instant already; another caller may not, so the crud converts it itself."""
    sent = datetime(2026, 10, 20, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        await repo.operator_set_expiry(
            db, world.foreign.id, sent, OperatorOverrideInput(reason='convert', confirmed=True)
        )

    row = await _only_row(world)
    assert row.new_value == '2026-10-20T00:00:00+00:00'


async def test_a_cleared_expiry_writes_null(client, world):
    """AS BUILT, flagged to the architect: the cleared expiry is NULL, the same as a row from before the column."""
    response = await client.patch(expiry_path(world.foreign.id), headers=HEADERS, json={'override': GOOD_OVERRIDE})

    assert response.status_code == 200, response.text
    row = await _only_row(world)
    assert (row.action, row.new_value) == (OverrideAction.SET_EXPIRY, None)


# --------------------------------------------------------------------------------------------- DELETE


async def test_an_overriding_delete_writes_null(client, world):
    response = await client.request(
        'DELETE', dataset_path(world.foreign.id), headers=HEADERS, json={'override': GOOD_OVERRIDE}
    )

    assert response.status_code == 204, response.text
    row = await _only_row(world)
    assert (row.action, row.new_value) == (OverrideAction.DELETE, None)


# --------------------------------------------------------------------------------------------- the seam


async def test_append_override_takes_new_value_as_a_required_keyword():
    """No default, so a caller states it: a forgotten new_value is a TypeError, never a silent NULL."""
    parameter = inspect.signature(append_override).parameters['new_value']
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# --------------------------------------------------------------------------------------------- a panel-owned dataset


async def _hand_to_the_panel(world: World) -> None:
    async with AsyncSession(world.engine) as db:
        await db.execute(
            sa.update(StoreDatasetEntry).where(StoreDatasetEntry.id == world.foreign.id).values(owner=PANEL_OWNER)
        )
        await db.commit()


@pytest.mark.parametrize('override', [None, {'reason': '   ', 'confirmed': True}, {'reason': 'x', 'confirmed': False}])
async def test_an_operator_write_on_a_panel_dataset_without_a_reason_is_409(client, world, override):
    await _hand_to_the_panel(world)
    before = (await world.entry(world.foreign.id)).collect
    body: dict = {'collect': not before} | ({} if override is None else {'override': override})

    response = await client.patch(collecting_path(world.foreign.id), headers=HEADERS, json=body)

    problem(response, status=409, reason='OVERRIDE_REQUIRED')
    assert PANEL_OWNER not in response.text, 'the 409 names the owner'
    assert await world.rows(DatasetOverrideLog) == []
    stored = await world.entry(world.foreign.id)
    assert stored is not None and stored.collect is before


async def test_an_operator_stop_of_a_panel_dataset_with_a_reason_is_logged_against_the_panel(client, world):
    await _hand_to_the_panel(world)

    response = await client.patch(
        collecting_path(world.foreign.id), headers=HEADERS, json={'collect': False, 'override': GOOD_OVERRIDE}
    )

    assert response.status_code == 200, response.text
    row = await _only_row(world)
    assert (row.owner, row.action, row.reason, row.new_value) == (
        PANEL_OWNER,
        OverrideAction.SET_COLLECTING,
        'rebalance cleanup',
        'false',
    )
