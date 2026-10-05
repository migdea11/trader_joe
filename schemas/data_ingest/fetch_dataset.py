"""The domain side of the internal FetchDataset contract: request, ack, page and done (tj-3mk3u5.27).

THESE ARE NOT GENERATED, AND THEY GENERATE NOTHING. ADR tj-8konfu D1 keeps the .proto the source of
truth for what crosses the wire and Pydantic the internal domain representation, with NEITHER generated
from the other and a hand-written mapping between them in common/rpc (tj-3mk3u5.28). Nothing here may
import trader_joe.proto: ruff's TID251 bans the generated package outside common/rpc, which is the seam.

EVERY FIELD NAME BELOW MATCHES ITS PROTO FIELD NAME, deliberately. D1's control (tj-3mk3u5.29) is a
descriptor-driven test whose third step asserts that the domain model's field names, minus a reviewed
NOT_ON_WIRE set, equal the descriptor's field names under the mapper's name table. Renaming a field here
to read better in Python costs an entry in that table, so the names stay in step with
proto/trader_joe/proto/internal/ingest/v1/ingest.proto and .../market/v1/bar.proto.

WHAT IS DELIBERATELY ABSENT:
    FetchDatasetResponse  the stream's envelope is pure transport. The oneof arm is dispatched by the
                          client seam, which yields the ack, the pages and the done; no domain model
                          restates the envelope.
    dataset_id            a data_store row id that must not cross this wire. Correlation belongs to the
                          transport and the store stamps its own rows (ADR tj-8konfu D7.2; tj-rh4b7f).
    expiry, expiry_type   retention policy. Nothing under data/ingest reads either one.

DATETIMES ARE AWARE, ALWAYS. AwareDatetime REFUSES a naive datetime rather than converting it (the
tj-1bl90i rule, ruled on tj-vhboky.20): google.protobuf.Timestamp carries no offset, so a Timestamp
decoded without tzinfo is naive and names no instant. The round-trip control asserts the same instant and
an aware UTC result, never a preserved offset (tj-3mk3u5.22 Q4 = O-a).

NO ENUM HERE HAS AN "UNSPECIFIED" MEMBER, which is the point. The wire carries a zero
<ENUM>_UNSPECIFIED only because proto3 and buf require one; it has no Python counterpart, so a message
that leaves a required enum at zero cannot produce a valid model and is refused on decode. That is the
"no sentinel for we do not know" ruling (tj-vhboky.1) holding on the wire.
"""

from typing import Self

from pydantic import AwareDatetime, Field, NonNegativeInt, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.sensitive import SensitiveStr
from schemas.inbound_contract import InboundContract


class FetchDatasetRequest(InboundContract):
    """What data_store asks data_ingest for: exactly the fields a reader consumes, and nothing more.

    Established by reading data/ingest rather than by copying GetDatasetRequest, which carries several
    fields no reader touches.
    """

    # Identity on the store's entry, carried so the fetch knows which principal it acts for and may not
    # invent one downstream. Nothing under data/ingest/app reads it today. Sensitive (tj-vhboky.45): kept
    # out of repr and str, still carried by model_dump -- see common/sensitive.py.
    owner: SensitiveStr
    # Selects the reader; routers/data_ingest/fetch_dataset_handler.py picks it out of the handler's
    # injected readers mapping.
    source: DataSource
    asset_symbol: str
    # Selects the asset path. The stock reader then hard-codes AssetType.STOCK onto the instrument it
    # builds, so this is what makes an unsupported type a refusal instead of a silent stock read.
    asset_type: AssetType
    # At least one. An empty list is refused by the reader, not served as an empty fetch.
    data_types: list[DataType]
    granularity: Granularity
    # Inclusive.
    start: AwareDatetime
    # None is an OPEN END, "up to whatever is current", which the reader serves as its as_of. Optional
    # with a default, unlike the superseded GetDatasetRequest's required-but-nullable end: on this
    # contract an absent end is a real and ordinary request, not a field the caller forgot.
    end: AwareDatetime | None = None
    # Becomes the rate budget's priority: STREAM -> LIVE, STATIC -> BACKFILL, anything else ->
    # INTERACTIVE (data/ingest/app/brokers/rate_budget.py).
    update_type: UpdateType
    # None means "the deployment decides" and maps to FEED_UNSPECIFIED on the wire. A value means "this
    # tape, or a refused ack": ingest resolves the tape it is entitled to and can only CHECK a named feed
    # against it, never be steered by one (tj-3mk3u5.22 Q5).
    feed: Feed | None = None


