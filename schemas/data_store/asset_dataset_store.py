from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_DATASET_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC
from schemas.inbound_contract import InboundContract


log = get_logger(__name__)


class StoreAssetDatasetBody(InboundContract):
    """The fields a caller supplies to ask for a dataset.

    EVERY FIELD HERE IS IDENTITY (tj-vhboky.1 section 2). Two requests name the same dataset only
    if they agree on all of owner, asset_symbol, asset_type, data_type, source, granularity,
    expiry_type, update_type, start and end -- the range included. They back a UNIQUE constraint,
    which is why the policy fields below are NOT optional: Postgres treats NULL as distinct from
    NULL in a unique index, so a request that explicitly sent null would write a NULL into a key
    column, the ON CONFLICT would never fire against it, and "an exact repeat returns the existing
    id" would silently become "an exact repeat creates a second row".

    THERE IS DELIBERATELY NO feed FIELD HERE, though an earlier version of this model carried one
    as `Feed | None = None`. tj-rh4b7f (2026-09-25) DEFERRED both the entry's feed column and feed
    as an accepted create-request field to the gRPC transport work. The reason is write order, not
    taste: data/store/app/ingest/data_action_request.py upserts the entry FROM THIS BODY and only
    then calls ingest, so at the moment the entry row is written nothing has resolved a feed yet --
    and feed is identity, so a placeholder written now and corrected later would MUTATE identity
    and silently merge two datasets that asked for different tapes. Deferring costs nothing today:
    no caller can select a feed, because there is exactly one feed per deployment and the ingest
    adapter alone decides it.

    THE FIELD ALSO HAD TO GO FOR A MORE IMMEDIATE REASON, AND IT IS THE QUIET KIND. StoreDatasetEntry
    has no feed column, and upsert_entry builds its values through AppBase.get_fields, which
    enumerates __table__.columns and keeps only schema attributes that match one
    (common/database/sql_alchemy_table.py, _get_columns). A field the table does not have matches
    neither branch and falls out with no else, no warning and no log -- so a caller that named a
    tape got a 200 and an entry that silently did not record it. Not a crash: a wrong answer,
    which is why leaving the field in place until the transport work would have been worse than
    removing it. See tj-rh4b7f for the full reasoning and for where feed comes back.
    """

    # The caller's declared principal. NO DEFAULT, because it is identity: a default principal
    # would put every strategy that forgot to name itself onto one shared dataset, which is the
    # problem owner-scoped writes exist to prevent. Note what this does and does not buy -- the
    # single instance secret authenticates THE DEPLOYMENT, not the caller, so "only the owner may
    # edit" is enforced against MISTAKES, not against anyone holding the key.
    owner: str

    source: DataSource

    granularity: Granularity
    # REQUIRED, and no sentinel (tj-vhboky.1, ruling closing open question 2). An open start would
    # mean "from the beginning of time", which no vendor serves, and a dataset with no declared
    # beginning cannot be checked for growth-only extension. The column is NOT NULL and the entry
    # upsert drops None values, so an absent start used to fail on a constraint deep in the write
    # rather than on validation at the edge. Fixed by making the body honest, not the column loose.
    start: datetime
    end: datetime | None = None

    # default_factory, not a computed default: a plain default is evaluated once at import, so
    # every instance in a long-lived process would share an expiry frozen at process start.
    # UTC, not naive local: this is stored as timestamptz, and datetime.now() with no tzinfo makes
    # "when does this data die" an environment-dependent answer.
    expiry: datetime | None = Field(default_factory=lambda: datetime.now(UTC) + timedelta(days=1))
    expiry_type: ExpiryType = ExpiryType.BULK
    update_type: UpdateType = UpdateType.STATIC

    @model_validator(mode='after')
    def validate_fields(cls, request: 'StoreAssetDatasetBody') -> 'StoreAssetDatasetBody':
        log.debug(f'Validating request: {request}')
        if request.update_type is not UpdateType.STATIC and request.end is not None:
            raise ValueError(f"The 'update_type' field must be '{ExpiryType.BULK.value}' when 'end' is provided.")

        if request.update_type is not UpdateType.STATIC and request.expiry_type is ExpiryType.BULK:
            raise ValueError(
                f"The 'update_type' field must be '{UpdateType.STATIC.value}' "
                f"when 'expiry_type' is '{ExpiryType.BULK.value}'."
            )
        return request

    @field_validator('expiry_type', mode='before')
    def validate_expiry_type(cls, value):
        return ExpiryType.validate(value)

    @field_validator('update_type', mode='before')
    def validate_update_type(cls, value):
        return UpdateType.validate(value)

    model_config = ConfigDict(json_encoders={ExpiryType: ExpiryType.encoder, UpdateType: UpdateType.encoder})


class StoreAssetDatasetPath(InboundContract):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)
    asset_symbol: str = Field(..., description=SYMBOL_DESC)

    @field_validator('asset_symbol')
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class StoreAssetDatasetQuery(InboundContract):
    # Same as body, but with optional fields. Optional is correct HERE and wrong on the body:
    # this is a search filter, where an absent field means "no constraint on that column", not a
    # value written into an identity column.
    owner: str | None = None
    source: DataSource | None = None
    # No feed filter, for the same reason the body has no feed field: there is no feed column on
    # store_dataset_entry to filter (tj-rh4b7f). Here it was not merely inert, it was a live
    # AttributeError -- search_entries loops this model's model_dump() and calls
    # getattr(StoreDatasetEntry, column) for every value that is not None
    # (data/store/app/database/crud/stock/store_dataset_entry.py), so any search that actually
    # named a tape raised rather than filtered.
    granularity: Granularity | None = None
    start: datetime | None = None
    end: datetime | None = None
    expiry_type: ExpiryType | None = None
    update_type: UpdateType | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator('expiry_type', mode='before')
    def validate_expiry_type(cls, value):
        if value is None:
            return value
        return ExpiryType.validate(value)

    @field_validator('update_type', mode='before')
    def validate_update_type(cls, value):
        if value is None:
            return value
        return UpdateType.validate(value)

    model_config = ConfigDict(json_encoders={ExpiryType: ExpiryType.encoder, UpdateType: UpdateType.encoder})


class AssetDatasetStoreCreate(StoreAssetDatasetPath, StoreAssetDatasetBody):
    pass


class AssetDatasetStoreUpdate(AssetDatasetStoreCreate):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreGetById(InboundContract):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreDelete(InboundContract):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStore(AssetDatasetStoreUpdate):
    id: UUID

    item_count: int
    expiry: datetime | None = None

    created_at: datetime
    updated_at: datetime

    # This one is what we SEND, and extra=forbid is inherited rather than intended. It is
    # harmless here: with from_attributes the validator only ever looks up declared field names
    # on the ORM row, so there is no extra for it to reject.
    model_config = ConfigDict(from_attributes=True)
