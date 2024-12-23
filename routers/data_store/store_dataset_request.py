import httpx

from fastapi import APIRouter, Body, Depends
from fastapi.encoders import jsonable_encoder

from common.endpoints import get_endpoint_url
from common.logging import get_logger
from data.store.app.db.crud.store_dataset_entry import upsert_entry
from data.store.app.db.database import get_instance
from schemas.data_ingest.get_dataset_request import GetDatasetRequestBody
from schemas.data_store.store_dataset_request import (
    StoreDatasetEntryCreate, StoreDatasetRequestBody, StoreDatasetRequestPath
)
from routers.data_ingest import app_endpoints as data_ingest_endpoints
from routers.data_store.app_endpoints import StoreDataInterface

router = APIRouter()
log = get_logger(__name__)


@router.post(StoreDataInterface.POST_STORE_STOCK)
async def store_data(
    db=Depends(get_instance),
    request_path: StoreDatasetRequestPath = Depends(),
    request_body: StoreDatasetRequestBody = Body(...),
):
    log.debug(f"Storing data for {request_path.asset_type.value}, {request_path.symbol}")
    dataset_id = upsert_entry(db, StoreDatasetEntryCreate(**request_path.model_dump(), **request_body.model_dump()))
    data_ingest_request = GetDatasetRequestBody(
        **request_body.model_dump(), dataset_id=dataset_id
    )

    # TODO add functionality to avoid redundant data storage
    url = get_endpoint_url(
        data_ingest_endpoints.APP_NAME,
        data_ingest_endpoints.APP_PORT_INTERNAL,
        data_ingest_endpoints.Interface.POST_STORE_DATASET,
        request_path.model_dump(mode='json')
    )
    log.debug(f"URL: {url}")
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data_ingest_request.model_dump(mode='json'))
        response.raise_for_status()
        return response.json()
