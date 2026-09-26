from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from schemas.data_store.stock import market_activity_data


log = get_logger(__name__)

# The Postgres wire protocol's hard cap on bind parameters in a single statement (tj-rpyv5u). A
# statement built from more rows than this fits refuses outright rather than degrading.
POSTGRES_MAX_BIND_PARAMETERS = 65_535


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f'Asset type not supported: {asset_type}')


class DuplicateBatchTimestamp(ValueError):
    """Raised when one batch carries two bars at the same timestamp.

    All rows in a batch share the same dataset_id, so two bars at the same timestamp collide on
    the natural key. A single INSERT ... ON CONFLICT DO UPDATE whose VALUES list holds two rows
    with the same conflict key fails outright with Postgres error 21000, "ON CONFLICT DO UPDATE
    command cannot affect row a second time", and takes the whole batch down with an opaque
    message. This defect is live on this branch today, unguarded, until this exception is raised
    here instead.
    """

    def __init__(self, timestamp: datetime):
        super().__init__(f'Duplicate timestamp within batch: {timestamp}')


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
    db.add(db_asset_market_activity_data)
    await db.commit()
    await db.refresh(db_asset_market_activity_data)
    return db_asset_market_activity_data.to_schema()


def _resolve_chunk_size(requested_chunk_size: int | None) -> int:
    """The rows per upsert statement, bounded by the wire-protocol limit.

    protocol_max_rows is derived from StockMarketActivity.bind_params_per_row() rather than a
    hard-coded twelve, so it re-derives itself the next time the column list changes instead of
    quietly going stale. requested_chunk_size is MARKET_ACTIVITY_BATCH_SIZE, read by the caller
    (data/store/app/ingest/data_action_request.py) -- the setting this bug exists to make the
    write path actually obey. Unset, non-positive, or larger than the protocol allows are all
    treated the same way: clamp to the derived safe ceiling and log that it happened, rather than
    building a statement Postgres would reject.
    """
    protocol_max_rows = POSTGRES_MAX_BIND_PARAMETERS // StockMarketActivity.bind_params_per_row()
    if not requested_chunk_size or requested_chunk_size <= 0:
        log.warning(
            f'MARKET_ACTIVITY_BATCH_SIZE is unset or non-positive ({requested_chunk_size!r}); '
            f'using the wire-protocol-derived chunk size of {protocol_max_rows} rows'
        )
        return protocol_max_rows
    if requested_chunk_size > protocol_max_rows:
        log.warning(
            f'MARKET_ACTIVITY_BATCH_SIZE={requested_chunk_size} would exceed the '
            f'{POSTGRES_MAX_BIND_PARAMETERS}-bind-parameter limit at '
            f'{StockMarketActivity.bind_params_per_row()} params/row; clamping to {protocol_max_rows} rows'
        )
        return protocol_max_rows
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


async def batch_create_market_activity_data(
    db: AsyncSession,
    batch_asset_data: market_activity_data.BatchStockDataMarketActivityCreate,
    requested_chunk_size: int | None = None,
) -> int:
    """Upsert a batch of bars, chunked to stay under the wire-protocol bind-parameter limit.

    ALL CHUNKS SHARE ONE TRANSACTION (tj-rpyv5u): every db.execute below runs before the single
    db.commit() at the end, so an oversized batch either lands whole or (on any exception,
    including one raised mid-chunk) rolls back whole. Committing per chunk was rejected
    deliberately -- it would turn one failed request into a dataset entry claiming coverage for
    bars that were never written, which is worse than today's all-or-nothing failure.

    The duplicate-timestamp guard runs across the WHOLE batch before any chunk is built, not
    per chunk: two bars at the same timestamp landing in different chunks would each pass a
    per-chunk guard and still collide on the natural key.
    """
    try:
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
        for chunk_start in range(0, len(values), chunk_size):
            chunk = values[chunk_start : chunk_start + chunk_size]
            await db.execute(build_market_activity_upsert(chunk))

        await db.commit()
        log.debug('Batch insert completed successfully')
        return len(batch_market_activity)
    except Exception as e:
        await db.rollback()
        log.error(f'Failed to batch insert asset market activity data: {e}')
        raise


# TODO replace with a search function
async def read_market_activity_data(
    db: AsyncSession, request: market_activity_data.StockDataMarketActivityQuery
) -> list[market_activity_data.StockDataMarketActivity]:
    log.debug('Reading stock market activity dataset')
    asset_table = StockMarketActivity

    # If using subset of dataset
    conditions = []
    if request.dataset_id:
        # This filter is trustworthy again. dataset_id is part of the bar's natural key
        # (BaseMarketActivity.NATURAL_KEY) and is NOT in StockMarketActivity.MUTABLE_COLUMNS, so
        # an overlapping re-fetch through a different dataset entry writes a DIFFERENT ROW rather
        # than re-owning this one. The last-write-wins ownership that made this filter lie
        # (tj-k207b7) is gone by construction, not by discipline.
        conditions.append(asset_table.dataset_id == request.dataset_id)
    if request.asset_symbol:
        conditions.append(asset_table.asset_symbol == request.asset_symbol)
    if request.granularity:
        conditions.append(asset_table.granularity == request.granularity)
    if request.start:
        conditions.append(asset_table.timestamp >= request.start)
    if request.end:
        conditions.append(asset_table.timestamp <= request.end)

    stmt = select(asset_table).filter(*conditions)
    results = await db.execute(stmt)
    db_asset_market_activities = results.scalars().all()
    return [obj.to_schema() for obj in db_asset_market_activities]
