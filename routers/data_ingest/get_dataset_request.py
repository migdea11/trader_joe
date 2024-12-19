from fastapi import APIRouter

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.ingest.app.ingest_control import (
    store_retrieve_crypto,
    store_retrieve_option,
    store_retrieve_stock
)
from schemas.data_ingest.get_dataset_request import GetDatasetRequest, StockDatasetRequest
from .app_endpoints import Interface

log = get_logger(__name__)

router = APIRouter()


@router.post(Interface.POST_STORE_DATASET)
async def store_data(request: GetDatasetRequest):
    asset_map = {
        AssetType.STOCK: store_retrieve_stock,
        AssetType.CRYPTO: store_retrieve_crypto,
        AssetType.OPTION: store_retrieve_option
    }
    log.debug(request)
    asset_map[request.asset_type](StockDatasetRequest(**request))

    return {"message": "Data retrieval started."}
