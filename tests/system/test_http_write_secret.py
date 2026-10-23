"""The instance write secret over real HTTP: tj-vhboky.14 items 7a-d (tj-vhboky.50, Sys-3).

Design: tj-vhboky.1 section 5 and tj-vhboky.8 (write routes carry the secret; reads are open),
routers/common/instance_secret.py. This is the one property the unit tier cannot approach: every
unit test overrides the dependency, so only a request through the running service shows the
header is actually checked on each write route, and that a rejection does not echo the secret.

THE MATRIX, for every write route -- the dataset POST, the dataset DELETE, the internal
single-bar POST, the /ui/v1 expiry PATCH and dataset DELETE (tj-grna9p.86, added by
tj-grna9p.136), and the /ui/v1 collecting PATCH (tj-grna9p.175):
  * 7a no header, and an EMPTY header value -> 401, and nothing written or removed;
  * 7b a wrong secret -> 401, and nothing written or removed;
  * 7c the right secret -> the handler runs (below);
  * 7d a GET with no secret, or with a wrong one, succeeds: reads are deliberately open.
Every 401 response, and every 7c response, is checked for the secret -- in the body and in every
raw header value -- before anything else is asserted about it. Every 401 body carries exactly the
fixed rejection detail, imported rather than retyped.

7c ON THE DATASET POST IS A 409, NOT A 2xx, AND ON PURPOSE. A 2xx there means data_store called
data_ingest and the broker, and this part never drives the broker (fake-broker plan, decision
tj-vhboky.54; the dataset POST's success paths are Sys-7, tj-vhboky.63). So each dataset POST here
overlaps an entry of the same owner seeded by SQL: with the right secret the handler runs and
refuses with 409 before the upsert and before the RPC (the own-overlap check,
data/store/app/database/crud/stock/store_dataset_entry.py). A 401 there means the secret was not
accepted; a 409 means it was. And a regression that let a rejected POST through would ALSO stop
at that 409 rather than reach ingest. Any entry a dataset POST might create is adopted into the
cleanup registry regardless.

THE /ui/v1 WRITES CARRY A CONFIRMED OVERRIDE, ON PURPOSE. The seeded entry is the run's owner's,
not the operator's, so without an override the handler would stop at 409 OVERRIDE_REQUIRED and a
regression that dropped the guard would change nothing either way -- the 401 test's "nothing
changed" would then be vacuous for these two routes. With the override, a request that got past
the guard really sets the expiry or really deletes the entry, so the 401 test's checks bite, and
7c is the write itself: PATCH 200 with the expiry stored, DELETE 204 with the entry gone, each
with its audit row. The audit rows are this module's to delete, by the seeded entry's id.

THE COLLECTING PATCH NEEDS A SUBSCRIPTION. The seeded entry is STATIC, and a STATIC dataset is a 422
on that route, so a regression that dropped its guard would stop at the 422 and change nothing: the
401 test's "nothing changed" would be vacuous. Its case therefore turns the seeded entry into a DAILY
subscription that is collecting (by SQL, before the one request), and the request stops it with a
confirmed override: past the guard, collect really goes false and an override row is written.

NOT HERE: 7e (secret unset, restart -> every write 401) and 7f (no secret in the container logs)
need container control; they are shell steps in the CI job (Sys-5, tj-vhboky.52).

UVICORN 0.53 / STARLETTE (tj-jon3d1 carry-over): covered implicitly -- every request in this
suite goes through the prod image's uvicorn and starlette, so a broken request path fails every
test here. There is deliberately no separate test for it.

SECRET HYGIENE (this module's own rule): no test takes the secret as an argument, and no
assertion expression contains a response body from a write route. See DataStoreHttp in
conftest.py for how a failure is kept from printing it.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import ExpiryType, UpdateType
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.common.instance_secret import INSTANCE_SECRET_REJECTION_DETAIL
from routers.data_store.app_endpoints import AssetDataInterface, AssetDatasetStoreInterface, UiDatasetWritesInterface
from tests.system.problem_json import PROBLEM_JSON


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
ENTRY_TABLE = StoreDatasetEntry.__table__
OVERRIDE_TABLE = DatasetOverrideLog.__table__
DELETION_TABLE = DatasetDeletion.__table__

# The seeded entry is another owner's from the operator's point of view; see the module docstring.
UI_OVERRIDE = {'reason': 'secret probe', 'confirmed': True}
UI_EXPIRY = datetime(2031, 1, 2, tzinfo=UTC)

SEED_START = datetime.fromisoformat('2001-01-02T14:30:00+00:00')
SEED_END = datetime.fromisoformat('2001-03-30T21:00:00+00:00')
# Overlaps the seed (Feb-Apr against Jan-Mar) without being an exact repeat of it.
POST_START = datetime.fromisoformat('2001-02-01T14:30:00+00:00')
POST_END = datetime.fromisoformat('2001-04-30T21:00:00+00:00')


class WriteRoute(StrEnum):
    DATASET_POST = 'dataset-post'
    DATASET_DELETE = 'dataset-delete'
    BAR_POST = 'bar-post'
    UI_EXPIRY_PATCH = 'ui-expiry-patch'
    UI_DATASET_DELETE = 'ui-dataset-delete'
    UI_COLLECTING_PATCH = 'ui-collecting-patch'


REJECTED = ('absent', 'empty', 'wrong')


def _dataset_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _bar_path() -> str:
    return AssetDataInterface.POST_ASSET_DATA.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
    )


def _bars_under(pg_engine: Engine, entry_id: Any) -> int:
    with pg_engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.count()).select_from(BAR_TABLE).where(BAR_TABLE.c.dataset_id == entry_id)
        ).scalar_one()


def _entry_exists(pg_engine: Engine, entry_id: Any) -> bool:
    with pg_engine.connect() as conn:
        return (
            conn.execute(
                sa.select(sa.func.count()).select_from(ENTRY_TABLE).where(ENTRY_TABLE.c.id == entry_id)
            ).scalar_one()
            == 1
        )


def _stored_expiry(pg_engine: Engine, entry_id: Any) -> Any:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(ENTRY_TABLE.c.expiry).where(ENTRY_TABLE.c.id == entry_id)).scalar_one()


def _stored_collect(pg_engine: Engine, entry_id: Any) -> Any:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(ENTRY_TABLE.c.collect).where(ENTRY_TABLE.c.id == entry_id)).scalar_one()


def _as_collecting_subscription(pg_engine: Engine, entry_id: Any) -> None:
    """The seeded STATIC entry as a collecting DAILY subscription, so the collecting PATCH can act on it."""
    with pg_engine.begin() as conn:
        conn.execute(
            sa.update(ENTRY_TABLE)
            .where(ENTRY_TABLE.c.id == entry_id)
            .values(update_type=UpdateType.DAILY.value, expiry_type=ExpiryType.ROLLING.value, collect=True)
        )


def _collect_before(route: 'WriteRoute', entry: sa.Row) -> Any:
    """The collect the entry holds when its one request is sent: the collecting case made it a collecting DAILY."""
    return True if route is WriteRoute.UI_COLLECTING_PATCH else entry.collect


def _audit_rows(pg_engine: Engine, entry_id: Any) -> tuple[int, int]:
    """(override-log rows, deletion rows) recorded against `entry_id`."""
    with pg_engine.connect() as conn:
        return tuple(
            conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.dataset_id == entry_id)
            ).scalar_one()
            for table in (OVERRIDE_TABLE, DELETION_TABLE)
        )


@pytest.fixture
def seeded_entry(insert_entry: Callable[..., sa.Row], own_symbol: str, pg_engine: Engine) -> Iterator[sa.Row]:
    """One entry of the run's owner, Jan-Mar, seeded by SQL: the target every write in a test addresses.

    Teardown deletes the audit rows a /ui/v1 write recorded against it (test infrastructure, as in
    test_http_ui_dataset_writes.py); the entry itself goes with conftest's registry.
    """
    entry = insert_entry(asset_symbol=own_symbol, start=SEED_START, end=SEED_END)
    yield entry
    with pg_engine.begin() as conn:
        for table in (OVERRIDE_TABLE, DELETION_TABLE):
            conn.execute(sa.delete(table).where(table.c.dataset_id == entry.id))


@pytest.fixture
def send_write(data_store, run_identity, bar_create_body, adopt_entries, pg_engine) -> Callable[..., httpx.Response]:
    """Send one write on `route` against `entry`, carrying `auth`. The owner is always the entry's own."""

    def _send(route: WriteRoute, entry: sa.Row, auth: str) -> httpx.Response:
        match route:
            case WriteRoute.DATASET_POST:
                body = {
                    'owner': run_identity.owner,
                    'source': entry.source.value,
                    'granularity': entry.granularity.value,
                    'start': POST_START.isoformat(),
                    'end': POST_END.isoformat(),
                }
                try:
                    return data_store.post(_dataset_path(entry.asset_symbol), json=body, auth=auth)
                finally:
                    adopt_entries(entry.asset_symbol)
            case WriteRoute.DATASET_DELETE:
                path = AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID.format(id=entry.id)
                return data_store.delete(path, params={'owner': run_identity.owner}, auth=auth)
            case WriteRoute.BAR_POST:
                return data_store.post(_bar_path(), json=bar_create_body(entry, entry.start), auth=auth)
            case WriteRoute.UI_EXPIRY_PATCH:
                path = UiDatasetWritesInterface.PATCH_UI_DATASET_EXPIRY.format(dataset_id=entry.id)
                body = {'expiry': UI_EXPIRY.isoformat(), 'override': UI_OVERRIDE}
                return data_store.patch(path, json=body, auth=auth)
            case WriteRoute.UI_DATASET_DELETE:
                path = UiDatasetWritesInterface.DELETE_UI_DATASET.format(dataset_id=entry.id)
                return data_store.delete(path, json={'override': UI_OVERRIDE}, auth=auth)
            case WriteRoute.UI_COLLECTING_PATCH:
                _as_collecting_subscription(pg_engine, entry.id)
                path = UiDatasetWritesInterface.PATCH_UI_DATASET_COLLECTING.format(dataset_id=entry.id)
                return data_store.patch(path, json={'collect': False, 'override': UI_OVERRIDE}, auth=auth)
        raise AssertionError(f'unhandled route {route!r}')

    return _send


