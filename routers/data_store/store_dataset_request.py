from typing import Annotated, List

from fastapi import APIRouter, Body, Depends, Query

from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.logging import get_logger
from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.crud.store_dataset_entry import search_entries, delete_entry_by_id
from data.store.app.database.database import async_db
from data.store.app.ingest.data_action_request import store_market_activity_worker
from schemas.data_store.store_dataset_request import (
    StoreDatasetEntry,
    StoreDatasetEntrySearch,
    StoreDatasetRequestBody,
    StoreDatasetRequestById,
    StoreDatasetRequestPath
)
from routers.data_store.app_endpoints import StoreDataInterface

router = APIRouter()
log = get_logger(__name__)


@router.post(StoreDataInterface.POST_STORE_STOCK)
async def store_data(
    request_path: StoreDatasetRequestPath = Depends(),
    request_body: StoreDatasetRequestBody = Body(...),
    db=Depends(async_db),
    rpc_clients: KafkaRpcFactory.RpcClients = Depends(get_rpc_clients)
):
    log.debug(f"Storing data for {request_path.asset_type.value}, {request_path.symbol}")
    data_points = await store_market_activity_worker(request_path, request_body, db, rpc_clients)
    return {
        "message": "Data stored",
        "data_points": data_points
    }


@router.get(StoreDataInterface.GET_STORE_STOCK)
async def get_data(
    request_query: Annotated[StoreDatasetEntrySearch, Query()],
    request_path: StoreDatasetRequestPath = Depends(),
    db=Depends(async_db)
) -> List[StoreDatasetEntry]:
    log.debug(f"Getting data for {request_path.asset_type.value}, {request_path.symbol}")
    log.debug(f"Query: {request_query}")
    return await search_entries(
        db,
        request_path,
        request_query
    )


@router.delete(StoreDataInterface.DELETE_STORE_STOCK)
async def delete_data(
    db=Depends(async_db),
    request_path: StoreDatasetRequestById = Depends(),
):
    log.debug(f"Deleting data for {request_path.dataset_id}")
    await delete_entry_by_id(db, request_path.dataset_id)
    return {"message": "Data deleted"}
