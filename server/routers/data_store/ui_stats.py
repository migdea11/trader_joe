"""The /ui/v1 usage and storage statistics of the Usage tab (tj-grna9p.64).

OPEN, like the other /ui/v1 GETs: no instance secret; the edge auth (tj-grna9p.6) protects them in production.
READ-ONLY, and NONE RECORDS USAGE. SERVED AS PROTOBUF CANONICAL JSON (ADR tj-grna9p.4), each declared with
proto_route. Every usage route takes the dataset list's client filter (client_id repeatable and winning, client_kind
repeatable, default SDK; UiClientFilterQuery) and echoes it; an unknown parameter is a 422, as everywhere here.

THE STORAGE ROUTE TAKES NO CLIENT FILTER: what the store holds is not a function of who read it. Quota burn is not
served (tj-lenn8u) and nothing here has a reader dimension beyond the client id.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource
from data.store.app.database.database import async_db
from data.store.app.dataset_catalog import (
    DEFAULT_PAGE_LIMIT,
    DEFAULT_READS_PER_DAY_DAYS,
    DEFAULT_STALE_DAYS,
    MAX_PAGE_LIMIT,
    MAX_READS_PER_DAY_DAYS,
    MAX_STALE_DAYS,
    last_read_buckets,
    reads_per_day,
    stale_page,
    storage_current,
    usage_summary,
)
from routers.common.proto_json import ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiStatsInterface
from routers.data_store.ui_datasets import UiClientFilterQuery, _client_filter, _member_by_name, utc_now
from routers.data_store.ui_mapping import (
    LastReadBucketsMessage,
    ReadsPerDayMessage,
    StaleDatasetPageMessage,
    StorageCurrentMessage,
    UsageSummaryMessage,
    last_read_buckets_message,
    reads_per_day_message,
    stale_dataset_page_message,
    storage_current_message,
    usage_summary_message,
)
from schemas.inbound_contract import InboundContract


router = APIRouter()


class UiUsageSummaryQuery(UiClientFilterQuery):
    """The usage summary's query: the client filter, the staleness threshold and a source.

    Attributes:
        stale_days: The staleness threshold in days, 1 to 3650.
        source: Count only this source's datasets.
    """

    stale_days: Annotated[int, Field(ge=1, le=MAX_STALE_DAYS)] = DEFAULT_STALE_DAYS
    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None


class UiLastReadBucketsQuery(UiClientFilterQuery):
    """The last-read buckets' query: the client filter and a source.

    Attributes:
        source: Count only this source's datasets.
    """

    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None


class UiReadsPerDayQuery(UiClientFilterQuery):
    """The reads-per-day query: the client filter, the window, and optionally one dataset or source.

    Attributes:
        days: The window in UTC days, today included, 1 to 365.
        dataset_id: Count only this dataset (the Dataset detail's chart).
        source: Count only this source's datasets.
    """

    days: Annotated[int, Field(ge=1, le=MAX_READS_PER_DAY_DAYS)] = DEFAULT_READS_PER_DAY_DAYS
    dataset_id: UUID | None = None
    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None


class UiStaleQuery(UiClientFilterQuery):
    """The stale list's query: the client filter, the threshold, a source and a page.

    Attributes:
        stale_days: The staleness threshold in days, 1 to 3650.
        source: Keep only this source's datasets.
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size.
    """

    stale_days: Annotated[int, Field(ge=1, le=MAX_STALE_DAYS)] = DEFAULT_STALE_DAYS
    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT


class UiStorageQuery(InboundContract):
    """The current storage's query.

    Attributes:
        source: Split only this source (its total is the sum of its shares).
    """

    source: Annotated[DataSource | None, *_member_by_name(DataSource, 'DATA_SOURCE_')] = None


@router.get(UiStatsInterface.GET_UI_USAGE_SUMMARY, **proto_route(UsageSummaryMessage))
async def get_usage_summary(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiUsageSummaryQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """The usage page's headline figures, as a UsageSummary, under the client filter.

    stale, read_last_30d and never_read use the day-aligned rule of the dataset list's usage filter, so stale equals
    the Stale facet's count and the stale list's total for the same filter; read_last_30d is DatasetFacets.read_30d.
    reads_today and distinct_clients_today count today's UTC date.

    Args:
        db: The request's session.
        request_query: The client filter, stale_days and source.
        now: The instant the figures are judged at.

    Returns:
        ProtoJSONResponse: A UsageSummary with the filter echoed.
    """
    summary = await usage_summary(
        db,
        now,
        client_filter=_client_filter(request_query),
        stale_days=request_query.stale_days,
        source=request_query.source,
    )
    return ProtoJSONResponse(usage_summary_message(summary))


@router.get(UiStatsInterface.GET_UI_USAGE_LAST_READ_BUCKETS, **proto_route(LastReadBucketsMessage))
async def get_last_read_buckets(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiLastReadBucketsQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """Datasets by whole UTC days since their last read, as a LastReadBuckets, under the client filter.

    Buckets are [0,1), [1,8), [8,31), [31,91) and [91, open) days, 0 being today; with never_read they sum to the
    number of datasets.

    Args:
        db: The request's session.
        request_query: The client filter and source.
        now: The instant the buckets are judged at.

    Returns:
        ProtoJSONResponse: A LastReadBuckets with the filter echoed.
    """
    result = await last_read_buckets(db, now, client_filter=_client_filter(request_query), source=request_query.source)
    return ProtoJSONResponse(last_read_buckets_message(result))


@router.get(UiStatsInterface.GET_UI_USAGE_READS_PER_DAY, **proto_route(ReadsPerDayMessage))
async def get_reads_per_day(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiReadsPerDayQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """Reads per UTC day per client over the last days days (today included), as a ReadsPerDay.

    One entry per (day, client id, kind) with a read, ascending by day. The per-read rows and the 90-day rollup are
    one series: a day split across the rollup boundary is one entry.

    Args:
        db: The request's session.
        request_query: The client filter, days, dataset_id and source.
        now: The instant the window ends at.

    Returns:
        ProtoJSONResponse: A ReadsPerDay with the filter echoed.
    """
    result = await reads_per_day(
        db,
        now,
        client_filter=_client_filter(request_query),
        days=request_query.days,
        dataset_id=request_query.dataset_id,
        source=request_query.source,
    )
    return ProtoJSONResponse(reads_per_day_message(result))


@router.get(UiStatsInterface.GET_UI_USAGE_STALE, **proto_route(StaleDatasetPageMessage))
async def get_stale_datasets(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiStaleQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """One page of the stale datasets under the client filter, as a StaleDatasetPage.

    Never-read first, then the oldest last read, then id: keyset paged with an opaque cursor bound to stale_days,
    source and the client filter. Each row carries the dataset, an estimated size and the last read by every client.

    Args:
        db: The request's session.
        request_query: The client filter, stale_days, source, cursor and page size.
        now: The instant staleness is judged at.

    Returns:
        ProtoJSONResponse: A StaleDatasetPage with the filter echoed.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a cursor that does not decode or belongs to another filter.
    """
    result = await stale_page(
        db,
        now,
        client_filter=_client_filter(request_query),
        stale_days=request_query.stale_days,
        source=request_query.source,
        cursor=request_query.cursor,
        limit=request_query.limit,
    )
    return ProtoJSONResponse(stale_dataset_page_message(result))


@router.get(UiStatsInterface.GET_UI_STORAGE_CURRENT, **proto_route(StorageCurrentMessage))
async def get_storage_current(
    db: Annotated[AsyncSession, Depends(async_db)],
    request_query: Annotated[UiStorageQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """What the bar table holds now, as a StorageCurrent: the table's size and an ESTIMATED split by source and feed.

    The split is each (source, feed)'s bar count times the average row size. There is no history and no growth.

    Args:
        db: The request's session.
        request_query: The source.
        now: The instant it was read at (as_of).

    Returns:
        ProtoJSONResponse: A StorageCurrent.
    """
    result = await storage_current(db, now, source=request_query.source)
    return ProtoJSONResponse(storage_current_message(result))
