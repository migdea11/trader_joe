import httpx

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder

from common.endpoints import get_endpoint_url
from common.logging import get_logger
from data.store.app.db.crud.store_dataset_entry import upsert_entry
from schemas.data_ingest.get_dataset_request import GetDatasetRequest
from schemas.data_store.store_dataset_request import StoreDatasetEntryCreate, StoreDatasetRequest
from routers.data_ingest import app_endpoints as data_ingest_endpoints
from routers.data_store.app_endpoints import StoreDataInterface

router = APIRouter()
log = get_logger(__name__)


@router.post(StoreDataInterface.POST_STORE_STOCK)
def store_data(
    # asset_type: AssetType = Path(..., description=StoreDatasetRequest.model_fields["asset_type"].description),
    # symbol: str = Path(..., description=StoreDatasetRequest.model_fields["symbol"].description),
    # data_type: str = Path(..., description=StoreDatasetRequest.model_fields["data_type"].description),
    # body: StoreDatasetRequestBody = Body(..., description="Data request parameters")
    request: StoreDatasetRequest
):
    log.debug("Storing data for %s, %s", request.asset_type.value, request.symbol)
    dataset_id = upsert_entry(StoreDatasetEntryCreate(**request.model_dump()))

    data_ingest_request = GetDatasetRequest(**request.model_dump(), dataset_id=dataset_id)
    url = get_endpoint_url(
        data_ingest_endpoints.APP_NAME,
        data_ingest_endpoints.APP_PORT_INTERNAL,
        data_ingest_endpoints.Interface.POST_STORE_DATASET,
        data_ingest_request.extract_path()
    )
    log.debug("URL: %s", url)
    serialized_body = jsonable_encoder(data_ingest_request.extract_body_obj())

    log.debug("Serialized body: %s", serialized_body)

    response = httpx.post(url, json=serialized_body)
    response.raise_for_status()
    return response.json()
