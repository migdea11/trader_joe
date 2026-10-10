from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from common.rpc.clients.ingest_fetch import IngestFetchClient
from data.store.app.app_depends import get_ingest_fetch_client
from data.store.app.database.crud.stock.store_dataset_entry import delete_entry_by_id, get_entry_by_id, search_entries
from data.store.app.database.database import async_db
from data.store.app.ingest.data_action_request import store_market_activity_worker
from routers.common.instance_secret import require_instance_secret
from routers.data_store.app_endpoints import AssetDatasetStoreInterface
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStore,
    AssetDatasetStoreDelete,
    AssetDatasetStoreGetById,
    ServedRange,
    StoreAssetDatasetBody,
    StoreAssetDatasetPath,
    StoreAssetDatasetQuery,
    StoreAssetDatasetResponse,
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
    # THE SEAM, AS THE INTERFACE (ADR tj-8konfu D3). This route never touches a generated stub --
    # ruff's TID251 bans trader_joe.proto outside common/rpc, but the reason is that a backtest
    # replay backend (tj-r6vcgv) and a test double implement this same interface, and a route
    # holding the transport would make both of them fake a server.
    fetch_client: Annotated[IngestFetchClient, Depends(get_ingest_fetch_client)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_body: Annotated[StoreAssetDatasetBody, Body(...)],
) -> StoreAssetDatasetResponse:
    """Fetch one dataset and store it, answering with what was written and the window it covers.

    THIS HANDLER CATCHES NOTHING (ADR tj-fa1rpu D6). Every failure below it is a TraderJoeError
    carrying a reason, and the handler set installed in data/store/app/main.py renders each as
    problem+json with that reason's status -- the own-overlap 409 with its colliding_ids, a refused
    ack as 422, a rate limit as 429 with Retry-After, an unreachable vendor or peer as 503, a
    deadline as 504, a database that is not there as 503. The hand-written HTTPException mapping
    that used to live here is gone: it rebuilt one of those answers by hand and made the other
    twenty a 500. A route that caught one would make the edge's rendering a lie for exactly that
    case, which is why D6 forbids it rather than leaving it to taste.

    THE RETURN TYPE IS DECLARED, and that is what puts served_range into OpenAPI and therefore into
    a generated client (user ruling, 2026-10-02). It is copied out of the fetch's FetchDone exactly
    as it arrived -- never rebuilt from the request, never clamped a second time here -- so a
    served-but-empty answer, a misspelled symbol among them, tells the caller which window the
    vendor actually answered for. A PUBLIC model of its own rather than a re-export of the internal
    FetchDone, so an internal change cannot move this contract silently.

    Args:
        db: The request's session.
        fetch_client: The FetchDataset seam, as the interface.
        request_path: The asset and data type being stored.
        request_body: What the caller asked for; also the entry's identity fields.

    Returns:
        StoreAssetDatasetResponse: The row count and the served window. Every 200 carries both,
        including a served window with no bars, where data_points is 0.
    """
    log.debug(f'Storing data for {request_path.asset_type.value}, {request_path.asset_symbol}')
    stored = await store_market_activity_worker(request_path, request_body, db, fetch_client)
    return StoreAssetDatasetResponse(
        message='Data stored',
        data_points=stored.data_points,
        # The two instants, copied across the internal/public seam. Spelled out field by field
        # rather than revalidated from the internal model, so adding a member to the internal
        # ServedRange cannot silently publish it.
        served_range=ServedRange(start=stored.served_range.start, end=stored.served_range.end),
    )


@router.get(AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET)
async def get_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_path: Annotated[StoreAssetDatasetPath, Depends()],
    request_query: Annotated[StoreAssetDatasetQuery, Query()],
) -> list[AssetDatasetStore]:
    log.debug(f'Getting data for {request_path.asset_type.value}, {request_path.asset_symbol}')
    log.debug(f'Query: {request_query}')
    return await search_entries(db, request_path, request_query)


@router.get(AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET_BY_ID)
async def get_data_by_id(
    db: Annotated[AsyncSession, Depends(async_db)], request_path: Annotated[AssetDatasetStoreGetById, Depends()]
) -> AssetDatasetStore:
    """Return one dataset entry by id (tj-967trx). Open, like the other GETs.

    NOTHING IS CAUGHT HERE (D6): an unknown id raises EntryNotFound, whose reason NOT_FOUND renders as a 404
    problem+json.

    Args:
        db: The request's session.
        request_path: The entry's id.

    Returns:
        AssetDatasetStore: The entry, with its bar count.
    """
    log.debug(f'Getting dataset {request_path.id}')
    return await get_entry_by_id(db, request_path.id)


@router.delete(
    AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID, dependencies=[Depends(require_instance_secret)]
)
async def delete_data(
    db: Annotated[AsyncSession, Depends(async_db)], request_path: Annotated[AssetDatasetStoreDelete, Depends()]
):
    log.debug(f'Deleting data for {request_path.id}')
    # NOTHING IS CAUGHT HERE EITHER (D6). OwnerMismatch carries OWNER_MISMATCH, whose row is 403,
    # and EntryNotFound carries NOT_FOUND, whose row is 404 -- the same two answers the hand-written
    # mapping produced, now from the one table. The 403 body still does not carry the real owner:
    # OwnerMismatch refuses to format it, and `owner` is not an allowlisted metadata key. A None
    # owner is the degenerate case of a wrong one, so an owner-less delete is still a 403.
    await delete_entry_by_id(db, request_path.id, request_path.owner)
    return {'message': 'Data deleted'}
