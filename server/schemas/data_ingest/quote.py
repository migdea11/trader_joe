"""The domain side of the internal Quote call: what one fetch would cost, and what capacity is left (tj-grna9p.44).

NOT GENERATED, like schemas/data_ingest/fetch_dataset.py, and for the same reason: the .proto is the source
of truth for what crosses the wire, this module is the domain representation, and a hand-written mapping in
common/rpc/mapping/quote.py sits between them. Nothing here may import trader_joe.proto.

FIELD NAMES MATCH THE PROTO'S, so the mapping needs no translation table.

THE ANSWER IS INFORMATIONAL. data_store uses it for the estimate card, the request's stored snapshot and the
quota cap, and never to refuse a request (decision tj-grna9p.9, addendum of 2026-10-06).

NO CREDENTIAL MATERIAL. quota_key is a non-secret configured label, never key material and never a hash of
it; it is not the request's ownership key, which is a separate field.
"""

from pydantic import AwareDatetime, NonNegativeInt

from schemas.inbound_contract import InboundContract


class ScopeCapacity(InboundContract):
    """One quota scope's capacity at the time of the quote."""

    # A non-secret configured label for the scope.
    quota_key: str
    # Calls allowed per window. Zero when the scope declares no cap.
    cap: NonNegativeInt
    remaining: NonNegativeInt
    window_seconds: NonNegativeInt
    # None when the scope has no known reset.
    reset_at: AwareDatetime | None = None


class QuoteResult(InboundContract):
    """What one fetch would cost, and every scope's capacity now."""

    # The vendor calls the fetch would make. Paging and batching are broker-specific, so this is an
    # estimate.
    estimated_vendor_calls: NonNegativeInt
    # The scope ingest would charge now: a non-secret configured label.
    quota_key: str
    # Every scope in the pool.
    scopes: list[ScopeCapacity]
