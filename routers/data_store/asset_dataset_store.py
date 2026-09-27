from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.logging import get_logger
from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.crud.stock.store_dataset_entry import (
    EntryNotFound,
    OwnerMismatch,
    OwnOverlapConflict,
    delete_entry_by_id,
    search_entries,
)
from data.store.app.database.database import async_db
from data.store.app.ingest.data_action_request import store_market_activity_worker
from routers.common.instance_secret import require_instance_secret
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


# dependencies=[...] ON THE DECORATOR, NOT A FUNCTION PARAMETER (tj-vhboky.8, routers/common/
# instance_secret.py module docstring). Applied per route, never router-wide, so the GET below
# stays open even if a future GET is added to this router without anyone editing this file. A
# function parameter would change this route's line in
# routers/tests/interface_manifest/data_store.manifest (the validator's file); a decorator-level
# dependency injects nothing into the handler, so the manifest is unaffected.
@router.post(AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET, dependencies=[Depends(require_instance_secret)])
async def store_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    rpc_clients: Annotated[KafkaRpcFactory.RpcClients, Depends(get_rpc_clients)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_body: Annotated[StoreAssetDatasetBody, Body(...)],
):
    log.debug(f'Storing data for {request_path.asset_type.value}, {request_path.asset_symbol}')
    try:
        data_points = await store_market_activity_worker(request_path, request_body, db, rpc_clients)
    except OwnOverlapConflict as e:
        # 409, carrying the colliding id(s) (tj-vhboky.8): the whole point of the ruling is that a
        # caller catches this, reads the id(s), and issues an extend itself -- a bare 500 or an
        # unstructured message makes that unbuildable. See OwnOverlapConflict's docstring
        # (data/store/app/database/crud/stock/store_dataset_entry.py) for why this is never raised
        # for a different owner's overlapping dataset, only the SAME owner's.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'message': str(e), 'colliding_ids': [str(entry_id) for entry_id in e.colliding_ids]},
        ) from e
    return {'message': 'Data stored', 'data_points': data_points}


@router.get(AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET)
async def get_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_query: Annotated[StoreAssetDatasetQuery, Query()],
) -> list[AssetDatasetStore]:
    log.debug(f'Getting data for {request_path.asset_type.value}, {request_path.asset_symbol}')
    log.debug(f'Query: {request_query}')
    return await search_entries(db, request_path, request_query)


@router.delete(
    AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID, dependencies=[Depends(require_instance_secret)]
)
async def delete_data(
    db: Annotated[AsyncSession, Depends(async_db)], request_path: Annotated[AssetDatasetStoreDelete, Depends()]
):
    log.debug(f'Deleting data for {request_path.id}')
    try:
        await delete_entry_by_id(db, request_path.id, request_path.owner)
    except OwnerMismatch as e:
        # 403, and the body deliberately does not carry the real owner: OwnerMismatch already
        # refuses to format it (see its docstring), and a None `owner` is the degenerate case of
        # a wrong one, so an owner-less delete lands here too rather than a distinct code.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except EntryNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return {'message': 'Data deleted'}
