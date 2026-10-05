"""THE IngestServicer SHIM: domain events in, wire messages out, and the one failure it converts.

The encode-side counterpart of ``test_rpc_fetch_seam.py``. That file drives the DECODE side against a
scripted peer; this one drives the real servicer in ``common/rpc/ingest.py`` and asserts what it puts on
the wire for a handler that behaves, and what it does for one that does not.

NOTHING HERE IMPORTS ``trader_joe.proto``. TID251 bans the generated package outside ``common/rpc`` and a
test is not that seam (architect, 05:11 UTC 2026-10-02). The request is built with the production mapper,
the responses are read through the descriptor-derived classes in ``fetch_contract.py``, and the one place
a generated symbol is needed -- ``add_IngestServiceServicer_to_server`` -- is reached by calling
``fetch_dataset_service()``, which is the production function whose whole job is to hold it.

THE DOUBLE IS A PLAIN CLASS AND INHERITS NOTHING. ``FetchDatasetHandler`` is a STRUCTURAL Protocol
(decision tj-tkm4tn D1): ``routers/data_ingest`` must never import or inherit from ``common/rpc/ingest.py``,
so a double that subclassed the Protocol would prove the opposite of the rule it is here to check -- it
would pass for a handler by ancestry while the real handler, which has no ancestry, went unexercised.
``_Handler`` below therefore declares no base, and every test that drives the servicer is evidence that
conformance by shape is enough.

WHY SOME CASES RUN WITHOUT A SERVER AND SOME OVER A REAL CONNECTION. The raises are asserted directly off
the async generator, because the error boundary's whole job is to hide them and a test that went through a
server would see INTERNAL for all four and distinguish nothing. The two claims that are ABOUT the wire --
that a refusal ends the call OK rather than as a status, and that a late refusal does not -- are only true
over a connection, so those run against a real ``grpc.aio`` server registered through the production
``fetch_dataset_service()``.
"""

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import get_args

import grpc
import pytest

from common.enums.data_stock import Feed
from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.ingest_fetch import GrpcIngestFetchClient, IngestFetchClient
from common.rpc.errors import ErrorBoundaryInterceptor
from common.rpc.ingest import SERVICE_NAME, FetchDatasetHandler, IngestServicer, fetch_dataset_service
from common.rpc.mapping import ProtoMappingError, request_to_proto
from common.rpc.server import ServiceRegistration
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import (
    ACK_FEED,
    FETCH_PATH,
    GUARD_S,
    LOOPBACK,
    OTHER_FEED,
    a_bar,
    a_done,
    a_request,
    collected,
)
from schemas.data_ingest import fetch_dataset as domain


pytestmark = pytest.mark.common

# What the handler raises when this deployment cannot serve the tape that was asked for: the hop's one
# in-band failure (user ruling tj-3mk3u5.22 Q5).
REFUSAL_DETAIL = 'this deployment serves IEX only'

# A reason that is NOT the in-band one, used to show the servicer converts by REASON and not merely by
# "something was raised before the ack". Any row but FEED_NOT_AVAILABLE would do.
OTHER_REASON = Reason.VENDOR_UNAVAILABLE


def a_refusal() -> TraderJoeError:
    """The one error this hop answers in band.

    Returns:
        TraderJoeError: FEED_NOT_AVAILABLE, on its own branch, naming the feed that was asked for.
    """
    return error_for(Reason.FEED_NOT_AVAILABLE, REFUSAL_DETAIL, metadata={'feed': str(OTHER_FEED)})


# ---------------------------------------------------------------------------------------------
# THE DOUBLE. One class, no base, used as a HANDLER below and as an IngestFetchClient in the mirror test.


