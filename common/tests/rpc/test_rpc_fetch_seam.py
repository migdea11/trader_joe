"""THE IngestFetchClient SEAM: the stream's grammar, the in-band refusal, and the deadline that bounds it.

This is the decode side of tj-3mk3u5.28, driven over a REAL grpc.aio connection to a peer built from the
contract's descriptors (``fetch_contract.py``). Nothing here imports ``trader_joe.proto``: TID251 bans it
outside ``common/rpc`` and a test is not that seam.

WHY THE GRAMMAR IS ENFORCED HERE AND NOWHERE ELSE. The peer is NOT always our own server -- a test
double, a replay backend (tj-r6vcgv) or a version-skewed deployment all arrive at this seam -- so a
server-side check protects nobody. Every departure from

    accepted : FetchAck(accepted) -> BarPage* -> FetchDone, then the call ends OK
    refused  : FetchAck(refused), and nothing else

is PEER_PROTOCOL_ERROR (ADR tj-fa1rpu C8). The one that is easiest to get wrong is the LAST: a stream
that merely stops looks like success, and distinguishing it from one that finished is the entire reason
FetchDone exists.

WHY A REFUSAL IS NOT A STATUS (user ruling, tj-3mk3u5.22 Q5). FEED_NOT_AVAILABLE travels in the ack
because ingest alone decides which tape it can serve and the store writes its dataset entry only once it
has the ack. Its REASONS row has no grpc_code at all, so it CANNOT be sent as a status. What this seam
owes in return is that the caller cannot tell: the refusal is rebuilt into the same InvalidRequestError a
REFUSED status would have produced, raised before anything is yielded.

THE DEADLINE. ``ingest.proto``'s header and ``common/rpc/channel.py``'s now state one rule, and this is
the seam that implements it: what ADR tj-8konfu D6.1 forbids is an UNBOUNDED wait, a finite deadline is
the bound that removes it, so a stream of BOUNDED WORK -- one backfill, which ends -- carries a
configurable deadline together with wait_for_ready. Both headers still warn against issuing a stream
with ``unary_call_options()``, on the narrower ground that the helper's options are the unary ones and
not this call's, which is why this seam spells its own out. A rule written in three headers is only
true if it is TESTED, which is what the unreachable-peer case below does.
"""

import inspect
import math

import grpc
import pytest

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, InvalidRequestError, Reason, TraderJoeError
from common.rpc.clients.ingest_fetch import DEFAULT_FETCH_DEADLINE_S, GrpcIngestFetchClient, IngestFetchClient
from common.rpc.mapping import ProtoMappingError, bar_to_proto, refused_response
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import (
    ACK_FEED,
    GUARD_S,
    OTHER_FEED,
    Response,
    a_bar,
    a_done,
    a_request,
    accepted_response,
    collected,
    done_response,
    message_class,
    peer_server,
    refused,
    sending,
    sending_then_raising,
)
from schemas.data_ingest import fetch_dataset as domain


pytestmark = pytest.mark.common

# A port nothing listens on. 1 is privileged and unbindable here, which is the point: the connection
# never completes, so wait_for_ready has something real to wait for.
UNREACHABLE = '127.0.0.1:1'


def _page(*bars: domain.Bar) -> Response:
    page = message_class('BarPage')()
    page.bars.extend(bar_to_proto(bar) for bar in bars)
    return Response(page=page)


async def _fetched(channel: grpc.aio.Channel, **options: object):
    client = GrpcIngestFetchClient(channel, deadline_s=options.pop('deadline_s', GUARD_S))
    return await collected(client.fetch(a_request(**options)))


# ---------------------------------------------------------------------------------------------
# THE HAPPY PATH, first, because every refusal below is only meaningful against it


@pytest.mark.asyncio
async def test_a_whole_accepted_stream_crosses_as_domain_objects_in_contract_order():
    """What the seam is FOR: data_store sees the resolved feed, then bars, then the served provenance.

    Asserted by value and not by type, because "three events arrived" would pass for a seam that decoded
    none of them.
    """
    bars = [a_bar(), a_bar(minute=1)]

    async with peer_server(sending(accepted_response(), _page(*bars), done_response())) as channel:
        events, error = await _fetched(channel)

    assert error is None
    assert events == [domain.FetchAccepted(feed=ACK_FEED), domain.BarPage(bars=bars), a_done()]


# ---------------------------------------------------------------------------------------------
# THE IN-BAND REFUSAL


@pytest.mark.asyncio
async def test_a_refused_ack_raises_the_error_a_refused_status_would_have_and_yields_nothing():
    """The caller cannot tell that this refusal travelled in-band, which is the whole obligation.

    It arrives on the reason's own branch, carrying the detail, the allowlisted metadata and the peer's
    error_id -- so data_store's HTTP edge renders it as problem+json exactly like any other, and the id a
    caller quotes is the one in INGEST's log.
    """
    async with peer_server(sending(refused(feed=str(OTHER_FEED)), done_response())) as channel:
        events, error = await _fetched(channel)

    assert events == []
    assert isinstance(error, InvalidRequestError)
    assert error.reason is Reason.FEED_NOT_AVAILABLE
    assert error.metadata['feed'] == str(OTHER_FEED)
    assert error.metadata['error_id']
    assert REASONS[error.reason].branch is InvalidRequestError


