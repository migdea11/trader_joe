from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_DATASET_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC
from schemas.inbound_contract import InboundContract


log = get_logger(__name__)


class StoreAssetDatasetBody(InboundContract):
    """The fields a caller supplies to ask for a dataset.

    EVERY FIELD HERE IS IDENTITY (tj-vhboky.1 section 2). Two requests name the same dataset only
    if they agree on all of owner, asset_symbol, asset_type, data_type, source, feed, granularity,
    expiry_type, update_type, start and end -- the range included. They back a UNIQUE constraint,
    which is why the policy fields below are NOT optional: Postgres treats NULL as distinct from
    NULL in a unique index, so a request that explicitly sent null would write a NULL into a key
    column, the ON CONFLICT would never fire against it, and "an exact repeat returns the existing
    id" would silently become "an exact repeat creates a second row".

    FEED IS THE ONE EXCEPTION AND IT IS NOT A LOOSENING. It is identity like the rest and its
    column is NOT NULL like the rest; what is optional is only the CALLER's obligation to choose
    a tape. The ingest adapter resolves it before anything is written, so no NULL ever reaches
    the key -- the optionality stops at the edge rather than travelling into the table.
    """

    # The caller's declared principal. NO DEFAULT, because it is identity: a default principal
    # would put every strategy that forgot to name itself onto one shared dataset, which is the
    # problem owner-scoped writes exist to prevent. Note what this does and does not buy -- the
    # single instance secret authenticates THE DEPLOYMENT, not the caller, so "only the owner may
    # edit" is enforced against MISTAKES, not against anyone holding the key.
    owner: str

    source: DataSource
    # OPTIONAL HERE, NOT NULL IN THE DATABASE, AND THE ADAPTER CLOSES THE GAP (tj-vhboky.1, user
    # ruling of 2026-09-25: "the API should be optional, but the ingest should specify it in the
    # data"). None means "I have no preference about the tape", which is a reasonable thing for a
    # caller to say and NOT the same as "nobody knows" -- by the time a row is written the ingest
    # adapter has resolved a concrete Feed, using the caller's selection if there is one and
    # otherwise the constant for a vendor with a single tape. That is why there is no UNKNOWN
    # member to default to any more: a value that should never reach a row is an error, not a
    # member of the vocabulary.
    feed: Feed | None = None

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
    feed: Feed | None = None
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