class _Handler:
    """A FetchDatasetHandler by SHAPE ONLY -- it declares no base and imports nothing from the servicer.

    It also satisfies ``IngestFetchClient``, which is the point of ``test_one_double_serves_both_ends``:
    ``deadline`` carries a default so the client's one-argument ``fetch(request)`` reaches it too.
    """

    def __init__(self, *events: object, raising: BaseException | None = None, after: int | None = None) -> None:
        """Script one fetch.

        Args:
            *events: The domain events to yield, in order.
            raising: What to raise, if anything.
            after: How many events to yield before raising. None means raise before the first.
        """
        self._events = events
        self._raising = raising
        self._after = after
        #: What the servicer passed across the seam, so a test can assert on it.
        self.seen_request: domain.FetchDatasetRequest | None = None
        self.seen_deadline: datetime | None = None
        self.called = False

    async def fetch(
        self, request: domain.FetchDatasetRequest, *, deadline: datetime | None = None
    ) -> AsyncIterator[object]:
        """Yield the scripted events, then raise if the script says to.

        Args:
            request: What to fetch. Recorded, never read.
            deadline: When the call expires. Recorded, never read. Defaulted so the same object also
                satisfies the client seam, whose ``fetch`` takes the request alone.

        Yields:
            object: Each scripted event.

        Raises:
            BaseException: Whatever the script was given, at the scripted point.
        """
        self.called = True
        self.seen_request = request
        self.seen_deadline = deadline
        if self._raising is not None and self._after is None:
            raise self._raising
        for index, event in enumerate(self._events, start=1):
            yield event
            if self._raising is not None and index == self._after:
                raise self._raising


class _Context:
    """Just enough ``grpc.aio.ServicerContext`` for the servicer, which reads its remaining time and nothing else."""

    def __init__(self, remaining_s: float | None) -> None:
        """Fix what the call has left.

        Args:
            remaining_s: Seconds remaining, or None for a call carrying no deadline.
        """
        self._remaining_s = remaining_s

    def time_remaining(self) -> float | None:
        """Report the call's remaining time.

        Returns:
            float | None: The seconds given at construction.
        """
        return self._remaining_s


async def driven(
    handler: _Handler, *, remaining_s: float | None = None, request: domain.FetchDatasetRequest | None = None
) -> tuple[list[object], BaseException | None]:
    """Drive the real servicer with no server at all, keeping what it sent AND what ended it.

    Args:
        handler: The scripted handler.
        remaining_s: What ``context.time_remaining()`` answers.
        request: The request to encode and pass in, defaulting to an ordinary one.

    Returns:
        tuple[list[object], BaseException | None]: The FetchDatasetResponse messages yielded, and the error
        that ended the stream, or None.
    """
    servicer = IngestServicer(handler)
    stream = servicer.FetchDataset(request_to_proto(request or a_request()), _Context(remaining_s))
    sent: list[object] = []
    try:
        async for response in stream:
            sent.append(response)
    except BaseException as error:
        # Caught this broadly on purpose: the error IS half of what every test below asserts.
        return sent, error
    return sent, None


def arms(sent: Sequence[object]) -> list[str]:
    """Which oneof arm each response set, which is the stream's grammar in one list.

    Args:
        sent: The responses the servicer yielded.

    Returns:
        list[str]: e.g. ``['ack', 'page', 'page', 'done']``.
    """
    return [response.WhichOneof('event') for response in sent]


@contextlib.asynccontextmanager
async def served(handler: _Handler) -> AsyncIterator[grpc.aio.Channel]:
    """Run the REAL servicer on a real server, registered the production way, and yield a channel to it.

    The registration goes through ``fetch_dataset_service()`` rather than through a generic handler, so the
    binding to ``add_IngestServiceServicer_to_server`` is part of what is under test. The boundary is
    installed because ``GrpcServerHost`` installs it in every server and offers no way to remove it
    (tj-19r2z5).

    Args:
        handler: The scripted handler to serve.

    Yields:
        grpc.aio.Channel: A channel to the running servicer. Both are torn down on the way out.
    """
    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()])
    fetch_dataset_service(handler).add_to_server(server)
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0, 'the servicer could not bind an ephemeral loopback port'
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        # Both, always. A grpc.aio server still running when the loop closes hangs the whole run.
        await channel.close()
        await server.stop(None)