@pytest.mark.parametrize('auth', REJECTED)
@pytest.mark.parametrize('route', list(WriteRoute))
def test_write_without_the_right_secret_is_401_and_changes_nothing(
    route: WriteRoute,
    auth: str,
    seeded_entry: sa.Row,
    send_write: Callable[..., httpx.Response],
    data_store,
    adopt_entries: Callable[[str], list],
    pg_engine: Engine,
) -> None:
    """7a/7b: absent, empty or wrong secret -> 401 with the fixed detail, no secret in the body, no change."""
    response = send_write(route, seeded_entry, auth)

    # FIRST, and as a bool: if the body leaked the secret, no later assertion may print the body.
    # Body and raw header values both: response_leaks_secret explains why not str(response.headers).
    leaked = data_store.response_leaks_secret(response)
    assert not leaked, f'{route} {auth}: the 401 response carries the instance secret (value withheld)'
    assert response.status_code == 401, data_store.describe(response)
    assert response.headers['content-type'] == PROBLEM_JSON, data_store.describe(response)
    # WHOLE-BODY EQUALITY IS KEPT, and keeping it is the point (tj-3mk3u5.37.9). The old assertion
    # compared the body to exactly {'detail': ...}, which is what proved the 401 carries NOTHING
    # ELSE -- no secret, no echo of the request, no stray member. TE-6 (caf68f5) re-rendered it as
    # problem+json, so the literal moved; if it had been relaxed to a membership check at the same
    # time, this test would have kept its name and quietly stopped guarding the thing it exists
    # for. The 401 goes through the HTTPException handler, which is deliberately NOT one of ours:
    # no reason, no domain and no error_id, which is why those three are absent here and asserting
    # the whole body is what pins their absence.
    assert response.json() == {
        'type': 'about:blank',
        'title': 'Unauthorized',
        'status': 401,
        'detail': INSTANCE_SECRET_REJECTION_DETAIL,
    }, data_store.describe(response)

    # Nothing written, nothing removed.
    assert _entry_exists(pg_engine, seeded_entry.id), f'{route} {auth}: the seeded entry is gone after a 401'
    assert adopt_entries(seeded_entry.asset_symbol) == [seeded_entry.id], (
        f'{route} {auth}: an entry other than the seeded one exists after a 401'
    )
    bars = _bars_under(pg_engine, seeded_entry.id)
    assert bars == 0, f'{route} {auth}: {bars} bar(s) under the seeded entry after a 401'
    expiry = _stored_expiry(pg_engine, seeded_entry.id)
    assert expiry == seeded_entry.expiry, f'{route} {auth}: the expiry moved to {expiry} after a 401'
    collect = _stored_collect(pg_engine, seeded_entry.id)
    assert collect == _collect_before(route, seeded_entry), f'{route} {auth}: collect moved to {collect} after a 401'
    audit = _audit_rows(pg_engine, seeded_entry.id)
    assert audit == (0, 0), f'{route} {auth}: (override, deletion) audit rows {audit} after a 401'


