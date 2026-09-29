"""The bar batch write against real Postgres: the chunk clamp and one transaction (tj-vhboky.51, Sys-4).

tj-vhboky.14 item 8, its crud halves. Design: tj-vhboky.7 and tj-rpyv5u (the batch is chunked
under the bind-parameter limit, and every chunk shares ONE transaction), tj-vhboky.41 S1 and
Addendum 1 (write_transaction: one commit on normal exit, one rollback on any error). Item 8's
HTTP half, a real ingest large enough to need chunking, belongs to Sys-7 (tj-vhboky.63).

WHY BELOW HTTP. These are component tests of data_store's database layer against a real
database (user ruling D3 on tj-vhboky.47). What they ask is a property of
batch_create_market_activity_data and of the driver underneath it, not a question of reach: how
many rows each statement carries once the requested chunk size is clamped, whether Postgres and
asyncpg accept those statements, and whether a failure on a later chunk leaves anything behind.
The crud coroutine is called directly, on an AsyncSession over asyncpg -- the driver data_store
runs -- so nothing between the test and the database is faked. These tests stay here after the
fake broker lands; only the items that needed a broker moved to Sys-7 (tj-vhboky.54).

WHAT "ONE TRANSACTION" IS MEASURED BY. Not by counting calls on a fake, which
test_bar_batch_chunking.py already does. Here, by Postgres itself: every row a transaction
inserts carries that transaction's id in its xmin system column, so a multi-chunk batch that
committed once leaves ONE distinct xmin across all its rows, and a per-chunk commit would leave
one per chunk. The engine's commit event is counted as well.

THE FAILURE ON CHUNK k. The whole-batch guard refuses two bars at one timestamp, and dataset_id,
symbol, source, feed and granularity are one value per batch, so neither a natural-key collision
nor a foreign-key violation can be placed on a LATER chunk only. The failing row is therefore the
batch's last bar, carrying a volume one past the int4 range of stock_market_activity.volume.
asyncpg refuses it while binding that chunk's statement, after the earlier chunks' INSERTs have
already executed on the server inside the open transaction; the test observes both counts
through the engine's cursor events rather than assuming them. If those rows survive, the batch
committed before it finished.

ROWS. Every entry comes from insert_entry under a symbol of this run, so the session teardown's
delete-by-id and the ON DELETE CASCADE take every bar written here. Nothing is truncated.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.crud.stock.asset_market_activity import (
    ASYNCPG_MAX_QUERY_ARGUMENTS,
    MAX_BIND_PARAMETERS,
    POSTGRES_MAX_BIND_PARAMETERS,
    _resolve_chunk_size,
    batch_create_market_activity_data,
    build_market_activity_upsert,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
ENTRY_TABLE = StoreDatasetEntry.__table__

# Both read from the code, never restated: the ceiling is what the write path derives, from
# MAX_BIND_PARAMETERS, the smaller of the server's bind cap and asyncpg's argument cap.
#
# SUPERSEDED (tj-vhboky.69): this used to divide POSTGRES_MAX_BIND_PARAMETERS, 65,535 // 12 = 5461
# rows, on the reasoning that the server's cap was the only one. asyncpg refuses above 32,767
# arguments before the server is reached, so a 5461-row chunk never reaches Postgres; since the
# clamp moved to MAX_BIND_PARAMETERS the ceiling is 2730 rows, and this module follows it.
PARAMS_PER_ROW = StockMarketActivity.bind_params_per_row()
CEILING_ROWS = MAX_BIND_PARAMETERS // PARAMS_PER_ROW

# More rows than one ceiling-sized statement holds, with a partial last chunk either way.
OVERSIZED_BAR_COUNT = CEILING_ROWS + CEILING_ROWS // 2 + 1
SMALL_CHUNK = 500
ABOVE_CEILING_CHUNK = CEILING_ROWS * 3

# ASYNCPG_MAX_QUERY_ARGUMENTS is imported from production, not restated. It used to be a local
# 32_767 here, which could only agree with itself. The literal in production is pinned against the
# installed driver's own source and version in data/store/tests/test_bar_batch_chunking.py.

# One past the int4 range of stock_market_activity.volume (Integer). The poison for chunk k.
INT4_OVERFLOW = 2**31

BAR_DATA = StockDataMarketActivityData(open=10.0, high=12.5, low=9.25, close=11.0, volume=1_000, trade_count=42)


def _batch(
    dataset_id: UUID,
    asset_symbol: str,
    source: DataSource,
    granularity: Granularity,
    start: datetime,
    bar_count: int,
    *,
    last_volume: int | None = None,
) -> BatchStockDataMarketActivityCreate:
    """bar_count IEX bars one minute apart from `start`; the last one's volume replaced when given."""
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=dataset_id,
        asset_symbol=asset_symbol,
        source=source,
        feed=Feed.IEX,
        granularity=granularity,
        dataset={},
    )
    for index in range(bar_count):
        data = BAR_DATA
        if last_volume is not None and index == bar_count - 1:
            data = BAR_DATA.model_copy(update={'volume': last_volume})
        batch.append_data(DataType.MARKET_ACTIVITY, data, start + timedelta(minutes=index))
    return batch


