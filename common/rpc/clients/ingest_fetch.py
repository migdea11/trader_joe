"""THE CLIENT SEAM for the internal FetchDataset contract: an interface in domain terms, and gRPC behind it.

data_store calls this; it never imports trader_joe.proto (ADR tj-8konfu D3, and ruff's TID251 enforces it).
What crosses this seam is domain objects and TraderJoeErrors, so the caller branches on a REASON and not on
a status (D6.4), and so the second implementation -- a test double today, a replay backend later
(tj-r6vcgv) -- is an ordinary class and not a fake server.

THE STREAM'S GRAMMAR IS PART OF THE CONTRACT, AND THIS IS WHERE IT IS ENFORCED:

    accepted : FetchAck(accepted) -> BarPage* -> FetchDone, then the call ends OK.
    refused  : FetchAck(refused), and nothing else.

An ack is always first. A stream that ends without a FetchDone is distinguishable from one that completed,
which is the whole reason FetchDone exists -- so this seam raises rather than returning a short stream as a
success. Every departure from the grammar is PEER_PROTOCOL_ERROR, because the peer is NOT always our own
server: a test double, a replay backend or a version-skewed deployment all arrive here.

THE BAR FEED MUST EQUAL THE ACK FEED (architect ruling, tj-3mk3u5.28, 02:28 UTC 2026-10-03). The client has
the ack before any page by construction, so it can check -- and it must, because data_store writes its
dataset entry from the ACK and its rows from the BARS: a disagreeing page would make the ledger record one
feed series identity (tj-u12tjo.11) while the rows contradict it, with no error anywhere. A page whose bars
do not all carry the ack's feed is PEER_PROTOCOL_ERROR. The check runs BEFORE the page is yielded, so no bar
on a disagreeing feed ever reaches the caller; pages already yielded are not recalled, because buffering a
whole backfill to make the failure atomic would defeat the paging the contract is built on, and the caller's
own write is already abandoned by the raise.

WHAT A REFUSED ACK BECOMES. The refusal mirrors google.rpc.ErrorInfo, so it is rebuilt into the SAME
TraderJoeError a REFUSED status would have produced -- an InvalidRequestError carrying FEED_NOT_AVAILABLE,
its detail, its allowlisted metadata and the peer's error_id -- and raised BEFORE anything is yielded. A
reason this release does not know, a foreign domain, or a payload common/errors refuses to rebuild is
PEER_PROTOCOL_ERROR instead of a decode failure: a client must survive a reason it has never heard of
(ADR tj-fa1rpu D10), which is why the field is a string on the wire.

>>> TWO OBLIGATIONS THIS SEAM CREATES FOR ITS CALLERS, carried only here. <<<

1. A SERVICER THAT CALLS THIS MUST CATCH AND CONVERT, NEVER LET THE ERROR ESCAPE (architect ruling,
   05:58 UTC 2026-10-03, candidate (a)). PEER_UNAVAILABLE, PEER_INTERNAL, PEER_PROTOCOL_ERROR and DEADLINE
   are the reasons this seam raises, and all four have NO grpc_code in the one REASONS table. So
   common/rpc/errors.py's boundary treats one that escapes a servicer as a bug: INTERNAL, a fresh error_id
   and an 'Unhandled ExogenousError' log line with a traceback. The caller still gets a reply and nothing
   leaks (ADR tj-fa1rpu D1(b) and D8 are kept), but a peer's failure is recorded as ours and the log line
   misleads whoever reads it. A servicer making an outbound call therefore catches the peer error and
   converts it DELIBERATELY into one of its own reasons. No upstream-failure reason was added to the table:
   that would change TE-1's table for zero call sites, and the first real relay site gets to reconsider it
   with something concrete to reason about. Nothing polls this obligation -- it lives in this docstring.
2. THE PEER_PROTOCOL_ERROR DETAIL IS A HUMAN FIELD CARRYING FOREIGN TEXT. It quotes what the peer sent so a
   disagreement is diagnosable (ADR tj-fa1rpu C8). NOTHING MAY PARSE IT. Both services ship from one
   release today, so that text is our own; the day a peer is not ours -- a replay backend (tj-r6vcgv), a
   third party, D10's SDK case -- it must be truncated or dropped before it reaches a response body.

THE DEADLINE, AND WHY THIS CALL HAS ONE THOUGH IT IS A STREAM. The architect's ruling (tj-3mk3u5.28, 01:25
UTC 2026-10-02, restated 05:58 UTC 2026-10-03) requires this fetch to carry a configurable deadline WITH
wait_for_ready, and that is what ADR tj-8konfu D6.1 asks for once its reason is read: what it forbids is an
unbounded wait, and a finite deadline is exactly the bound that removes it. This stream is bounded work --
one backfill, which ends -- and not the indefinitely-open subscription a deadline would wrongly kill.
Without a deadline ADR tj-fa1rpu U4's fail-fast would have nothing to bound either. ingest.proto's header
and common/rpc/channel.py's now both say so, and both still warn against issuing a stream with
common.rpc.channel.unary_call_options(): that helper's options are the unary ones and not this call's. So
this seam spells its own call options out rather than calling the helper, and the bound is explicit.
"""