@pytest.mark.asyncio
async def test_a_refusal_naming_a_reason_this_release_does_not_know_is_a_protocol_error():
    """A client must survive a reason it has never heard of (ADR tj-fa1rpu D10) -- which is why it is a STRING.

    Failing to DECODE would be the wrong answer: the message is well formed and the contract allows the
    vocabulary to grow. It is a protocol error because this release cannot act on it, and the peer being
    ours makes it deploy skew rather than D10's SDK case.
    """
    unknown = refused()
    unknown.ack.refused.reason = 'A_REASON_FROM_A_LATER_RELEASE'

    async with peer_server(sending(unknown)) as channel:
        events, error = await _fetched(channel)

    assert events == []
    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert 'A_REASON_FROM_A_LATER_RELEASE' in error.detail


@pytest.mark.asyncio
async def test_a_refusal_from_a_foreign_domain_is_a_protocol_error():
    """(domain, reason) is the error's identity: the same reason string in another vocabulary is another error."""
    foreign = refused()
    foreign.ack.refused.domain = 'somebody-else'

    async with peer_server(sending(foreign)) as channel:
        _, error = await _fetched(channel)

    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert ERROR_DOMAIN in error.detail


@pytest.mark.asyncio
async def test_a_refusal_carrying_a_metadata_key_outside_the_allowlist_is_a_protocol_error():
    """ADR tj-fa1rpu D8's allowlist is not advisory: a payload common/errors refuses to rebuild is the peer's problem.

    Rebuilding it anyway would let a peer put arbitrary keys into an error this service then renders.
    """
    leaky = refused()
    leaky.ack.refused.metadata['account_number'] = '12345'

    async with peer_server(sending(leaky)) as channel:
        _, error = await _fetched(channel)

    assert error.reason is Reason.PEER_PROTOCOL_ERROR


@pytest.mark.parametrize('reason', [reason for reason in REASONS if reason is not Reason.FEED_NOT_AVAILABLE])
def test_only_an_unservable_feed_may_be_answered_in_the_ack(reason: Reason):
    """The encode side of the same ruling: every OTHER failure on this hop is a gRPC status.

    Driven off the one REASONS table rather than a hand list, so a reason added tomorrow is in this gate
    tomorrow -- and a reason that is renderable as a status must never reach the ack instead. A servicer
    that answered one here would put an error into the stream that the client is obliged to decode as a
    refusal, bypassing the status path and the boundary's own rendering entirely.

    Args:
        reason: Every reason but the one the ack carries.
    """
    with pytest.raises(ProtoMappingError, match='not answered in the FetchDataset ack'):
        refused_response(error_for(reason))


# ---------------------------------------------------------------------------------------------
# THE GRAMMAR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('why', 'script'),
    [
        ('a page before any ack', lambda: [_page(a_bar())]),
        ('a done before any ack', lambda: [done_response()]),
        ('a second ack', lambda: [accepted_response(), accepted_response(), done_response()]),
        ('an event after the done', lambda: [accepted_response(), done_response(), _page(a_bar())]),
        ('no done at all', lambda: [accepted_response(), _page(a_bar())]),
        ('a message with no arm set', lambda: [Response()]),
        ('nothing at all', lambda: []),
    ],
)
async def test_a_stream_that_breaks_the_contracts_grammar_is_a_protocol_error(why: str, script):
    """Every departure from ack -> page* -> done, including the one that looks like success.

    "No done at all" is the case this seam exists for: the call ends OK, the bars that arrived are
    perfectly valid, and returning them would hand data_store a partial backfill it would record as
    complete. The others are cheap to get wrong in a servicer and free to catch here.

    Args:
        why: What this script does wrong.
        script: Builds the messages the peer sends.
    """
    async with peer_server(sending(*script())) as channel:
        _, error = await _fetched(channel)

    assert isinstance(error, TraderJoeError), f'{why} was accepted'
    assert error.reason is Reason.PEER_PROTOCOL_ERROR, why


@pytest.mark.asyncio
async def test_a_message_this_release_cannot_read_is_a_protocol_error_and_not_a_raw_mapping_error():
    """A ProtoMappingError escaping the seam would be a ValueError in data_store's call stack, not a reason.

    The sub-microsecond Timestamp is the case that is genuinely unreadable rather than merely wrong: the
    wire can name an instant a datetime cannot hold, and the mapper refuses it instead of rounding.
    """
    bar = bar_to_proto(a_bar())
    bar.bar_start.nanos += 1
    page = message_class('BarPage')(bars=[bar])

    async with peer_server(sending(accepted_response(), Response(page=page), done_response())) as channel:
        events, error = await _fetched(channel)

    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert [type(event).__name__ for event in events] == ['FetchAccepted']


