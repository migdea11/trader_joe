"""THE SERVER SEAM for the internal result-path contract: StoreService, hosted by data_store (tj-7zjov1.16).

data_store supplies a DeliveryHandler and never holds a generated message (ADR tj-8konfu D3, enforced by
ruff's TID251 everywhere but common/rpc). This module owns every call to the delivery mappers on the
serving side, as ingest.py does for the opposite direction.

THE DIRECTION IS THE OPPOSITE OF ingest.py: data_store serves this and data_ingest calls it (owner ruling
on tj-26jt9w, 2026-10-24: push). The internal ingest-to-store service is separate from the future SDK-facing
service but uses the same machinery: GrpcServerHost, so it carries the error boundary, the health service
and a named, non-wildcard bind (APP_INTERNAL_GRPC_HOST / APP_INTERNAL_GRPC_PORT; the compose file binds the
data-store-grpc alias on ingest_store). It may run in the same process as the REST app, on its own port.

THE ACK GATE LIVES IN THE HANDLER'S CONTRACT. DeliverAttempt returns only what the handler returns, and the
handler must have committed the attempt, its data, the count, the spend and the verdict before it returns
(store.proto). This servicer adds nothing before and nothing after: it assembles the stream into one
AttemptDelivery, calls the handler once, and encodes the answer. Idempotency on (request_id, attempt_no) is
the handler's: a duplicate is answered from the stored row.

A STREAM THAT BREAKS THE GRAMMAR IS THE SENDER'S ERROR, not ours: it is refused as INVALID_REQUEST (an
InvalidRequestError, rendered as a status the client reads back) and the handler is never called, so a
stream that ends without a trailer writes nothing and ingest redelivers it whole. A stream the peer cut
short never reaches this code at all: gRPC cancels the call.
"""

from collections.abc import AsyncIterator
from typing import Protocol

import grpc

from common.errors.vocabulary import InvalidRequestError, Reason
from common.rpc.mapping.delivery import (
    AttemptAssembler,
    register_instance_request_to_domain,
    register_instance_response_to_proto,
    response_to_proto,
)
from common.rpc.mapping.values import ProtoMappingError
from common.rpc.server import ServiceRegistration
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    RegisterInstanceRequest,
    RegisterInstanceResponse,
)
from trader_joe.proto.internal.store.v1 import store_pb2, store_pb2_grpc


SERVICE_NAME = store_pb2.DESCRIPTOR.services_by_name['StoreService'].full_name


class DeliveryHandler(Protocol):
    """The store's side of the result path, in domain terms: the interface data_store implements.

    Structural, like BrokerRead: an implementation conforms by shape and never imports this module.
    """

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        """Write one reported attempt durably, then answer.

        The answer IS the acknowledgement, so this returns only after the attempt record, any data, the
        durable attempt count, the summed spend and the verdict are committed in one transaction. A
        duplicate (request_id, attempt_no) is answered from the stored row and writes nothing.

        Args:
            attempt: The whole attempt: header, payload, trailer.

        Returns:
            DeliverAttemptResponse: CONTINUE or FINAL, with the durable attempt count.
        """
        ...

    async def register_instance(self, request: RegisterInstanceRequest) -> None:
        """Record the instance id ingest minted at start. Idempotent: an id already held is answered again.

        Args:
            request: The instance id and when that process started.
        """
        ...


class StoreServicer(store_pb2_grpc.StoreServiceServicer):
    """Serves StoreService by assembling each stream and handing the handler one attempt."""

    def __init__(self, handler: DeliveryHandler) -> None:
        """Bind the servicer to the handler that does the writing.

        Args:
            handler: The store implementation. One handler serves every call.
        """
        self._handler = handler

    async def DeliverAttempt(
        self, request_iterator: AsyncIterator[store_pb2.DeliverAttemptRequest], context: grpc.aio.ServicerContext
    ) -> store_pb2.DeliverAttemptResponse:
        """Assemble one attempt from the client stream, hand it to the handler, return the acknowledgement.

        The method name is the generated base class's, so it is not snake_case.

        Args:
            request_iterator: The call's request messages, in arrival order.
            context: The call's context. Unused: nothing about the call crosses the seam.

        Returns:
            store_pb2.DeliverAttemptResponse: The handler's acknowledgement, encoded.

        Raises:
            InvalidRequestError: INVALID_REQUEST, if the stream breaks the header-payload-trailer grammar or
                carries a message this release cannot read. The handler is not called.
            ProtoMappingError: If the handler's answer cannot be put on the wire (our own defect; the error
                boundary answers INTERNAL).
            TraderJoeError: Whatever the handler raises.
        """
        assembler = AttemptAssembler()
        try:
            async for message in request_iterator:
                assembler.feed(message)
            attempt = assembler.finish()
        except ProtoMappingError as error:
            raise InvalidRequestError(
                Reason.INVALID_REQUEST, f'the DeliverAttempt stream is unreadable: {error}'
            ) from error
        response = await self._handler.deliver(attempt)
        return response_to_proto(response)

    async def RegisterInstance(
        self, request: store_pb2.RegisterInstanceRequest, context: grpc.aio.ServicerContext
    ) -> store_pb2.RegisterInstanceResponse:
        """Record the announced instance.

        Args:
            request: The announcement as it arrived.
            context: The call's context. Unused.

        Returns:
            store_pb2.RegisterInstanceResponse: The empty answer.

        Raises:
            InvalidRequestError: INVALID_REQUEST, if the announcement cannot be read.
            TraderJoeError: Whatever the handler raises.
        """
        try:
            domain_request = register_instance_request_to_domain(request)
        except ProtoMappingError as error:
            raise InvalidRequestError(
                Reason.INVALID_REQUEST, f'the RegisterInstance request is unreadable: {error}'
            ) from error
        await self._handler.register_instance(domain_request)
        return register_instance_response_to_proto(RegisterInstanceResponse())


def store_service(handler: DeliveryHandler) -> ServiceRegistration:
    """A registration for GrpcServerHost that serves StoreService through the given handler.

    Args:
        handler: The store implementation to serve.

    Returns:
        ServiceRegistration: The store servicer, bound to its generated add_*_to_server function.
    """
    servicer = StoreServicer(handler)
    return ServiceRegistration(
        name=SERVICE_NAME,
        add_to_server=lambda server: store_pb2_grpc.add_StoreServiceServicer_to_server(servicer, server),
    )
