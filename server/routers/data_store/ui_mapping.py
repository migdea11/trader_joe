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
from datetime import datetime
from typing import Any

from common.enums.data_stock import ExpiryType, Granularity
from common.rpc.mapping.fetch_dataset import bar_to_proto
from common.rpc.mapping.values import ASSET_TYPE, DATA_SOURCE, DATA_TYPE, FEED, GRANULARITY, UPDATE_TYPE, to_timestamp
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.crud.stock.dataset_catalog import BarExtent
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.dataset_catalog import (
    BarPageResult,
    CoverageResult,
    DatasetDetail,
    DatasetPage,
    DatasetView,
    Facets,
    RunPageResult,
    effective_end,
    is_retired,
)
from data.store.app.events import DatasetChanged, DomainEvent, IngestRunChanged, ReaperPendingChanged, RequestChanged
from data.store.app.ui_config import UiConfigView
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.ui.v1 import data_pb2, events_pb2, shell_pb2, stats_pb2  # noqa: TID251


# The message types the routes declare as x-proto-message (routers.common.proto_json.proto_route). Re-exported so a
# route never imports the generated module itself.
DatasetPageMessage = data_pb2.DatasetPage
DatasetSummaryMessage = data_pb2.DatasetSummary
DatasetFacetsMessage = data_pb2.DatasetFacets
BarPageMessage = data_pb2.BarPage
IngestRunPageMessage = data_pb2.IngestRunPage
CoverageStripMessage = stats_pb2.CoverageStrip
UiEventMessage = events_pb2.UiEvent
UiConfigMessage = shell_pb2.UiConfig


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


def _summary(
    view: DatasetView,
    extent: BarExtent,
    siblings: list[tuple[uuid.UUID, Granularity]],
    as_of: datetime,
    last_run: IngestRunRecord | None = None,
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
            view, page.extents[view.row.id], page.siblings[view.row.id], page.as_of, page.last_runs.get(view.row.id)
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
    return _summary(detail.view, detail.extent, detail.siblings, detail.as_of, detail.last_run)


def facets_message(facets: Facets) -> data_pb2.DatasetFacets:
    """The sidebar counts as a DatasetFacets message.

    Args:
        facets: The counts the catalog produced.

    Returns:
        data_pb2.DatasetFacets: The two view totals and a count for every source, update type and status.
    """
    message = data_pb2.DatasetFacets(all=facets.all, needs_attention=facets.needs_attention, running=facets.running)
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
    'BarPageMessage',
    'CoverageStripMessage',
    'DatasetFacetsMessage',
    'DatasetPageMessage',
    'DatasetSummaryMessage',
    'IngestRunPageMessage',
    'UiConfigMessage',
    'UiEventMessage',
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