# ---------------------------------------------------------------------------------------------
# WHAT THE TRANSPORT ITSELF SAYS


@pytest.mark.asyncio
async def test_a_peer_that_fails_mid_stream_arrives_as_peer_internal_carrying_the_peers_error_id():
    """The boundary the production host installs turns the peer's bug into INTERNAL with an id; this reads it back.

    Keeping the id is what makes an operator's search work across the hop: the id data_store renders is
    the one in INGEST's log, not a fresh one minted on arrival.
    """
    peer = sending_then_raising([accepted_response()], RuntimeError('the reader fell over'))

    async with peer_server(peer) as channel:
        events, error = await _fetched(channel)

    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_INTERNAL
    assert error.metadata['error_id'] in error.detail
    assert 'the reader fell over' not in error.detail, 'the boundary must not leak the peer internals'
    assert [type(event).__name__ for event in events] == ['FetchAccepted']


@pytest.mark.asyncio
async def test_an_unreachable_peer_fails_at_the_deadline_rather_than_waiting_without_bound():
    """THE BOUND BOTH HEADERS CLAIM, made true rather than argued.

    wait_for_ready keeps the call queued while the peer is restarting instead of failing it instantly
    (D6.5 = O1), and WITHOUT a deadline that is an unbounded wait -- exactly what the .proto forbids.
    With one, the call ends as DEADLINE. A hang here is the failure this test exists to catch, so it is
    bounded twice: by the client's own deadline and by the harness guard around it.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        events, error = await collected(GrpcIngestFetchClient(channel, deadline_s=0.3).fetch(a_request()))
    finally:
        await channel.close()

    assert events == []
    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.DEADLINE


def test_the_default_deadline_is_a_finite_bound_and_is_not_the_kafka_rpcs_five_seconds():
    """The number is a judgement, so what is pinned is the reasoning that rules values OUT.

    tj-6znw1h records a single Alpaca 429 costing about 9 s of SDK sleep, and one fetch is one BarsQuery
    that alpaca-py drains internally over many such round trips. So the deadline this migration REPLACES
    -- the Kafka RPC's timeout=5 -- is not a baseline, it is a value that abandoned requests the vendor
    was still serving. A later edit back to a transport-shaped number reds this.
    """
    assert math.isfinite(DEFAULT_FETCH_DEADLINE_S)
    assert DEFAULT_FETCH_DEADLINE_S >= 60.0, 'a one-transaction backfill outlives any transport-shaped deadline'


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', [0.0, -1.0, math.inf, math.nan])
async def test_a_deadline_that_is_not_a_finite_positive_number_is_refused_at_construction(deadline: float):
    """An infinite deadline plus wait_for_ready is precisely the unbounded wait the .proto forbids.

    Refused when the client is BUILT, not on the first call: a deployment misconfiguration should not
    wait for traffic to surface.

    Args:
        deadline: The rejected value.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        with pytest.raises(ValueError, match='finite, positive'):
            GrpcIngestFetchClient(channel, deadline_s=deadline)
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_a_request_this_service_cannot_encode_is_our_bug_and_is_not_dressed_up_as_the_peers():
    """ProtoMappingError, deliberately -- not PEER_PROTOCOL_ERROR, which would blame a peer that never saw it.

    The servicer boundary answers INTERNAL with an error_id for it, which is correct: nothing a caller
    sent can cause it. ``model_construct`` is what reaches the state, because the twin's own validator
    refuses a naive datetime long before the mapper would.
    """
    unencodable = a_request().model_construct(
        **a_request().model_dump() | {'start': a_request().start.replace(tzinfo=None)}
    )

    async with peer_server(sending(accepted_response(), done_response())) as channel:
        _, error = await collected(GrpcIngestFetchClient(channel, deadline_s=GUARD_S).fetch(unencodable))

    assert isinstance(error, ProtoMappingError)
    assert not isinstance(error, TraderJoeError)


# ---------------------------------------------------------------------------------------------
# THE INTERFACE ITSELF


def test_the_grpc_client_matches_the_interface_a_test_double_or_a_replay_backend_implements():
    """ADR tj-8konfu D3's point: the second implementation is an ordinary class, never a fake server.

    A Protocol is checked by a type checker and by nothing at runtime, so the signature is compared here
    -- a parameter renamed on one side and not the other is a double that cannot be substituted, and
    tj-n0nvx1 section 10's replay path is the caller that would find out.
    """
    declared = inspect.signature(IngestFetchClient.fetch)
    implemented = inspect.signature(GrpcIngestFetchClient.fetch)

    assert list(declared.parameters) == list(implemented.parameters)
    assert declared.parameters['request'].annotation == implemented.parameters['request'].annotation
    assert inspect.isasyncgenfunction(GrpcIngestFetchClient.fetch)
