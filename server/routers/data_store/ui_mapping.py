"""Catalog results to the ui/v1 messages the Data section reads (tj-grna9p.20, ADR tj-grna9p.4).

THIS MAPPING LIVES HERE, NOT IN common/rpc. ADR tj-grna9p.4 item 5 names common/rpc/mapping as the home of
domain-to-message mapping (the seam TID251 protects), but common/ is builder-shared's and this task is
builder-store's. The ui/v1 package is imported here under a line-level `noqa: TID251` instead: the "reviewed
exemption" that record offers as the alternative, accepted for phase 1 (addendum on tj-grna9p.4). Everything the ui/v1
package shares with the internal contract still goes THROUGH the seam: the market/v1 enums and Timestamps by
common.rpc.mapping.values, and a Bar by common.rpc.mapping.bar_to_proto. Only the messages that exist nowhere
but ui/v1 are built from the generated module directly. When builder-shared takes the mapping into
common/rpc, this module reduces to imports from there.

ENUMS CROSS BY MEMBER NAME, as everywhere (common.rpc.mapping.values): the ui/v1 enums that market/v1 does not
carry (ExpiryType, DatasetState, FreshnessStatus) are looked up by their own prefixed names.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from common.enums.data_stock import ExpiryType, Granularity
from common.rpc.mapping.fetch_dataset import bar_to_proto
from common.rpc.mapping.values import ASSET_TYPE, DATA_SOURCE, DATA_TYPE, FEED, GRANULARITY, UPDATE_TYPE, to_timestamp
from data.store.app.database.crud.dataset_override import OperatorOverrideInput
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.crud.stock.dataset_catalog import BarExtent
from data.store.app.database.models.dataset_read import ReadClientKind
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.dataset_catalog import (
    BarPageIndexResult,
    BarPageResult,
    ClientFilter,
    CoverageResult,
    DatasetDetail,
    DatasetPage,
    DatasetUsageView,
    DatasetView,
    Facets,
    LastReadBucketsResult,
    ReadsPerDayResult,
    RunPageResult,
    StaleDatasetPageResult,
    StorageCurrent,
    UsageSummary,
    effective_end,
    is_collecting,
    is_retired,
)
from data.store.app.events import DatasetChanged, DomainEvent, IngestRunChanged, ReaperPendingChanged, RequestChanged
from data.store.app.ui_config import UiConfigView
from routers.common.proto_json import ProtoBody
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.ui.v1 import data_pb2, events_pb2, shell_pb2, stats_pb2  # noqa: TID251


# The message types the routes declare as x-proto-message (routers.common.proto_json.proto_route). Re-exported so a
# route never imports the generated module itself.
DatasetPageMessage = data_pb2.DatasetPage
DatasetSummaryMessage = data_pb2.DatasetSummary
DatasetFacetsMessage = data_pb2.DatasetFacets
BarPageMessage = data_pb2.BarPage
BarPageIndexMessage = data_pb2.BarPageIndex
IngestRunPageMessage = data_pb2.IngestRunPage
ReaperPendingPageMessage = data_pb2.ReaperPendingPage
CoverageStripMessage = stats_pb2.CoverageStrip
UsageSummaryMessage = stats_pb2.UsageSummary
LastReadBucketsMessage = stats_pb2.LastReadBuckets
ReadsPerDayMessage = stats_pb2.ReadsPerDay
StaleDatasetPageMessage = stats_pb2.StaleDatasetPage
StorageCurrentMessage = stats_pb2.StorageCurrent
UiEventMessage = events_pb2.UiEvent
UiConfigMessage = shell_pb2.UiConfig
# The request bodies of the expiry and delete writes (tj-grna9p.86); parsed by ProtoBody, never by a pydantic model.
SetExpiryRequestMessage = data_pb2.SetExpiryRequest
DeleteDatasetRequestMessage = data_pb2.DeleteDatasetRequest
SetCollectingRequestMessage = data_pb2.SetCollectingRequest


def _enum_value(enum_type: Any, prefix: str, member: Any) -> int:
    return enum_type.Value(f'{prefix}{member.name}')


def _freshness(view: DatasetView, as_of: datetime) -> data_pb2.Freshness:
    message = data_pb2.Freshness()
    message.as_of.CopyFrom(to_timestamp(as_of))
    health = view.health
    if health is None:
        # No trading calendar for the source: nothing is computed, and UNSPECIFIED says so.
        return message
    message.status = _enum_value(data_pb2.FreshnessStatus, 'FRESHNESS_STATUS_', health.status)
    message.gap_count = health.gap_count
    if health.expected_last_bar is not None:
        message.expected_last_bar.CopyFrom(to_timestamp(health.expected_last_bar))
    return message


def _client_kind(kind: ReadClientKind) -> int:
    return _enum_value(data_pb2.ClientKind, 'CLIENT_KIND_', kind)


def _echo(client_filter: ClientFilter) -> tuple[list[str], list[int]]:
    """The client filter's echo: exactly one of the two lists is non-empty (the ECHO RULE in data.proto)."""
    ids = list(client_filter.applied_ids)
    return ids, [] if ids else [_client_kind(kind) for kind in client_filter.applied_kinds]