# ---------------------------------------------------------------------------------------------
# THE SHIM DOES NOT RESHAPE THE STREAM (decision tj-tkm4tn D2)


@pytest.mark.asyncio
async def test_two_pages_stay_two_pages_and_keep_their_own_bars():
    """The shim-level half of the chunking proof: one page message per page yielded, neither merged nor split.

    D2 puts the chunking in the HANDLER, which is coherent only if the shim passes pages through
    one-for-one -- a shim that coalesced two pages would make the handler's MAX_PAGE_BARS arithmetic
    pointless, and one that re-split them would make ``FetchStreamEncoder.page``'s raise unreachable.
    Asserted by the bars each page carries and not only by the count, because two pages of the wrong bars
    is still two pages.

    The handler-level half -- a BrokerRead yielding MAX_PAGE_BARS + 1 bars -- belongs to tj-3mk3u5.9.
    """
    first = [a_bar(minute=0), a_bar(minute=1)]
    second = [a_bar(minute=2), a_bar(minute=3), a_bar(minute=4)]
    handler = _Handler(
        domain.FetchAccepted(feed=ACK_FEED),
        domain.BarPage(bars=first),
        domain.BarPage(bars=second),
        a_done(bar_count=5),
    )

    sent, error = await driven(handler)

    assert error is None
    assert arms(sent) == ['ack', 'page', 'page', 'done']
    pages = [response for response in sent if response.WhichOneof('event') == 'page']
    assert [len(page.page.bars) for page in pages] == [2, 3]
    assert [[bar.bar_start.ToDatetime(UTC) for bar in page.page.bars] for page in pages] == [
        [bar.bar_start for bar in first],
        [bar.bar_start for bar in second],
    ]


@pytest.mark.asyncio
async def test_a_fetch_that_serves_no_bars_is_an_ack_and_a_done_and_nothing_between():
    """An empty but genuinely served window is a success carrying provenance, not a page the shim invents."""
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), a_done(bar_count=0))

    sent, error = await driven(handler)

    assert error is None
    assert arms(sent) == ['ack', 'done']


@pytest.mark.asyncio
async def test_the_ack_carries_the_feed_the_handler_resolved_and_every_bar_is_stamped_with_it():
    """The encoder is built from the ack's feed HERE and nowhere else, so the two cannot disagree."""
    handler = _Handler(
        domain.FetchAccepted(feed=OTHER_FEED), domain.BarPage(bars=[a_bar(feed=OTHER_FEED)]), a_done(bar_count=1)
    )

    sent, error = await driven(handler)

    assert error is None
    ack, page, _ = sent
    assert ack.ack.accepted.feed == page.page.bars[0].feed


# ---------------------------------------------------------------------------------------------
# THE ONE IN-BAND CONVERSION, AND THE ASYMMETRY THAT IS EASIEST TO GET WRONG (tj-tkm4tn D3)


@pytest.mark.asyncio
async def test_a_feed_refusal_before_the_ack_is_the_streams_only_message_and_the_stream_ends_ok():
    """User ruling tj-3mk3u5.22 Q5: FEED_NOT_AVAILABLE is answered in the ack and never as a status.

    Asserted on three things, because any one alone would pass for a broken shim: the stream ended with no
    error, it carried exactly one message, and that message is the REFUSED arm rather than an accepted one.
    """
    handler = _Handler(raising=a_refusal())

    sent, error = await driven(handler)

    assert error is None
    assert arms(sent) == ['ack']
    refused = sent[0].ack.refused
    assert refused.reason == Reason.FEED_NOT_AVAILABLE.value
    assert refused.detail == REFUSAL_DETAIL
    assert sent[0].ack.WhichOneof('outcome') == 'refused'


