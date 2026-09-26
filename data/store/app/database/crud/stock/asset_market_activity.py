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
    db: AsyncSession, batch_asset_data: market_activity_data.BatchStockDataMarketActivityCreate
) -> int:
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

        stmt = build_market_activity_upsert(StockMarketActivity.from_batch_create(batch_asset_data))
        await db.execute(stmt)

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
