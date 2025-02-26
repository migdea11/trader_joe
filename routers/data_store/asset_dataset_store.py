from typing import Annotated, List

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.logging import get_logger
from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.crud.stock.store_dataset_entry import delete_entry_by_id, search_entries
from data.store.app.database.database import async_db
from data.store.app.ingest.data_action_request import store_market_activity_worker
from routers.data_store.app_endpoints import AssetDatasetStoreInterface
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStore,
    AssetDatasetStoreDelete,
    StoreAssetDatasetBody,
    StoreAssetDatasetPath,
    StoreAssetDatasetQuery,
)

router = APIRouter()
log = get_logger(__name__)


@router.post(AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET)
async def store_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    rpc_clients: Annotated[KafkaRpcFactory.RpcClients, Depends(get_rpc_clients)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_body: Annotated[StoreAssetDatasetBody, Body(...)]
):
    log.debug(f"Storing data for {request_path.asset_type.value}, {request_path.asset_symbol}")
    data_points = await store_market_activity_worker(request_path, request_body, db, rpc_clients)
    return {
        "message": "Data stored",
        "data_points": data_points
    }


@router.get(AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET)
async def get_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_query: Annotated[StoreAssetDatasetQuery, Query()]
) -> List[AssetDatasetStore]:
    log.debug(f"Getting data for {request_path.asset_type.value}, {request_path.asset_symbol}")
    log.debug(f"Query: {request_query}")
    return await search_entries(
        db,
        request_path,
        request_query
    )


@router.delete(AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID)
async def delete_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_path: Annotated[AssetDatasetStoreDelete, Depends()]
):
    log.debug(f"Deleting data for {request_path.id}")
    await delete_entry_by_id(db, request_path.id)
    return {"message": "Data deleted"}
