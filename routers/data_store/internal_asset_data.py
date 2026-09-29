from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
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
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    asset_query: Annotated[StockDataMarketActivityQuery, Query()],
):
    """Read stock market activity data.

    Repointed at read_market_activity_data (tj-vhboky.8 / tj-xoz4ll): the function this route used
    to call, read_all_asset_market_activity_data, no longer exists -- tj-vhboky.5 deleted it, and
    this route was left pointing at the deleted name, a 500 on every call.

    THE QUERY IS BOUND WITH Query(), NOT Depends() (tj-vhboky.25 item 1, measured on FastAPI
    0.141.1): Query() honours the model's extra='forbid', so an unknown query parameter is a 422
    rather than being silently dropped, which is the same defect class as tj-6z03hd.

    dataset_id, asset_symbol, source, feed and granularity each filter on an exact match when
    given; an absent parameter puts no constraint on that column (tj-vhboky.1 section 8). start
    and end bound the timestamp range and are INCLUSIVE (timestamp >= start, timestamp <= end);
    both are AwareDatetime, so a naive start or end is refused with a 422 (loc ['query',
    '<field>'], type timezone_aware), and an unknown parameter is refused with a 422 (loc ['query',
    '<name>'], type extra_forbidden), both before the handler runs. Results are ordered by
    timestamp, then dataset_id, and no other column (user ruling, tj-vhboky.25 addendum) --
    deterministic today because one feed serves one deployment, and by construction once feed
    joins the dataset entry's identity (tj-rh4b7f), since a dataset then has exactly one feed.

    A QUERY NAMING NEITHER dataset_id NOR asset_symbol IS REFUSED: a model_validator on the shared
    AssetDataQuery (tj-vhboky.26) raises before the handler runs, reported as a 422 (loc ['query'],
    type value_error), so an unbounded read of the whole table cannot be expressed. THE SAME RULE
    ALSO REFUSES A BLANK asset_symbol ('' or whitespace only), reported with the same 422 (loc
    ['query'], type value_error), even when dataset_id is given (tj-vhboky.28): a blank symbol
    names no symbol, so it is treated as if neither selector had been named.
    """
    log.debug(f'Reading data: /{asset_path.asset_type}/{asset_path.data_type}')
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            return await crud_stock_market_activity.read_market_activity_data(db, asset_query)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)
