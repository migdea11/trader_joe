"""The store contract's feed rule, checked on the wire form of a delivery header (tj-7zjov1.66).

Built from the store.proto comment on DeliveryHeader.feed, not from the ingest code: "For a BARS attempt it is
the resolved tape, and never FEED_UNSPECIFIED. For an ASSET_LIST or CORPORATE_ACTIONS attempt it is always
FEED_UNSPECIFIED: those kinds have no tape. An EXPIRED or EXHAUSTED report made no vendor call and carries the
request's kind rule unchanged, so a bars request that expired still names the resolved tape." The store's
delivery intake (P34, tj-7zjov1.27 rule e) refuses a bars report with no resolved feed as INVALID_REQUEST.

The header goes through the production encoder (common/rpc's header_to_proto) and the check reads the
store.proto enum value's NAME from the encoded message's own descriptor, so what is judged is what the store
would receive. The generated module itself is never imported: it is private to common/rpc (ADR tj-8konfu D3).
"""

from typing import Final

from common.enums.data_stock import Feed
from common.rpc.mapping.delivery import header_to_proto
from schemas.data_ingest.delivery import DeliveryHeader


# The wire names of the tapes a bars request can resolve to, written out rather than taken from the mapping.
_TAPE_ON_THE_WIRE: Final = {Feed.IEX: 'FEED_IEX', Feed.SIP: 'FEED_SIP'}


def wire_feed_name(header: DeliveryHeader) -> str:
    """The store.proto enum value name the header's feed is sent as, read from the encoded message."""
    wire = header_to_proto(header)
    feed_enum = wire.DESCRIPTOR.fields_by_name['feed'].enum_type
    return feed_enum.values_by_number[wire.feed].name


def assert_a_bars_report_names_the_tape(header: DeliveryHeader, tape: Feed) -> None:
    """A bars report, a no-call one included, names the resolved tape and never FEED_UNSPECIFIED."""
    name = wire_feed_name(header)
    assert name != 'FEED_UNSPECIFIED', 'a bars report with no resolved feed: the store refuses it'
    assert name == _TAPE_ON_THE_WIRE[tape], 'the report names a tape other than the resolved one'


def assert_a_no_tape_report_is_unspecified(header: DeliveryHeader) -> None:
    """A report for a kind with no tape is FEED_UNSPECIFIED on the wire."""
    assert wire_feed_name(header) == 'FEED_UNSPECIFIED', 'a kind with no tape named a feed'