def _usage(usage: DatasetUsageView, client_filter: ClientFilter) -> data_pb2.DatasetUsage:
    client_ids, client_kinds = _echo(client_filter)
    message = data_pb2.DatasetUsage(
        reads_30d=usage.reads_30d,
        read_by_30d=[_client_kind(kind) for kind in usage.read_by_30d],
        read_by_clients_30d=usage.read_by_clients_30d,
        client_ids=client_ids,
        client_kinds=client_kinds,
    )
    if usage.last_read_at is not None:
        message.last_read_at.CopyFrom(to_timestamp(usage.last_read_at))
    return message


def _summary(
    view: DatasetView,
    extent: BarExtent,
    siblings: list[tuple[uuid.UUID, Granularity]],
    as_of: datetime,
    last_run: IngestRunRecord | None = None,
    reaper_pending: bool = False,
    usage: DatasetUsageView | None = None,
    client_filter: ClientFilter | None = None,
) -> data_pb2.DatasetSummary:
    row = view.row
    message = data_pb2.DatasetSummary(
        id=str(row.id),
        asset_symbol=row.asset_symbol,
        asset_type=ASSET_TYPE.to_proto(row.asset_type),
        data_type=DATA_TYPE.to_proto(row.data_type),
        source=DATA_SOURCE.to_proto(row.source),
        feed=FEED.to_proto(row.feed),
        granularity=GRANULARITY.to_proto(row.granularity),
        update_type=UPDATE_TYPE.to_proto(row.update_type),
        expiry_type=_enum_value(data_pb2.ExpiryType, 'EXPIRY_TYPE_', ExpiryType(row.expiry_type)),
        owner=row.owner,
        state=data_pb2.DATASET_STATE_RETIRED if is_retired(row) else data_pb2.DATASET_STATE_ACTIVE,
        bar_count=extent.count,
        reaper_pending=reaper_pending,
        # Derived: a subscription's collect intent; for STATIC, whether its latest run is RUNNING (no extra read).
        collecting=is_collecting(row, last_run is not None and last_run.state is IngestRunState.RUNNING),
    )
    message.start.CopyFrom(to_timestamp(row.start))
    message.end.CopyFrom(to_timestamp(effective_end(row, as_of)))
    if row.expiry is not None:
        message.expiry.CopyFrom(to_timestamp(row.expiry))
    if extent.first_bar is not None:
        message.first_bar.CopyFrom(to_timestamp(extent.first_bar))
    if extent.last_bar is not None:
        message.last_bar.CopyFrom(to_timestamp(extent.last_bar))
    message.freshness.CopyFrom(_freshness(view, as_of))
    message.siblings.extend(
        data_pb2.DatasetSibling(id=str(sibling_id), granularity=GRANULARITY.to_proto(granularity))
        for sibling_id, granularity in siblings
    )
    if last_run is not None:
        message.last_run.CopyFrom(ingest_run_message(last_run))
    # Always set on a list or detail read, an unread dataset included (the zero view).
    message.usage.CopyFrom(_usage(usage or DatasetUsageView(), client_filter or ClientFilter()))
    return message