@pytest.mark.parametrize('route', list(WriteRoute))
def test_write_with_the_right_secret_reaches_the_handler(
    route: WriteRoute,
    seeded_entry: sa.Row,
    send_write: Callable[..., httpx.Response],
    data_store,
    adopt_entries: Callable[[str], list],
    pg_engine: Engine,
) -> None:
    """7c: the right secret is accepted and the handler does its work.

    Dataset POST: 409 own-overlap on the seeded entry -- the handler ran; see the module
    docstring for why this is the success signal here. DELETE: 200 and the entry is gone. Bar
    POST: 200 and the bar is stored. /ui/v1 PATCH: 200, the expiry stored and one override row.
    /ui/v1 DELETE: 204, the entry gone, one override row and one deletion row. /ui/v1 collecting
    PATCH: 200, collect stored false and one override row.
    """
    response = send_write(route, seeded_entry, 'right')

    # FIRST, as in the 401 test. This is the one path where the RECEIVED header value IS the
    # secret, so a response that echoed a request header would leak here and nowhere else --
    # hence the headers, not only the body.
    leaked = data_store.response_leaks_secret(response)
    assert not leaked, f'{route}: the response carries the instance secret (value withheld)'
    match route:
        case WriteRoute.DATASET_POST:
            assert response.status_code == 409, data_store.describe(response)
            assert adopt_entries(seeded_entry.asset_symbol) == [seeded_entry.id], data_store.describe(response)
        case WriteRoute.DATASET_DELETE:
            assert response.status_code == 200, data_store.describe(response)
            assert not _entry_exists(pg_engine, seeded_entry.id), data_store.describe(response)
        case WriteRoute.BAR_POST:
            assert response.status_code == 200, data_store.describe(response)
            bars = _bars_under(pg_engine, seeded_entry.id)
            assert bars == 1, f'{bars} bar(s) under the seeded entry; {data_store.describe(response)}'
        case WriteRoute.UI_EXPIRY_PATCH:
            assert response.status_code == 200, data_store.describe(response)
            assert _stored_expiry(pg_engine, seeded_entry.id) == UI_EXPIRY, data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 0), data_store.describe(response)
        case WriteRoute.UI_DATASET_DELETE:
            assert response.status_code == 204, data_store.describe(response)
            assert not _entry_exists(pg_engine, seeded_entry.id), data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 1), data_store.describe(response)
        case WriteRoute.UI_COLLECTING_PATCH:
            assert response.status_code == 200, data_store.describe(response)
            assert _stored_collect(pg_engine, seeded_entry.id) is False, data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 0), data_store.describe(response)


@pytest.mark.parametrize('auth', ['absent', 'wrong'])
def test_reads_are_open_without_the_secret(
    auth: str, seeded_entry: sa.Row, data_store, bar_create_body: Callable[..., dict[str, Any]]
) -> None:
    """7d: both GETs answer 200 with no secret, or a wrong one, and return what was written."""
    stored = data_store.post(_bar_path(), json=bar_create_body(seeded_entry, seeded_entry.start), auth='right')
    assert stored.status_code == 200, data_store.describe(stored)

    entries = data_store.get(_dataset_path(seeded_entry.asset_symbol), auth=auth)
    assert entries.status_code == 200, data_store.describe(entries)
    assert [entry['id'] for entry in entries.json()] == [str(seeded_entry.id)], data_store.describe(entries)

    bars = data_store.get(
        AssetDataInterface.GET_ASSET_DATA.format(
            asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
        ),
        params={'asset_symbol': seeded_entry.asset_symbol},
        auth=auth,
    )
    assert bars.status_code == 200, data_store.describe(bars)
    assert [bar['id'] for bar in bars.json()] == [stored.json()['id']], data_store.describe(bars)
