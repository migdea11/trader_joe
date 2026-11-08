"""THE CLIENT SEAM for the store's result path: domain terms in front, gRPC behind (tj-7zjov1.16).

data_ingest calls this and never imports trader_joe.proto (ADR tj-8konfu D3; ruff's TID251 enforces it).
What crosses the seam is domain objects and TraderJoeErrors.

WHAT THE ACK GATE MEANS FOR THIS CLIENT. The contract (store.proto, the owner's ruling of 2026-10-24) gates
at the ATTEMPT, not at the message: every attempt is reported and acknowledged before ingest runs a retry
or makes any further vendor call for that request. Inside one attempt the header, payload and trailer are a
single client stream with no per-part acknowledgement, so this client sends all of an attempt's messages in
one call and returns only when the store has answered, and that answer is the acknowledgement. It does not
hold back the next PART for an acknowledgement, because the contract has none to wait for. It is
deliver()'s return that opens the gate, and holding the gate is the caller's (the scheduler's outbox).

THIS CLIENT NEVER RETRIES. A call that fails -- the report did not reach the store, or its answer did not
reach us -- raises a TraderJoeError, and the caller redelivers the SAME AttemptDelivery with the same
attempt_no, which is not a broker retry and costs no vendor call. That redelivery is safe because the store
is idempotent on (request_id, attempt_no) and answers a duplicate from its stored row. Backoff, the
bounded buffer and the alarm on a stuck report are the caller's (ruling R2); a retry loop in here would
hide the very failures that alarm exists for.

THE CALL IS BOUNDED, though it is a stream: one finite deadline per attempt call together with
wait_for_ready (ADR tj-8konfu D6.1 and D6.5), so a store that is starting or restarting is waited for, but
never past the deadline. The call options are spelled out here, as ingest_fetch.py spells its own, rather
than taken from common.rpc.channel.unary_call_options, whose options are the unary ones.

THE ANSWER MUST MAP. An acknowledgement with no verdict, or one this release cannot read, is
PEER_PROTOCOL_ERROR: an answer that says nothing is not an acknowledgement, and the caller must not open the
gate on it.
"""

import math
from typing import Final, Protocol

import grpc

from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.channel import unary_call_options
from common.rpc.errors import from_rpc_error
from common.rpc.mapping.delivery import (
    attempt_to_messages,
    register_instance_request_to_proto,
    register_instance_response_to_domain,
    response_to_domain,
)
from common.rpc.mapping.values import ProtoMappingError
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    RegisterInstanceRequest,
    RegisterInstanceResponse,
)
from trader_joe.proto.internal.store.v1 import store_pb2_grpc


# One attempt's report: the stream out (an asset list is some megabytes at most, in messages of about 1 MiB),
# then the store's single transaction (the attempt row, the data, the count, the verdict) before it answers.
# That is seconds on a healthy store; a minute leaves room for a store that is mid-restart, together with
# the channel's reconnect backoff (about 2.4 s, common/rpc/config.py), and still bounds a stuck call. A
# DEFAULT: the caller that builds the client may set its own.
DEFAULT_DELIVERY_DEADLINE_S: Final = 60.0

# RegisterInstance is answered from memory or one small row, so it takes the unary bound.
DEFAULT_REGISTER_DEADLINE_S: Final = 10.0


class StoreDeliveryClient(Protocol):
    """DeliverAttempt and RegisterInstance, in domain terms. The interface a test double implements."""

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        """Report one attempt and wait for the store's acknowledgement.

        Args:
            attempt: The whole attempt: header, payload, trailer. Redeliver the same object, unchanged, after a failure.

        Returns:
            DeliverAttemptResponse: CONTINUE or FINAL, with the store's durable attempt count. Returned only
            once the store has committed the attempt.

        Raises:
            TraderJoeError: If the call fails or the answer cannot be read. Nothing is known to be written.
        """
        ...

    async def register_instance(self, request: RegisterInstanceRequest) -> RegisterInstanceResponse:
        """Announce the instance id ingest minted at start.

        Args:
            request: The instance id and when the process started.

        Returns:
            RegisterInstanceResponse: The empty answer; the call succeeding is the acknowledgement.

        Raises:
            TraderJoeError: If the call fails or the answer cannot be read.
        """
        ...


class GrpcStoreDeliveryClient:
    """StoreDeliveryClient over grpc.aio. Takes a channel from its caller and reads no environment variable."""

    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        deadline_s: float = DEFAULT_DELIVERY_DEADLINE_S,
        register_deadline_s: float = DEFAULT_REGISTER_DEADLINE_S,
    ) -> None:
        """Bind the client to a channel and its deadlines.

        Args:
            channel: A channel from common.rpc.channel.create_channel, owned and closed by the caller.
            deadline_s: How long one attempt report may take, in seconds from the call.
            register_deadline_s: How long RegisterInstance may take.

        Raises:
            ValueError: If a deadline is not a finite, positive number of seconds. wait_for_ready without a
                bound would wait forever for a store that is down.
        """
        for name, value in (('deadline_s', deadline_s), ('register_deadline_s', register_deadline_s)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'a store call needs a finite, positive {name} in seconds, got {value!r}')
        self._stub = store_pb2_grpc.StoreServiceStub(channel)
        self._deadline_s = deadline_s
        self._register_deadline_s = register_deadline_s

    @property
    def deadline_s(self) -> float:
        """float: How long one attempt report may take, in seconds."""
        return self._deadline_s

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        """Report one attempt over gRPC and return the acknowledgement.

        Args:
            attempt: The whole attempt. Split across bounded messages by the mapper.

        Returns:
            DeliverAttemptResponse: The store's acknowledgement.

        Raises:
            TraderJoeError: What from_rpc_error reads out of a failed call (PEER_UNAVAILABLE, DEADLINE,
                PEER_INTERNAL, or the peer's own typed reason), or PEER_PROTOCOL_ERROR for an acknowledgement
                this release cannot read.
            ProtoMappingError: If the ATTEMPT cannot be put on the wire (our own bug, not the peer's). No
                call has been made when it is raised.
        """
        # Outside the try on purpose: an unmappable attempt is ours, and must not be reported as the peer's.
        messages = attempt_to_messages(attempt)
        call = self._stub.DeliverAttempt(iter(messages), timeout=self._deadline_s, wait_for_ready=True)
        try:
            response = await call
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        finally:
            # A finished call is unaffected; one abandoned by a cancelled caller must not stay open on the peer.
            call.cancel()
        try:
            return response_to_domain(response)
        except ProtoMappingError as error:
            raise ExogenousError(
                Reason.PEER_PROTOCOL_ERROR,
                f'the DeliverAttempt answer carried a message this release cannot read: {error}',
            ) from error

    async def register_instance(self, request: RegisterInstanceRequest) -> RegisterInstanceResponse:
        """Announce the instance over gRPC.

        Args:
            request: The announcement.

        Returns:
            RegisterInstanceResponse: The empty answer.

        Raises:
            TraderJoeError: What from_rpc_error reads out of a failed call.
            ProtoMappingError: If the REQUEST cannot be put on the wire.
        """
        message = register_instance_request_to_proto(request)
        try:
            response = await self._stub.RegisterInstance(message, **unary_call_options(self._register_deadline_s))
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        return register_instance_response_to_domain(response)
