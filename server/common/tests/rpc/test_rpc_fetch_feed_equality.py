"""THE BAR FEED EQUALS THE ACK FEED -- a CROSS-MESSAGE invariant the round trip structurally cannot see.

WHY THIS IS A SEPARATE FILE (architect ruling, tj-3mk3u5.28 and .29 item 5, 02:27 UTC 2026-10-03).
``test_rpc_fetch_mapping.py`` is descriptor-driven and PER-MESSAGE: it proves every field of every
message survives the mapping, and a green run there says NOTHING about whether the bars of one stream
agree with the ack that opened it. That is a rule about the stream, so it needs its own pins -- and the
shape of failure it guards against is precisely "an adjacent green test made it look covered".

WHAT GOES WRONG WITHOUT IT. ``market/v1``'s Bar carries its own feed, deliberately: a bar reaching the UI
or an external stream has no fetch envelope to read one from. On THIS contract the feed is ALSO settled
once, in ``FetchAccepted``, and ``bar.proto`` line 41 asserts the two must agree -- which until
tj-3mk3u5.28 was prose with nothing behind it. data_store writes its dataset entry from the ACK and its
rows from the BARS, so a page that disagrees makes the ledger record one feed series identity
(tj-u12tjo.11) while the rows contradict it, with no error anywhere.

THE TWO ENFORCEMENT POINTS, AND WHY BOTH ARE PINNED.

(a) ENCODE, in ``FetchStreamEncoder``. The resolved feed is fixed when the encoder is built, so the
    server cannot emit an ack and a page that disagree. A domain bar arriving with a DIFFERENT feed is an
    ingest defect and must RAISE: overwriting it would conceal exactly the bug the single resolved feed
    exists to prevent. (The escape hatch in the bead -- "if the domain bar has no feed, stamp
    unconditionally" -- does not apply: ``schemas/data_ingest/fetch_dataset.py`` Bar.feed is required and
    populated upstream, so the raise is reachable.)

(b) DECODE, in the ``IngestFetchClient`` seam, and this is the half that protects data_store, because the
    peer is NOT always our own encoder: a test double, a replay backend (tj-r6vcgv) or a version-skewed
    server all arrive here. The bad stream is therefore built AT THE WIRE -- a proto ``BarPage`` whose
    bar carries the other feed -- and not by reaching past the encoder, which cannot produce one.

(c) AND THE PASSING CASE, which is not optional: a check that rejected every page would satisfy (a) and
    (b) and break the contract.
"""

import pytest

from common.enums.data_stock import Feed
from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.ingest_fetch import GrpcIngestFetchClient
from common.rpc.mapping import FEED, MAX_PAGE_BARS, FetchStreamEncoder, ProtoMappingError, bar_to_proto, page_to_domain
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
    sending,
)
from schemas.data_ingest import fetch_dataset


pytestmark = pytest.mark.common


# ---------------------------------------------------------------------------------------------
# (a) ENCODE -- the server cannot build a disagreeing page


def test_the_encoder_stamps_every_bar_it_emits_with_the_streams_resolved_feed():
    """The feed is not a per-event argument, so an ack and a page that disagree are not CONSTRUCTIBLE here.

    Both events are read back through the mapping rather than off the wire fields, so this asserts what a
    client would actually see and not merely that two integers match.
    """
    encoder = FetchStreamEncoder(ACK_FEED)

    acknowledged = encoder.accepted().ack.accepted.feed
    page = page_to_domain(encoder.page([a_bar(), a_bar(minute=1)]).page)

    assert FEED.to_domain(acknowledged) is ACK_FEED
    assert [bar.feed for bar in page.bars] == [ACK_FEED, ACK_FEED]
    assert encoder.feed is ACK_FEED


def test_the_encoder_raises_on_a_bar_whose_feed_is_not_the_resolved_one_rather_than_overwriting_it():
    """OVERWRITING WOULD HIDE THE BUG THE STAMPING EXISTS TO PREVENT (architect ruling, 02:28 UTC 2026-10-03).

    The domain Bar's feed is required and independently sourced -- the reader populates it upstream -- so
    a bar reaching the encoder on another tape is an INGEST DEFECT, not a formatting detail. Raising sends
    it through the servicer boundary as INTERNAL with an error_id, which is the correct treatment:
    nothing a caller sent can cause it.
    """
    encoder = FetchStreamEncoder(ACK_FEED)

    with pytest.raises(ProtoMappingError) as excinfo:
        encoder.page([a_bar(), a_bar(minute=1, feed=OTHER_FEED)])

    assert str(OTHER_FEED) in str(excinfo.value)
    # And the page is refused WHOLE: nothing silently re-stamped and sent on.
    assert encoder.page([a_bar()]).page.bars[0].feed == FEED.to_proto(ACK_FEED)


