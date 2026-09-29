from abc import ABC
from datetime import datetime
from typing import Generic, Self, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC
from schemas.inbound_contract import InboundContract


DT = TypeVar('DT')  # Data Type


class _AssetDataType(InboundContract, Generic[DT], ABC):
    """Basic Data for a financial asset.

    THE LIFETIME BELONGS TO THE DATASET, NOT THE BAR (tj-vhboky.1 section 9). There is
    deliberately no expiry here: expiry was a per-FETCH attribute stored on a per-BAR row, which
    is how it acquired the same last-write-wins defect as dataset_id. It is a column on the
    dataset entry, which is the row that IS the fetch, and the entry is upserted before any bar
    is written -- so nothing at this level needs to carry it.

    Args:
        Generic (DT): The data type for the asset.
    """

    timestamp: datetime
    data: DT

    def add_data(self, data: DT, timestamp: datetime):
        self.timestamp = timestamp
        self.data = data


class _AssetIdentifier(InboundContract, ABC):
    """Basic Identifiers for a financial asset's data.

    feed is NOT here even though it is part of the bar's natural key, and neither is dataset_id.
    Both are declared on the concrete models that need them instead: this base is shared with
    AssetDataUpdate, which addresses an existing row by id, and a required feed here would oblige
    every update to restate one. AssetData (the read model) and the create models each declare
    their own required feed (tj-5dvgaa). See AssetDataCreate.feed and AssetData.feed.
    """

    asset_symbol: str
    source: DataSource
    granularity: Granularity

    @field_validator('asset_symbol')
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class _AssetIdentifierQuery(InboundContract, ABC):
    """Similar to _AssetIdentifier but with optional fields for querying data.

    EACH FIELD IS INDIVIDUALLY OPTIONAL (tj-vhboky.1 section 8): an absent parameter means NO
    CONSTRAINT on that column. They carry a None default rather than a bare "| None", which in
    Pydantic v2 would be required-but-nullable and oblige a caller to pass every one explicitly.
    Optional one at a time is not optional all at once: AssetDataQuery requires that a query names
    dataset_id or asset_symbol, so an unbounded read cannot be constructed (tj-vhboky.26, user
    ruling (5) on tj-vhboky.20). The rule sits there, not here, because dataset_id is declared
    there.

    feed is HERE, unlike on _AssetIdentifier: a filter on it is optional, so it costs no caller
    anything, and without it a read could not select one tape (tj-p78ng6).
    """

    asset_symbol: str | None = None

    source: DataSource | None = None
    feed: Feed | None = None
    granularity: Granularity | None = None
    dataset_id: UUID | None = None

    @field_validator('asset_symbol')
    def uppercase_item_id(cls, value: str | None) -> str | None:
        # None means "do not filter on symbol". The annotation already claimed to accept None
        # while the body could not, so this raised AttributeError the moment a default existed.
        if value is None:
            return value
        return value.upper()


class _AssetDataQuery(InboundContract, ABC):
    """Remaining base query parameters that aren't direct identifiers.

    start and end are AwareDatetime and a naive value is REFUSED, not converted (tj-vhboky.20
    D2, extending tj-1bl90i): compared against a timestamptz column, a naive bound is read in the
    session timezone, so the same query would return different bars on differently configured
    hosts.

    expiry is gone: a bar carries no expiry (tj-vhboky Ruling 1), so nothing could read it
    (tj-wdjpmq). query is gone: a nested model cannot be an HTTP query parameter, nothing read
    it, and an extra='forbid' contract that accepts a field and ignores it only looks validating
    (tj-vhboky.25).
    """

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None


class AssetDataPath(InboundContract):
    """Asset Properties, as path to identify the endpoint for the desired data and asset type."""

    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)