@pytest.mark.asyncio
async def test_the_same_refusal_raised_after_the_ack_escapes_instead_of_being_converted_a_second_time():
    """THE ASYMMETRY. Once the ack is on the wire there is no refusal left to send, so a late one is a bug.

    This is the easiest thing in the shim to get wrong and the hardest to notice, because a shim that
    converted unconditionally would look correct on the test above and would then send a SECOND ack after
    an accepted one -- a stream the decode side rejects as PEER_PROTOCOL_ERROR, with the real failure lost.
    Asserted on both halves: the error escapes, AND no refused ack was appended behind the accepted one.
    """
    refusal = a_refusal()
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), raising=refusal, after=1)

    sent, error = await driven(handler)

    assert error is refusal
    assert arms(sent) == ['ack']
    assert sent[0].ack.WhichOneof('outcome') == 'accepted'


@pytest.mark.asyncio
async def test_an_error_that_is_not_a_feed_refusal_escapes_even_before_the_ack():
    """The other half of the condition: the shim converts by REASON, not by "nothing has been yielded yet".

    ``refused_response`` already refuses every reason but this one, so a shim that tested only the position
    would turn an ordinary vendor outage into a ProtoMappingError out of the mapping layer and answer
    INTERNAL for a condition that has a perfectly good status of its own.
    """
    other = error_for(OTHER_REASON)
    handler = _Handler(raising=other)

    sent, error = await driven(handler)

    assert error is other
    assert sent == []


@pytest.mark.asyncio
async def test_a_refusal_after_a_page_also_escapes_rather_than_being_answered_in_an_ack():
    """The asymmetry holds past the ack, not merely at it: pages have already crossed, so there is no ack left."""
    refusal = a_refusal()
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), domain.BarPage(bars=[a_bar()]), raising=refusal, after=2)

    sent, error = await driven(handler)

    assert error is refusal
    assert arms(sent) == ['ack', 'page']


# ---------------------------------------------------------------------------------------------
# THE ONLY GRAMMAR POLICED HERE: ACK-FIRST AND ONE-ACK (tj-tkm4tn D4)


@pytest.mark.asyncio
@pytest.mark.parametrize(('what', 'event'), [('BarPage', domain.BarPage(bars=[a_bar()])), ('FetchDone', a_done())])
async def test_a_page_or_a_done_before_the_ack_is_a_proto_mapping_error(what: str, event: object):
    """A construction necessity, not order policing: the encoder cannot exist before the feed is resolved.

    Args:
        what: The event type's name, which the message must name so a log says which one arrived.
        event: The event yielded with no ack before it.
    """
    handler = _Handler(event)

    sent, error = await driven(handler)

    assert isinstance(error, ProtoMappingError)
    assert not isinstance(error, TraderJoeError), 'our bug, so the boundary answers INTERNAL and not a typed reason'
    assert what in str(error)
    assert sent == []


@pytest.mark.asyncio
async def test_a_second_accepted_ack_is_a_proto_mapping_error_and_the_first_ack_still_crossed():
    """A second ack would need a second encoder, and the feed is settled once, by the first."""
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), domain.FetchAccepted(feed=OTHER_FEED))

    sent, error = await driven(handler)

    assert isinstance(error, ProtoMappingError)
    assert arms(sent) == ['ack']
    assert sent[0].ack.accepted.feed != OTHER_FEED.value, 'the first ack settled the feed; the second never encoded'


@pytest.mark.asyncio
async def test_nothing_after_the_done_is_policed_here_because_the_decode_side_does_that():
    """D4's negative half, and it is deliberate: a second copy of the grammar here would be a weaker one.

    fetch_stream.py's docstring rules order enforcement onto the decode side, where it protects a peer that
    is not ours. A page after the done therefore encodes without complaint HERE -- and
    ``test_rpc_fetch_seam.py`` is where it is caught. Pinning the absence stops a later edit from
    "tightening" the shim into the state machine D4 rejected.
    """
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), a_done(bar_count=0), domain.BarPage(bars=[a_bar()]))

    sent, error = await driven(handler)

    assert error is None
    assert arms(sent) == ['ack', 'done', 'page']


