from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_serializer,
    field_validator,
    model_validator,
)

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.enums.pydantic_enums import NamedIntEnum
from common.logging import get_logger
from common.sensitive import OptionalSensitiveStr, SensitiveStr
from schemas.data_store.field_descriptions import ASSET_DATASET_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC
from schemas.inbound_contract import InboundContract


log = get_logger(__name__)


def _member_names_schema(enum: type[NamedIntEnum]) -> WithJsonSchema:
    """Document an enum field as the string enum of member names that the wire carries.

    Args:
        enum: The enum whose member names are the documented values.

    Returns:
        WithJsonSchema: The replacement schema, used in validation and serialization mode alike.
    """
    return WithJsonSchema({'type': 'string', 'enum': [member.name for member in enum]})


# The documented type of expiry_type/update_type on the dataset-store models (user ruling A on
# tj-vhboky.38). Left alone, pydantic documents an IntEnum as {type: integer, enum: [1..5]}, which
# contradicts both the wire (serialize_enum_name below sends names) and the documented default
# ('BULK' is not in [1..5]). Schema only: validation and serialization are untouched, so the wire
# bytes do not change and NamedIntEnum.validate still accepts an integer, undocumented.
# FIELD-LOCAL, NOT A HOOK ON NamedIntEnum: GetDatasetRequest (schemas/data_ingest) still declares
# these same enums as plain integers, so a class-level schema would make that model lie. Its Kafka
# transport went on tj-3mk3u5.14 and it has no non-test importer left, but the model is still in
# the tree and so is the reason this annotation is per field rather than on the enum class.
ExpiryTypeByName = Annotated[ExpiryType, _member_names_schema(ExpiryType)]
UpdateTypeByName = Annotated[UpdateType, _member_names_schema(UpdateType)]


