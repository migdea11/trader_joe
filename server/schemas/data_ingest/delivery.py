"""The domain side of the internal result-path contract: DeliverAttempt and RegisterInstance (tj-7zjov1.16).

Not generated, and generating nothing: the same ADR tj-8konfu D1 arrangement as submit.py, with the mapping
written by hand in common/rpc/mapping/delivery.py. Nothing here may import trader_joe.proto. Contract:
proto/trader_joe/proto/internal/store/v1/store.proto.

EVERY FIELD NAME MATCHES ITS PROTO FIELD NAME, so the descriptor-driven control needs no name table. A
proto enum zero (_UNSPECIFIED) has no member here; a field whose absence is meaningful is None.

WHAT IS REUSED. The store contract restates BarPage and ServedRange because a contract never imports
another contract, and the two shapes are the same on purpose (store.proto, header). The domain keeps ONE
model for each: fetch_dataset.BarPage and fetch_dataset.ServedRange, which the store mappers produce and
consume.

WHAT IS DELIBERATELY ABSENT:
    DeliverAttemptRequest  the stream's envelope is pure transport. A whole attempt is the AttemptDelivery
                           below; the mapper splits it across messages and the assembler puts it back.
    cross-field verdict rules  that an EXPIRED or EXHAUSTED trailer carries no stats, that error_class belongs to FAILED,
                           and what a given outcome means for the request are the STORE's judgement
                           (tj-7zjov1.27). A model here that refused a combination would make the
                           descriptor-driven round trip unable to populate it, and would put a verdict in
                           a layer that only carries one.
    credential material    none crosses the hop. quota_key is a non-secret configured label and
                           vendor_asset_id is the vendor's public instrument key, opaque.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, NonNegativeInt, model_validator

from common.enums.data_stock import Feed
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange
from schemas.inbound_contract import InboundContract


# attempt_no, attempt_count and the call counts are uint32 / uint64 on the wire.
MAX_UINT32 = 2**32 - 1
MAX_UINT64 = 2**64 - 1


class DeliveryOutcome(StrEnum):
    """How an attempt ended. Names match store.proto's DeliveryOutcome with the prefix removed."""

    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    # Taken off the queue after its ready_by: never sent to the vendor, so not an attempt.
    EXPIRED = 'EXPIRED'
    # Arrived with its attempt budget already spent: never sent to the vendor, so not an attempt.
    EXHAUSTED = 'EXHAUSTED'


class DeliveryErrorClass(StrEnum):
    """What kind of failure. Names match store.proto's DeliveryErrorClass with the prefix removed."""

    TRANSIENT = 'TRANSIENT'
    RATE_LIMITED = 'RATE_LIMITED'
    PERMANENT = 'PERMANENT'


class DeliveryVerdict(StrEnum):
    """What the store tells ingest to do next. Names match store.proto's DeliveryVerdict."""

    CONTINUE = 'CONTINUE'
    FINAL = 'FINAL'


class DeliveryHeader(InboundContract):
    """ALWAYS the first message of an attempt: everything the store needs to place it."""

    # The store's durable handle for the request, and the first half of the idempotency key.
    request_id: str = Field(min_length=1)
    # The ingest process making this report; the id it minted at start.
    instance_id: str = Field(min_length=1)
    # Numbered from the request's durable attempt count plus one, so at least 1 (store.proto). The second
    # half of the idempotency key: a redelivery sends the same number.
    attempt_no: int = Field(ge=1, le=MAX_UINT32)
    # A NON-SECRET CONFIGURED LABEL, never key material and never a hash of it. Empty on an EXPIRED or EXHAUSTED report.
    quota_key: str = ''
    # The resolved tape for a bars attempt; None (FEED_UNSPECIFIED) for the other kinds. Never a nominal feed.
    feed: Feed | None = None
    # The vendor's key for the instrument, OPAQUE: nothing here parses, compares or interprets it. Empty
    # when the attempt has no single instrument, failed before the vendor named one, expired or exhausted.
    vendor_asset_id: str = ''


class VendorReported(InboundContract):
    """What the vendor's response headers said about its limit, when they said anything."""

    # Absent is not zero: proto3 optional gives these real presence.
    limit: int | None = Field(default=None, ge=0, le=MAX_UINT64)
    remaining: int | None = Field(default=None, ge=0, le=MAX_UINT64)
    reset_at: AwareDatetime | None = None