# ---------------------------------------------------------------------------------------------
# WHAT CROSSES THE SEAM FROM THE CALL


@pytest.mark.asyncio
async def test_the_deadline_crossing_the_seam_is_an_aware_utc_instant_built_from_the_calls_remaining_time():
    """``BarsQuery.deadline`` takes an instant, so the shim converts the call's remaining SECONDS into one.

    Aware is asserted explicitly: a naive datetime names no instant, and ``AwareDatetime`` downstream
    refuses one rather than guessing (the tj-1bl90i rule).
    """
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), a_done(bar_count=0))

    before = datetime.now(UTC)
    _, error = await driven(handler, remaining_s=30.0)
    after = datetime.now(UTC)

    assert error is None
    deadline = handler.seen_deadline
    assert deadline is not None
    assert deadline.tzinfo is not None and deadline.utcoffset() == timedelta(0)
    assert before + timedelta(seconds=30) <= deadline <= after + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_a_call_carrying_no_deadline_hands_the_handler_none_rather_than_an_invented_instant():
    """None is "no deadline", and inventing one here would bound a call the caller chose not to bound."""
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), a_done(bar_count=0))

    _, error = await driven(handler, remaining_s=None)

    assert error is None
    assert handler.called
    assert handler.seen_deadline is None


@pytest.mark.asyncio
async def test_the_request_crossing_the_seam_is_the_domain_model_and_never_the_wire_message():
    """D1's whole point: ``routers/data_ingest`` never holds a generated object, in either direction."""
    asked = a_request(asset_symbol='XIC', feed=Feed.SIP)
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), a_done(bar_count=0))

    _, error = await driven(handler, request=asked)

    assert error is None
    assert isinstance(handler.seen_request, domain.FetchDatasetRequest)
    assert handler.seen_request == asked


# ---------------------------------------------------------------------------------------------
# THE SEAM ITSELF: STRUCTURAL, MIRRORED, AND REGISTERED


def test_fetch_dataset_is_an_async_generator_function_so_the_boundary_guards_it_as_a_stream():
    """The pin on this file. ``common/rpc/errors.py`` branches on ``inspect.isasyncgenfunction``.

    A coroutine that RETURNED an iterator would be wrapped as a UNARY handler, and gRPC would be handed an
    un-awaited async generator object as the single response -- a failure that appears at the client as a
    serialisation error with nothing in it pointing back here.
    """
    assert inspect.isasyncgenfunction(IngestServicer.FetchDataset)


def test_the_handler_seam_is_structural_so_data_ingest_inherits_nothing_from_this_module():
    """Conformance is by SHAPE (tj-tkm4tn D1). Nothing in routers/data_ingest may import the servicer module.

    The double below is served by a real server in the tests above while having no ancestor in common with
    the Protocol, which is the evidence. Asserted here as well so a later edit that made the Protocol
    runtime-checkable, or a double that started subclassing it, is caught by a named test rather than by a
    reviewer noticing.
    """
    assert FetchDatasetHandler not in _Handler.__mro__
    assert _Handler.__mro__ == (_Handler, object)
    # Not runtime_checkable on purpose: a shape check at runtime would invite an isinstance gate in
    # production code, and the seam is a type-checker contract.
    with pytest.raises(TypeError):
        isinstance(_Handler(), FetchDatasetHandler)  # type: ignore[misc]


