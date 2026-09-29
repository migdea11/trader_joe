"""The bar row's shape and the read path that converts it (tj-vhboky.11 items 14 and 15).

WHAT TIER THIS IS. The model is inspected directly, and read_market_activity_data is driven
against a recording fake session that hands back detached ORM rows. There is no database: what the
filter conditions MATCH is not asserted here (tj-vhboky.11 items 21-23, held on tj-xoz4ll), only
what the read does with the rows it gets.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.crud.stock.asset_market_activity import read_market_activity_data
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from schemas.data_store.stock.market_activity_data import StockDataMarketActivity, StockDataMarketActivityQuery


pytestmark = pytest.mark.data_store

TIMESTAMP = datetime(2026, 1, 2, tzinfo=UTC)
DATASET_ID = UUID('00000000-0000-0000-0000-00000000000b')

# The columns the epic removed from the bar: the two corporate-action factors (tj-1ltrur: raw
# immutable bars, adjustment applied on read) and expiry, which moved to the dataset entry
# (tj-vhboky.1 section 9, Amendment 1).
DEAD_BAR_COLUMNS = ('split_factor', 'dividends_factor', 'expiry')


def _row(row_id: int = 1, close: float = 1.5) -> StockMarketActivity:
    return StockMarketActivity(
        id=row_id,
        dataset_id=DATASET_ID,
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
        close=close,
        volume=100,
        trade_count=10,
    )


class _ScalarRows:
    """The two accessors read_market_activity_data uses: result.scalars().all()."""

    def __init__(self, rows: list[StockMarketActivity]):
        self._rows = rows

    def scalars(self) -> '_ScalarRows':
        return self

    def all(self) -> list[StockMarketActivity]:
        return self._rows


class _ReadSession:
    def __init__(self, rows: list[StockMarketActivity]):
        self._rows = rows
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarRows(self._rows)


@pytest.mark.parametrize('dead_column', DEAD_BAR_COLUMNS)
def test_the_bar_model_carries_no_dead_column(dead_column: str):
    """tj-vhboky.11 item 14, the MODEL half. The read schema half is in schemas/tests.

    WHY THIS IS NOT ALREADY COVERED. test_upsert_refreshes_every_correctable_column notices a new
    column only when it is left OUT of MUTABLE_COLUMNS. The pre-epic model had split_factor and
    dividends_factor IN MUTABLE_COLUMNS, so a merge restoring that shape passes it; and a restored
    bar expiry that nobody refreshes is just as plausible. Asserted on the table's columns and on
    the mapped class, then on what to_schema emits, so the column cannot come back by any route.
    """
    assert dead_column not in StockMarketActivity.__table__.columns, f'the bar table has {dead_column} again'
    assert not hasattr(StockMarketActivity, dead_column), f'the bar model maps {dead_column} again'

    emitted = _row().to_schema().model_dump()
    assert dead_column not in emitted and dead_column not in emitted['data'], f'to_schema emits {dead_column}'


# Plain dicts, and the query is built INSIDE the test (tj-vhboky.26 architect gate). Built here, at
# collection, a future tightening of the query model would crash the whole module with a collection
# error instead of redding the one case it breaks.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    'query_fields',
    [
        {'dataset_id': DATASET_ID},
        {'asset_symbol': 'aapl', 'granularity': Granularity.ONE_DAY, 'start': TIMESTAMP, 'end': TIMESTAMP},
    ],
    ids=['dataset-scoped', 'symbol-granularity-range'],
)
async def test_the_read_path_converts_rows_with_to_schema(query_fields: dict):
    """tj-vhboky.11 item 15: to_schema(), not model_validate(), on the rows the read returns.

    model_validate on a flat ORM row raises -- the read schema nests OHLCV under `data`
    (test_bar_batch_guard.py::test_model_validate_on_a_stored_row_still_raises) -- so the defect
    this pins is a read that 500s on its first non-empty result. test_bar_batch_guard.py calls
    to_schema on a row directly and never reaches read_market_activity_data, so reverting the
    call inside the read is invisible there.

    "BOTH BRANCHES" (the bead's wording) no longer exists: the read was an if/else on dataset_id
    and is now one statement built from optional conditions. Both query shapes that used to pick a
    branch are driven, so a branch coming back with the old call in it is still caught. The empty
    query is deliberately NOT driven: it can no longer be constructed (tj-vhboky.26), which is
    pinned in schemas/tests/test_schemas_smoke_data_store.py and, over HTTP, test_filtering_read.py.
    """
    query = StockDataMarketActivityQuery(**query_fields)
    rows = [_row(1, close=1.5), _row(2, close=2.5)]
    db = _ReadSession(rows)

    read = await read_market_activity_data(db, query)

    assert len(db.statements) == 1
    assert all(isinstance(bar, StockDataMarketActivity) for bar in read)
    assert read == [row.to_schema() for row in rows]
    assert [bar.data.close for bar in read] == [1.5, 2.5], 'the rows were converted out of order or not at all'