import math
from collections.abc import AsyncIterator
from typing import Final, Protocol

import grpc

from common.enums.data_stock import Feed
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, ExogenousError, Reason, TraderJoeError
from common.rpc.errors import from_rpc_error
from common.rpc.mapping.fetch_dataset import ack_to_domain, done_to_domain, page_to_domain, request_to_proto
from common.rpc.mapping.values import ProtoMappingError
from schemas.data_ingest import fetch_dataset as domain
from schemas.data_ingest.fetch_dataset import FetchEvent
from trader_joe.proto.internal.ingest.v1 import ingest_pb2, ingest_pb2_grpc


# WHAT A FETCH IS ALLOWED TO TAKE, and why it is this number.
#
# It is NOT the 5 s the deleted Kafka RPC client defaulted to. That value is what this migration REPLACED,
# not a baseline: tj-6znw1h records a single Alpaca 429 costing about 9 s of SDK sleep, which already
# outlived it, so the old deadline abandoned requests the vendor was still serving.
#
# Transport is not the constraint either (tj-q3zugf): gRPC's fixed per-call cost on the measured rig is
# about 4-5 ms and its p99 at 100 concurrent 512 KiB calls was 0.076 s. Both terms are noise here.
#
# The deadline is governed by the VENDOR CALL and the SIZE OF A ONE-TRANSACTION BACKFILL:
#   * a vendor read in data/ingest is bounded by nothing of its own -- no socket timeout is configured --
#     and alpaca-py retries a 429 or 504 up to three times with a blocking 3 s sleep between attempts, so
#     ONE rate-limited vendor call can cost 10 s or more before it succeeds;
#   * one fetch is one BarsQuery, and alpaca-py drains the whole requested range internally, so a backfill
#     of years of one-minute bars is tens to hundreds of vendor round trips inside this single call;
#   * the channel's reconnect backoff is capped at 2 s plus gRPC's 20 % jitter (common/rpc/config.py), so
#     any deadline must comfortably exceed about 2.4 s merely to notice a peer that has come back.
# Five minutes covers a backfill of that shape with several rate-limit cycles inside it, and still bounds a
# call that is genuinely stuck. It is a DEFAULT: the caller that builds the client may set its own, and
# this seam reads no environment variable of its own (ADR tj-q9ae5u addendum 5 -- the caller resolves the
# target and owns the channel).
DEFAULT_FETCH_DEADLINE_S: Final = 300.0

# The arms of FetchDatasetResponse's oneof, named rather than spelled at each comparison.
_ACK: Final = 'ack'
_PAGE: Final = 'page'
_DONE: Final = 'done'