class Bar(InboundContract):
    """One OHLCV bar, the unit a page carries.

    The domain twin of trader_joe.proto.market.v1.Bar. Bars are raw and never corrected, so there is no
    split or dividend factor here either.
    """

    # When the bar OPENS: the data time, never a server's response time. Named for ADR tj-r6vcgv B1,
    # which forbids a field called bare "timestamp" anywhere in a contract because an unqualified name
    # invites the data time and the response time to collapse into one. The response time on this
    # contract is the single FetchDone.as_of.
    bar_start: AwareDatetime
    open: float
    high: float
    low: float
    close: float
    # A float, as the broker-neutral reader produces it; the stored stock shape narrows it downstream.
    volume: float
    # Absent, not zero, when the vendor reports neither.
    trade_count: int | None = None
    vwap: float | None = None
    # The tape this bar came from. On this contract it is the same resolved feed for every bar of the
    # fetch, and the mapper stamps it from FetchAccepted.feed rather than deciding per bar, so the two
    # cannot disagree. It is carried per bar because market/v1 is shared vocabulary: a bar reaching the
    # UI or the external stream arrives with no fetch ack to read a feed from.
    feed: Feed


class BarPage(InboundContract):
    """A bounded page of bars.

    No dataset_id: correlation belongs to the transport. The page size is the server's to honour and is
    documented on the .proto; the wire cannot express it.
    """

    bars: list[Bar]


class FetchAccepted(InboundContract):
    """The fetch is viable. The store writes its dataset entry, feed included, on this."""

    # The RESOLVED feed: the tape this deployment is entitled to, decided once per fetch and the one
    # value only ingest may decide.
    feed: Feed


class FetchRefused(InboundContract):
    """The fetch is refused for its feed, and this is the stream's only event.

    An ADR tj-fa1rpu REFUSED outcome: permanent for the request as asked, so it carries no reset_at and no
    retry hint, because no wait cures it. The fields mirror google.rpc.ErrorInfo so the client seam builds
    the same domain error a REFUSED status would have produced.
    """

    # A value of the closed Reason vocabulary in common/errors, canonical in Python and not on the wire
    # (ADR tj-fa1rpu U1). Today only FEED_NOT_AVAILABLE. A str and NOT the Reason enum on purpose: a
    # client must survive a reason it has never heard of (D10), so an unrecognised one has to reach the
    # seam and become PEER_PROTOCOL_ERROR rather than failing validation here.
    reason: str
    # ERROR_DOMAIN, "trader-joe". (domain, reason) is the error's identity.
    domain: str
    # For a human, never parsed, and carrying no secret, vendor body or stack trace.
    detail: str
    # Allowlisted context, keys from common/errors METADATA_KEYS; for this reason, the feed asked for.
    metadata: dict[str, str] = Field(default_factory=dict)


class FetchAck(InboundContract):
    """ALWAYS the first event of the stream: either the fetch is accepted or its feed is refused.

    The domain twin of a proto oneof, so exactly one arm is set. Both arms are declared rather than a
    union of the two models, because the descriptor-driven control compares field names against the
    oneof's arm names.
    """

    accepted: FetchAccepted | None = None
    refused: FetchRefused | None = None

    @model_validator(mode='after')
    def _exactly_one_arm(self) -> Self:
        """Refuse an ack that sets both arms or neither, as a oneof can hold only one.

        Returns:
            Self: The validated ack.

        Raises:
            ValueError: If both arms are set, or neither is.
        """
        if (self.accepted is None) == (self.refused is None):
            raise ValueError('a FetchAck sets exactly one of accepted or refused, as its oneof holds one arm')
        return self


class ServedRange(InboundContract):
    """The window actually served, as opposed to the one asked for."""

    # Inclusive.
    start: AwareDatetime
    # EXCLUSIVE, and never open: a request with no end is served up to as_of, and that instant lands here.
    end: AwareDatetime


class FetchDone(InboundContract):
    """Terminates an accepted stream, so a stream that merely stops is distinguishable from one that finished.

    It carries the SERVED provenance of ADR tj-fa1rpu D2. "No data" is a success carrying provenance rather
    than an error, so a genuinely served but empty window ends here with bar_count 0, never with a failure.
    """

    bar_count: NonNegativeInt
    served_range: ServedRange
    # When the vendor answered. UTC.
    as_of: AwareDatetime


type FetchEvent = FetchAccepted | BarPage | FetchDone
"""What a fetch yields, in order: the accepted ack, then zero or more pages, then the done.

ONE HOME, AND IT IS HERE BECAUSE THIS IS A DOMAIN FACT (tj-47tzic). The union says what a fetch
produces, not how it travels, so it belongs beside the three models it unions and not in either
transport seam. Both common/rpc/ingest.py's FetchDatasetHandler and
common/rpc/clients/ingest_fetch.py's IngestFetchClient import this one alias, which is what makes
decision tj-tkm4tn D1's "one test double serves both ends" structural rather than coincidental.

IT USED TO BE DECLARED TWICE, once per seam, with identical text (ADDENDUM 2 to tj-tkm4tn). Being
PEP 695 aliases the two were distinct TypeAliasType objects that compare UNEQUAL, so the mirror
rested on nobody editing one side alone; a fourth arm on either would have left every double still
type-checking against the end it was written for. Re-declaring a local copy in a seam, or
re-exporting this name from common/rpc as a convenience, brings that back in a new shape.
"""
