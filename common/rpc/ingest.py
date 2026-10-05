"""THE SERVER SEAM for the internal FetchDataset contract: domain events in, wire messages out.

data/ingest supplies a FetchDatasetHandler and never holds a generated message. This module owns every
call to FetchStreamEncoder and every FetchDatasetResponse on the hop (decision tj-tkm4tn D1), which is
what ADR tj-8konfu D3 asks for and what ruff's TID251 enforces for every path but common/rpc. A handler
that drove the encoder itself would hold and annotate generated messages, which is the thing D3 exists to
prevent.

THE HANDLER IS THE MIRROR IMAGE OF THE CLIENT SEAM. FetchDatasetHandler yields the same FetchEvent union
that common/rpc/clients/ingest_fetch.py's IngestFetchClient yields on the decode side, so ONE test double
serves both ends of the hop. It is a STRUCTURAL Protocol, as BrokerRead is: nothing in routers/data_ingest
inherits from it, and conformance is by shape, so the handler does not have to import this module.

WHAT CROSSES THE SEAM FROM THE CALL IS A DEADLINE, NOT A CONTEXT. An aware UTC datetime, or None when the
call carries no deadline. It is the only thing the handler needs from the call, it is already the shape
BarsQuery.deadline takes, and keeping grpc out means the handler is exercised with a plain async for --
no server, no channel, no generated type in its signature.

THE HANDLER CHUNKS; THIS ENCODES ONE PAGE PER YIELDED PAGE (tj-tkm4tn D2). FetchStreamEncoder.page()
RAISES above MAX_PAGE_BARS rather than splitting, and that is coherent only because the splitting happens
in the handler, beside the code that drains the vendor iterator and therefore decides the memory profile.
There is no chunking loop here. More generally there is NO BUSINESS LOGIC here: no feed resolution, no
get_bars, no rate budget, no single-flight. If a line of this file needed to know what a bar is, it would
be in the wrong file.

THE ONE IN-BAND FAILURE (tj-tkm4tn D3, user ruling tj-3mk3u5.22 Q5). A TraderJoeError whose reason is
FEED_NOT_AVAILABLE, raised BEFORE anything has been yielded, is converted with refused_response() and sent
as the stream's only message; the call then ends OK, with no page and no done. Every other error, and a
FEED_NOT_AVAILABLE raised AFTER the ack, escapes untouched: once the ack is on the wire there is no
refusal left to send, so a late one is a handler bug and INTERNAL is the honest answer. refused_response()
already refuses every other reason itself, so the only judgement this module adds is "has anything been
yielded yet".

THE ONLY GRAMMAR POLICED HERE IS ACK-FIRST AND ONE-ACK (tj-tkm4tn D4). A page or a done before the ack has
no encoder to go through, because the encoder cannot be built until the feed is resolved; a second ack
would need a second encoder. Both are construction necessities, not order policing. Nothing after the done
is checked and there is no state machine: fetch_stream.py's docstring rules order enforcement onto the
DECODE side, where it protects a peer that is not ours, and a second copy here would be a weaker one.

NO INTERCEPTOR IS INSTALLED HERE. GrpcServerHost puts ErrorBoundaryInterceptor first in every server's
chain and offers no way to remove it (tj-19r2z5; ADR tj-fa1rpu D1(b)), so the escaping errors above are
already guarded by the time this servicer is attached.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Protocol

import grpc

from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.mapping import FetchStreamEncoder, ProtoMappingError, refused_response, request_to_domain
from common.rpc.server import ServiceRegistration
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.internal.ingest.v1 import ingest_pb2, ingest_pb2_grpc


SERVICE_NAME = ingest_pb2.DESCRIPTOR.services_by_name['IngestService'].full_name


type FetchEvent = domain.FetchAccepted | domain.BarPage | domain.FetchDone
"""What a fetch yields, in order: the accepted ack, then zero or more pages, then the done."""


class FetchDatasetHandler(Protocol):
    """One dataset fetch, in domain terms: the interface data/ingest implements for this servicer.

    Structural, like BrokerRead -- an implementation conforms by shape and never imports this module.
    Nothing about gRPC appears here, which is the point: the handler is the mirror image of
    common/rpc/clients/ingest_fetch.py's IngestFetchClient, so a double written for one end works at the
    other, and a handler is testable with a plain async for.
    """

    def fetch(self, request: domain.FetchDatasetRequest, *, deadline: datetime | None) -> AsyncIterator[FetchEvent]:
        """Fetch one dataset, yielding the accepted ack, then its pages, then its done.

        Args:
            request: What to fetch.
            deadline: When the call expires, as an aware UTC datetime, or None when it carries no
                deadline. The only thing the handler is given from the call.

        Returns:
            AsyncIterator[FetchEvent]: The stream's events, in contract order. The ack comes first, so the
            servicer has the resolved feed before any bar. Each page holds at most
            common.rpc.mapping.MAX_PAGE_BARS bars: the handler chunks, because the encoder refuses an
            oversized page rather than splitting it (decision tj-tkm4tn D2).

        Raises:
            TraderJoeError: FEED_NOT_AVAILABLE before the first event when this deployment cannot serve
                the requested feed -- the hop's one in-band refusal. Any other error, at any point, and
                FEED_NOT_AVAILABLE after the ack, become a gRPC status through the error boundary.
        """
        ...


class IngestServicer(ingest_pb2_grpc.IngestServiceServicer):
    """Serves IngestService by encoding one handler's domain events onto the wire."""

    def __init__(self, handler: FetchDatasetHandler) -> None:
        """Bind the servicer to the handler that does the fetching.

        Args:
            handler: The fetch implementation. One handler serves every call.
        """
        self._handler = handler

    async def FetchDataset(
        self, request: ingest_pb2.FetchDatasetRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[ingest_pb2.FetchDatasetResponse]:
        """Stream one dataset fetch: the ack, then its pages, then its done.

        The method name is the generated base class's, so it is not snake_case.

        An ASYNC GENERATOR on purpose. common/rpc/errors.py branches on inspect.isasyncgenfunction to
        decide how to guard a behaviour, so a coroutine returning an iterator would be wrapped as a UNARY
        handler and gRPC would get an un-awaited generator object as the single response.

        Args:
            request: The fetch request as it arrived.
            context: The call's context. Read only for its remaining time.

        Yields:
            ingest_pb2.FetchDatasetResponse: The accepted ack, then one page per page the handler yielded,
            then the done -- or, when the fetch is refused, the refused ack as the stream's only message.

        Raises:
            ProtoMappingError: If the request cannot be decoded, if a value cannot be put on the wire, or
                if the handler yields a page or a done before the ack, or a second ack. The error boundary
                answers INTERNAL with an error_id, which is correct: nothing a caller sent causes it.
            TraderJoeError: Whatever the handler raises, except the one refusal converted in band. The
                boundary renders it as this hop's status.
        """
        domain_request = request_to_domain(request)
        remaining_s = context.time_remaining()
        deadline = None if remaining_s is None else datetime.now(UTC) + timedelta(seconds=remaining_s)

        encoder: FetchStreamEncoder | None = None
        try:
            async for event in self._handler.fetch(domain_request, deadline=deadline):
                if isinstance(event, domain.FetchAccepted):
                    if encoder is not None:
                        raise ProtoMappingError(
                            'the handler yielded a second FetchAccepted; the feed is settled once, by the '
                            'first, and the stream already carries its ack'
                        )
                    # Built here and nowhere else: the encoder exists only once the feed is resolved, and
                    # from then on every event of this stream carries that one feed.
                    encoder = FetchStreamEncoder(event.feed)
                    yield encoder.accepted()
                    continue
                if encoder is None:
                    raise ProtoMappingError(
                        f'the handler yielded a {type(event).__name__} before its FetchAccepted; there is '
                        'no encoder to put it through until the ack has resolved the feed'
                    )
                if isinstance(event, domain.BarPage):
                    yield encoder.page(event.bars)
                else:
                    yield encoder.done(event)
        except TraderJoeError as error:
            # The hop's one in-band failure, and only while the ack is still unsent.
            if encoder is not None or error.reason is not Reason.FEED_NOT_AVAILABLE:
                raise
            yield refused_response(error)


def fetch_dataset_service(handler: FetchDatasetHandler) -> ServiceRegistration:
    """A registration for GrpcServerHost that serves FetchDataset through the given handler.

    Args:
        handler: The fetch implementation to serve.

    Returns:
        ServiceRegistration: The ingest servicer, bound to its generated add_*_to_server function.
    """
    servicer = IngestServicer(handler)
    return ServiceRegistration(
        name=SERVICE_NAME,
        add_to_server=lambda server: ingest_pb2_grpc.add_IngestServiceServicer_to_server(servicer, server),
    )
