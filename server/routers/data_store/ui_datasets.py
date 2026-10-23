"""The /ui/v1 read routes of the Data section: dataset catalog, facets, detail, bar pages, runs and coverage.

SERVED AS PROTOBUF CANONICAL JSON (ADR tj-grna9p.4): each handler returns ProtoJSONResponse(message) and each
route declares its message with proto_route, so OpenAPI records only the message's name under x-proto-message
and the .proto is the schema. Request parameters are ordinary query and path parameters, validated as the rest
of this service validates them: a bad one is a 422 problem+json, an unknown dataset is a 404 problem+json.

READ-ONLY, AND OPEN. Like the GETs under /store, these take no instance secret; the edge auth (tj-grna9p.6)
protects them in production. The one record is a bar read (GET .../bars), buffered by the usage recorder and
labelled by the X-Client-Id header; every other route here records nothing, and nothing here writes the database.

THE QUERY IS BOUND WITH Query() (the rule internal_asset_data.py documents): the models forbid extra fields, so an
unknown parameter is a 422 rather than silently dropped.

ENUM FILTERS TAKE THE MEMBER NAME, with or without the wire prefix, in any case: source=ALPACA_API and
source=DATA_SOURCE_ALPACA_API are the same filter, so a client can hand back a name it read from a facet.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import AwareDatetime, BeforeValidator, Field, WithJsonSchema
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.client_label import CLIENT_ID_HEADER, MAX_CLIENT_ID_LENGTH, classify_client_label
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter
from data.store.app.database.database import async_db
from data.store.app.database.models.dataset_read import ReadClientKind
from data.store.app.dataset_catalog import (
    DEFAULT_BAR_LIMIT,
    DEFAULT_COVERAGE_BUCKETS,
    DEFAULT_PAGE_INDEX_OVERLAP,
    DEFAULT_PAGE_INDEX_SIZE,
    DEFAULT_PAGE_LIMIT,
    DEFAULT_RUN_LIMIT,
    DEFAULT_STALE_DAYS,
    MAX_BAR_LIMIT,
    MAX_COVERAGE_BUCKETS,
    MAX_PAGE_INDEX_SIZE,
    MAX_PAGE_LIMIT,
    MAX_RUN_LIMIT,
    MAX_STALE_DAYS,
    CatalogQuery,
    ClientFilter,
    DatasetSort,
    StatusGroup,
    UsageFilter,
    bar_page,
    bar_page_index,
    dataset_coverage,
    dataset_detail,
    facets,
    list_page,
    run_page,
)
from data.store.app.usage_recorder import usage_recorder
from routers.common.proto_json import ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiDatasetsInterface
from routers.data_store.ui_mapping import (
    BarPageIndexMessage,
    BarPageMessage,
    CoverageStripMessage,
    DatasetFacetsMessage,
    DatasetPageMessage,
    DatasetSummaryMessage,
    IngestRunPageMessage,
    bar_page_index_message,
    bar_page_message,
    coverage_strip_message,
    dataset_page_message,
    dataset_summary_message,
    facets_message,
    ingest_run_page_message,
)
from schemas.inbound_contract import InboundContract


router = APIRouter()

# The most client ids one request may filter by.
MAX_CLIENT_IDS = 32


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


class UiClientFilterQuery(InboundContract):
    """The client filter, shared by the dataset list, its facets and the dataset detail.

    Attributes:
        client_id: Whose reads the usage figures and the usage filter count: exact client ids, repeatable. Wins
            over client_kind. It is the reader's label; the /store owner filter is unrelated.
        client_kind: The client kinds (UI, SDK; the CLIENT_KIND_ prefix and any case accepted), repeatable. With
            neither client_id nor client_kind the kind is SDK.
    """

    client_id: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=MAX_CLIENT_ID_LENGTH)]], Field(max_length=MAX_CLIENT_IDS)
    ] = Field(default_factory=list)
    client_kind: Annotated[
        list[Annotated[ReadClientKind, *_member_by_name(ReadClientKind, 'CLIENT_KIND_')]], Field(max_length=8)
    ] = Field(default_factory=list)


class UiDatasetFiltersQuery(UiClientFilterQuery):
    """The filters shared by the dataset list and its facets (the client filter is inherited).

    Attributes:
        asset_symbol: Keep datasets whose symbol starts with this, case-insensitively (the Viewer's symbol picker).
        source: Keep datasets from this source.
        update_type: Keep datasets of this update type.
        status: Keep datasets whose freshness is in this UI group: healthy, late, failed or retired.
        needs_attention: Keep only late and failed datasets.
        running: Keep only datasets with a run in flight. Orthogonal to status.
        usage: Keep datasets in this usage group under the client filter: READ_30D, STALE or NEVER_READ.
        stale_days: The staleness threshold of the STALE group in days, 1 to 3650.
        expires_before: The Expiring Soon view: keep datasets expiring before this instant plus every dataset the
            reaper would delete; the facets read uses now + 7 days for its expiring_soon count when it is absent.
    """

    asset_symbol: Annotated[str | None, Field(min_length=1, max_length=32)] = None
    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None
    update_type: Annotated[UpdateType | None, *_member_by_name(UpdateType, 'UPDATE_TYPE_')] = None
    status: Annotated[StatusGroup | None, BeforeValidator(_lowered)] = None
    needs_attention: bool = False
    running: bool = False
    usage: Annotated[UsageFilter | None, *_member_by_name(UsageFilter, 'USAGE_FILTER_')] = None
    stale_days: Annotated[int, Field(ge=1, le=MAX_STALE_DAYS)] = DEFAULT_STALE_DAYS
    expires_before: AwareDatetime | None = None


class UiDatasetsQuery(UiDatasetFiltersQuery):
    """The dataset list's query: the shared filters, the order, and the page.

    Attributes:
        sort: symbol (default), expires (soonest first, no-expiry last) or last_read (most recent first, never-read
            last, by the selected clients).
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    sort: DatasetSort = DatasetSort.SYMBOL
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT


class UiRunsQuery(InboundContract):
    """The runs list's query: a page.

    Attributes:
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_RUN_LIMIT)] = DEFAULT_RUN_LIMIT


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


class UiBarPagesQuery(InboundContract):
    """The bar page index's query: a half-open window and the page geometry.

    Attributes:
        start: Include bars at or after this instant. Timezone-aware.
        end: Exclude bars at or after this instant. Timezone-aware.
        size: The page width in bars.
        overlap: The look-ahead bars the client adds from the next block; must be below size. It does not move
            the page offsets.
    """

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    size: Annotated[int, Field(ge=1, le=MAX_PAGE_INDEX_SIZE)] = DEFAULT_PAGE_INDEX_SIZE
    overlap: Annotated[int, Field(ge=0)] = DEFAULT_PAGE_INDEX_OVERLAP


class UiCoverageQuery(InboundContract):
    """The coverage strip's query.

    Attributes:
        buckets: How many buckets at most. Fewer when the range holds fewer sessions.
    """

    buckets: Annotated[int, Field(ge=1, le=MAX_COVERAGE_BUCKETS)] = DEFAULT_COVERAGE_BUCKETS


def _catalog_filter(query: UiDatasetFiltersQuery) -> CatalogFilter:
    return CatalogFilter(asset_symbol_prefix=query.asset_symbol, source=query.source, update_type=query.update_type)


def _client_filter(query: UiClientFilterQuery) -> ClientFilter:
    return ClientFilter(client_ids=tuple(query.client_id), client_kinds=tuple(query.client_kind))


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
        running=request_query.running,
        client_filter=_client_filter(request_query),
        usage=request_query.usage,
        stale_days=request_query.stale_days,
        expires_before=request_query.expires_before,
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
    The usage counts follow the client filter and stale_days; expiring_soon and the clients list ignore the client
    filter.

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
        running=request_query.running,
        client_filter=_client_filter(request_query),
        usage=request_query.usage,
        stale_days=request_query.stale_days,
        expires_before=request_query.expires_before,
    )
    return ProtoJSONResponse(facets_message(await facets(db, query, now)))


@router.get(UiDatasetsInterface.GET_UI_DATASET, **proto_route(DatasetSummaryMessage))
async def get_dataset(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiClientFilterQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """One dataset, as a DatasetSummary with its siblings and freshness.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The client filter: whose reads the usage counts, echoed on the usage.
        now: The instant freshness is computed at.

    Returns:
        ProtoJSONResponse: A DatasetSummary.

    Raises:
        InvalidRequestError: NOT_FOUND, a 404 problem+json, when there is no such dataset.
    """
    detail = await dataset_detail(db, dataset_id, now, client_filter=_client_filter(request_query))
    if detail is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(dataset_summary_message(detail))


@router.get(UiDatasetsInterface.GET_UI_DATASET_BARS, **proto_route(BarPageMessage))
async def get_dataset_bars(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiBarsQuery, Query()],
    # The reader's label (tj-grna9p.131 OPTION R); the web sends 'operator'. Absent or malformed is recorded as
    # '_unattributed'. A label for telemetry, never an authority.
    client_id_header: Annotated[str | None, Header(alias=CLIENT_ID_HEADER)] = None,
) -> ProtoJSONResponse:
    """One page of a dataset's bars in the half-open window [start, end), ascending by bar_start, as a BarPage.

    The cursor is the last bar's timestamp, which is unique within a dataset, so bars inserted while a client
    pages are never shown twice and never make it skip one that was ahead of it. The limit defaults to 1000 and
    is at most 50000. An empty or inverted window is not an error: it is an empty page.

    A CALL WITHOUT A CURSOR IS RECORDED AS ONE READ of the dataset, labelled by X-Client-Id; a call WITH a cursor
    (an export or a chart continuing) records nothing, so a session is one read. Buffered and best effort: no
    statement, never changes the answer (tj-grna9p.85).

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The window, cursor and page size.
        client_id_header: The reader's label.

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
    if request_query.cursor is None:
        usage_recorder.record_read(
            dataset_id,
            classify_client_label(client_id_header),
            UiDatasetsInterface.GET_UI_DATASET_BARS.value,
            len(page.bars),
        )
    return ProtoJSONResponse(bar_page_message(page))


@router.get(UiDatasetsInterface.GET_UI_DATASET_BAR_PAGES, **proto_route(BarPageIndexMessage))
async def get_dataset_bar_pages(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiBarPagesQuery, Query()],
) -> ProtoJSONResponse:
    """The page index of a dataset's bar window [start, end), as a BarPageIndex: the Viewer's pager.

    Pages are fixed blocks of size bars counted back from the newest bar. With N bars in the half-open window, q =
    N div size and r = N mod size: N = 0 has no pages and N <= size is one page at offset 0. Otherwise the pages,
    oldest first, are offset 0 when r > 0 (the earliest page, which holds size bars and so overlaps the next
    block), then r, r + size, ..., N - size (r = 0 lists offset 0 once). Page k's bars are the bars read with
    start=pages[k].start, the same end and limit=size + overlap, the overlap being the look-ahead taken from the
    next block. The read runs once per window in a constant number of statements. size defaults to 1000 (at most
    1000), overlap to 50 (below size; it does not move the offsets). An empty or inverted window is an empty
    index, not an error.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The window, page size and overlap.

    Returns:
        ProtoJSONResponse: A BarPageIndex.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; INVALID_REQUEST (422) when overlap is not
            below size.
    """
    index = await bar_page_index(
        db,
        dataset_id,
        start=request_query.start,
        end=request_query.end,
        size=request_query.size,
        overlap=request_query.overlap,
    )
    if index is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(bar_page_index_message(index))