class StoreAssetDatasetBody(InboundContract):
    """The fields a caller supplies to ask for a dataset.

    CONTRACT CHANGE (user ruling 2026-10-06, tj-grna9p.71): expiry is optional and defaults to None,
    meaning the dataset never expires. It used to default to now + 1 day. A caller that relied on
    the old default and wants a dataset that lapses must now send an explicit expiry.

    EVERY FIELD HERE IS IDENTITY (tj-vhboky.1 section 2) WITH ONE NAMED EXCEPTION, feed, which is
    a preference rather than a value written -- see its own paragraph below. Two requests name the
    same dataset only
    if they agree on all of owner, asset_symbol, asset_type, data_type, source, granularity,
    expiry_type, update_type, start and end -- the range included. They back a UNIQUE constraint,
    which is why the policy fields below are NOT optional: Postgres treats NULL as distinct from
    NULL in a unique index, so a request that explicitly sent null would write a NULL into a key
    column, the ON CONFLICT would never fire against it, and "an exact repeat returns the existing
    id" would silently become "an exact repeat creates a second row".

    feed IS THE ONE FIELD HERE THAT IS NOT IDENTITY, which is why it is the one optional field
    among them. It is the CALLER'S PREFERENCE, never the value stored: absent means "the deployment
    decides", and a named tape means "this tape, or a refused ack" -- ingest resolves the tape it is
    entitled to and can only CHECK a named feed against it, never be steered by one (tj-3mk3u5.22
    Q5). The value that IS stored is a DIFFERENT VALUE WITH THE SAME NAME: the RESOLVED feed, which
    arrives on FetchAccepted and is declared, required, on AssetDatasetStoreCreate below. Read that
    model's docstring before touching either field; conflating the two is the defect this pair of
    declarations exists to keep apart.

    THE FIELD IS ACCEPTED NOW BECAUSE THE WRITE ORDER FINALLY ALLOWS IT. tj-rh4b7f (2026-09-25)
    deferred both the entry's feed column and feed as an accepted create-request field to the gRPC
    transport work, and the reason was write order rather than taste: data/store upserted the entry
    FROM THIS BODY and only then called ingest, so at the moment the row was written nothing had
    resolved a feed -- and feed is identity, so a placeholder written then and corrected later would
    MUTATE identity and silently merge two datasets that asked for different tapes. The FetchDataset
    cutover (tj-3mk3u5.10) reversed that order: the acknowledgement arrives FIRST and carries the
    resolved feed, early enough to write it, so the entry is written with a tape that is already
    decided and never with a placeholder. The entry's own column, its migration and the crud that
    writes it from the ack are tj-3mk3u5.31, which is blocked on this task so the two land in order.
    """

    # The caller's declared principal. NO DEFAULT, because it is identity: a default principal
    # would put every strategy that forgot to name itself onto one shared dataset, which is the
    # problem owner-scoped writes exist to prevent. Note what this does and does not buy -- the
    # single instance secret authenticates THE DEPLOYMENT, not the caller, so "only the owner may
    # edit" is enforced against MISTAKES, not against anyone holding the key.
    #
    # Sensitive (tj-vhboky.45): left out of the repr and str of this model and every model that
    # inherits it, so validate_fields' debug line below does not log it. model_dump and JSON are
    # unchanged -- see common/sensitive.py for what is and is not guarded.
    owner: SensitiveStr

    source: DataSource

    # The caller's OPTIONAL tape preference, directly below source because the two answer adjacent
    # questions: source names the VENDOR we ask, feed names the TAPE the answer comes from, and one
    # vendor can resell several (common/enums/data_stock.py). None is not "unknown" and not a
    # sentinel -- it is a real, ordinary request meaning "the deployment decides", and it is the
    # value almost every caller sends, because there is normally one feed per deployment.
    #
    # NOT the value written to the entry. AssetDatasetStoreCreate.feed is, and it is required; this
    # one is a preference the ack either honours or refuses. See both docstrings.
    #
    # ENUM NAMES ON THE WIRE COST NOTHING HERE, unlike expiry_type and update_type below. Feed is a
    # str enum whose value IS its member name, so pydantic already documents it as a string enum of
    # those names and already serialises the name; ExpiryType and UpdateType are NamedIntEnum, which
    # is the whole reason ExpiryTypeByName and serialize_enum_name exist. Adding feed to that
    # serializer would be a no-op that implied the opposite about this field's wire form.
    feed: Feed | None = None

    granularity: Granularity
    # REQUIRED, and no sentinel (tj-vhboky.1, ruling closing open question 2). An open start would
    # mean "from the beginning of time", which no vendor serves, and a dataset with no declared
    # beginning cannot be checked for growth-only extension. The column is NOT NULL and the entry
    # upsert drops None values, so an absent start used to fail on a constraint deep in the write
    # rather than on validation at the edge. Fixed by making the body honest, not the column loose.
    #
    # AwareDatetime on start, end AND expiry, and the choice is REFUSE, not convert (user ruling on
    # tj-1bl90i, 2026-09-27). All three land in timestamptz columns, where a naive value is read in
    # the SESSION timezone -- an environment-dependent instant, silently. Assuming UTC for the
    # caller was offered and declined: it is the same guess, just moved into our code. So a value
    # with no offset is a 422 naming the field, in line with InboundContract's reject-don't-guess
    # stance. These annotations are inherited by AssetDatasetStoreCreate, AssetDatasetStoreUpdate
    # and the read model AssetDatasetStore (start/end); that is safe because every one of them is
    # either built from this body or read off timestamptz columns, which always come back aware.
    start: AwareDatetime
    end: AwareDatetime | None = None

    # OPTIONAL, DEFAULT None: NO EXPIRY (user ruling 2026-10-06 on tj-grna9p.71, option A; PUBLIC
    # CONTRACT CHANGE, see the class docstring). This used to default to now + 1 day, so a caller who
    # said nothing got a dataset that was due to die tomorrow; it is now null, "never expires", and
    # a time is stored only when the caller sets one. Both null and omission mean the same thing.
    #
    # The earlier reason this was NOT optional no longer holds: it was that BaseGetDatasetRequest
    # .expiry (schemas/data_ingest/get_dataset_request.py) is required and the store splatted this
    # body into it. That path is gone (data/store/app/ingest/data_action_request.py builds a
    # FetchDatasetRequest field by field, which carries no expiry), and the entry column is
    # nullable (tj-vhboky.1 Amendment 1 item P). Nothing is written into a unique-key column by
    # this, because expiry is not identity.
    #
    # AwareDatetime when set, REFUSE not convert (the tj-1bl90i rule above). On DAILY and STREAM a
    # non-null expiry retires the dataset (tj-vhboky.1 addenda); a null one means it never does.
    expiry: AwareDatetime | None = None
    # json_schema_extra keeps the documented default the NAME the wire carries. The JSON schema's
    # default is encoded from the config, never from a field serializer, so without it the OpenAPI
    # default would silently turn from 'BULK' into 1 when json_encoders was replaced below.
    expiry_type: ExpiryTypeByName = Field(default=ExpiryType.BULK, json_schema_extra={'default': ExpiryType.BULK.name})
    update_type: UpdateTypeByName = Field(
        default=UpdateType.STATIC, json_schema_extra={'default': UpdateType.STATIC.name}
    )

    @model_validator(mode='after')
    def validate_fields(self) -> Self:
        log.debug(f'Validating request: {self}')
        if self.update_type is not UpdateType.STATIC and self.end is not None:
            raise ValueError(f"The 'update_type' field must be '{UpdateType.STATIC.name}' when 'end' is provided.")

        if self.update_type is not UpdateType.STATIC and self.expiry_type is ExpiryType.BULK:
            raise ValueError(
                f"The 'update_type' field must be '{UpdateType.STATIC.name}' "
                f"when 'expiry_type' is '{ExpiryType.BULK.name}'."
            )
        return self

    # RANGES ARE HALF-OPEN [start, end) (tj-vhboky.1 addendum, 2026-09-30), so a declared end that is
    # not after the start names an entry nothing can ever fill: end == start is empty, end < start
    # never meant anything. Refused at the edge as a 422. end None (open-ended) is untouched. Both
    # bounds are AwareDatetime, so the comparison is between instants. Inherited by the create and
    # update models and by the read model AssetDatasetStore; the latter is safe because no stored
    # row violates it (tj-86g751.1, waived by the user as the data is disposable). Not applied to
    # StoreAssetDatasetQuery: an empty read window is a valid query that returns nothing.
    @model_validator(mode='after')
    def validate_range_is_not_empty(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError(
                f"The 'end' field must be after the 'start' field: ranges are half-open [start, end), "
                f'so end ({self.end.isoformat()}) must be later than start ({self.start.isoformat()}).'
            )
        return self

    @field_validator('expiry_type', mode='before')
    def validate_expiry_type(cls, value):
        return ExpiryType.validate(value)

    @field_validator('update_type', mode='before')
    def validate_update_type(cls, value):
        return UpdateType.validate(value)

    # Enum NAMES on the wire, not the integer values (decision tj-vhboky.30): GET /store's JSON
    # carries 'BULK'/'STATIC' and the private SDK reads them. JSON only, so model_dump() still
    # yields the enum members. Replaces the deprecated json_encoders; no return annotation, so
    # the JSON schema is left exactly as it was. Subclasses inherit it.
    @field_serializer('expiry_type', 'update_type', when_used='json-unless-none')
    def serialize_enum_name(self, value: ExpiryType | UpdateType):
        return value.name


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
    # value written into an identity column. Sensitive: OptionalSensitiveStr, never
    # `SensitiveStr | None`, which silently keeps the value in the repr (common/sensitive.py).
    owner: OptionalSensitiveStr = None
    source: DataSource | None = None
    # An optional feed FILTER, which is what "feed on the entry" means on the read side: having
    # recorded which tape served a dataset, a caller must be able to ask for one. Optional for the
    # same reason every filter here is -- absent means "no constraint on that column" -- and the
    # body's reasoning about identity does not apply, because nothing here is written.
    #
    # IT NEEDS THE COLUMN TO EXIST, AND THE COLUMN IS tj-3mk3u5.31's. search_entries loops this
    # model's model_dump() and calls getattr(StoreDatasetEntry, column) for every value that is not
    # None (data/store/app/database/crud/stock/store_dataset_entry.py), so a filter with no column
    # behind it is not inert, it is a live AttributeError on any search that names a tape -- which
    # is why this field was removed under tj-rh4b7f rather than left declared. .31 is blocked on
    # this task precisely so the column lands immediately after the filter it answers.
    feed: Feed | None = None
    granularity: Granularity | None = None
    # AwareDatetime on every time bound, and REFUSE, not convert (user ruling D2 = A on
    # tj-vhboky.20, the tj-1bl90i rule applied to the read side). These are compared against
    # timestamptz columns, where a naive bound is read in the SESSION timezone, so the same search
    # would return different rows on differently configured hosts. A value with no offset is a 422
    # naming the field; a caller typing ?start=2026-01-01 must add an offset or Z.
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    expiry_type: ExpiryTypeByName | None = None
    update_type: UpdateTypeByName | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

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

    # Same wire form as StoreAssetDatasetBody's serializer; 'unless-none' keeps an absent filter null.
    @field_serializer('expiry_type', 'update_type', when_used='json-unless-none')
    def serialize_enum_name(self, value: ExpiryType | UpdateType):
        return value.name


class AssetDatasetStoreCreate(StoreAssetDatasetPath, StoreAssetDatasetBody):
    """The entry as it is WRITTEN: the caller's body, the path, and the tape that was resolved.

    THIS MODEL'S feed AND THE BODY'S ARE TWO DIFFERENT VALUES THAT SHARE A NAME, and keeping them
    apart is the point of declaring it twice. StoreAssetDatasetBody.feed is what the CALLER asked
    for and may be absent. This one is the RESOLVED feed, read off FetchAccepted -- the single value
    only ingest may decide (tj-3mk3u5.22 Q5) -- and it is what the entry row records. Building this
    model by passing the body's feed straight through would write a preference where a resolution
    belongs, and on an identity column, so the two would be indistinguishable afterwards.

    THE OVERRIDE IS DELIBERATE: the inherited field is `Feed | None = None` and this one is
    required, so a value that was never resolved cannot reach the write by being forgotten. There
    is no sentinel to fall back on either -- Feed carries no UNKNOWN member (tj-vhboky.1, ruling of
    2026-09-25) -- so an unresolved feed is a loud failure at the boundary rather than a placeholder
    on an identity column that no later correction could rewrite.

    feed JOINS THE ENTRY'S IDENTITY once the column exists (tj-3mk3u5.31): two datasets that asked
    for the same window on different tapes are different datasets, which is the same reasoning that
    put feed on the bar's natural key (tj-u12tjo.11).
    """

    feed: Feed


class AssetDatasetStoreUpdate(AssetDatasetStoreCreate):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreGetById(InboundContract):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreDelete(InboundContract):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)

    # THE CALLER'S DECLARED PRINCIPAL ON AN ID-ADDRESSED DELETE. It is a FIELD ON THIS EXISTING
    # CLASS, and that is the whole reason this is a one-line change rather than a coordinated one:
    # routers/tests/interface_manifest/data_store.manifest records this type's QUALIFIED NAME, not
    # its fields, so adding one leaves the manifest -- the validator's file -- untouched, while
    # adding a parameter to delete_data would not. FastAPI derives it as a QUERY parameter, since
    # `owner` does not match the /store/{id} path template.
    #
    # Without it the route was broken outright: delete_entry_by_id(db, id, owner) grew a required
    # third parameter under tj-vhboky.6, and routers/data_store/asset_dataset_store.py had no way
    # to supply one, so every authenticated DELETE raised TypeError.
    #
    # OPTIONAL, AND THE REASON IS NOT THE ONE THAT APPLIES TO `start` AND `expiry` ABOVE -- read
    # this before "making it honest" like those two, because the conclusion here is the opposite
    # and it is opposite on purpose. Those are DATA fields feeding required downstream fields, so a
    # value they cannot supply is a malformed body and belongs in a 422. `owner` is an
    # AUTHORISATION ASSERTION: a DELETE naming a well-formed id is fully processable and is being
    # REFUSED, not found malformed. An absent owner is just the degenerate case of a WRONG owner,
    # which is uncontroversially a 403 -- so declaring this required would report an authorisation
    # failure as a validation error, and would differ in status code depending on whether the
    # caller got the principal wrong or omitted it. Optional keeps both answers the same.
    #
    # WHAT MAKES OPTIONAL SAFE IS A COLUMN CONSTRAINT, SO IT IS NAMED HERE RATHER THAN TRUSTED.
    # None must never AUTHORISE, and it cannot: _check_owner compares `existing.owner != declared`,
    # and StoreDatasetEntry.owner is Column(SensitiveString, nullable=False,
    # server_default='unassigned'), so existing.owner is never None and the comparison is always
    # true -- verified against both a normal owner and the migration's 'unassigned' server_default.
    # If that column ever became nullable, a None here would start matching legacy rows and this
    # field would turn into an authorisation bypass.
    #
    # The status codes are not set here: delete_data in routers/data_store/asset_dataset_store.py
    # maps OwnerMismatch to 403 and EntryNotFound to 404, so an owner-less delete is a 403.
    #
    # Sensitive (tj-vhboky.45): out of the repr and str, unchanged in model_dump (common/sensitive.py).
    owner: OptionalSensitiveStr = None


