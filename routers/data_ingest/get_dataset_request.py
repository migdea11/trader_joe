from fastapi import APIRouter, Body, Depends

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.ingest.app.ingest_control import (
    store_retrieve_crypto,
    store_retrieve_option,
    store_retrieve_stock
)
from schemas.data_ingest.get_dataset_request import GetDatasetRequestBody, GetDatasetRequestPath, StockDatasetRequest
from .app_endpoints import Interface

log = get_logger(__name__)

router = APIRouter()


@router.post(Interface.POST_STORE_DATASET)
async def store_data(
    request_path: GetDatasetRequestPath = Depends(),
    request_body: GetDatasetRequestBody = Body(...)
):
    asset_map = {
        AssetType.STOCK: store_retrieve_stock,
        AssetType.CRYPTO: store_retrieve_crypto,
        AssetType.OPTION: store_retrieve_option
    }
    log.debug(request_path)
    asset_map[request_path.asset_type](StockDatasetRequest(**request_path.model_dump(), **request_body.model_dump()))

    return {"message": "Data retrieval started."}