@router.get(UiDatasetsInterface.GET_UI_DATASET_RUNS, **proto_route(IngestRunPageMessage))
async def get_dataset_runs(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiRunsQuery, Query()],
) -> ProtoJSONResponse:
    """One page of a dataset's collection runs, newest first, as an IngestRunPage.

    Keyset on (started_at, id) with an opaque cursor, so a run inserted while a client pages never shifts a page
    it has not read. The limit defaults to 50 and is at most 500. The runs of a deleted dataset are not reached
    here: the dataset is unknown, which is a 404.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The cursor and page size.

    Returns:
        ProtoJSONResponse: An IngestRunPage.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; INVALID_REQUEST (422) for a bad cursor.
    """
    page = await run_page(db, dataset_id, cursor=request_query.cursor, limit=request_query.limit)
    if page is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(ingest_run_page_message(page))


@router.get(UiDatasetsInterface.GET_UI_DATASET_COVERAGE, **proto_route(CoverageStripMessage))
async def get_dataset_coverage(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiCoverageQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """A dataset's coverage over the trading calendar, as a CoverageStrip: bucketed sessions and its gaps.

    Ranges are half-open [start, end). No pagination: one strip per dataset, at most 400 buckets and the first
    500 gaps (gap_count says how many there are). Metadata: a read records no usage. The rules are in
    data/store/app/dataset_catalog.py (dataset_coverage).

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        request_query: The bucket count.
        now: The instant coverage is computed at.

    Returns:
        ProtoJSONResponse: A CoverageStrip.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; INVALID_REQUEST (422) when no trading
            calendar covers the dataset.
    """
    coverage = await dataset_coverage(db, dataset_id, now, request_query.buckets)
    if coverage is None:
        raise _not_found(dataset_id)
    return ProtoJSONResponse(coverage_strip_message(coverage))
