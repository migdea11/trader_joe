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
    return [market_activity_data.StockDataMarketActivity.model_validate(obj) for obj in db_asset_market_activities]


# TODO replace with a search function
async def read_all_asset_market_activity_data(db: AsyncSession) -> list[market_activity_data.StockDataMarketActivity]:
    log.debug('Reading all stock market activity dataset')
    asset_table = StockMarketActivity

    query = select(asset_table)
    result = await db.execute(query)
    db_stock_market_activities = result.scalars().all()

    schema_objects = [model_data.to_schema() for model_data in db_stock_market_activities]
    return schema_objects
