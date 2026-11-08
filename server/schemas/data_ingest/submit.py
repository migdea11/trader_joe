"""The domain side of the internal Submit and Instance contract (tj-7zjov1.15; contract: tj-7zjov1.13).

Not generated, and generating nothing: the same ADR tj-8konfu D1 arrangement as fetch_dataset.py, with the
mapping written by hand in common/rpc/mapping/submit.py. Nothing here may import trader_joe.proto.

EVERY FIELD NAME MATCHES ITS PROTO FIELD NAME, as in fetch_dataset.py, so the descriptor-driven control
needs no name table. The oneofs (SubmitRequest.kind, SubmitAck.outcome) are declared arm by arm with an
exactly-one validator, as FetchAck is.

WHAT IS DELIBERATELY ABSENT:
    The refused arm's own model.  A refusal is the FetchRefused of fetch_dataset.py: same message on the
                          wire, same meaning, and it is permanent for the request as asked.
    The bare FetchAck.  A Submit ack differs from a FetchAck in one way that matters: the resolved feed is
                          a fact about BARS only, so SubmitAccepted.feed is None for the other kinds.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.sensitive import SensitiveStr
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.inbound_contract import InboundContract


# attempt_count is a uint32 on the wire. The domain refuses a count outside that range before encoding.
MAX_ATTEMPT_COUNT = 2**32 - 1


class RequestKind(StrEnum):
    """Which arm of SubmitRequest.kind a request is, and so what its answer may carry."""

    BARS = 'bars'
    ASSET_LIST = 'asset_list'
    CORPORATE_ACTIONS = 'corporate_actions'


class BarsRequest(InboundContract):
    """Bars for one instrument: what FetchDatasetRequest asks for, minus the envelope fields."""

    source: DataSource
    asset_symbol: str
    asset_type: AssetType
    # At least one. An empty list is refused by the reader, not served as an empty fetch.
    data_types: list[DataType]
    granularity: Granularity
    # Inclusive.
    start: AwareDatetime
    # None is an OPEN END, served up to the reader's as_of.
    end: AwareDatetime | None = None
    update_type: UpdateType
    # None means the deployment decides (FEED_UNSPECIFIED on the wire); a value is checked, never selected.
    feed: Feed | None = None


class AssetListStatus(StrEnum):
    """Which of the vendor's two asset lists a request wants. There is no member for "unspecified"."""

    ACTIVE = 'active'
    INACTIVE = 'inactive'


class AssetListRequest(InboundContract):
    """The vendor's asset list for one source, as it stands now."""

    source: DataSource
    asset_type: AssetType
    # Required: a request always names which list it wants.
    status: AssetListStatus


class CorporateActionsRequest(InboundContract):
    """Corporate actions for one instrument over an EXPLICIT window; ingest never defaults it."""

    source: DataSource
    asset_symbol: str
    # Inclusive.
    start: AwareDatetime
    # Exclusive.
    end: AwareDatetime


class SubmitRequest(InboundContract):
    """One request of any kind, as the store hands it to ingest."""

    # The store's durable handle and the idempotency key: required and never empty.
    request_id: str = Field(min_length=1)
    # Sensitive like FetchDatasetRequest.owner: out of repr and str.
    owner: SensitiveStr
    # RESERVED. None is the empty string on the wire. Carried and unused.
    client: str | None = Field(default=None, min_length=1)
    # None means no deadline and the request never expires; it is unset on the wire, never the epoch.
    ready_by: AwareDatetime | None = None
    best_effort: bool = False
    # True when the store re-submits a request an earlier ingest instance held (restart ruling, tj-26jt9w
    # addendum 5).
    recovered: bool = False
    # The attempts the store has already counted. uint32 on the wire, so a negative count is refused here
    # and never reaches the encoder.
    attempt_count: int = Field(default=0, ge=0, le=MAX_ATTEMPT_COUNT)

    bars: BarsRequest | None = None
    asset_list: AssetListRequest | None = None
    corporate_actions: CorporateActionsRequest | None = None

    @model_validator(mode='after')
    def _exactly_one_kind(self) -> Self:
        """Refuse a request that sets two kinds or none, as the oneof holds one arm.

        Returns:
            Self: The validated request.

        Raises:
            ValueError: If the number of kinds set is not one.
        """
        if sum(arm is not None for arm in (self.bars, self.asset_list, self.corporate_actions)) != 1:
            raise ValueError('a SubmitRequest sets exactly one of bars, asset_list or corporate_actions')
        return self

    @property
    def kind(self) -> RequestKind:
        """RequestKind: Which kind this request is."""
        if self.bars is not None:
            return RequestKind.BARS
        if self.asset_list is not None:
            return RequestKind.ASSET_LIST
        return RequestKind.CORPORATE_ACTIONS


class SubmitAccepted(InboundContract):
    """The request is held. Nothing in it is data."""

    # The resolved feed for BARS; None for asset_list and corporate_actions, which have no tape. A nominal
    # feed on those would record a false fact, so the mapper refuses one.
    feed: Feed | None = None
    # A non-secret configured label: the quota scope this request is charged to.
    quota_key: str = Field(min_length=1)


class SubmitAck(InboundContract):
    """Accepted or refused, exactly one. A refusal arises only for bars."""

    accepted: SubmitAccepted | None = None
    refused: FetchRefused | None = None

    @model_validator(mode='after')
    def _exactly_one_arm(self) -> Self:
        """Refuse an ack that sets both arms or neither.

        Returns:
            Self: The validated ack.

        Raises:
            ValueError: If both arms are set, or neither.
        """
        if (self.accepted is None) == (self.refused is None):
            raise ValueError('a SubmitAck sets exactly one of accepted or refused, as its oneof holds one arm')
        return self


class SubmitResponse(InboundContract):
    """The answer to a Submit: the ack, and the instance that gave it."""

    ack: SubmitAck
    # The id of the ingest process that answered, on accepted and refused alike.
    instance_id: str = Field(min_length=1)


class InstanceRequest(InboundContract):
    """The Instance probe carries nothing."""


class InstanceResponse(InboundContract):
    """Which ingest process is answering, and when it started."""

    instance_id: str = Field(min_length=1)
    started_at: AwareDatetime