class VendorCallStats(InboundContract):
    """The vendor calls one attempt made, counted by ingest."""

    calls: int = Field(ge=0, le=MAX_UINT64)
    pages: int = Field(ge=0, le=MAX_UINT64)
    throttled_429: int = Field(ge=0, le=MAX_UINT64)
    retries: int = Field(ge=0, le=MAX_UINT64)
    first_call_at: AwareDatetime | None = None
    last_call_at: AwareDatetime | None = None
    # None when the vendor reported nothing. Corroboration only.
    vendor_reported: VendorReported | None = None


class DeliveryTrailer(InboundContract):
    """ALWAYS the last message of an attempt."""

    outcome: DeliveryOutcome
    # None (UNSPECIFIED) unless the outcome is FAILED. The store judges the combination, not this model.
    error_class: DeliveryErrorClass | None = None
    # For a human, never parsed.
    detail: str = ''
    # What the attempt cost at the vendor, failed attempts included. None on EXPIRED and EXHAUSTED.
    vendor_stats: VendorCallStats | None = None
    # The window actually served, on SUCCEEDED for bars and corporate actions.
    served_range: ServedRange | None = None
    # When the vendor answered. None on EXPIRED and EXHAUSTED.
    as_of: AwareDatetime | None = None
    # Set by ingest when its attempt budget is spent with this attempt. Meaningful only on FAILED.
    last_attempt: bool = False


class AssetRecord(InboundContract):
    """One vendor asset, carried RAW: a few vendor-neutral fields and the vendor's own payload untouched."""

    # The vendor's key, opaque.
    vendor_asset_id: str
    # The ticker the vendor lists it under now.
    symbol: str
    # The vendor's record as received, byte for byte.
    raw_payload: bytes
    # PRESENCE, NOT A VALUE: None means the vendor said nothing, and is never read as inactive or untradable.
    active: bool | None = None
    tradable: bool | None = None
    # The vendor's venue wording, uninterpreted. Empty when the vendor gave none.
    exchange: str = ''
    # The vendor's name for the asset. Empty when the vendor gave none.
    name: str = ''


class AssetRecords(InboundContract):
    """A bounded message of asset records. A list of any length is split across several of these."""

    records: list[AssetRecord]


class CorporateActionRecord(InboundContract):
    """One vendor corporate action, carried RAW. The dates are the vendor's; None when it gave none."""

    # The vendor's id for the action, opaque.
    vendor_action_id: str
    # The vendor's type wording, uninterpreted.
    action_type: str
    ex_date: AwareDatetime | None = None
    record_date: AwareDatetime | None = None
    payable_date: AwareDatetime | None = None
    process_date: AwareDatetime | None = None
    effective_date: AwareDatetime | None = None
    # Instrument identifiers the vendor gave, keyed by the vendor's own name.
    identifiers: dict[str, str] = Field(default_factory=dict)
    raw_payload: bytes


class CorporateActionRecords(InboundContract):
    """A bounded message of corporate-action records."""

    records: list[CorporateActionRecord]


type DeliveryPayload = BarPage | AssetRecords | CorporateActionRecords
"""One payload message of an attempt. All the payload messages of one attempt are of the one kind."""


class AttemptDelivery(InboundContract):
    """One whole attempt: the header, the payload, the trailer.

    The unit ingest hands the delivery client and the store's servicer hands its handler. The payload is a
    list of messages because the wire bounds each one; a consumer reads the records of all of them.
    """

    header: DeliveryHeader
    parts: list[DeliveryPayload] = Field(default_factory=list)
    trailer: DeliveryTrailer

    @model_validator(mode='after')
    def _one_payload_kind(self) -> Self:
        """Refuse an attempt whose payload mixes kinds, as the contract says all parts are of one kind.

        Returns:
            Self: The validated attempt.

        Raises:
            ValueError: If the parts are not all of the one type.
        """
        if len({type(part) for part in self.parts}) > 1:
            raise ValueError('the payload messages of one attempt are all of one kind (bars, assets or actions)')
        return self


class DeliverAttemptResponse(InboundContract):
    """THE ACKNOWLEDGEMENT: sent only after the attempt is committed."""

    verdict: DeliveryVerdict
    # The store's durable attempt count after this report.
    attempt_count: NonNegativeInt = Field(le=MAX_UINT32)


class RegisterInstanceRequest(InboundContract):
    """Ingest announces the instance id it minted at start."""

    instance_id: str = Field(min_length=1)
    started_at: AwareDatetime


class RegisterInstanceResponse(InboundContract):
    """Nothing to say: the call succeeding is the acknowledgement."""
