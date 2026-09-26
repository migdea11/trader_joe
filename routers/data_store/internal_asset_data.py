from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from data.store.app.database.crud.stock import asset_market_activity as crud_stock_market_activity
from data.store.app.database.database import async_db
from routers.common.instance_secret import require_instance_secret
from routers.data_store.app_endpoints import AssetDataInterface
from schemas.data_store.asset_data_interface import AssetDataPath
from schemas.data_store.stock.market_activity_data import (
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityQuery,
)


router = APIRouter()
log = get_logger(__name__)


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f'Asset type not supported: {asset_type}')


# dependencies=[...] ON THE DECORATOR, NOT A FUNCTION PARAMETER (tj-vhboky.8, routers/common/
# instance_secret.py). This is the ONLY write route in this module -- the GET below stays open --
# and applying it per route rather than router-wide is deliberate: a GET added later to this
# router must not silently inherit it. A function parameter would also change this route's entry
# in routers/tests/interface_manifest/data_store.manifest (the validator's file, ADR tj-8fxxfb);
# a decorator-level dependency injects nothing into the handler, so the manifest is unaffected.
@router.post(
    AssetDataInterface.POST_ASSET_DATA,
    response_model=StockDataMarketActivity,
    dependencies=[Depends(require_instance_secret)],
)
async def create_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    asset_data: Annotated[dict, Body(...)],
):
    log.debug(f'Storing data: /{asset_path.asset_type}/{asset_path.data_type}')
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            stock_market_activity = StockDataMarketActivityCreate(**asset_data)
            return await crud_stock_market_activity.create_market_activity_data(db, stock_market_activity)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)


@router.get(AssetDataInterface.GET_ASSET_DATA, response_model=list[StockDataMarketActivity])
async def read_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)], asset_path: Annotated[AssetDataPath, Depends()]
):
    """Read stock market activity data.

    Repointed at read_market_activity_data (tj-vhboky.8 / tj-xoz4ll): the function this route used
    to call, read_all_asset_market_activity_data, no longer exists -- tj-vhboky.5 deleted it, and
    this route was left pointing at the deleted name, a 500 on every call.

    NO QUERY DEPENDENCY IS BOUND HERE, DELIBERATELY, THOUGH read_market_activity_data ACCEPTS ONE.
    The architect's plan (tj-vhboky.8 Amendment 2, item S) asks for a real optional query
    (asset_symbol, granularity, start, end, dataset_id) bound to this route. That is parked: the
    user has an open question about whether the filtering read should be wired end to end, and a
    separate task owns it. Binding StockDataMarketActivityQuery as a route parameter here would
    also change this route's request signature and therefore its line in
    routers/tests/interface_manifest/data_store.manifest -- the validator's file, out of scope for
    a builder-store change. See the handback for the full reasoning.

    THE CONSEQUENCE: an empty StockDataMarketActivityQuery() is built here with every field unset,
    which read_market_activity_data treats as "no constraint on that column" -- so this route
    still reads every row of stock_market_activity, unfiltered and unbounded, exactly as it did
    before this fix. Only the function it reaches is now one that exists. Do not read the absence
    of a 500 as the full-table-scan defect (tj-xoz4ll) being closed; it is not.
    """
    log.debug(f'Reading data: /{asset_path.asset_type}/{asset_path.data_type}')
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            return await crud_stock_market_activity.read_market_activity_data(db, StockDataMarketActivityQuery())
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)
