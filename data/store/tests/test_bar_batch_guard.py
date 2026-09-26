"""The whole-batch duplicate-timestamp guard, and the read path's schema conversion.

WHY THIS FILE EXISTS (validator, gating tj-vhboky.5). Commit e907bc6 added three pieces of
production code that nothing in the suite touched: DuplicateBatchTimestamp, _as_utc, and the
to_schema() call that replaced a model_validate() that always raised. test_bar_write_path.py
covers the column dicts and stops there -- it never calls the crud layer, so the guard could be
deleted tomorrow and that file would stay green.

WHAT THE GUARD IS FOR, because it looks like a redundant check against a unique constraint and is
not. A single INSERT ... ON CONFLICT DO UPDATE whose VALUES list holds two rows with the same
conflict key does not upsert one over the other -- Postgres aborts the STATEMENT with error 21000,
"ON CONFLICT DO UPDATE command cannot affect row a second time". Every bar in a batch shares one
dataset_id, so two bars at one timestamp collide on the natural key and take the whole batch down
with a message that names no timestamp. The guard converts that into a named exception before any
SQL is built. tj-vhboky.7 chunks this write and relies on the guard running across the WHOLE batch
first, which is why "no SQL was built" is asserted rather than just "it raised".

NOT PROVEN HERE, and it is the half that matters most operationally: that Postgres actually raises
21000 on such a batch. That needs a live database and belongs to tj-vhboky.14. This file proves we
never hand it one.
"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.crud.stock.asset_market_activity import (
    DuplicateBatchTimestamp,
    _as_utc,
    batch_create_market_activity_data,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityData,
)


pytestmark = pytest.mark.data_store

TIMESTAMP = datetime(2026, 1, 2, tzinfo=UTC)


def _bar(close: float = 1.5) -> StockDataMarketActivityData:
    return StockDataMarketActivityData(open=1.0, high=2.0, low=0.5, close=close, volume=100, trade_count=10)


def _batch(*timestamps: datetime) -> BatchStockDataMarketActivityCreate:
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=uuid4(),
        asset_symbol='AAPL',
        source=DataSource.ALPACA_API,
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        dataset={},
    )
    for index, timestamp in enumerate(timestamps):
        batch.append_data(DataType.MARKET_ACTIVITY, _bar(1.5 + index), timestamp)
    return batch


@pytest.fixture
def session() -> MagicMock:
    """An async session that records calls and reaches no database.

    execute/commit/rollback are the three the write path uses. Each is an AsyncMock so the
    production code's `await` works, and the recording is what lets a test assert that the guard
    fired BEFORE the statement rather than after it.
    """
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_a_batch_of_distinct_timestamps_reaches_the_upsert(session: MagicMock):
    """The guard's negative case, so a guard that rejected everything would not pass this file."""
    written = await batch_create_market_activity_data(session, _batch(TIMESTAMP, TIMESTAMP + timedelta(days=1)))

    assert written == 2
    assert session.execute.await_count == 1, 'the distinct-timestamp batch never reached the upsert'
    assert session.commit.await_count == 1
    assert session.rollback.await_count == 0


@pytest.mark.asyncio
async def test_a_duplicate_timestamp_raises_before_any_sql_is_built(session: MagicMock):
    """The point of the guard: pre-empt Postgres 21000 rather than let the batch reach it.

    `execute` never being awaited is the real assertion. A guard placed after the statement was
    built and sent would still raise this exception and would still be the defect it exists to
    prevent, so raising alone is not enough to pass.
    """
    with pytest.raises(DuplicateBatchTimestamp):
        await batch_create_market_activity_data(session, _batch(TIMESTAMP, TIMESTAMP))

    assert session.execute.await_count == 0, 'the colliding batch was sent to the database anyway'
    assert session.commit.await_count == 0


@pytest.mark.asyncio
async def test_the_rejected_batch_is_rolled_back(session: MagicMock):
    """The write path's own error handling has to cover the guard, not just database errors.

    The guard raises inside the try block, so the rollback is reached. Asserted because a future
    refactor that hoists the guard above the try would silently leave the session dirty.
    """
    with pytest.raises(DuplicateBatchTimestamp):
        await batch_create_market_activity_data(session, _batch(TIMESTAMP, TIMESTAMP))

    assert session.rollback.await_count == 1


@pytest.mark.asyncio
async def test_the_rejection_names_the_colliding_timestamp(session: MagicMock):
    """Keeping the failure loud is an acceptance criterion of this bead, so it is asserted.

    The opaque message is what makes the unguarded Postgres failure expensive to diagnose; an
    exception that also named nothing would reproduce the problem it replaces.
    """
    with pytest.raises(DuplicateBatchTimestamp) as raised:
        await batch_create_market_activity_data(session, _batch(TIMESTAMP, TIMESTAMP))

    assert str(TIMESTAMP) in str(raised.value)