def test_the_server_seam_mirrors_the_client_seam_so_one_double_serves_both_ends():
    """tj-tkm4tn D1's stated justification for the shape, checked rather than taken on trust.

    The LOAD-BEARING half is the event union: both protocols yield ``FetchAccepted | BarPage | FetchDone``,
    which is what lets a double written for one end be read at the other. The signatures are NOT identical
    -- the server's ``fetch`` takes a keyword-only ``deadline`` the client's does not -- so "one double
    serves both ends" holds exactly when that parameter carries a default, as ``_Handler``'s does and as
    this test pins.

    AND THE UNION IS DECLARED TWICE, NOT SHARED. ``common/rpc/ingest.py`` and
    ``common/rpc/clients/ingest_fetch.py`` each spell out their own ``type FetchEvent = ...``; the two
    alias OBJECTS are distinct and compare unequal, so the mirror rests on the two right-hand sides saying
    the same thing. Nothing but this assertion notices if one of them gains a fourth arm -- the mirror
    would be quietly false and every double would still type-check against the end it was written for.
    """
    server_side = inspect.signature(FetchDatasetHandler.fetch)
    client_side = inspect.signature(IngestFetchClient.fetch)

    server_event, *rest = get_args(server_side.return_annotation)
    client_event, *_ = get_args(client_side.return_annotation)
    assert rest == [], 'AsyncIterator carries one argument; the seam yields one union'
    assert server_event.__value__ == client_event.__value__
    assert get_args(server_event.__value__) == (domain.FetchAccepted, domain.BarPage, domain.FetchDone)
    assert server_side.parameters['request'].annotation == client_side.parameters['request'].annotation
    extra = set(server_side.parameters) - set(client_side.parameters)
    assert extra == {'deadline'}, 'the two seams differ by the deadline alone; anything more breaks the mirror'
    assert server_side.parameters['deadline'].kind is inspect.Parameter.KEYWORD_ONLY

    double = inspect.signature(_Handler.fetch)
    assert double.parameters['deadline'].default is None, 'a default is what lets this double be read as a client'


def test_the_registration_names_the_service_the_descriptor_declares():
    """``SERVICE_NAME`` is read off the descriptor, and the health service reports the service under it.

    Compared against ``FETCH_PATH``, which ``fetch_contract.py`` derives from the descriptor independently,
    so a rename on the .proto reaches this as a failure and not as two stale constants agreeing.
    """
    registration = fetch_dataset_service(_Handler())

    assert isinstance(registration, ServiceRegistration)
    assert registration.name == SERVICE_NAME
    assert f'/{SERVICE_NAME}/FetchDataset' == FETCH_PATH


# ---------------------------------------------------------------------------------------------
# OVER A REAL CONNECTION: the two claims that are only true on the wire


@pytest.mark.asyncio
async def test_a_whole_fetch_crosses_the_wire_and_arrives_as_the_events_the_handler_yielded():
    """The round trip, which is the mirror claim made concrete: what goes in at one seam comes out at the other.

    It also proves the registration: the server is built by ``fetch_dataset_service()`` alone, so a
    servicer bound to the wrong ``add_*_to_server`` would be UNIMPLEMENTED here rather than passing.
    """
    events = [
        domain.FetchAccepted(feed=ACK_FEED),
        domain.BarPage(bars=[a_bar(), a_bar(minute=1)]),
        domain.BarPage(bars=[a_bar(minute=2)]),
        a_done(bar_count=3),
    ]
    handler = _Handler(*events)

    async with served(handler) as channel:
        client = GrpcIngestFetchClient(channel, deadline_s=GUARD_S)
        received, error = await collected(client.fetch(a_request()))

    assert error is None
    assert received == events


@pytest.mark.asyncio
async def test_a_refused_fetch_ends_the_call_ok_and_reaches_the_caller_as_the_error_a_status_would_have():
    """The in-band refusal AT THE WIRE: an OK call whose only message is the refused ack.

    Driving it directly cannot show this -- "the generator finished" is not "the call ended OK". Here the
    client sees no gRPC error at all and still raises the SAME typed error, which is the whole obligation
    the in-band design owes its caller.
    """
    handler = _Handler(raising=a_refusal())

    async with served(handler) as channel:
        client = GrpcIngestFetchClient(channel, deadline_s=GUARD_S)
        received, error = await collected(client.fetch(a_request()))

    assert received == []
    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.FEED_NOT_AVAILABLE
    assert not isinstance(error, grpc.aio.AioRpcError)
    assert error.detail == REFUSAL_DETAIL


