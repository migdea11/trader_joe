import httpx

from fastapi import APIRouter, Body, Depends

from common.endpoints import get_endpoint_url
from common.logging import get_logger
from data.store.app.db.crud.store_dataset_entry import upsert_entry, search_entries, delete_entry_by_id
from data.store.app.db.database import get_instance
from schemas.data_ingest.get_dataset_request import GetDatasetRequestBody
from schemas.data_store.store_dataset_request import (
    StoreDatasetEntryCreate, StoreDatasetEntrySearch, StoreDatasetRequestBody, StoreDatasetRequestById, StoreDatasetRequestPath
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
    dataset_id = await upsert_entry(db, StoreDatasetEntryCreate(**request_path.model_dump(), **request_body.model_dump()))
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


@router.get(StoreDataInterface.GET_STORE_STOCK)
async def get_data(
    db=Depends(get_instance),
    request_path: StoreDatasetRequestPath = Depends(),
    request_query: StoreDatasetEntrySearch = Depends()
):
    log.debug(f"Getting data for {request_path.asset_type.value}, {request_path.symbol}")
    return await search_entries(
        db,
        request_path,
        request_query
    )


@router.delete(StoreDataInterface.DELETE_STORE_STOCK)
async def delete_data(
    db=Depends(get_instance),
    dataset_id: StoreDatasetRequestById = Depends(),
):
    log.debug(f"Deleting data for {dataset_id}")
    return await delete_entry_by_id(db, dataset_id)