def dataset_page_message(page: DatasetPage) -> data_pb2.DatasetPage:
    """The catalog page as a DatasetPage message.

    Args:
        page: The page the catalog produced.

    Returns:
        data_pb2.DatasetPage: One summary per dataset, and the next cursor (empty on the last page).
    """
    message = data_pb2.DatasetPage(next_cursor=page.next_cursor)
    message.items.extend(
        _summary(
            view,
            page.extents[view.row.id],
            page.siblings[view.row.id],
            page.as_of,
            page.last_runs.get(view.row.id),
            reaper_pending=view.row.id in page.reaper_pending,
            usage=page.usage.get(view.row.id),
            client_filter=page.client_filter,
        )
        for view in page.views
    )
    return message


def reaper_pending_page_message(page: DatasetPage, deletes_enabled: bool) -> data_pb2.ReaperPendingPage:
    """The reaper's pending page as a ReaperPendingPage message.

    Args:
        page: The page the catalog produced from the pending set.
        deletes_enabled: Whether the reaper deletes; False means dry-run.

    Returns:
        data_pb2.ReaperPendingPage: One summary per pending dataset, each with reaper_pending true, the next cursor
        (empty on the last page), deletes_enabled and as_of.
    """
    message = data_pb2.ReaperPendingPage(next_cursor=page.next_cursor, deletes_enabled=deletes_enabled)
    message.as_of.CopyFrom(to_timestamp(page.as_of))
    message.items.extend(
        _summary(
            view,
            page.extents[view.row.id],
            page.siblings[view.row.id],
            page.as_of,
            page.last_runs.get(view.row.id),
            reaper_pending=True,
            usage=page.usage.get(view.row.id),
            client_filter=page.client_filter,
        )
        for view in page.views
    )
    return message


def dataset_summary_message(detail: DatasetDetail) -> data_pb2.DatasetSummary:
    """One dataset as a DatasetSummary message.

    Args:
        detail: The dataset the catalog produced.

    Returns:
        data_pb2.DatasetSummary: The dataset with its freshness, extent and siblings.
    """
    return _summary(
        detail.view,
        detail.extent,
        detail.siblings,
        detail.as_of,
        detail.last_run,
        reaper_pending=detail.reaper_pending,
        usage=detail.usage,
        client_filter=detail.client_filter,
    )


def facets_message(facets: Facets) -> data_pb2.DatasetFacets:
    """The sidebar counts as a DatasetFacets message.

    Args:
        facets: The counts the catalog produced.

    Returns:
        data_pb2.DatasetFacets: The two view totals and a count for every source, update type and status, the
        usage and expiry counts with the filter echo, and the clients seen reading.
    """
    client_ids, client_kinds = _echo(facets.client_filter)
    message = data_pb2.DatasetFacets(
        all=facets.all,
        needs_attention=facets.needs_attention,
        running=facets.running,
        client_ids=client_ids,
        client_kinds=client_kinds,
        stale=facets.stale,
        read_30d=facets.read_30d,
        never_read=facets.never_read,
        expiring_soon=facets.expiring_soon,
        stale_days=facets.stale_days,
    )
    if facets.expires_before is not None:
        message.expires_before.CopyFrom(to_timestamp(facets.expires_before))
    message.clients.extend(
        data_pb2.ClientCount(
            client_id=item.client_id, client_kind=_client_kind(item.client_kind), datasets=item.datasets
        )
        for item in facets.clients
    )
    message.sources.extend(
        data_pb2.SourceFacet(source=DATA_SOURCE.to_proto(source), count=count)
        for source, count in facets.sources.items()
    )
    message.update_types.extend(
        data_pb2.UpdateTypeFacet(update_type=UPDATE_TYPE.to_proto(update_type), count=count)
        for update_type, count in facets.update_types.items()
    )
    message.statuses.extend(
        data_pb2.StatusFacet(status=_enum_value(data_pb2.FreshnessStatus, 'FRESHNESS_STATUS_', status), count=count)
        for status, count in facets.statuses.items()
    )
    return message


