"""The /ui/v1 read routes of the Data section: dataset catalog, facets, detail and bar pages (tj-grna9p.20).

SERVED AS PROTOBUF CANONICAL JSON (ADR tj-grna9p.4): each handler returns ProtoJSONResponse(message) and each
route declares its message with proto_route, so OpenAPI records only the message's name under x-proto-message
and the .proto is the schema. Request parameters are ordinary query and path parameters, validated as the rest
of this service validates them: a bad one is a 422 problem+json, an unknown dataset is a 404 problem+json.

READ-ONLY, AND OPEN. Like the GETs under /store, these take no instance secret; the edge auth (tj-grna9p.6)
protects them in production. Phase 1 records nothing about who read what (usage is phase 3), and it runs no
ingest, so nothing here writes.

THE QUERY IS BOUND WITH Query() (the rule internal_asset_data.py documents): the models forbid extra fields, so an
unknown parameter is a 422 rather than silently dropped.

ENUM FILTERS TAKE THE MEMBER NAME, with or without the wire prefix, in any case: source=ALPACA_API and
source=DATA_SOURCE_ALPACA_API are the same filter, so a client can hand back a name it read from a facet.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime, BeforeValidator, Field, WithJsonSchema
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter
from data.store.app.database.database import async_db
from data.store.app.dataset_catalog import (
    DEFAULT_BAR_LIMIT,
    DEFAULT_PAGE_LIMIT,
    MAX_BAR_LIMIT,
    MAX_PAGE_LIMIT,
    CatalogQuery,
    DatasetSort,
    StatusGroup,
    bar_page,
    dataset_detail,
    facets,
    list_page,
)
from routers.common.proto_json import ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiDatasetsInterface
from routers.data_store.ui_mapping import (
    BarPageMessage,
    DatasetFacetsMessage,
    DatasetPageMessage,
    DatasetSummaryMessage,
    bar_page_message,
    dataset_page_message,
    dataset_summary_message,
    facets_message,
)
from schemas.inbound_contract import InboundContract


router = APIRouter()


def _member_by_name(enum_type: Any, prefix: str) -> tuple[BeforeValidator, WithJsonSchema]:
    """A validator that reads an enum member from its name, in any case, with or without the wire prefix.

    Also documents the parameter as the string enum of member names it accepts. Left alone, an IntEnum such as
    UpdateType would publish as integers and a StrEnum as its values, both of which this parameter refuses.
    """

    def convert(value: Any) -> Any:
        if value is None or isinstance(value, enum_type):
            return value
        if isinstance(value, str):
            name = value.upper().removeprefix(prefix)
            if name in enum_type.__members__:
                return enum_type[name]
        raise ValueError(f'not one of {sorted(enum_type.__members__)}')

    return BeforeValidator(convert), WithJsonSchema({'type': 'string', 'enum': list(enum_type.__members__)})


def _lowered(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


async def utc_now() -> datetime:
    """The instant a request is judged at. A dependency so a test can pin the clock without patching a module."""
    return datetime.now(UTC)


class UiDatasetFiltersQuery(InboundContract):
    """The filters shared by the dataset list and its facets.

    Attributes:
        asset_symbol: Keep datasets whose symbol starts with this, case-insensitively (the Viewer's symbol picker).
        source: Keep datasets from this source.
        update_type: Keep datasets of this update type.
        status: Keep datasets whose freshness is in this UI group: healthy, late, failed or retired.
        needs_attention: Keep only late and failed datasets.
    """

    asset_symbol: Annotated[str | None, Field(min_length=1, max_length=32)] = None
    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None
    update_type: Annotated[UpdateType | None, *_member_by_name(UpdateType, 'UPDATE_TYPE_')] = None
    status: Annotated[StatusGroup | None, BeforeValidator(_lowered)] = None
    needs_attention: bool = False


class UiDatasetsQuery(UiDatasetFiltersQuery):
    """The dataset list's query: the shared filters, the order, and the page.

    Attributes:
        sort: symbol (default) or expires, soonest first with no-expiry datasets last.
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    sort: DatasetSort = DatasetSort.SYMBOL
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT


class UiBarsQuery(InboundContract):
    """The bar page's query: a half-open window and a page.

    Attributes:
        start: Include bars at or after this instant. Timezone-aware.
        end: Exclude bars at or after this instant. Timezone-aware.
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_BAR_LIMIT)] = DEFAULT_BAR_LIMIT


def _catalog_filter(query: UiDatasetFiltersQuery) -> CatalogFilter:
    return CatalogFilter(asset_symbol_prefix=query.asset_symbol, source=query.source, update_type=query.update_type)


def _not_found(dataset_id: UUID) -> InvalidRequestError:
    return InvalidRequestError(Reason.NOT_FOUND, f'No dataset found with ID {dataset_id}')


@router.get(UiDatasetsInterface.GET_UI_DATASETS, **proto_route(DatasetPageMessage))
async def list_datasets(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiDatasetsQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """One page of the dataset catalog, as a DatasetPage.

    Keyset paging over (asset_symbol, id), or (expiry, asset_symbol, id) under sort=expires: the cursor is the
    last item's key, so a dataset added or removed between pages never makes a client see one twice or skip one
    that was ahead of it. A cursor that does not decode, or was issued under the other sort, is a 422.

    Args:
        db: The request's session.
        request_query: The filters, sort, cursor and page size.
        now: The instant freshness is computed at.

    Returns:
        ProtoJSONResponse: A DatasetPage. Empty items and an empty next_cursor for an empty store.
    """
    query = CatalogQuery(
        catalog_filter=_catalog_filter(request_query),
        status=request_query.status,
        needs_attention=request_query.needs_attention,
        sort=request_query.sort,
        cursor=request_query.cursor,
        limit=request_query.limit,
    )
    return ProtoJSONResponse(dataset_page_message(await list_page(db, query, now)))


# Registered BEFORE the by-id route below, which would otherwise take 'facets' for a dataset id.
@router.get(UiDatasetsInterface.GET_UI_DATASET_FACETS, **proto_route(DatasetFacetsMessage))
async def get_dataset_facets(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiDatasetFiltersQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """The sidebar's counts, as a DatasetFacets, under the same filters the list takes.

    Each count equals the total of the list it describes; the definitions are in data/store/app/dataset_catalog.py.

    Args:
        db: The request's session.
        request_query: The filters.
        now: The instant freshness is computed at.

    Returns:
        ProtoJSONResponse: A DatasetFacets, with a count for every source, update type and freshness status.
    """
    query = CatalogQuery(
        catalog_filter=_catalog_filter(request_query),
        status=request_query.status,
        needs_attention=request_query.needs_attention,
    )
    return ProtoJSONResponse(facets_message(await facets(db, query, now)))


@router.get(UiDatasetsInterface.GET_UI_DATASET, **proto_route(DatasetSummaryMessage))
async def get_dataset(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """One dataset, as a DatasetSummary with its siblings and freshness.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        now: The instant freshness is computed at.

    Returns:
        ProtoJSONResponse: A DatasetSummary.

    Raises:
        InvalidRequestError: NOT_FOUND, a 404 problem+json, when there is no such dataset.
    """
    detail = await dataset_detail(db, dataset_id, now)
    if detail is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(dataset_summary_message(detail))


@router.get(UiDatasetsInterface.GET_UI_DATASET_BARS, **proto_route(BarPageMessage))
async def get_dataset_bars(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiBarsQuery, Query()],
) -> ProtoJSONResponse:
    """One page of a dataset's bars in the half-open window [start, end), ascending by bar_start, as a BarPage.

    The cursor is the last bar's timestamp, which is unique within a dataset, so bars inserted while a client
    pages are never shown twice and never make it skip one that was ahead of it. The limit defaults to 1000 and
    is at most 10000. An empty or inverted window is not an error: it is an empty page.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The window, cursor and page size.

    Returns:
        ProtoJSONResponse: A BarPage.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; INVALID_REQUEST (422) for a bad cursor.
    """
    page = await bar_page(
        db,
        dataset_id,
        start=request_query.start,
        end=request_query.end,
        cursor=request_query.cursor,
        limit=request_query.limit,
    )
    if page is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(bar_page_message(page))
