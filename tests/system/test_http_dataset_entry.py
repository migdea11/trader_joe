"""Dataset entries over real HTTP: tj-vhboky.14 items 5 (409 half), 6 and 12 (tj-vhboky.50, Sys-3).

Design: tj-vhboky.1 sections 2-5 (identity, own-overlap, owner-scoped id writes, cascade),
tj-vhboky.8 (409 carrying the colliding ids; 403 for a non-owner), tj-vhboky.11 item 11 (a bar
belongs to exactly one entry, so two overlapping fetches store two rows per instant).

Entries are seeded by SQL through conftest's insert_entry: the only HTTP route that creates one
is the dataset POST, which reaches data_ingest and the broker (never driven here; decision
tj-vhboky.54, Sys-7 tj-vhboky.63). Bars are written over HTTP through the internal single-bar
POST, and every delete, list and read goes over HTTP. Row counts are read by SQL, because the
point of item 6 is what the DATABASE holds after the cascade, not what an endpoint reports.

  * 5 (409 half): an overlapping, non-identical request by the SAME owner is refused with 409,
    naming the seeded id in colliding_ids, and creates nothing: it is refused before the upsert
    and before any call to data_ingest. A request that reached the RPC would be a finding, not a
    test to relax. (The other-owner success half needs ingest: Sys-7.)
  * 6: two owners' overlapping entries, both with bars, sharing instants (asserted: an instant
    with 2 rows). A DELETE by the wrong owner, or with no owner, is 403 and removes nothing. The
    owner's DELETE removes that entry and its bars and leaves the other entry's bars and its
    item_count (read through GET /store) unchanged. Counts before and after are in every message.
  * 12: GET /store lists expiry for an entry that has NO bars, from the entry's own column,
    with item_count 0 -- which a min() over bars could never produce.

Every request goes through the prod image's uvicorn/starlette (tj-jon3d1 carry-over, covered
implicitly; see test_http_write_secret.py).
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.errors.vocabulary import Reason
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.data_store.app_endpoints import AssetDataInterface, AssetDatasetStoreInterface
from tests.system.problem_json import assert_problem


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
ENTRY_TABLE = StoreDatasetEntry.__table__

JAN_START = datetime.fromisoformat('2001-01-02T14:30:00+00:00')
MAR_END = datetime.fromisoformat('2001-03-30T21:00:00+00:00')
FEB_START = datetime.fromisoformat('2001-02-01T14:30:00+00:00')
APR_END = datetime.fromisoformat('2001-04-30T21:00:00+00:00')


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _delete_path(entry_id: UUID) -> str:
    return AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID.format(id=entry_id)


BAR_PATH = AssetDataInterface.POST_ASSET_DATA.format(
    asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
)


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f'{value!r} came back without an offset'
    return parsed


def _bars_by_dataset(pg_engine: Engine, symbol: str) -> dict[UUID, int]:
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(BAR_TABLE.c.dataset_id, sa.func.count())
            .where(BAR_TABLE.c.asset_symbol == symbol)
            .group_by(BAR_TABLE.c.dataset_id)
        ).all()
    return dict(rows)


def _rows_per_instant(pg_engine: Engine, symbol: str) -> dict[datetime, int]:
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(BAR_TABLE.c.timestamp, sa.func.count())
            .where(BAR_TABLE.c.asset_symbol == symbol)
            .group_by(BAR_TABLE.c.timestamp)
        ).all()
    return dict(rows)


def _entry_ids(pg_engine: Engine, symbol: str) -> set[UUID]:
    with pg_engine.connect() as conn:
        return set(conn.execute(sa.select(ENTRY_TABLE.c.id).where(ENTRY_TABLE.c.asset_symbol == symbol)).scalars())


# ---------------------------------------------------------------------------------------------
# Item 5, the 409 half.


def test_own_overlap_is_409_naming_the_seeded_entry_and_creates_nothing(
    insert_entry: Callable[..., sa.Row],
    own_symbol: str,
    run_identity,
    data_store,
    adopt_entries: Callable[[str], list[UUID]],
    pg_engine: Engine,
) -> None:
    seeded = insert_entry(asset_symbol=own_symbol, start=JAN_START, end=MAR_END)
    body = {
        'owner': run_identity.owner,
        'source': seeded.source.value,
        'granularity': seeded.granularity.value,
        'start': FEB_START.isoformat(),
        'end': APR_END.isoformat(),
    }
    try:
        response = data_store.post(_store_path(own_symbol), json=body, auth='right')
    finally:
        entries_after = adopt_entries(own_symbol)

    # BOTH OLD ASSERTIONS SURVIVE, ONE OF THEM STRENGTHENED (tj-3mk3u5.37.9). TE-6 replaced the
    # ad-hoc {'detail': {'colliding_ids': ..., 'message': ...}} with problem+json, so
    # colliding_ids is a TOP-LEVEL member now -- it is an allowlisted metadata key precisely so a
    # caller can read it off the body -- and the human sentence is the envelope's `detail`.
    problem = assert_problem(
        response, status=409, reason=Reason.OWN_OVERLAP_CONFLICT.value, title='Conflict', describe=data_store.describe
    )
    assert problem['colliding_ids'] == [str(seeded.id)], data_store.describe(response)

    # The old pin on the sentence was "a non-empty str", which any sentence satisfies. It is
    # replaced by the guard tj-8feral settled on: the sentence NAMES every colliding id, which is
    # the part an operator reading a log actually needs, while its wording stays free to change.
    # Asserting the wording instead would red on a harmless reword and teach people to edit the
    # test rather than read it.
    assert str(seeded.id) in problem['detail'], data_store.describe(response)
    assert 'UUID(' not in problem['detail'], (
        f'the 409 detail carries a Python repr, which tj-8feral took out of it: {problem["detail"]!r}'
    )
    assert entries_after == [seeded.id], (
        f'the refused POST left {len(entries_after)} entries for the symbol, expected only the seeded one'
    )
    bars = _bars_by_dataset(pg_engine, own_symbol)
    assert bars == {}, f'bars exist for the symbol after a refused POST: {bars}'


# ---------------------------------------------------------------------------------------------
# Item 12.


def test_store_listing_reads_expiry_from_an_entry_with_no_bars(
    insert_entry: Callable[..., sa.Row], own_symbol: str, data_store, pg_engine: Engine
) -> None:
    expiry = datetime.fromisoformat('2031-03-04T05:06:07.123456+00:00')
    seeded = insert_entry(asset_symbol=own_symbol, expiry=expiry)
    assert _bars_by_dataset(pg_engine, own_symbol) == {}

    response = data_store.get(_store_path(own_symbol))

    assert response.status_code == 200, data_store.describe(response)
    listed = response.json()
    assert [entry['id'] for entry in listed] == [str(seeded.id)], data_store.describe(response)
    assert listed[0]['item_count'] == 0, data_store.describe(response)
    assert listed[0]['expiry'] is not None, f'expiry missing on a bar-less entry: {data_store.describe(response)}'
    assert _parse_instant(listed[0]['expiry']) == expiry, data_store.describe(response)


# ---------------------------------------------------------------------------------------------
# Item 6: the cascade, with the same instant held by both entries.

# Entry X holds bars at minutes 0-2, entry Y at minutes 1-3: minutes 1 and 2 are held by BOTH.
X_MINUTES = (0, 1, 2)
Y_MINUTES = (1, 2, 3)


@pytest.fixture
def overlapping_pair(
    insert_entry: Callable[..., sa.Row],
    own_symbol: str,
    run_identity,
    data_store,
    bar_create_body: Callable[..., dict[str, Any]],
) -> tuple[sa.Row, sa.Row]:
    """Two owners' entries over one symbol and window, each with bars written over HTTP.

    X belongs to the run's owner and Y to a second owner; the specs are otherwise identical, so
    they are separate entries only because owner is identity. Returns (X, Y).
    """
    x_entry = insert_entry(asset_symbol=own_symbol)
    y_entry = insert_entry(asset_symbol=own_symbol, owner=run_identity.alt_owner('other'))
    for entry, minutes in ((x_entry, X_MINUTES), (y_entry, Y_MINUTES)):
        for minute in minutes:
            timestamp = entry.start + timedelta(minutes=minute)
            response = data_store.post(BAR_PATH, json=bar_create_body(entry, timestamp), auth='right')
            assert response.status_code == 200, data_store.describe(response)
    return x_entry, y_entry


def _item_count(data_store, symbol: str, entry_id: UUID, owner: str) -> int:
    response = data_store.get(_store_path(symbol), params={'owner': owner})
    assert response.status_code == 200, data_store.describe(response)
    counts = [entry['item_count'] for entry in response.json() if entry['id'] == str(entry_id)]
    assert len(counts) == 1, f'entry {entry_id} listed {len(counts)} times: {data_store.describe(response)}'
    return counts[0]


def test_both_entries_hold_a_row_at_a_shared_instant(
    overlapping_pair: tuple[sa.Row, sa.Row], own_symbol: str, pg_engine: Engine
) -> None:
    """tj-vhboky.11 item 11, observed rather than inferred: an instant with exactly 2 rows."""
    x_entry, _ = overlapping_pair
    per_instant = _rows_per_instant(pg_engine, own_symbol)
    shared = x_entry.start + timedelta(minutes=1)
    assert per_instant.get(shared) == 2, f'rows per instant for {own_symbol}: {per_instant}'
    expected_twice = {x_entry.start + timedelta(minutes=m) for m in set(X_MINUTES) & set(Y_MINUTES)}
    assert {instant for instant, count in per_instant.items() if count == 2} == expected_twice, per_instant


@pytest.mark.parametrize('declared', ['other-owner', 'no-owner'])
def test_delete_by_a_non_owner_is_403_and_removes_nothing(
    declared: str, overlapping_pair: tuple[sa.Row, sa.Row], own_symbol: str, run_identity, data_store, pg_engine: Engine
) -> None:
    x_entry, y_entry = overlapping_pair
    before = _bars_by_dataset(pg_engine, own_symbol)
    params = {'owner': run_identity.alt_owner('other')} if declared == 'other-owner' else None

    response = data_store.delete(_delete_path(x_entry.id), params=params, auth='right')

    after = _bars_by_dataset(pg_engine, own_symbol)
    assert response.status_code == 403, data_store.describe(response)
    assert _entry_ids(pg_engine, own_symbol) == {x_entry.id, y_entry.id}, 'an entry vanished after a 403'
    assert after == before, f'bars per dataset changed on a 403: before {before}, after {after}'


def test_owner_delete_cascades_to_its_bars_and_leaves_the_other_entry_whole(
    overlapping_pair: tuple[sa.Row, sa.Row], own_symbol: str, run_identity, data_store, pg_engine: Engine
) -> None:
    x_entry, y_entry = overlapping_pair
    other_owner = run_identity.alt_owner('other')
    before = _bars_by_dataset(pg_engine, own_symbol)
    y_count_before = _item_count(data_store, own_symbol, y_entry.id, other_owner)
    assert before == {x_entry.id: len(X_MINUTES), y_entry.id: len(Y_MINUTES)}, f'seed wrong: {before}'
    assert y_count_before == len(Y_MINUTES), f'item_count of Y before the delete: {y_count_before}'

    response = data_store.delete(_delete_path(x_entry.id), params={'owner': run_identity.owner}, auth='right')

    after = _bars_by_dataset(pg_engine, own_symbol)
    y_count_after = _item_count(data_store, own_symbol, y_entry.id, other_owner)
    counts = f'bars per dataset before {before}, after {after}; Y item_count {y_count_before} -> {y_count_after}'
    assert response.status_code == 200, f'{data_store.describe(response)}; {counts}'
    assert _entry_ids(pg_engine, own_symbol) == {y_entry.id}, counts
    assert after == {y_entry.id: len(Y_MINUTES)}, counts
    assert y_count_after == y_count_before, counts
    # And the shared instants now hold one row each: Y's.
    per_instant = _rows_per_instant(pg_engine, own_symbol)
    assert set(per_instant.values()) == {1}, f'{counts}; rows per instant after: {per_instant}'