def test_the_encoder_refuses_a_page_larger_than_the_contracts_page_size():
    """The page size is the SERVER's to honour because the wire cannot express it, so nothing downstream catches this.

    Pinned beside the feed rule because both are stream-level obligations the message mappers cannot see.
    """
    encoder = FetchStreamEncoder(ACK_FEED)
    one_bar = a_bar()

    assert encoder.page([one_bar] * MAX_PAGE_BARS).page.bars, "the contract's own page size must be acceptable"
    with pytest.raises(ProtoMappingError, match='at most'):
        encoder.page([one_bar] * (MAX_PAGE_BARS + 1))


# ---------------------------------------------------------------------------------------------
# (b) DECODE -- the seam refuses a disagreeing page built at the wire


def _page_on(*feeds: object) -> Response:
    # AT THE WIRE, not through the encoder: the encoder is the thing that cannot produce this, and the
    # peer is not always our encoder. Each bar is otherwise valid; only its feed field is moved.
    page = message_class('BarPage')()
    for minute, feed in enumerate(feeds):
        bar = bar_to_proto(a_bar(minute=minute))
        bar.feed = FEED.to_proto(feed)
        page.bars.append(bar)
    return Response(page=page)


@pytest.mark.asyncio
async def test_the_seam_refuses_a_page_whose_bar_feed_is_not_the_acks_and_yields_no_bars():
    """THE HALF THAT PROTECTS data_store. The client has the ack before any page, so it CAN check -- and must.

    PEER_PROTOCOL_ERROR per ADR tj-fa1rpu's boundary contract, the same treatment the seam gives an
    unrecognised refusal reason. The caller receives the accepted ack and then the error: no bar on a
    disagreeing feed reaches it, so the store's own write is abandoned before a row is written.
    """
    peer = sending(accepted_response(), _page_on(ACK_FEED, OTHER_FEED), done_response())

    async with peer_server(peer) as channel:
        events, error = await collected(GrpcIngestFetchClient(channel, deadline_s=GUARD_S).fetch(a_request()))

    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert [type(event).__name__ for event in events] == ['FetchAccepted']
    assert str(OTHER_FEED) in error.detail
    assert str(ACK_FEED) in error.detail


# ---------------------------------------------------------------------------------------------
# (c) THE PASSING CASE -- a check that rejected everything would pass (a) and (b)


@pytest.mark.asyncio
async def test_a_page_whose_bars_all_carry_the_ack_feed_reaches_the_caller_untouched():
    """Without this, "reject every page" satisfies both pins above and breaks the contract completely.

    The bars are compared by VALUE against what the encoder was handed, so a check that passed the page
    through while mangling it would not satisfy this either.
    """
    bars = [a_bar(), a_bar(minute=1)]
    encoder = FetchStreamEncoder(ACK_FEED)
    peer = sending(encoder.accepted(), encoder.page(bars), done_response())

    async with peer_server(peer) as channel:
        events, error = await collected(GrpcIngestFetchClient(channel, deadline_s=GUARD_S).fetch(a_request()))

    assert error is None
    assert [type(event).__name__ for event in events] == ['FetchAccepted', 'BarPage', 'FetchDone']
    assert events[0].feed is ACK_FEED
    assert events[1].bars == bars
    assert events[2] == a_done()


@pytest.mark.asyncio
async def test_a_feed_the_ack_never_settled_is_refused_even_when_every_bar_agrees_with_itself():
    """The rule is equality with the ACK, not internal consistency of the page.

    A peer that resolved one tape and then served another, consistently, is the version-skew case: every
    bar agrees with every other bar and the dataset entry still names the wrong series.
    """
    peer = sending(accepted_response(), _page_on(OTHER_FEED, OTHER_FEED), done_response())

    async with peer_server(peer) as channel:
        events, error = await collected(GrpcIngestFetchClient(channel, deadline_s=GUARD_S).fetch(a_request()))

    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert not [event for event in events if type(event).__name__ == 'BarPage']


def test_the_domain_bar_carries_an_independently_sourced_feed_so_the_raise_is_reachable():
    """The architect checked this before ruling, and it is what makes (a)'s raise more than decoration.

    If ``Bar.feed`` were absent or optional, the encoder would have nothing to compare against and the
    ruling's escape hatch -- stamp unconditionally -- would apply instead. Pinned so that making the field
    optional is a decision somebody takes deliberately rather than one that quietly empties this file.
    """
    field = fetch_dataset.Bar.model_fields['feed']

    assert field.is_required()
    assert field.annotation is Feed