class AssetDataCreate(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Create a new asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """

    dataset_id: UUID
    # Part of the bar's natural key, alongside dataset_id above: (dataset_id, asset_symbol,
    # source, feed, granularity, timestamp). Declared HERE rather than on _AssetIdentifier for
    # the same reason dataset_id is -- _AssetIdentifier is also the base of AssetDataUpdate, which
    # addresses an existing row by id, and a required feed there would oblige every update to
    # restate one. AssetData declares its own required feed (tj-5dvgaa); see _AssetIdentifier.
    #
    # REQUIRED, AND WITH NO DEFAULT ON PURPOSE. The column is NOT NULL with no server default
    # (data/store/app/database/models/base_market_activity.py), because the "no sentinel for we
    # do not know" ruling removed the UNKNOWN member a default would have pointed at. A default
    # here would be UNKNOWN under another name: it would put a guessed tape on a real bar in
    # exactly the place a resolved value was wanted. Required instead means a missing feed fails
    # as a ValidationError at the edge, naming the field, rather than as a NOT NULL violation
    # deep inside the insert -- the same correction start got on the dataset body.
    #
    # WHO SUPPLIES IT: the ingest adapter, which resolves the tape and stamps it on the bars it
    # returns. The bar is written after the fetch, so by the time this model is constructed the
    # value exists (tj-rh4b7f, and the resolution order on common.enums.data_stock.Feed).
    feed: Feed


class BatchAssetDataCreate(_AssetIdentifier, Generic[DT], ABC):
    """Create multiple asset data entries linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        Generic (DT): Data for the asset including asset type specific data
    """

    dataset_id: UUID
    # One feed for the whole batch, matching dataset_id directly above: a batch is the product of
    # ONE fetch, and a fetch is served by one tape. See AssetDataCreate.feed for why it is
    # required and why it has no default. StockMarketActivity.from_batch_create reads it off the
    # batch, not off the individual bars, which is why it sits here and not on _AssetDataType.
    feed: Feed
    dataset: dict[DataType, list[_AssetDataType[DT]]]

    def append_data(self, data_type: DataType, data: DT, timestamp: datetime):
        if data_type not in self.dataset:
            self.dataset[data_type] = []
        self.dataset[data_type].append(_AssetDataType(timestamp=timestamp, data=data))


class AssetDataUpdate(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Update an existing asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """

    id: int = Field(..., description=ASSET_DATA_ID_DESC)
    dataset_id: UUID


class AssetDataQuery(_AssetIdentifierQuery, _AssetDataQuery, ABC):
    """Query for asset data entries linked to the dataset provided.

    Fields are individually optional (absent = no constraint on that column), but a query MUST
    name dataset_id or asset_symbol; one that names neither is refused with a ValidationError,
    which FastAPI reports as a 422 (tj-vhboky.26). User ruling (5) on tj-vhboky.20: an unbounded
    read (every symbol, all time) must be impossible to construct anywhere, so the refusal lives
    on this shared model, not in a route and not on a route-only subclass. It supersedes the
    empty-query half of tj-vhboky.1 section 8; the per-field half stands.

    mode='after' so a field-level error (a bad enum, a naive datetime) is still reported against
    its own field rather than masked by this rule.

    A BLANK asset_symbol ('' or whitespace only) NAMES NO SYMBOL, and a query carrying one is
    refused by this same rule with the same error shape (loc ['query'], type value_error) as a
    query naming nothing (user ruling on tj-vhboky.28). That holds even when dataset_id is given:
    a blank symbol filter matches no rows, so accepting it returns a silent empty 200 for a
    malformed request, and dropping it would hide the caller's bug. A padded but non-blank symbol
    is not trimmed here or in the field validator; it is uppercased and filtered as sent.

    Args:
        _AssetIdentifierQuery: Queries the asset, data source, feed and granularity.
        _AssetDataQuery: Queries the time range of the asset data.
    """

    dataset_id: UUID | None = None

    @model_validator(mode='after')
    def require_dataset_or_symbol(self) -> Self:
        if self.asset_symbol is not None and not self.asset_symbol.strip():
            raise ValueError('asset_symbol must not be blank; omit it or give a symbol')
        if self.dataset_id is None and self.asset_symbol is None:
            raise ValueError('a bars query must name dataset_id or asset_symbol; an unbounded read is refused')
        return self


class AssetData(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Asset Data entry linked to the dataset provided as found in DB.

    feed IS HERE, REQUIRED, NO DEFAULT. It records which tape served this row. The column is NOT
    NULL with no server default (data/store/app/database/models/base_market_activity.py), because
    the "no sentinel for we do not know" ruling removed the UNKNOWN member a default would have
    pointed at -- a default here would smuggle that removed member back in under another name.
    Required means a reader that cannot supply one fails loudly with a ValidationError naming the
    field, rather than silently reporting None for a value the database actually holds.
    StockMarketActivity.to_schema (data/store/app/database/models/stock_market_activity.py,
    builder-store's scope) passes it in the same commit that added it here, since the two cannot
    be split: see tj-5dvgaa and the decision record tj-q68jcd.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """

    id: int
    dataset_id: UUID
    feed: Feed

    created_at: datetime
    updated_at: datetime
