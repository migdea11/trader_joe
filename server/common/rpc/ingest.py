"""THE SERVER SEAM for the internal ingest contract: domain requests in, wire messages out.

data/ingest supplies a SubmitHandler and an InstanceHandler and never holds a generated message. This
module owns every call that decodes a request and encodes an answer on the hop, which is what ADR
tj-8konfu D3 asks for and what ruff's TID251 enforces for every path but common/rpc.

The handlers are STRUCTURAL Protocols, as BrokerRead is: nothing in routers/data_ingest inherits from
them, and conformance is by shape, so a handler does not have to import this module. There is NO BUSINESS
LOGIC here: no feed resolution, no get_bars, no rate budget. If a line of this file needed to know what a
bar is, it would be in the wrong file.

THE ONE IN-BAND REFUSAL is a Submit ack's refused arm for an unservable feed (user ruling tj-3mk3u5.22 Q5);
the handler returns it in its SubmitResponse and this module only encodes it. Every other error escapes
untouched and is rendered by the error boundary.

NO INTERCEPTOR IS INSTALLED HERE. GrpcServerHost puts ErrorBoundaryInterceptor first in every server's
chain and offers no way to remove it (tj-19r2z5; ADR tj-fa1rpu D1(b)), so the escaping errors above are
already guarded by the time this servicer is attached.
"""

from typing import Protocol

import grpc

from common.rpc.mapping.submit import (
    instance_request_to_domain,
    instance_response_to_proto,
    submit_request_to_domain,
    submit_response_to_proto,
)
from common.rpc.server import ServiceRegistration
from schemas.data_ingest.submit import InstanceResponse, SubmitRequest, SubmitResponse
from trader_joe.proto.internal.ingest.v1 import ingest_pb2, ingest_pb2_grpc


SERVICE_NAME = ingest_pb2.DESCRIPTOR.services_by_name['IngestService'].full_name


class SubmitHandler(Protocol):
    """One Submit, in domain terms: hold the request and answer with the ack (tj-7zjov1.48).

    Structural, like BrokerRead: an implementation conforms by shape and never imports this module.
    Queueing and idempotency on request_id belong to the implementation, not to the servicer.
    """

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Hold one request of any kind and answer it.

        Args:
            request: The request as decoded.

        Returns:
            SubmitResponse: The ack and the instance that gave it. The servicer encodes it under
            request.kind, so an accepted non-bars answer carries no feed.

        Raises:
            TraderJoeError: Any failure, rendered by the error boundary as this hop's status.
        """
        ...


class InstanceHandler(Protocol):
    """The Instance probe, in domain terms: which ingest process is answering."""

    async def instance(self) -> InstanceResponse:
        """Report this ingest process.

        Returns:
            InstanceResponse: The instance id and when it started.

        Raises:
            TraderJoeError: Any failure, rendered by the error boundary as this hop's status.
        """
        ...


class IngestServicer(ingest_pb2_grpc.IngestServiceServicer):
    """Serves IngestService through its Submit and Instance handlers."""

    def __init__(self, *, submit: SubmitHandler | None = None, instance: InstanceHandler | None = None) -> None:
        """Bind the servicer to the handlers that do the work.

        Args:
            submit: The Submit implementation, or None, in which case Submit answers UNIMPLEMENTED.
            instance: The Instance implementation, or None, in which case Instance answers UNIMPLEMENTED.
        """
        self._submit = submit
        self._instance = instance

    async def Submit(
        self, request: ingest_pb2.SubmitRequest, context: grpc.aio.ServicerContext
    ) -> ingest_pb2.SubmitResponse:
        """Hold one request through the Submit handler and encode its answer under the request's kind.

        A COROUTINE, not an async generator: common/rpc/errors.py guards a unary behaviour differently.
        The method name is the generated base class's, so it is not snake_case.

        Args:
            request: The submit request as it arrived.
            context: The call's context. Used only to abort with UNIMPLEMENTED when no handler is bound.

        Returns:
            ingest_pb2.SubmitResponse: The handler's answer on the wire.

        Raises:
            grpc.aio.AbortError: With UNIMPLEMENTED when no Submit handler is bound; the call ends with that
                status, and the error boundary lets it through unchanged.
            ProtoMappingError: If the request cannot be decoded or the answer breaks the feed rule for the
                request's kind; the error boundary answers INTERNAL with an error_id.
            TraderJoeError: Whatever the handler raises.
        """
        if self._submit is None:
            # Not the generated base method: it sets UNIMPLEMENTED and then raises NotImplementedError, which the
            # error boundary every server has answers INTERNAL with an error_id and an ERROR traceback.
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, 'Submit is not served by this ingest service')
        domain_request = submit_request_to_domain(request)
        response = await self._submit.submit(domain_request)
        # The kind must be passed: a non-bars accept with no feed fails to encode under the bars default.
        return submit_response_to_proto(response, kind=domain_request.kind)

    async def Instance(
        self, request: ingest_pb2.InstanceRequest, context: grpc.aio.ServicerContext
    ) -> ingest_pb2.InstanceResponse:
        """Answer the Instance probe through the Instance handler.

        Args:
            request: The probe as it arrived; it carries nothing.
            context: The call's context. Used only to abort with UNIMPLEMENTED when no handler is bound.

        Returns:
            ingest_pb2.InstanceResponse: The handler's answer on the wire.

        Raises:
            grpc.aio.AbortError: With UNIMPLEMENTED when no Instance handler is bound; the call ends with that
                status, and the error boundary lets it through unchanged.
            ProtoMappingError: If the answer cannot be put on the wire.
            TraderJoeError: Whatever the handler raises.
        """
        if self._instance is None:
            # See Submit: aborting keeps the status UNIMPLEMENTED behind the error boundary.
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, 'Instance is not served by this ingest service')
        instance_request_to_domain(request)
        return instance_response_to_proto(await self._instance.instance())


def ingest_service(
    *, submit: SubmitHandler | None = None, instance: InstanceHandler | None = None
) -> ServiceRegistration:
    """A registration for GrpcServerHost that serves IngestService through the given handlers.

    Args:
        submit: The Submit implementation; without it Submit answers UNIMPLEMENTED.
        instance: The Instance implementation; without it Instance answers UNIMPLEMENTED.

    Returns:
        ServiceRegistration: The ingest servicer, bound to its generated add_*_to_server function.
    """
    servicer = IngestServicer(submit=submit, instance=instance)
    return ServiceRegistration(
        name=SERVICE_NAME,
        add_to_server=lambda server: ingest_pb2_grpc.add_IngestServiceServicer_to_server(servicer, server),
    )
