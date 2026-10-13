"""THE CLIENT SEAM for the internal Quote call: an interface in domain terms, and gRPC behind it (tj-grna9p.44).

data_store calls this to learn what a fetch would cost and what capacity is left, and never imports
trader_joe.proto (ADR tj-8konfu D3; ruff's TID251 enforces it). What crosses the seam is domain objects and
TraderJoeErrors.

THE ANSWER IS INFORMATIONAL. The caller uses it for the estimate card, a request's stored snapshot and the
quota cap, and never to refuse a request (decision tj-grna9p.9, addendum of 2026-10-06).

UNARY, so it takes common.rpc.channel.unary_call_options(): a call issued while the peer starts waits for the
channel, bounded by the call's own deadline. Until data_ingest serves Quote (tj-grna9p.51) every call is
answered UNIMPLEMENTED with no google.rpc.Status, which from_rpc_error reports as PEER_PROTOCOL_ERROR: a typed
TraderJoeError, never a raw grpc error. That is the expected behaviour until then.

THE SAME OBLIGATION AS ingest_fetch.py applies: a servicer that calls this converts the peer's error
deliberately rather than letting it escape into the error boundary.
"""

import math
from typing import Final, Protocol

import grpc

from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.channel import unary_call_options
from common.rpc.errors import from_rpc_error
from common.rpc.mapping.quote import quote_request_to_proto, quote_to_domain
from common.rpc.mapping.values import ProtoMappingError
from schemas.data_ingest.fetch_dataset import FetchDatasetRequest
from schemas.data_ingest.quote import QuoteResult
from trader_joe.proto.internal.ingest.v1 import ingest_pb2_grpc


# A quote does no vendor call of its own: it reads the pool's counters, so it is a cheap unary call. The
# bound is for a peer that is starting or stuck, and is a DEFAULT the caller may override.
DEFAULT_QUOTE_DEADLINE_S: Final = 10.0


class IngestQuoteClient(Protocol):
    """One quote, in domain terms. The interface a test double or a replay backend implements."""

    async def quote(self, request: FetchDatasetRequest) -> QuoteResult:
        """Quote one fetch.

        Args:
            request: The fetch to quote. One fetch per quote: the caller sums the quotes of a
                multi-symbol request.

        Returns:
            QuoteResult: The estimated vendor calls, the scope that would be charged and every scope's
            capacity now.

        Raises:
            TraderJoeError: If the call fails or the answer cannot be read.
        """
        ...


class GrpcIngestQuoteClient:
    """IngestQuoteClient over grpc.aio, against data_ingest's IngestService.

    It takes a channel from its caller and reads no environment variable, like GrpcIngestFetchClient.
    """

    def __init__(self, channel: grpc.aio.Channel, *, deadline_s: float = DEFAULT_QUOTE_DEADLINE_S) -> None:
        """Bind the client to a channel and a deadline.

        Args:
            channel: A channel from common.rpc.channel.create_channel, owned and closed by the caller.
            deadline_s: How long one quote may take, in seconds from the call.

        Raises:
            ValueError: If the deadline is not a finite, positive number of seconds.
        """
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise ValueError(f'a Quote call needs a finite, positive deadline in seconds, got {deadline_s!r}')
        self._stub = ingest_pb2_grpc.IngestServiceStub(channel)
        self._deadline_s = deadline_s

    @property
    def deadline_s(self) -> float:
        """float: How long one quote may take, in seconds."""
        return self._deadline_s

    async def quote(self, request: FetchDatasetRequest) -> QuoteResult:
        """Quote one fetch over gRPC.

        Args:
            request: The fetch to quote.

        Returns:
            QuoteResult: The answer, in domain terms.

        Raises:
            TraderJoeError: What from_rpc_error reads out of a failed call (PEER_UNAVAILABLE, DEADLINE,
                PEER_INTERNAL, PEER_PROTOCOL_ERROR for an UNIMPLEMENTED answer, or the peer's own typed
                reason), or PEER_PROTOCOL_ERROR for an answer this release cannot read.
            ProtoMappingError: If the REQUEST cannot be put on the wire. That is this service's own bug,
                not the peer's, and it is deliberately not dressed up as a peer error.
        """
        # Outside the try on purpose: an unmappable request is ours, and must not be reported as the peer's.
        message = quote_request_to_proto(request)
        try:
            response = await self._stub.Quote(message, **unary_call_options(self._deadline_s))
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        try:
            return quote_to_domain(response)
        except ProtoMappingError as error:
            raise ExogenousError(
                Reason.PEER_PROTOCOL_ERROR, f'the Quote answer carried a message this release cannot read: {error}'
            ) from error