def usage_summary_message(summary: UsageSummary) -> stats_pb2.UsageSummary:
    """The usage page's headline figures as a UsageSummary message, with the client filter echoed."""
    client_ids, client_kinds = _echo(summary.client_filter)
    return stats_pb2.UsageSummary(
        stale=summary.stale,
        read_last_30d=summary.read_last_30d,
        total=summary.total,
        never_read=summary.never_read,
        reads_today=summary.reads_today,
        distinct_clients_today=summary.distinct_clients_today,
        client_ids=client_ids,
        client_kinds=client_kinds,
        stale_days=summary.stale_days,
    )


def last_read_buckets_message(result: LastReadBucketsResult) -> stats_pb2.LastReadBuckets:
    """The datasets by days since last read as a LastReadBuckets message; the open bucket has no max_days."""
    client_ids, client_kinds = _echo(result.client_filter)
    message = stats_pb2.LastReadBuckets(never_read=result.never_read, client_ids=client_ids, client_kinds=client_kinds)
    for min_days, max_days, datasets in result.buckets:
        bucket = message.buckets.add(min_days=min_days, datasets=datasets)
        if max_days is not None:
            bucket.max_days = max_days
    return message


def reads_per_day_message(result: ReadsPerDayResult) -> stats_pb2.ReadsPerDay:
    """Reads per UTC day per client as a ReadsPerDay message, days as 'YYYY-MM-DD'."""
    client_ids, client_kinds = _echo(result.client_filter)
    message = stats_pb2.ReadsPerDay(client_ids=client_ids, client_kinds=client_kinds)
    message.days.extend(
        stats_pb2.ReadsPerDayEntry(
            day=entry.day.isoformat(),
            client_id=entry.client_id,
            client_kind=_client_kind(entry.client_kind),
            reads=entry.reads,
        )
        for entry in result.entries
    )
    return message


def stale_dataset_page_message(result: StaleDatasetPageResult) -> stats_pb2.StaleDatasetPage:
    """One page of stale datasets as a StaleDatasetPage message: each row with its size estimate and last reads."""
    page = result.page
    client_ids, client_kinds = _echo(page.client_filter)
    message = stats_pb2.StaleDatasetPage(
        next_cursor=page.next_cursor, client_ids=client_ids, client_kinds=client_kinds, stale_days=result.stale_days
    )
    for view in page.views:
        row = message.items.add(est_bytes=result.est_bytes[view.row.id])
        row.dataset.CopyFrom(
            _summary(
                view,
                page.extents[view.row.id],
                page.siblings[view.row.id],
                page.as_of,
                page.last_runs.get(view.row.id),
                reaper_pending=view.row.id in page.reaper_pending,
                usage=page.usage.get(view.row.id),
                client_filter=page.client_filter,
            )
        )
        for read in result.last_reads.get(view.row.id, []):
            item = row.last_read_by_client.add(client_id=read.client_id, client_kind=_client_kind(read.client_kind))
            item.last_read_at.CopyFrom(to_timestamp(read.last_read_at))
    return message


def storage_current_message(result: StorageCurrent) -> stats_pb2.StorageCurrent:
    """The bar table's size and its estimated split by source and feed as a StorageCurrent message."""
    message = stats_pb2.StorageCurrent(total_bytes=result.total_bytes)
    message.as_of.CopyFrom(to_timestamp(result.as_of))
    message.by_source.extend(
        stats_pb2.StorageBySource(
            source=DATA_SOURCE.to_proto(share.source), feed=FEED.to_proto(share.feed), est_bytes=share.est_bytes
        )
        for share in result.by_source
    )
    return message


def bar_page_index_message(index: BarPageIndexResult) -> data_pb2.BarPageIndex:
    """A bar window's page index as a BarPageIndex message.

    Args:
        index: The index the catalog produced.

    Returns:
        data_pb2.BarPageIndex: The bar count, the size and overlap it was cut with, and each page's start.
    """
    message = data_pb2.BarPageIndex(bar_count=index.bar_count, page_size=index.page_size, overlap=index.overlap)
    for page in index.pages:
        entry = message.pages.add(offset=page.offset)
        entry.start.CopyFrom(to_timestamp(page.start))
    return message