def _protocol_error(why: str) -> ExogenousError:
    # C8: a reply that will not map is its own reason, never a hang and never an escaping ValueError. See
    # obligation 2 in the module docstring -- the text may quote the peer, and nothing parses it.
    return ExogenousError(Reason.PEER_PROTOCOL_ERROR, f'the FetchDataset stream {why}')


def _refusal_error(refused: domain.FetchRefused) -> TraderJoeError:
    # Rebuild the error the refusal mirrors, exactly as common/rpc/errors.py rebuilds one from an ErrorInfo
    # on a status, so the in-band refusal and the out-of-band one are indistinguishable to the caller.
    if refused.domain != ERROR_DOMAIN:
        return _protocol_error(
            f'was refused in a domain this release does not answer for: {refused.domain!r}, not {ERROR_DOMAIN!r}'
        )
    if refused.reason not in Reason.__members__:
        return _protocol_error(f'was refused for {refused.reason!r}, which is not a reason this release knows')
    reason = Reason(refused.reason)
    try:
        # On the reason's own branch, never on a leaf, whose constructor may take different arguments. A
        # REFUSED reason carries no reset_at, and a metadata key outside the allowlist is refused here --
        # both land in the except below rather than building an error the vocabulary would not allow.
        return REASONS[reason].branch(reason, refused.detail, metadata=dict(refused.metadata))
    except (TypeError, ValueError) as error:
        return _protocol_error(f'was refused for {reason} with a payload that will not rebuild here: {error}')


class _StreamReader:
    # The grammar, as state. One instance per call; it sees every message in order and nothing else.
    def __init__(self) -> None:
        self._accepted: domain.FetchAccepted | None = None
        self._done = False

    def event(self, response: ingest_pb2.FetchDatasetResponse) -> FetchEvent:
        arm = response.WhichOneof('event')
        if arm is None:
            raise _protocol_error('carried a message with no event set; its oneof holds one arm')
        if self._done:
            raise _protocol_error(f'carried a {arm} after its FetchDone, which terminates the stream')
        if arm == _ACK:
            return self._ack(response.ack)
        if self._accepted is None:
            raise _protocol_error(f'began with a {arm}; an ack is always the first message')
        if arm == _PAGE:
            return self._page(self._accepted.feed, response.page)
        self._done = True
        return done_to_domain(response.done)

    def _ack(self, message: ingest_pb2.FetchAck) -> domain.FetchAccepted:
        if self._accepted is not None:
            raise _protocol_error('carried a second ack; the feed is settled once, by the first')
        ack = ack_to_domain(message)
        if ack.refused is not None:
            raise _refusal_error(ack.refused)
        if ack.accepted is None:
            # Unreachable: the domain model's own validator refuses an ack with neither arm set. Spelled
            # out rather than asserted, so this reader stays total whatever a later edit does to that model.
            raise _protocol_error('carried an ack with neither outcome set')
        self._accepted = ack.accepted
        return ack.accepted

    def _page(self, resolved: Feed, message: ingest_pb2.BarPage) -> domain.BarPage:
        page = page_to_domain(message)
        disagreeing = sorted({str(bar.feed) for bar in page.bars if bar.feed is not resolved})
        if disagreeing:
            raise _protocol_error(
                f'acknowledged the feed {resolved} and then sent a page carrying bars on {disagreeing}; the '
                'dataset entry is written from the ack and its rows from the bars, so the two disagreeing '
                'would record one feed series identity against rows from another (tj-u12tjo.11)'
            )
        return page

    def ended(self) -> None:
        if not self._done:
            raise _protocol_error(
                'ended without a FetchDone; a stream that merely stops is not one that finished, which is '
                'what FetchDone exists to distinguish'
            )


