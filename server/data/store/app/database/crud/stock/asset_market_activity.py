from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import DataType
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.transaction import write_transaction
from schemas.data_store.stock import market_activity_data


log = get_logger(__name__)

# The Postgres wire protocol's hard cap on bind parameters in a single statement (tj-rpyv5u). A
# statement built from more rows than this fits refuses outright rather than degrading. Still true
# of the server; kept even though the driver below binds tighter, since a future driver without
# the int16 limit makes this one binding again.
POSTGRES_MAX_BIND_PARAMETERS = 65_535

# asyncpg's own client-side cap, enforced before the server ever sees the statement: asyncpg
# 0.31.0, asyncpg/protocol/prepared_stmt.pyx line 130, "the number of query arguments cannot
# exceed 32767". asyncpg does not export this limit, so it is a literal here rather than a runtime
# derivation (tj-vhboky.69).
ASYNCPG_MAX_QUERY_ARGUMENTS = 32_767

# The asyncpg version the limit above was verified against. Pinned so an asyncpg upgrade is loud:
# the validator checks this against the installed version in the PR gate, and a mismatch means
# re-verify the argument limit rather than drift silently.
ASYNCPG_LIMIT_VERIFIED_VERSION = '0.31.0'

# The binding ceiling is whichever of the two real limits above is smaller -- today the driver's,
# not the server's.
MAX_BIND_PARAMETERS = min(POSTGRES_MAX_BIND_PARAMETERS, ASYNCPG_MAX_QUERY_ARGUMENTS)


# UnsupportedAssetType IS GONE FROM HERE (C5, TE-6). This module's copy was never raised -- the
# only raise sites were the two `case _` branches in routers/data_store/internal_asset_data.py,
# which now raise InvalidRequestError(UNSUPPORTED_ASSET_TYPE) directly. A second class with the
# same name and no raise site is exactly the duplication the one vocabulary replaces.


class DuplicateBatchTimestamp(InvalidRequestError):
    """Raised when one batch carries two bars at the same timestamp.

    All rows in a batch share the same dataset_id, so two bars at the same timestamp collide on
    the natural key. A single INSERT ... ON CONFLICT DO UPDATE whose VALUES list holds two rows
    with the same conflict key fails outright with Postgres error 21000, "ON CONFLICT DO UPDATE
    command cannot affect row a second time", and takes the whole batch down with an opaque
    message. This defect is live on this branch today, unguarded, until this exception is raised
    here instead.

    A LEAF OF InvalidRequestError carrying DUPLICATE_BAR_TIMESTAMP (ADR tj-fa1rpu D5, TE-6): the
    batch as sent cannot be written whatever the database does, so it is the caller's to fix, and
    the sentence this class has always formatted is now the error's detail.
    """

    def __init__(self, timestamp: datetime):
        super().__init__(Reason.DUPLICATE_BAR_TIMESTAMP, f'Duplicate timestamp within batch: {timestamp}')


def _as_utc(timestamp: datetime) -> datetime:
    """Normalise a timestamp for the duplicate-timestamp guard above.

    A naive and an aware datetime naming the same instant must compare equal here, or the guard
    would miss a real duplicate.
    """
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


async def create_market_activity_data(
    db: AsyncSession, asset_data: market_activity_data.StockDataMarketActivityCreate
) -> market_activity_data.StockDataMarketActivity:
    log.debug('Storing asset market activity data')
    asset_table = StockMarketActivity
    db_asset_market_activity_data = asset_table(**asset_table.from_create(asset_data))
    async with write_transaction(db, 'store market activity'):
        db.add(db_asset_market_activity_data)
    await db.refresh(db_asset_market_activity_data)
    return db_asset_market_activity_data.to_schema()


def _resolve_chunk_size(requested_chunk_size: int | None) -> int:
    """The rows per upsert statement, bounded by the driver's argument-count limit.

    max_rows is derived from StockMarketActivity.bind_params_per_row() rather than a hard-coded
    twelve, so it re-derives itself the next time the column list changes instead of quietly going
    stale. The ceiling it divides is MAX_BIND_PARAMETERS, the smaller of two real limits: the
    Postgres wire protocol's bind-parameter cap (POSTGRES_MAX_BIND_PARAMETERS) and asyncpg's own
    client-side argument cap (ASYNCPG_MAX_QUERY_ARGUMENTS), enforced before the server ever sees
    the statement. Today the driver's limit binds. requested_chunk_size is
    MARKET_ACTIVITY_BATCH_SIZE, read by the caller (data/store/app/ingest/data_action_request.py)
    -- the setting this bug exists to make the write path actually obey. Unset, non-positive, or
    larger than the driver allows are all treated the same way: clamp to the derived safe ceiling
    and log that it happened, rather than building a statement asyncpg would refuse client-side.
    """
    max_rows = MAX_BIND_PARAMETERS // StockMarketActivity.bind_params_per_row()
    if not requested_chunk_size or requested_chunk_size <= 0:
        log.warning(
            f'MARKET_ACTIVITY_BATCH_SIZE is unset or non-positive ({requested_chunk_size!r}); '
            f'using the asyncpg-argument-limit-derived chunk size of {max_rows} rows'
        )
        return max_rows
    if requested_chunk_size > max_rows:
        log.warning(
            f"MARKET_ACTIVITY_BATCH_SIZE={requested_chunk_size} would exceed asyncpg's "
            f'{ASYNCPG_MAX_QUERY_ARGUMENTS}-argument client-side limit (Postgres itself allows '
            f'{POSTGRES_MAX_BIND_PARAMETERS}) at {StockMarketActivity.bind_params_per_row()} '
            f'params/row; clamping to {max_rows} rows'
        )
        return max_rows
    return requested_chunk_size


