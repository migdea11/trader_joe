import httpx

from fastapi import APIRouter, Body, HTTPException, Path
from fastapi.encoders import jsonable_encoder

from common.enums.data_select import AssetType
from common.endpoints import get_endpoint_url
from common.logging import get_logger
from schemas.store_broker_data import DataRequestBody
from routers.data_ingest import app_endpoints as data_ingest_endpoints
from routers.data_store.app_endpoints import StoreDataInterface

router = APIRouter()
log = get_logger(__name__)


@router.post(StoreDataInterface.POST_STORE_STOCK)
def store_data(
    asset_type: AssetType = Path(..., description="The type of the asset"),
    symbol: str = Path(..., description="The symbol of the asset"),
    body: DataRequestBody = Body(..., description="Data request parameters")
):
    log.debug("Storing data for %s, %s", asset_type.value, symbol)
    url = get_endpoint_url(
        data_ingest_endpoints.APP_NAME,
        data_ingest_endpoints.APP_PORT_INTERNAL,
        data_ingest_endpoints.Interface.POST_STORE_STOCK,
        {
            "asset_type": asset_type.value,
            "symbol": symbol,
        }
    )
    log.debug("URL: %s", url)
    serialized_body = jsonable_encoder(body)

    log.debug("Serialized body: %s", serialized_body)

    response = httpx.post(url, json=serialized_body)
    response.raise_for_status()
    return response.json()
