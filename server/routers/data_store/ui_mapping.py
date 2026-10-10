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
from data.store.app.database.crud.stock.dataset_catalog import BarExtent
from data.store.app.dataset_catalog import (
    BarPageResult,
    DatasetDetail,
    DatasetPage,
    DatasetView,
    Facets,
    effective_end,
    is_retired,
)
from data.store.app.ui_config import UiConfigView
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.ui.v1 import data_pb2, shell_pb2  # noqa: TID251


# The message types the routes declare as x-proto-message (routers.common.proto_json.proto_route). Re-exported so a
# route never imports the generated module itself.
DatasetPageMessage = data_pb2.DatasetPage
DatasetSummaryMessage = data_pb2.DatasetSummary
DatasetFacetsMessage = data_pb2.DatasetFacets
BarPageMessage = data_pb2.BarPage
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
    view: DatasetView, extent: BarExtent, siblings: list[tuple[uuid.UUID, Granularity]], as_of: datetime
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
        _summary(view, page.extents[view.row.id], page.siblings[view.row.id], page.as_of) for view in page.views
    )
    return message


def dataset_summary_message(detail: DatasetDetail) -> data_pb2.DatasetSummary:
    """One dataset as a DatasetSummary message.

    Args:
        detail: The dataset the catalog produced.

    Returns:
        data_pb2.DatasetSummary: The dataset with its freshness, extent and siblings.
    """
    return _summary(detail.view, detail.extent, detail.siblings, detail.as_of)


def facets_message(facets: Facets) -> data_pb2.DatasetFacets:
    """The sidebar counts as a DatasetFacets message.

    Args:
        facets: The counts the catalog produced.

    Returns:
        data_pb2.DatasetFacets: The two view totals and a count for every source, update type and status.
    """
    message = data_pb2.DatasetFacets(all=facets.all, needs_attention=facets.needs_attention)
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


def ui_config_message(config: UiConfigView) -> shell_pb2.UiConfig:
    """Build the UiConfig the shell reads, enums by their prefixed member names."""
    return shell_pb2.UiConfig(
        allowed_groups=[
            _enum_value(shell_pb2.AccountGroup, 'ACCOUNT_GROUP_', group) for group in config.allowed_groups
        ],
        deployment_label=config.deployment_label,
        server_version=config.server_version,
    )


__all__ = [
    'BarPageMessage',
    'DatasetFacetsMessage',
    'DatasetPageMessage',
    'DatasetSummaryMessage',
    'UiConfigMessage',
    'bar_page_message',
    'dataset_page_message',
    'dataset_summary_message',
    'facets_message',
    'ui_config_message',
]
