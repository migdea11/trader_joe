"""The expiry reaper's pending-deletions read: GET /ui/v1/reaper/pending (tj-grna9p.23).

OPEN, like the other /ui/v1 GETs: no instance secret; the edge auth (tj-grna9p.6) protects it in production.
READ-ONLY and it records no usage. SERVED AS PROTOBUF CANONICAL JSON (ADR tj-grna9p.4), declared with proto_route.

The set is the one the reaper's pass deletes from and the one DatasetSummary.reaper_pending reads: datasets whose
expiry has passed (crud list_pending). Whether anything is deleted is the page's deletes_enabled.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.database.database import async_db
from data.store.app.dataset_catalog import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, reaper_pending_page
from data.store.app.reaper import deletes_enabled
from routers.common.proto_json import ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiReaperInterface
from routers.data_store.ui_datasets import utc_now
from routers.data_store.ui_mapping import ReaperPendingPageMessage, reaper_pending_page_message
from schemas.inbound_contract import InboundContract


router = APIRouter()


class UiReaperPendingQuery(InboundContract):
    """The pending read's query: a page.

    Attributes:
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT


@router.get(UiReaperInterface.GET_UI_REAPER_PENDING, **proto_route(ReaperPendingPageMessage))
async def get_reaper_pending(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiReaperPendingQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """One page of the datasets the reaper would delete (or is deleting), as a ReaperPendingPage.

    Keyset paging over (expiry, id), oldest expiry first, with an opaque cursor: a dataset added or removed between
    pages never makes a client see one twice or skip one that was ahead of it. A cursor that does not decode is a
    422.

    Args:
        db: The request's session.
        request_query: The cursor and page size.
        now: The instant the pending set is computed at.

    Returns:
        ProtoJSONResponse: A ReaperPendingPage; empty items and an empty next_cursor when nothing is pending.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a bad cursor.
    """
    page = await reaper_pending_page(db, now, cursor=request_query.cursor, limit=request_query.limit)
    return ProtoJSONResponse(reaper_pending_page_message(page, deletes_enabled()))