class AssetDatasetStore(AssetDatasetStoreUpdate):
    # feed IS HERE, REQUIRED, BY INHERITANCE FROM AssetDatasetStoreCreate, and that is a reported
    # value rather than a requested one: this model reads the entry's NOT NULL feed column back, so
    # the required declaration is what makes it report the tape the row actually holds. Not
    # redeclared, because there is nothing to change -- an optional one here would answer None for
    # a column that always has a value, which is the removed UNKNOWN sentinel under a new name
    # (the same reasoning AssetData.feed carries for the bar, tj-5dvgaa).
    id: UUID

    item_count: int
    expiry: datetime | None = None

    created_at: datetime
    updated_at: datetime

    # This one is what we SEND, and extra=forbid is inherited rather than intended. It is
    # harmless here: with from_attributes the validator only ever looks up declared field names
    # on the ORM row, so there is no extra for it to reject.
    model_config = ConfigDict(from_attributes=True)


class ServedRange(BaseModel):
    """The window the vendor ACTUALLY ANSWERED FOR, which is not the window that was asked for.

    ADR tj-fa1rpu D2: a SERVED outcome carries its own provenance. A vendor that holds only part of
    the requested range answers for that part, so these bounds can be NARROWER than the request's
    start and end, and comparing the two field by field is how a caller learns what it did not get.
    The range is never widened and never recomputed here: data_store copies it unchanged from the
    fetch's FetchDone (schemas/data_ingest/fetch_dataset.py, tj-3mk3u5.27).

    THE BOUNDS MEAN WHAT THE REQUEST'S OWN BOUNDS MEAN -- start inclusive, end exclusive -- so the
    two are comparable without a convention lookup (data/ingest/app/brokers/interface.py documents
    the same for the requested range).

    end IS NEVER NULL, though StoreAssetDatasetBody.end may be. An open request end means "up to
    whatever is current", and the fetch clamps it: end = min(requested end, as_of), with an open end
    served as as_of. So "open" is a property of the REQUEST only; the answer always names an instant.

    VALUES ARRIVE IN UTC AND ARE AWARE, ALWAYS. The model itself carries whatever offset it is
    handed, but in production these bounds have crossed the internal gRPC hop as a
    google.protobuf.Timestamp, which has no offset to carry (tj-3mk3u5.22 Q4), so what a client
    receives is the right instant expressed in UTC rather than the offset it happened to send:
    a start sent as '2026-01-01T00:00:00-05:00' comes back as '2026-01-01T05:00:00Z'. COMPARE THESE
    AS INSTANTS, NEVER AS STRINGS. AwareDatetime REFUSES a naive value rather than guessing a zone
    for it, the tj-1bl90i rule, which is what makes "an instant" true of every value here.
    """

    start: AwareDatetime
    end: AwareDatetime