class IngestFetchClient(Protocol):
    """One dataset fetch, in domain terms. The interface a replay backend or a test double implements.

    Implementations deal only in schemas/data_ingest models and TraderJoeErrors. Nothing about gRPC is
    visible here, which is the point: ADR tj-8konfu D3's seam, and tj-n0nvx1 section 10's replay path, both
    need a fetch that is not a server.
    """

    def fetch(self, request: domain.FetchDatasetRequest) -> AsyncIterator[FetchEvent]:
        """Fetch one dataset, yielding the accepted ack, then its pages, then its done.

        Args:
            request: What to fetch.

        Returns:
            AsyncIterator[FetchEvent]: The stream's events, in contract order. The ack comes first, so a
            caller has the resolved feed before any bar.

        Raises:
            TraderJoeError: Before the first event when the fetch is refused, and at any point when the
                call or the stream fails. See this module's docstring: a servicer that calls this converts
                the error deliberately rather than letting it escape into the error boundary.
        """
        ...


class GrpcIngestFetchClient:
    """IngestFetchClient over grpc.aio, against data_ingest's IngestService.

    It takes a channel from its caller and reads NO environment variable: the peer's target is resolved by
    whoever owns the channel, with common.rpc.channel.target_from_env (ADR tj-q9ae5u addendum 5), and one
    channel is shared by every stub that calls the same peer.
    """

    def __init__(self, channel: grpc.aio.Channel, *, deadline_s: float = DEFAULT_FETCH_DEADLINE_S) -> None:
        """Bind the client to a channel and a deadline.

        Args:
            channel: A channel from common.rpc.channel.create_channel, owned and closed by the caller.
            deadline_s: How long one fetch may take, in seconds from the call. See DEFAULT_FETCH_DEADLINE_S
                for what the default is derived from.

        Raises:
            ValueError: If the deadline is not a finite, positive number of seconds. wait_for_ready without
                a bound would wait forever for a peer that is down, which is what ingest.proto warns about.
        """
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise ValueError(f'a FetchDataset call needs a finite, positive deadline in seconds, got {deadline_s!r}')
        self._stub = ingest_pb2_grpc.IngestServiceStub(channel)
        self._deadline_s = deadline_s

    @property
    def deadline_s(self) -> float:
        """float: How long one fetch may take, in seconds."""
        return self._deadline_s

    async def fetch(self, request: domain.FetchDatasetRequest) -> AsyncIterator[FetchEvent]:
        """Fetch one dataset over gRPC, yielding the accepted ack, then its pages, then its done.

        Args:
            request: What to fetch.

        Yields:
            FetchEvent: The accepted ack, then zero or more pages, then the done.

        Raises:
            TraderJoeError: A refused ack, rebuilt as the error a REFUSED status would have given, raised
                before anything is yielded; the error from_rpc_error reads out of a failed call
                (PEER_UNAVAILABLE, DEADLINE, PEER_INTERNAL, or the peer's own typed reason); or
                PEER_PROTOCOL_ERROR for a stream that breaks the contract's grammar, carries a message this
                release cannot read, or sends a bar whose feed is not the one the ack settled.
            ProtoMappingError: If the REQUEST cannot be put on the wire. That is this service's own bug,
                not the peer's, and it is deliberately not dressed up as a peer error.
        """
        # Outside the try on purpose: an unmappable request is ours, and must not be reported as the peer's.
        message = request_to_proto(request)
        reader = _StreamReader()
        call = self._stub.FetchDataset(message, timeout=self._deadline_s, wait_for_ready=True)
        try:
            async for response in call:
                yield reader.event(response)
        except grpc.aio.AioRpcError as error:
            raise from_rpc_error(error) from error
        except ProtoMappingError as error:
            raise _protocol_error(f'carried a message this release cannot read: {error}') from error
        finally:
            # A stream abandoned mid-way -- by a raise here or by a caller that stops iterating -- would
            # otherwise stay open on the peer until a keepalive noticed. Cancelling a finished call is a
            # no-op.
            call.cancel()
        reader.ended()
