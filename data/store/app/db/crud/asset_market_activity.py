from typing import Dict, List
from sqlalchemy import delete, select, insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.store.app.db.models.stock_market_activity import StockMarketActivity
from schemas.data_store import asset_market_activity_data

log = get_logger(__name__)


_ASSET_TABLE_MAP = {
    AssetType.STOCK: StockMarketActivity
}


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f"Asset type not supported: {asset_type}")


async def create_asset_market_activity_data(
    db: AsyncSession,
    asset_data: asset_market_activity_data.AssetMarketActivityDataCreate,
):
    log.debug("Storing asset market activity data")
    if asset_data.asset_type not in _ASSET_TABLE_MAP:
        raise UnsupportedAssetType(asset_data.asset_type)

    asset_table = _ASSET_TABLE_MAP[asset_data.asset_type]
    db_asset_market_activity_data = asset_table(**asset_data.model_dump(exclude={"asset_type"}))
    await db.add(db_asset_market_activity_data)
    await db.commit()


async def batch_insert_asset_market_activity_data(
    db: AsyncSession, batch_asset_data: Dict[AssetType, List[asset_market_activity_data.AssetMarketActivityDataCreate]]
):
    try:
        # Insert in batches for each asset type
        for asset_type, data_list in batch_asset_data.items():
            log.debug(f"Batch storing market activity: {asset_type.value}[{len(data_list)}]")
            if asset_type not in _ASSET_TABLE_MAP:
                raise UnsupportedAssetType(asset_type)

            asset_table = _ASSET_TABLE_MAP[asset_type]
            stmt = insert(asset_table).values(
                [data.model_dump(exclude={"asset_type"}) for data in data_list]
            )
            await db.execute(stmt)

        await db.commit()
        log.debug("Batch insert completed successfully")
    except Exception as e:
        await db.rollback()
        log.error(f"Failed to batch insert asset market activity data: {e}")
        raise


# TODO replace with a search function
async def read_asset_market_activity_dataset(
    db: AsyncSession, request: asset_market_activity_data.AssetMarketActivityDataGet
) -> List[asset_market_activity_data.AssetMarketActivityData]:
    log.debug("Reading stock market activity dataset")

    if request.asset_type not in _ASSET_TABLE_MAP:
        raise UnsupportedAssetType(request.asset_type)

    asset_table = _ASSET_TABLE_MAP[request.asset_type]

    # If using subset of dataset
    conditions = []
    if request.dataset_id:
        conditions.append(asset_table.dataset_id == request.dataset_id)
    if request.symbol:
        conditions.append(asset_table.symbol == request.symbol)
    if request.granularity:
        conditions.append(asset_table.granularity == request.granularity)
    if request.start:
        conditions.append(asset_table.timestamp >= request.start)
    if request.end:
        conditions.append(asset_table.timestamp <= request.end)

    stmt = select(asset_table).filter(*conditions)
    results = await db.execute(stmt)
    db_asset_market_activities = results.scalars().all()
    return [
        asset_market_activity_data.AssetMarketActivityData.model_validate(obj) for obj in db_asset_market_activities
    ]


# TODO replace with a search function
async def read_all_asset_market_activity_data(
    db: AsyncSession,
    asset_type: AssetType
) -> List[asset_market_activity_data.AssetMarketActivityData]:
    if asset_type not in _ASSET_TABLE_MAP:
        raise UnsupportedAssetType(asset_type)

    asset_table = _ASSET_TABLE_MAP[asset_type]

    query = select(asset_table)
    result = await db.execute(query)
    db_stock_market_activities = result.scalars().all()

    schema_objects = [
        asset_market_activity_data.AssetMarketActivityData.model_validate(
            {**obj.__dict__, "asset_type": AssetType.STOCK}
        ) for obj in db_stock_market_activities
    ]
    return schema_objects


async def delete_all_asset_market_activity_data(db: AsyncSession):
    log.debug("Deleting all stock market activity data")
    stmt = delete(StockMarketActivity)
    await db.execute(stmt)
    db.commit()
