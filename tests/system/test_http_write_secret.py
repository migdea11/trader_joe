"""The instance write secret over real HTTP: tj-vhboky.14 items 7a-d (tj-vhboky.50, Sys-3).

Design: tj-vhboky.1 section 5 and tj-vhboky.8 (write routes carry the secret; reads are open),
routers/common/instance_secret.py. This is the one property the unit tier cannot approach: every
unit test overrides the dependency, so only a request through the running service shows the
header is actually checked on each write route, and that a rejection does not echo the secret.

THE MATRIX, for every write route -- the dataset POST, the dataset DELETE and the internal
single-bar POST:
  * 7a no header, and an EMPTY header value -> 401, and nothing written or removed;
  * 7b a wrong secret -> 401, and nothing written or removed;
  * 7c the right secret -> the handler runs (below);
  * 7d a GET with no secret, or with a wrong one, succeeds: reads are deliberately open.
Every 401 body is checked for the secret before anything else is asserted about it, and carries
exactly the fixed rejection detail, imported rather than retyped.

7c ON THE DATASET POST IS A 409, NOT A 2xx, AND ON PURPOSE. A 2xx there means data_store called
data_ingest and the broker, and this part never drives the broker (fake-broker plan, decision
tj-vhboky.54; the dataset POST's success paths are Sys-7, tj-vhboky.63). So each dataset POST here
overlaps an entry of the same owner seeded by SQL: with the right secret the handler runs and
refuses with 409 before the upsert and before the RPC (the own-overlap check,
data/store/app/database/crud/stock/store_dataset_entry.py). A 401 there means the secret was not
accepted; a 409 means it was. And a regression that let a rejected POST through would ALSO stop
at that 409 rather than reach ingest. Any entry a dataset POST might create is adopted into the
cleanup registry regardless.

NOT HERE: 7e (secret unset, restart -> every write 401) and 7f (no secret in the container logs)
need container control; they are shell steps in the CI job (Sys-5, tj-vhboky.52).

UVICORN 0.53 / STARLETTE (tj-jon3d1 carry-over): covered implicitly -- every request in this
suite goes through the prod image's uvicorn and starlette, so a broken request path fails every
test here. There is deliberately no separate test for it.

SECRET HYGIENE (this module's own rule): no test takes the secret as an argument, and no
assertion expression contains a response body from a write route. See DataStoreHttp in
conftest.py for how a failure is kept from printing it.
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.common.instance_secret import INSTANCE_SECRET_REJECTION_DETAIL
from routers.data_store.app_endpoints import AssetDataInterface, AssetDatasetStoreInterface


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
ENTRY_TABLE = StoreDatasetEntry.__table__

SEED_START = datetime.fromisoformat('2001-01-02T14:30:00+00:00')
SEED_END = datetime.fromisoformat('2001-03-30T21:00:00+00:00')
# Overlaps the seed (Feb-Apr against Jan-Mar) without being an exact repeat of it.
POST_START = datetime.fromisoformat('2001-02-01T14:30:00+00:00')
POST_END = datetime.fromisoformat('2001-04-30T21:00:00+00:00')


class WriteRoute(StrEnum):
    DATASET_POST = 'dataset-post'
    DATASET_DELETE = 'dataset-delete'
    BAR_POST = 'bar-post'


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


@pytest.fixture
def seeded_entry(insert_entry: Callable[..., sa.Row], own_symbol: str) -> sa.Row:
    """One entry of the run's owner, Jan-Mar, seeded by SQL: the target every write in a test addresses."""
    return insert_entry(asset_symbol=own_symbol, start=SEED_START, end=SEED_END)


@pytest.fixture
def send_write(data_store, run_identity, bar_create_body, adopt_entries) -> Callable[..., httpx.Response]:
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
    leaked = data_store.leaks_secret(response.text) or data_store.leaks_secret(str(response.headers))
    assert not leaked, f'{route} {auth}: the 401 response carries the instance secret (value withheld)'
    assert response.status_code == 401, data_store.describe(response)
    assert response.json() == {'detail': INSTANCE_SECRET_REJECTION_DETAIL}, data_store.describe(response)

    # Nothing written, nothing removed.
    assert _entry_exists(pg_engine, seeded_entry.id), f'{route} {auth}: the seeded entry is gone after a 401'
    assert adopt_entries(seeded_entry.asset_symbol) == [seeded_entry.id], (
        f'{route} {auth}: an entry other than the seeded one exists after a 401'
    )
    bars = _bars_under(pg_engine, seeded_entry.id)
    assert bars == 0, f'{route} {auth}: {bars} bar(s) under the seeded entry after a 401'


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
    POST: 200 and the bar is stored.
    """
    response = send_write(route, seeded_entry, 'right')

    leaked = data_store.leaks_secret(response.text)
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