@pytest.mark.asyncio
async def test_the_guard_sees_through_timezone_representation(session: MagicMock):
    """The entire reason _as_utc exists, stated as a batch-level behaviour.

    Two bars naming the SAME INSTANT in different offsets are one row to Postgres, because the
    column is timestamptz. A guard comparing raw datetimes would pass this batch straight into the
    21000 it exists to prevent. 07:00-05:00 and 12:00Z are the same instant.
    """
    same_instant_elsewhere = datetime(2026, 1, 2, 7, tzinfo=timezone(-timedelta(hours=5)))
    noon_utc = datetime(2026, 1, 2, 12, tzinfo=UTC)
    assert same_instant_elsewhere == noon_utc, 'the fixture stopped naming one instant'

    with pytest.raises(DuplicateBatchTimestamp):
        await batch_create_market_activity_data(session, _batch(noon_utc, same_instant_elsewhere))

    assert session.execute.await_count == 0


def test_as_utc_treats_a_naive_timestamp_as_utc():
    """Naive input is assumed UTC rather than local, which is the only safe reading here.

    Assuming the container's local zone would make the guard's behaviour depend on TZ, so a batch
    that collides on one host would pass on another. Pinned directly because the batch-level test
    above cannot reach it: the schema coerces naive input before the guard ever sees it.
    """
    assert _as_utc(datetime(2026, 1, 2, 12)) == datetime(2026, 1, 2, 12, tzinfo=UTC)


def test_as_utc_converts_rather_than_relabels_an_aware_timestamp():
    """A relabelling bug (replace(tzinfo=UTC) on an aware value) moves the instant.

    That is the plausible wrong implementation, and it would make two bars at one instant look
    distinct -- the exact miss the guard is built to avoid.
    """
    converted = _as_utc(datetime(2026, 1, 2, 7, tzinfo=timezone(-timedelta(hours=5))))

    assert converted == datetime(2026, 1, 2, 12, tzinfo=UTC)
    assert converted.utcoffset() == timedelta(0)


def test_to_schema_converts_a_stored_row_to_the_read_schema():
    """The read path's conversion, which e907bc6 changed from a call that always raised.

    Built as a detached ORM instance rather than through a query: the conversion is pure, and this
    keeps the assertion on the mapping instead of on a session.
    """
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    row = StockMarketActivity(
        id=1,
        dataset_id=uuid4(),
        source=DataSource.ALPACA_API,
        asset_symbol='AAPL',
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        timestamp=TIMESTAMP,
        created_at=now,
        updated_at=now,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100,
        trade_count=10,
    )

    schema = row.to_schema()

    assert isinstance(schema, StockDataMarketActivity)
    assert schema.asset_symbol == 'AAPL'
    assert schema.timestamp == TIMESTAMP
    assert schema.feed is Feed.SIP
    assert schema.data.close == 1.5
    assert schema.data.trade_count == 10


@pytest.mark.parametrize('stored_feed', [Feed.IEX, Feed.SIP, Feed.NOT_APPLICABLE])
def test_to_schema_reports_the_tape_that_served_the_row(stored_feed: Feed):
    """tj-5dvgaa's acceptance criterion, pinned against a CONSTANT and not just against absence.

    The bead added a required ``AssetData.feed`` and made ``to_schema()`` supply it in the same
    commit. Deleting the ``feed=self.feed`` argument is caught by any to_schema test at all, because
    the read model now rejects the construction outright -- but HARD-CODING the argument is not.
    ``feed=Feed.IEX`` would satisfy every other assertion in this file and in the schemas smoke
    tests, and would report the wrong tape for every SIP bar the store holds, which is precisely the
    wrong-answer-rather-than-missing-answer failure the bead's reasoning rejects an optional field to
    avoid.

    Parametrising over every member is what closes that: no single constant satisfies three cases.
    ``NOT_APPLICABLE`` is included because it is a real stored value -- the final, correct answer for
    a source with no tape distinction -- and not a sentinel standing in for one that was never
    resolved (tj-vhboky.1, 2026-09-25).

    Args:
        stored_feed: The feed on the stored row, which the converted schema must report back.
    """
    row = StockMarketActivity(
        id=1,
        dataset_id=uuid4(),
        source=DataSource.ALPACA_API,
        asset_symbol='AAPL',
        feed=stored_feed,
        granularity=Granularity.ONE_DAY,
        timestamp=TIMESTAMP,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100,
        trade_count=10,
    )

    assert row.to_schema().feed is stored_feed


def test_model_validate_on_a_stored_row_still_raises():
    """Why the to_schema() fix was a fix and not a preference.

    StockDataMarketActivity declares no from_attributes and nests its payload under `data`, so
    feeding it an ORM row raises. This pins the reason: if someone later adds from_attributes and
    makes model_validate work, this test goes red and the to_schema call can be reconsidered
    deliberately rather than reverted by accident.
    """
    row = StockMarketActivity(
        id=1,
        dataset_id=uuid4(),
        source=DataSource.ALPACA_API,
        asset_symbol='AAPL',
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        timestamp=TIMESTAMP,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100,
        trade_count=10,
    )

    with pytest.raises(Exception):  # noqa: B017 -- pydantic's ValidationError is the expected one
        StockDataMarketActivity.model_validate(row)
