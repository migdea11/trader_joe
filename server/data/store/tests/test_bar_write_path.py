"""The bar insert path supplies every column the table will not accept a NULL in.

WHY THIS FILE EXISTS AT ALL (tj-1njw7c, orchestrator ruling 2026-09-25). eec8f88a7443 made
stock_market_activity.feed NOT NULL with no server default, and the write path
(StockMarketActivity.from_create / .from_batch_create) omitted it. Nothing in the suite noticed,
and nothing COULD: the only test that reaches the insert path is the POST route in
test_http_smoke.py, and that route runs against a FakeSession which enforces no NOT NULL. So
repairing that file's stale fixture turned its POST case green over a write path that could not
have stored a bar -- a green result asserting nothing, which is the failure shape this repo keeps
finding (tj-0qxnzw, tj-06uflo, both test_app_import.py files).

WHAT TIER THIS IS, stated so it is not mistaken for the other one. These are DIRECT assertions on
the dicts the two classmethods return. No database, no session, no fake -- they run in the PR gate
and go red the moment the write path stops supplying a required column. A real insert against a
live NOT NULL constraint is the only thing that proves the end-to-end path and it stays on the
host-verified tier (tj-vhboky.14); this file is not a substitute for it and that one is not a
substitute for this.

THE REQUIRED SET IS DERIVED FROM THE MODEL, NOT LISTED HERE. A hand-written list would pin today's
columns and say nothing the day a NOT NULL column is added, which is exactly how feed got in. The
one thing a derived set cannot catch is the column being quietly made NULLABLE -- that would
shrink the set rather than fail it -- so the two columns this task turns on are named explicitly
in test_the_required_set_is_not_vacuous below.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


pytestmark = pytest.mark.data_store

TIMESTAMP = datetime(2026, 1, 2, tzinfo=UTC)


def _required_columns() -> set[str]:
    """Every column an INSERT must name, read off the model.

    A column is required when the server will neither generate it nor tolerate its absence:
    NOT NULL, no Python-side default, no server default, and not the autoincrementing surrogate
    key. created_at and updated_at fall out through `default`, id through `primary_key`.
    """
    return {
        column.name
        for column in StockMarketActivity.__table__.columns
        if not column.nullable and column.default is None and column.server_default is None and not column.primary_key
    }


def _table_columns() -> set[str]:
    """Every column the table HAS, required or not.

    The upper bound on what the write path may name. SQLAlchemy answers a key the table does not
    have with CompileError rather than by ignoring it, so an unknown key is a real defect -- but a
    NULLABLE key is not, which is why this and not _required_columns() is the ceiling.
    """
    return {column.name for column in StockMarketActivity.__table__.columns}


def _bar_data(close: float = 1.5) -> StockDataMarketActivityData:
    return StockDataMarketActivityData(open=1.0, high=2.0, low=0.5, close=close, volume=100, trade_count=10)


@pytest.fixture
def create() -> StockDataMarketActivityCreate:
    return StockDataMarketActivityCreate(
        dataset_id=uuid4(),
        asset_symbol='AAPL',
        source=DataSource.ALPACA_API,
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        timestamp=TIMESTAMP,
        data=_bar_data(),
    )


@pytest.fixture
def batch_create() -> BatchStockDataMarketActivityCreate:
    """Two bars, so 'stamped on every bar' below is a real claim and not a claim about one row."""
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=uuid4(),
        asset_symbol='AAPL',
        source=DataSource.ALPACA_API,
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        dataset={},
    )
    batch.append_data(DataType.MARKET_ACTIVITY, _bar_data(1.5), TIMESTAMP)
    batch.append_data(DataType.MARKET_ACTIVITY, _bar_data(2.5), datetime(2026, 1, 3, tzinfo=UTC))
    return batch


def test_the_required_set_is_not_vacuous():
    """The guard on the derivation above, because the derivation has one blind spot.

    _required_columns() reads the model, so making feed NULLABLE would remove it from the set and
    leave every other test in this file passing over a write path that no longer supplies it.
    This test is the one place a column is named by hand, and it is deliberate: feed being NOT
    NULL with no server default is a RULING (tj-vhboky.1, "no sentinel for we do not know"), not
    an implementation detail, so a change to it should cost a red test and a decision record.
    """
    required = _required_columns()
    assert 'feed' in required, 'feed is no longer a required column -- the ruling it rests on has been reversed'
    # Every natural-key column is required by definition: Postgres treats NULL as distinct from
    # NULL in a unique index, so a nullable member of the key silently stops ON CONFLICT matching.
    assert required >= set(StockMarketActivity.NATURAL_KEY), (
        f'a natural-key column is nullable or defaulted: {sorted(set(StockMarketActivity.NATURAL_KEY) - required)}'
    )


def test_from_create_supplies_every_required_column_and_nothing_unknown(create: StockDataMarketActivityCreate):
    """Keys, not values: this is what a NOT NULL violation on the server would have been.

    TWO BOUNDS, NOT ONE EQUALITY. A MISSING required key is the feed defect. An UNKNOWN key is the
    other half of the same change -- split_factor, dividends_factor and expiry were dropped from
    the table, and SQLAlchemy answers a key the table does not have with CompileError.

    Equality against _required_columns() would bundle a third claim nobody intended: that the write
    path supplies NOTHING NULLABLE. Adding a nullable bar column and populating it is a correct
    write path, and it would have reddened this test. The bounds below are the two intents this
    docstring has always stated, and only those.
    """
    supplied = set(StockMarketActivity.from_create(create))
    assert supplied >= _required_columns(), (
        f'the write path omits required columns: {sorted(_required_columns() - supplied)}'
    )
    assert supplied <= _table_columns(), (
        f'the write path names columns the table lacks: {sorted(supplied - _table_columns())}'
    )


def test_from_create_carries_the_resolved_feed_through(create: StockDataMarketActivityCreate):
    """The key being present is not enough to prove the column can be written.

    A write path emitting `'feed': None` satisfies the keys assertion above and still violates
    the NOT NULL column, so the value is pinned separately.

    Feed.SIP is used rather than the first member so a write path that hard-coded a tape instead
    of reading the one the adapter resolved fails here.
    """
    assert StockMarketActivity.from_create(create)['feed'] == Feed.SIP


def test_from_batch_create_supplies_every_required_column_and_nothing_unknown(
    batch_create: BatchStockDataMarketActivityCreate,
):
    """The batch path is a separate implementation of the same dict and drifts independently.

    It built its rows from a comprehension over the batch while reading feed off the batch rather
    than the bar, which is right but is not the same code as from_create -- so it gets its own
    assertion rather than sharing one.

    Same two bounds as the single-bar case above, and for the same reason.
    """
    rows = StockMarketActivity.from_batch_create(batch_create)
    assert len(rows) == 2, 'the batch lost a bar on the way through'
    for row in rows:
        supplied = set(row)
        assert supplied >= _required_columns(), (
            f'a batch row omits required columns: {sorted(_required_columns() - supplied)}'
        )
        assert supplied <= _table_columns(), (
            f'a batch row names columns the table lacks: {sorted(supplied - _table_columns())}'
        )


def test_from_batch_create_stamps_the_batch_feed_on_every_bar(batch_create: BatchStockDataMarketActivityCreate):
    """One fetch is served by one tape, so one feed covers the whole batch.

    Asserted per row rather than on the first: a comprehension that read feed off the per-bar
    object would produce None on every row after the first, or raise, and checking one row finds
    neither reliably.
    """
    rows = StockMarketActivity.from_batch_create(batch_create)
    assert [row['feed'] for row in rows] == [Feed.SIP, Feed.SIP]
    # And the bars are still distinguishable, so the rows are not one bar repeated.
    assert [row['close'] for row in rows] == [1.5, 2.5]


def test_no_required_column_is_left_to_the_server(create: StockDataMarketActivityCreate):
    """States the derivation's premise as an assertion instead of a comment.

    If someone adds a NOT NULL column WITH a server default, _required_columns() drops it and the
    tests above stay honest. If they add one WITHOUT, this file goes red -- which is the intended
    cost, because the write path has to be taught to supply it.
    """
    supplied = set(StockMarketActivity.from_create(create))
    missing = [
        column
        for column in StockMarketActivity.__table__.columns
        if not column.nullable
        and not column.primary_key
        and column.default is None
        and column.server_default is None
        and column.name not in supplied
    ]
    assert missing == [], f'the write path omits NOT NULL columns with no default: {[c.name for c in missing]}'