class StoreAssetDatasetResponse(BaseModel):
    """What POST /store/{asset_type}/{data_type}/{asset_symbol} answers with on success.

    THIS EXISTS SO served_range IS VISIBLE TO A GENERATED CLIENT. The route used to return an
    undeclared dict, and the interface manifest recorded its response as '-'; a member added to an
    undeclared dict is absent from OpenAPI and therefore from every client generated out of it. The
    user ruled on 2026-10-02 that served_range is exposed in PR 2 precisely so a SERVED-BUT-EMPTY
    answer -- a misspelled symbol among them (tj-lldllr) -- reaches the caller rather than only an
    INFO line in our logs. Declaring the model is what makes that exposure real.

    message and data_points keep today's keys and today's meaning, so the only wire change is one
    added member: D2 applied at the edge, additive. Every 200 from the route carries served_range,
    including the empty window where data_points is 0; a failure is problem+json and carries none.

    A MODEL OF ITS OWN, NOT A RE-EXPORT of the internal FetchDone (tj-3mk3u5.27). This response is
    public and the gRPC hop it draws from is internal, so a re-export would let an internal change
    move the public contract silently. The semantics are kept in step by hand, deliberately.

    NO as_of, by user ruling of 2026-10-02 04:54 UTC: the ruling names served_range only, and a
    client that wants the vendor's answer time can query the data for it.
    """

    message: str
    data_points: int
    served_range: ServedRange