def bar_page_message(page: BarPageResult) -> data_pb2.BarPage:
    """A page of bars as a BarPage message.

    Each bar crosses through the seam's own mapper (common.rpc.mapping.bar_to_proto), the same one the internal
    FetchDataset contract uses. The store keeps no vwap, so none is set.

    Args:
        page: The bar rows the catalog produced.

    Returns:
        data_pb2.BarPage: The bars, ascending, and the next cursor (empty on the last page).
    """
    message = data_pb2.BarPage(next_cursor=page.next_cursor)
    message.bars.extend(
        bar_to_proto(
            domain.Bar(
                bar_start=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=float(bar.volume),
                trade_count=bar.trade_count,
                feed=bar.feed,
            )
        )
        for bar in page.bars
    )
    return message


def coverage_strip_message(coverage: CoverageResult) -> stats_pb2.CoverageStrip:
    """A dataset's coverage as a CoverageStrip message.

    Args:
        coverage: What the catalog computed.

    Returns:
        stats_pb2.CoverageStrip: The range, the buckets, the listed gaps, the total gap count and as_of.
    """
    message = stats_pb2.CoverageStrip(gap_count=coverage.gap_count)
    message.start.CopyFrom(to_timestamp(coverage.start))
    message.end.CopyFrom(to_timestamp(coverage.end))
    message.as_of.CopyFrom(to_timestamp(coverage.as_of))
    for bucket in coverage.buckets:
        entry = message.buckets.add(
            expected_sessions=bucket.expected_sessions, sessions_with_bars=bucket.sessions_with_bars
        )
        entry.start.CopyFrom(to_timestamp(bucket.start))
        entry.end.CopyFrom(to_timestamp(bucket.end))
    for gap in coverage.gaps:
        entry = message.gaps.add(sessions=gap.sessions)
        entry.start.CopyFrom(to_timestamp(gap.start))
        entry.end.CopyFrom(to_timestamp(gap.end))
    return message


def ui_config_message(config: UiConfigView) -> shell_pb2.UiConfig:
    """Build the UiConfig the shell reads, enums by their prefixed member names."""
    return shell_pb2.UiConfig(
        allowed_groups=[
            _enum_value(shell_pb2.AccountGroup, 'ACCOUNT_GROUP_', group) for group in config.allowed_groups
        ],
        deployment_label=config.deployment_label,
        server_version=config.server_version,
    )


def ingest_run_message(run: IngestRunRecord) -> data_pb2.IngestRun:
    """One run as an IngestRun message.

    The dataset id is empty until the run's ack, the label is the symbol and bar width, and the error is
    set only for a FAILED run (an untyped failure carries an empty domain and reason).

    Args:
        run: The run, as the repository snapshots it.

    Returns:
        data_pb2.IngestRun: The run.
    """
    message = data_pb2.IngestRun(
        id=str(run.id),
        dataset_id=str(run.dataset_id) if run.dataset_id is not None else '',
        label=f'{run.asset_symbol} {run.granularity.value}',
        trigger=_enum_value(data_pb2.RunTrigger, 'RUN_TRIGGER_', run.trigger),
        actor=run.actor,
        state=_enum_value(data_pb2.IngestRunState, 'INGEST_RUN_STATE_', run.state),
        bars_written=run.bars_written,
        request_id=str(run.request_id) if run.request_id is not None else '',
    )
    if run.requested_start is not None:
        message.start.CopyFrom(to_timestamp(run.requested_start))
    if run.requested_end is not None:
        message.end.CopyFrom(to_timestamp(run.requested_end))
    message.started_at.CopyFrom(to_timestamp(run.started_at))
    if run.finished_at is not None:
        message.finished_at.CopyFrom(to_timestamp(run.finished_at))
    if run.state is IngestRunState.FAILED:
        message.error.CopyFrom(
            data_pb2.IngestRunError(
                domain=run.error_domain or '', reason=run.error_reason or '', message=run.error_message or ''
            )
        )
    return message


def ingest_run_page_message(page: RunPageResult) -> data_pb2.IngestRunPage:
    """A page of runs as an IngestRunPage message.

    Args:
        page: The runs the catalog produced, newest first.

    Returns:
        data_pb2.IngestRunPage: The runs and the next cursor (empty on the last page).
    """
    message = data_pb2.IngestRunPage(next_cursor=page.next_cursor)
    message.items.extend(ingest_run_message(run) for run in page.runs)
    return message