def build_market_activity_upsert(values: list[dict[str, Any]]) -> Insert:
    """Build the idempotent bar insert.

    A repeat of an already-stored bar collides on the natural key and refreshes the existing
    row instead of adding a second one, so re-fetching an overlapping window leaves the row
    count unchanged. DO UPDATE rather than DO NOTHING, so a vendor correction to a stored bar
    is applied rather than discarded.
    """
    stmt = insert(StockMarketActivity).values(values)
    updates = {column: getattr(stmt.excluded, column) for column in StockMarketActivity.MUTABLE_COLUMNS}
    # ON CONFLICT DO UPDATE does not exercise the column's Python-side onupdate, so updated_at
    # has to be set here explicitly. created_at is left alone: it records first storage.
    updates['updated_at'] = func.now()
    return stmt.on_conflict_do_update(constraint=StockMarketActivity.NATURAL_KEY_CONSTRAINT, set_=updates)


async def write_market_activity_in_transaction(
    db: AsyncSession,
    batch_asset_data: market_activity_data.BatchStockDataMarketActivityCreate,
    requested_chunk_size: int | None = None,
) -> int:
    """Upsert a batch of bars INSIDE A TRANSACTION THE CALLER OWNS, chunked under the bind-parameter limit.

    THIS FUNCTION NEVER COMMITS AND NEVER ROLLS BACK (tj-vz1eta s2, carried onto tj-3mk3u5.10).
    It is the one shared bar writer: the dataset fetch calls it once per page as the page arrives
    (data/store/app/ingest/data_action_request.py), inside the single transaction that also holds
    that fetch's entry upsert, and nothing is committed until the stream's FetchDone. The ledger
    work (tj-3mk3u5.34) moves that commit to once per page against this same function, and the
    external stream reuses it -- which is why the commit is the caller's and not buried here.
    batch_create_market_activity_data below is the committing wrapper.

    THE BIND-PARAMETER GUARD IS STILL IN FORCE (tj-rpyv5u). Chunking is per CALL, so a caller
    writing page by page gets the same ceiling per page that one large batch got across the
    whole batch; _resolve_chunk_size clamps whatever MARKET_ACTIVITY_BATCH_SIZE asks for to what
    the driver will actually accept.

    THE DUPLICATE-TIMESTAMP GUARD IS PER CALL, AND THAT IS A REAL NARROWING FROM THE ONE-BATCH
    PATH -- named rather than left to be discovered. It exists because a single INSERT ... ON
    CONFLICT DO UPDATE whose VALUES list holds one conflict key twice fails outright with
    Postgres 21000, and it still catches every case of that, since the whole of one call's rows
    are checked before any chunk is built. What it no longer catches is the same timestamp
    arriving in two different CALLS of one fetch -- two pages. That is not a 21000: the second
    statement's ON CONFLICT DO UPDATE refreshes the row the first wrote, which is the idempotent
    behaviour build_market_activity_upsert is built for. Carrying a per-fetch timestamp set
    across pages would mean accumulating state proportional to the stream, which is the memory
    profile the paged transport exists to remove.

    Args:
        db: The session, already inside the caller's transaction.
        batch_asset_data: The bars to upsert, all sharing one dataset_id and one feed.
        requested_chunk_size: Rows per statement, MARKET_ACTIVITY_BATCH_SIZE at the call site.
            Unset, non-positive or above the driver's ceiling are all clamped; see
            _resolve_chunk_size.

    Returns:
        int: How many bars were written. Zero for a batch carrying no market activity, which
        touches the caller's transaction not at all.

    Raises:
        DuplicateBatchTimestamp: If two bars in THIS call share a timestamp.
    """
    batch_market_activity = batch_asset_data.dataset.get(DataType.MARKET_ACTIVITY)
    if not batch_market_activity:
        log.warning('No market activity data in batch')
        return 0

    log.debug(f'Batch storing market activity[{len(batch_market_activity)}]')
    log.debug(f'Batch storing market activity: {next(iter(batch_market_activity))}')

    seen_timestamps: set[datetime] = set()
    for bar in batch_market_activity:
        normalized_timestamp = _as_utc(bar.timestamp)
        if normalized_timestamp in seen_timestamps:
            raise DuplicateBatchTimestamp(bar.timestamp)
        seen_timestamps.add(normalized_timestamp)

    values = StockMarketActivity.from_batch_create(batch_asset_data)
    chunk_size = _resolve_chunk_size(requested_chunk_size)
    # The effective chunk size and the chunk count, per write (tj-vz1eta item 3). A configured
    # size far BELOW the derived ceiling is silent otherwise -- _resolve_chunk_size only warns
    # when it has to clamp -- so a deployment paying for ten-row statements has nothing to read.
    chunk_count = -(-len(values) // chunk_size)
    log.debug(f'Chunked bar write: {len(values)} rows at chunk size {chunk_size} -> {chunk_count} statement(s)')
    for chunk_start in range(0, len(values), chunk_size):
        chunk = values[chunk_start : chunk_start + chunk_size]
        await db.execute(build_market_activity_upsert(chunk))

    log.debug('Batch insert completed successfully')
    return len(batch_market_activity)


async def batch_create_market_activity_data(
    db: AsyncSession,
    batch_asset_data: market_activity_data.BatchStockDataMarketActivityCreate,
    requested_chunk_size: int | None = None,
) -> int:
    """Upsert a batch of bars in a transaction of its own, for a caller writing nothing else.

    ALL CHUNKS SHARE ONE TRANSACTION (tj-rpyv5u): every db.execute runs inside one
    write_transaction block, so an oversized batch either lands whole (the block's single commit
    on normal exit) or (on any exception, including one raised mid-chunk) rolls back whole.
    Committing per chunk was rejected deliberately -- it would turn one failed request into a
    dataset entry claiming coverage for bars that were never written, which is worse than today's
    all-or-nothing failure.

    The empty-batch return is ABOVE the block: it neither commits nor rolls back anything, since
    there is nothing to write.

    Args:
        db: The session.
        batch_asset_data: The bars to upsert, all sharing one dataset_id and one feed.
        requested_chunk_size: Rows per statement; see write_market_activity_in_transaction.

    Returns:
        int: How many bars were written.
    """
    if not batch_asset_data.dataset.get(DataType.MARKET_ACTIVITY):
        log.warning('No market activity data in batch')
        return 0

    async with write_transaction(db, 'batch store market activity'):
        return await write_market_activity_in_transaction(db, batch_asset_data, requested_chunk_size)


async def read_market_activity_data(
    db: AsyncSession, request: market_activity_data.StockDataMarketActivityQuery
) -> list[market_activity_data.StockDataMarketActivity]:
    log.debug('Reading stock market activity dataset')
    asset_table = StockMarketActivity

    # Each predicate tests "is not None", not truthiness: a falsy-but-set value (an enum member
    # whose value is falsy, a UUID, etc.) must still filter. An absent field means "no constraint
    # on that column" (tj-vhboky.1 section 8).
    conditions = []
    if request.dataset_id is not None:
        # This filter is trustworthy again. dataset_id is part of the bar's natural key
        # (BaseMarketActivity.NATURAL_KEY) and is NOT in StockMarketActivity.MUTABLE_COLUMNS, so
        # an overlapping re-fetch through a different dataset entry writes a DIFFERENT ROW rather
        # than re-owning this one. The last-write-wins ownership that made this filter lie
        # (tj-k207b7) is gone by construction, not by discipline.
        conditions.append(asset_table.dataset_id == request.dataset_id)
    if request.asset_symbol is not None:
        conditions.append(asset_table.asset_symbol == request.asset_symbol)
    if request.source is not None:
        conditions.append(asset_table.source == request.source)
    if request.feed is not None:
        conditions.append(asset_table.feed == request.feed)
    if request.granularity is not None:
        conditions.append(asset_table.granularity == request.granularity)
    if request.start is not None:
        conditions.append(asset_table.timestamp >= request.start)
    if request.end is not None:
        # Half-open [start, end): a bar AT end belongs to the next range (tj-vhboky.1 addendum
        # HALF-OPEN RANGES, 2026-09-30).
        conditions.append(asset_table.timestamp < request.end)

    # ORDER BY timestamp, dataset_id -- NOTHING ELSE (user ruling, tj-vhboky.25 addendum, 21:41
    # UTC 2026-09-27). No row id: this is deterministic today because one feed serves one
    # deployment, and by construction once feed joins the dataset entry's identity (tj-rh4b7f),
    # since a dataset then has exactly one feed.
    stmt = select(asset_table).filter(*conditions).order_by(asset_table.timestamp, asset_table.dataset_id)
    results = await db.execute(stmt)
    db_asset_market_activities = results.scalars().all()
    return [obj.to_schema() for obj in db_asset_market_activities]