def _batch_for(entry: sa.Row, bar_count: int, *, last_volume: int | None = None) -> BatchStockDataMarketActivityCreate:
    """bar_count bars under `entry`, from the entry's start, identity copied from the entry row."""
    return _batch(
        entry.id, entry.asset_symbol, entry.source, entry.granularity, entry.start, bar_count, last_volume=last_volume
    )


def _compiled_arguments(row_count: int) -> int:
    """The positional arguments the real upsert binds for row_count bars, under data_store's asyncpg dialect."""
    rows = StockMarketActivity.from_batch_create(
        _batch(
            uuid4(),
            'ZZSYSSTATIC',
            DataSource.ALPACA_API,
            Granularity.ONE_MINUTE,
            datetime(2001, 2, 5, 14, 30, tzinfo=UTC),
            row_count,
        )
    )
    compiled = build_market_activity_upsert(rows).compile(dialect=asyncpg_dialect.dialect())
    return len(compiled.positiontup)


def _chunk_layout(total: int, chunk: int) -> list[int]:
    """The rows each statement should carry: full chunks, then the remainder."""
    full, remainder = divmod(total, chunk)
    return [chunk] * full + ([remainder] if remainder else [])


@dataclass
class BarStatements:
    """What the engine saw of the bar INSERTs: rows per statement attempted, per statement completed, commits."""

    attempted: list[int] = field(default_factory=list)
    completed: list[int] = field(default_factory=list)
    commits: int = 0


