"""THE CLIENT SEAM for the internal Submit and Instance calls: domain terms in front, gRPC behind (tj-7zjov1.15).

data_store calls this and never imports trader_joe.proto (ADR tj-8konfu D3; ruff's TID251 enforces it). What
crosses the seam is domain objects and TraderJoeErrors.

SUBMIT ANSWERS AT ONCE AND CARRIES NO DATA. The result comes back on the store's own service, pushed by
ingest (tj-26jt9w, 2026-10-24); this call only settles whether the request is held. It returns the domain
SubmitResponse: accepted (resolved feed for bars, quota key) or, for bars, refused for its feed. Turning a
refusal into an error is the caller's decision, as ingest_fetch.py does for FetchDataset.

BOTH CALLS ARE UNARY AND TAKE THE EXISTING DEADLINE RULE: common.rpc.channel.unary_call_options, a finite
deadline with wait_for_ready bounded by it. Submit is idempotent on request_id, so a caller may retry it.

THE ANSWER IS MAPPED KNOWING THE REQUEST'S KIND (the feed rule in mapping/submit.py), and an answer that
breaks the rule, or will not map, is PEER_PROTOCOL_ERROR. As in ingest_fetch.py, a servicer that calls this
converts the peer's error deliberately rather than letting it escape into the error boundary.
"""

import math
from typing import Final, Protocol

import grpc

from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.channel import unary_call_options
from common.rpc.errors import from_rpc_error
from common.rpc.mapping.submit import (
    instance_request_to_proto,
    instance_response_to_domain,
    submit_request_to_proto,
    submit_response_to_domain,
)
from common.rpc.mapping.values import ProtoMappingError
from schemas.data_ingest.submit import InstanceRequest, InstanceResponse, SubmitRequest, SubmitResponse
from trader_joe.proto.internal.ingest.v1 import ingest_pb2_grpc


# Both calls are answered from memory (Submit queues, Instance reads a constant), so the bound is for a peer
# that is starting or stuck. A DEFAULT: the caller that builds the client may set its own.
DEFAULT_SUBMIT_DEADLINE_S: Final = 10.0


class IngestSubmitClient(Protocol):
    """Submit and Instance, in domain terms. The interface a test double implements."""

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Hand ingest one request.

        Args:
            request: The request, of any kind.

        Returns:
            SubmitResponse: Accepted or refused, with the instance that answered.

        Raises:
            TraderJoeError: If the call fails or the answer cannot be read.
        """
        ...

    async def instance(self) -> InstanceResponse:
        """Ask which ingest process is answering.

        Returns:
            InstanceResponse: The instance id and when it started.

        Raises:
            TraderJoeError: If the call fails or the answer cannot be read.
        """
        ...


class GrpcIngestSubmitClient:
    """IngestSubmitClient over grpc.aio. Takes a channel from its caller and reads no environment variable."""

    def __init__(self, channel: grpc.aio.Channel, *, deadline_s: float = DEFAULT_SUBMIT_DEADLINE_S) -> None:
        """Bind the client to a channel and a deadline.

        Args:
            channel: A channel from common.rpc.channel.create_channel, owned and closed by the caller.
            deadline_s: How long one call may take, in seconds from the call.

        Raises:
            ValueError: If the deadline is not a finite, positive number of seconds.
        """
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise ValueError(f'a Submit call needs a finite, positive deadline in seconds, got {deadline_s!r}')
        self._stub = ingest_pb2_grpc.IngestServiceStub(channel)
        self._deadline_s = deadline_s

    @property
    def deadline_s(self) -> float:
        """float: How long one call may take, in seconds."""
        return self._deadline_s

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Submit one request over gRPC.

        Args:
            request: The request to hand over.

        Returns:
            SubmitResponse: The answer, mapped under the feed rule for the request's kind.

        Raises:
            TraderJoeError: What from_rpc_error reads out of a failed call, or PEER_PROTOCOL_ERROR for an
                answer this release cannot read or that breaks the feed rule.
            ProtoMappingError: If the REQUEST cannot be put on the wire (our own bug, not the peer's).
        """
        # Outside the try on purpose: an unmappable request is ours, and must not be reported as the peer's.
        message = submit_request_to_proto(request)
        try:
            response = await self._stub.Submit(message, **unary_call_options(self._deadline_s))
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        try:
            return submit_response_to_domain(response, kind=request.kind)
        except ProtoMappingError as error:
            raise ExogenousError(
                Reason.PEER_PROTOCOL_ERROR, f'the Submit answer carried a message this release cannot read: {error}'
            ) from error

    async def instance(self) -> InstanceResponse:
        """Probe the ingest process over gRPC.

        Returns:
            InstanceResponse: The answer, in domain terms.

        Raises:
            TraderJoeError: What from_rpc_error reads out of a failed call, or PEER_PROTOCOL_ERROR for an
                answer this release cannot read.
        """
        message = instance_request_to_proto(InstanceRequest())
        try:
            response = await self._stub.Instance(message, **unary_call_options(self._deadline_s))
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        try:
            return instance_response_to_domain(response)
        except ProtoMappingError as error:
            raise ExogenousError(
                Reason.PEER_PROTOCOL_ERROR, f'the Instance answer carried a message this release cannot read: {error}'
            ) from error
