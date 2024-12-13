from fastapi import APIRouter, Body, Path

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.ingest.app.ingest_control import (
    store_retrieve_crypto,
    store_retrieve_option,
    store_retrieve_stock
)
from schemas.store_broker_data import DataRequest, DataRequestBody
from .app_endpoints import Interface

log = get_logger(__name__)

router = APIRouter()


@router.post(Interface.POST_STORE_STOCK)
async def store_data(
    asset_type: AssetType = Path(..., description="The type of the asset"),
    symbol: str = Path(..., description="The symbol of the asset"),
    body: DataRequestBody = Body(..., description="Data request parameters")
):
    asset_map = {
        AssetType.STOCK: store_retrieve_stock,
        AssetType.CRYPTO: store_retrieve_crypto,
        AssetType.OPTION: store_retrieve_option
    }
    body_dict = body.model_dump()

    request = {**body_dict, "asset_type": asset_type, "symbol": symbol}
    log.debug(request)
    asset_map[asset_type](DataRequest(**request))

    return {"message": "Data retrieval started."}