@pytest.fixture
def bar_statements(pg_async_engine: AsyncEngine) -> Iterator[BarStatements]:
    """Record every INSERT into stock_market_activity and every commit on the asyncpg engine.

    before_cursor_execute fires as a statement is handed to the driver; after_cursor_execute
    fires only once the driver has executed it on the server. asyncpg binds positionally, so a
    statement's parameter count divided by PARAMS_PER_ROW is its row count.
    """
    seen = BarStatements()
    prefix = f'INSERT INTO {BAR_TABLE.name} '

    def _before(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.startswith(prefix):
            seen.attempted.append(len(parameters) // PARAMS_PER_ROW)

    def _after(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.startswith(prefix):
            seen.completed.append(len(parameters) // PARAMS_PER_ROW)

    def _commit(conn) -> None:
        seen.commits += 1

    sync_engine = pg_async_engine.sync_engine
    listeners: list[tuple[str, Callable[..., None]]] = [
        ('before_cursor_execute', _before),
        ('after_cursor_execute', _after),
        ('commit', _commit),
    ]
    for name, listener in listeners:
        sa.event.listen(sync_engine, name, listener)
    yield seen
    for name, listener in listeners:
        sa.event.remove(sync_engine, name, listener)


def _session(engine: AsyncEngine) -> AsyncSession:
    """A session configured as data_store's own (common/database/postgres_tools.py: autoflush off)."""
    return AsyncSession(engine, autoflush=False)


def _stored(pg_engine: Engine, dataset_id: Any) -> tuple[set[datetime], int]:
    """The timestamps stored under dataset_id, and how many distinct transactions inserted them."""
    with pg_engine.connect() as conn:
        timestamps = set(
            conn.execute(sa.select(BAR_TABLE.c.timestamp).where(BAR_TABLE.c.dataset_id == dataset_id)).scalars()
        )
        # xmin is a system column the model does not map, hence text; the bind is typed with the
        # column's own UUID type so psycopg2 receives a value it can adapt.
        transactions = conn.execute(
            sa.text(
                f'SELECT count(DISTINCT xmin::text) FROM {BAR_TABLE.name} WHERE dataset_id = :dataset_id'
            ).bindparams(sa.bindparam('dataset_id', dataset_id, type_=BAR_TABLE.c.dataset_id.type))
        ).scalar_one()
    return timestamps, transactions


def test_the_sizes_this_module_relies_on() -> None:
    """The batch really is oversized and the two requested sizes really sit either side of the ceiling.

    Guards the parametrisation below: if the column list shrank far enough, or SMALL_CHUNK were
    edited above the ceiling, the clamp tests would stop testing a clamp without failing.

    SUPERSEDED (tj-vhboky.69): the last line used to expect the server-derived 5461 rows, and went
    red when the resolver moved to MAX_BIND_PARAMETERS (2730 rows). The assertion is unchanged; the
    ceiling it compares against now comes from the same limit the resolver divides, so a resolver
    pointed back at the server's 65,535 reds here.
    """
    assert OVERSIZED_BAR_COUNT > CEILING_ROWS, 'the batch must need more than one ceiling-sized statement'
    assert 0 < SMALL_CHUNK < CEILING_ROWS
    assert ABOVE_CEILING_CHUNK > CEILING_ROWS
    assert _resolve_chunk_size(SMALL_CHUNK) == SMALL_CHUNK
    assert _resolve_chunk_size(ABOVE_CEILING_CHUNK) == CEILING_ROWS


def test_the_clamped_chunk_fits_the_asyncpg_argument_limit() -> None:
    """The largest statement the clamp allows is one asyncpg will bind. No database needed.

    Compiles the real upsert for one ceiling-sized chunk with data_store's own dialect
    (postgresql+asyncpg) and counts the positional arguments it binds. The clamp exists so the
    write path never builds a statement the wire refuses (tj-rpyv5u); asyncpg refuses above
    ASYNCPG_MAX_QUERY_ARGUMENTS before the server sees anything, so that is the limit a chunk
    has to meet here, not the server's 65,535.

    SUPERSEDED (tj-vhboky.69): this test was written while the clamp still divided the server's
    POSTGRES_MAX_BIND_PARAMETERS, and it was red for exactly that reason: 5461 rows bind 65,532
    arguments. It is what exposed the defect. The limit is now imported from production rather than
    restated as a local 32_767; the literal itself is pinned against the installed asyncpg's source
    and version in data/store/tests/test_bar_batch_chunking.py, so importing it here does not make
    this test agree with itself.

    NOT VACUOUS. A chunk sized off the server's limit alone is compiled as well and must exceed
    asyncpg's cap. If the two limits ever stopped differing, the first assertion would pass for any
    ceiling up to the server's, and this test could no longer tell the two ceilings apart; the
    second assertion goes red instead of letting that happen quietly.
    """
    arguments = _compiled_arguments(_resolve_chunk_size(ABOVE_CEILING_CHUNK))
    assert arguments <= ASYNCPG_MAX_QUERY_ARGUMENTS, (
        f'a clamped chunk of {_resolve_chunk_size(ABOVE_CEILING_CHUNK)} rows binds {arguments} arguments '
        f'({PARAMS_PER_ROW} per row); asyncpg refuses more than {ASYNCPG_MAX_QUERY_ARGUMENTS}. The clamp must '
        f'divide MAX_BIND_PARAMETERS={MAX_BIND_PARAMETERS}, not POSTGRES_MAX_BIND_PARAMETERS='
        f'{POSTGRES_MAX_BIND_PARAMETERS}.'
    )

    server_only_rows = POSTGRES_MAX_BIND_PARAMETERS // PARAMS_PER_ROW
    assert _compiled_arguments(server_only_rows) > ASYNCPG_MAX_QUERY_ARGUMENTS, (
        f'a {server_only_rows}-row chunk, sized off the server limit alone, fits asyncpg too: the two limits no '
        'longer differ, so the assertion above cannot distinguish the two ceilings'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('requested', 'effective'),
    [(SMALL_CHUNK, SMALL_CHUNK), (ABOVE_CEILING_CHUNK, CEILING_ROWS)],
    ids=['small-chunk-honoured', 'above-ceiling-clamped'],
)
async def test_an_oversized_batch_lands_whole_in_one_transaction(
    requested: int,
    effective: int,
    pg_async_engine: AsyncEngine,
    pg_engine: Engine,
    insert_entry: Callable[..., sa.Row],
    own_symbol: str,
    bar_statements: BarStatements,
) -> None:
    """More bars than one statement may carry: every row stored, chunked as requested or clamped, one commit.

    The small chunk is honoured as given; the chunk above the ceiling is clamped to it. Either
    way Postgres must accept every statement -- the point of the clamp is that a real server does
    -- and every bar must be stored, by exactly one transaction.
    """
    entry = insert_entry(asset_symbol=own_symbol)
    batch = _batch_for(entry, OVERSIZED_BAR_COUNT)
    sent = {entry.start + timedelta(minutes=index) for index in range(OVERSIZED_BAR_COUNT)}

    async with _session(pg_async_engine) as session:
        written = await batch_create_market_activity_data(session, batch, requested_chunk_size=requested)

    expected_layout = _chunk_layout(OVERSIZED_BAR_COUNT, effective)
    assert written == OVERSIZED_BAR_COUNT
    assert bar_statements.completed == expected_layout, (
        f'requested {requested}: expected statements of {expected_layout} rows, the server executed '
        f'{bar_statements.completed} (attempted {bar_statements.attempted})'
    )
    assert len(expected_layout) > 1, 'the batch must have taken more than one statement'
    assert bar_statements.commits == 1, f'{bar_statements.commits} commits for one batch'

    stored, transactions = _stored(pg_engine, entry.id)
    assert len(stored) == OVERSIZED_BAR_COUNT, f'{len(stored)} of {OVERSIZED_BAR_COUNT} bars stored'
    assert stored == sent, f'{len(sent - stored)} sent bars missing, {len(stored - sent)} unexpected'
    assert transactions == 1, (
        f'{OVERSIZED_BAR_COUNT} bars in {len(expected_layout)} statements were inserted by {transactions} '
        'transactions (distinct xmin); one transaction was required'
    )


@pytest.mark.asyncio
async def test_a_failure_on_a_later_chunk_leaves_nothing_in_the_database(
    pg_async_engine: AsyncEngine,
    pg_engine: Engine,
    insert_entry: Callable[..., sa.Row],
    own_symbol: str,
    bar_statements: BarStatements,
) -> None:
    """Chunks 1..k-1 execute on the server, chunk k fails: not one row survives, and nothing commits.

    Four chunks of SMALL_CHUNK // 5 rows, the last bar's volume outside int4. The earlier chunks'
    completion is observed, not assumed, so a pass cannot come from a batch that failed before
    writing anything.
    """
    chunk = SMALL_CHUNK // 5
    bar_count = chunk * 3 + chunk // 2
    layout = _chunk_layout(bar_count, chunk)
    entry = insert_entry(asset_symbol=own_symbol)
    batch = _batch_for(entry, bar_count, last_volume=INT4_OVERFLOW)

    async with _session(pg_async_engine) as session:
        with pytest.raises(sa.exc.DBAPIError) as raised:
            await batch_create_market_activity_data(session, batch, requested_chunk_size=chunk)

    assert bar_statements.attempted == layout, (
        f'expected every chunk {layout} to be attempted, saw {bar_statements.attempted}; '
        f'error: {type(raised.value).__name__}'
    )
    assert bar_statements.completed == layout[:-1], (
        f'expected chunks {layout[:-1]} to execute on the server before the last one failed, saw '
        f'{bar_statements.completed}; error: {type(raised.value).__name__}'
    )
    assert bar_statements.commits == 0, f'{bar_statements.commits} commits on a failed batch'

    stored, _ = _stored(pg_engine, entry.id)
    assert not stored, (
        f'{len(stored)} bars survived a batch whose chunk {len(layout)} of {len(layout)} failed; '
        f'{sum(layout[:-1])} rows from the earlier chunks had executed'
    )
    with pg_engine.connect() as conn:
        entries = conn.execute(
            sa.select(sa.func.count()).select_from(ENTRY_TABLE).where(ENTRY_TABLE.c.id == entry.id)
        ).scalar_one()
    assert entries == 1, 'the entry the batch was written under must be untouched by the rollback'