def ui_event_message(event: DomainEvent, server_time: datetime) -> events_pb2.UiEvent:
    """A domain event as the UiEvent frame the socket sends.

    Args:
        event: What the hub carried.
        server_time: The clock at the moment the frame is sent.

    Returns:
        events_pb2.UiEvent: The event, with server_time set.

    Raises:
        TypeError: If the event is not one of the hub's event types.
    """
    message = events_pb2.UiEvent()
    message.server_time.CopyFrom(to_timestamp(server_time))
    match event:
        case DatasetChanged():
            message.dataset_changed.CopyFrom(
                events_pb2.DatasetChanged(
                    id=str(event.id), change=_enum_value(events_pb2.DatasetChange, 'DATASET_CHANGE_', event.change)
                )
            )
        case IngestRunChanged():
            message.ingest_run_changed.run.CopyFrom(ingest_run_message(event.run))
        case RequestChanged():
            # Its entry type does not exist until the scheduler's request log (tj-pd41mt) does; nothing
            # publishes it yet.
            message.request_changed.SetInParent()
        case ReaperPendingChanged():
            message.reaper_pending_changed.SetInParent()
        case _:
            raise TypeError(f'Not a hub event: {type(event).__name__}')
    return message


__all__ = [
    'BarPageIndexMessage',
    'BarPageMessage',
    'CoverageStripMessage',
    'DatasetFacetsMessage',
    'DatasetPageMessage',
    'DatasetSummaryMessage',
    'IngestRunPageMessage',
    'UiConfigMessage',
    'UiEventMessage',
    'bar_page_index_message',
    'bar_page_message',
    'coverage_strip_message',
    'dataset_page_message',
    'dataset_summary_message',
    'facets_message',
    'ingest_run_message',
    'ingest_run_page_message',
    'ui_config_message',
    'ui_event_message',
]


def _override_input(request: Any) -> OperatorOverrideInput | None:
    """The override a write body carried, as plain values; None when the body has none."""
    if not request.HasField('override'):
        return None
    return OperatorOverrideInput(reason=request.override.reason, confirmed=request.override.confirmed)


def set_expiry_values(request: data_pb2.SetExpiryRequest) -> tuple[datetime | None, OperatorOverrideInput | None]:
    """A SetExpiryRequest as plain values: the new expiry (None clears it) and the override, if sent.

    Args:
        request: The parsed body.

    Returns:
        tuple[datetime | None, OperatorOverrideInput | None]: The aware UTC expiry and the override.
    """
    expiry = request.expiry.ToDatetime(tzinfo=UTC) if request.HasField('expiry') else None
    return expiry, _override_input(request)


def set_collecting_values(request: data_pb2.SetCollectingRequest) -> tuple[bool | None, OperatorOverrideInput | None]:
    """A SetCollectingRequest as plain values: the collect intent and the override, if sent.

    Args:
        request: The parsed body.

    Returns:
        tuple[bool | None, OperatorOverrideInput | None]: collect, or None when the body did not carry it
        (presence: a missing key is never read as false), and the override.
    """
    collect = request.collect if request.HasField('collect') else None
    return collect, _override_input(request)


def delete_values(request: data_pb2.DeleteDatasetRequest) -> OperatorOverrideInput | None:
    """A DeleteDatasetRequest's override as plain values; None when the body has none.

    Args:
        request: The parsed body.

    Returns:
        OperatorOverrideInput | None: The override.
    """
    return _override_input(request)


_parse_delete_body = ProtoBody(DeleteDatasetRequestMessage)


async def delete_request_body(request: Request) -> data_pb2.DeleteDatasetRequest:
    """The body of a dataset delete: ProtoBody's parse, except that no body at all is an empty request.

    A DELETE of the operator's own dataset needs no override, so a client may send no body. Anything sent is
    parsed strictly: an unknown field or a body that is not a JSON object is still a 422.

    Args:
        request: The request.

    Returns:
        data_pb2.DeleteDatasetRequest: The parsed body, or an empty message.
    """
    if not (await request.body()).strip():
        return DeleteDatasetRequestMessage()
    return await _parse_delete_body(request)