@pytest.mark.asyncio
async def test_cancelling_mid_stream_closes_the_handlers_iterator_exactly_once_and_leaks_nothing():
    """The SERVICER's half of decision tj-tkm4tn D5, which puts the cleanup duty on the handler.

    D5 is satisfiable only if the servicer lets the handler's cleanup run. It iterates with a plain
    ``async for`` and never calls ``aclose()``, so the handler's generator is finalised when the servicer's
    own frame unwinds and drops the last reference to it -- and the question D5 leaves open is whether that
    actually happens on the cancellation path, once, and without stranding anything.

    Measured here rather than reasoned about: the handler below is shaped as a real one is, with the whole
    body INCLUDING the ack yield inside ``try/finally``, and the assertions are the three things that could
    go wrong -- the cleanup never running, running twice, or the loop being left with an unretrieved error
    such as "async generator was garbage collected without being closed".

    What this does NOT pin is a deadline. It polls instead of asserting a tick, because the finalisation
    runs as a loop task and the schedule is the loop's business; a servicer that RETAINED the iterator --
    on self, or in a list -- would never close it at all and is what this catches.
    """
    closed: list[str] = []
    cleaned = asyncio.Event()
    loop_errors: list[object] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: loop_errors.append(context))

    class _CleanupHandler:
        """A handler that owns something it must close, as the adapter's bars iterator is."""

        async def fetch(
            self, request: domain.FetchDatasetRequest, *, deadline: datetime | None = None
        ) -> AsyncIterator[object]:
            """Yield an unending accepted stream, closing the iterator however it ends.

            Args:
                request: Unused.
                deadline: Unused.

            Yields:
                object: The ack, then pages without end, so the caller is always cancelling mid-stream.
            """
            try:
                yield domain.FetchAccepted(feed=ACK_FEED)
                while True:
                    yield domain.BarPage(bars=[a_bar()])
            finally:
                closed.append('bars')
                cleaned.set()

    async with served(_CleanupHandler()) as channel:  # type: ignore[arg-type]
        client = GrpcIngestFetchClient(channel, deadline_s=GUARD_S)
        stream = client.fetch(a_request())
        received = 0
        async for _ in stream:
            received += 1
            if received == 2:
                break
        # The client seam cancels the call in its own finally, which is what reaches the servicer.
        await stream.aclose()

        # Waited for rather than asserted outright: the finalisation runs as a loop task, so the
        # schedule is the loop's business. A servicer that RETAINED the iterator never closes it at
        # all, and that is what times out here.
        await asyncio.wait_for(cleaned.wait(), GUARD_S)

    assert closed == ['bars'], 'the handler cleanup must run exactly once on the cancellation path'
    assert loop_errors == []


@pytest.mark.asyncio
async def test_a_refusal_after_the_ack_reaches_the_caller_as_the_peers_internal_error_and_not_as_a_refusal():
    """The asymmetry's observable consequence, which is the reason it is worth the extra condition.

    FEED_NOT_AVAILABLE's REASONS row carries no grpc_code at all, so the boundary cannot render it as a
    status and answers INTERNAL with an error_id. The caller therefore learns "the peer broke", which is
    true, instead of "your feed is unavailable" arriving impossibly after the feed was acknowledged.
    """
    handler = _Handler(domain.FetchAccepted(feed=ACK_FEED), raising=a_refusal(), after=1)

    async with served(handler) as channel:
        client = GrpcIngestFetchClient(channel, deadline_s=GUARD_S)
        received, error = await collected(client.fetch(a_request()))

    assert received == [domain.FetchAccepted(feed=ACK_FEED)]
    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_INTERNAL
