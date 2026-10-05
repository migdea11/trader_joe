"""The round trip over a REAL hop: render -> abort -> grpc.aio -> from_rpc_error, for every reason.

This is the file that answers the bead's first gate item, and it answers it over a real grpc.aio server
on 127.0.0.1 rather than by handing render()'s output straight to from_rpc_error(). The two are not the
same test. A status only reaches the peer by being packed into the grpc-status-details-bin trailer, sent
over HTTP/2 and read back out, and that path has its own ways to lose things: a trailer gRPC strips, a
size limit, a detail the runtime rewrites. Pinning the pair in memory would pass while the hop dropped
everything.

EVERY RENDERABLE REASON, DRIVEN OFF THE TABLE. RENDERABLE comes from REASONS (errors_harness.py), so a row
added to the table is round-tripped on the next run with no list to remember.

BOTH CARDINALITIES THE REPOSITORY SERVES, and both ways a typed error leaves a servicer: RAISED, caught by
ErrorBoundaryInterceptor, and RETURNED, sent by a servicer calling abort_with_error itself (the amendment
of 2026-10-02: tj-3mk3u5.9's servicer converts a BarsFailure, and must get what a raised error gets).
"""

import ast
import inspect
import logging
import re
from types import SimpleNamespace

import grpc
import pytest
from google.rpc import status_pb2
from grpc_status import rpc_status

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Disposition, Reason, new_error_id
from common.rpc import errors as rpc_errors
from common.rpc.errors import from_rpc_error

from .errors_harness import (
    RENDERABLE,
    RESET_AFTER_S,
    STREAM_STREAM,
    UNARY_STREAM,
    UNARY_UNARY,
    Replied,
    aborting,
    boundary_server,
    error_for,
    invoke,
    path,
    raising,
)


pytestmark = pytest.mark.common

UUID4 = re.compile(r'\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')

# The disposition each reason is logged at, per D4. The ADRs fix WHAT the edge logs -- one line naming the
# id, carrying the chain only under an id minted here -- and are silent on the level, so the level comes
# from the disposition table, as TE-3 does at the HTTP edge. Pinned here so the two halves cannot drift.
LEVELS = {Disposition.PAGE: logging.ERROR, Disposition.RECORD: logging.WARNING, Disposition.CLIENT_FIX: logging.INFO}


async def _hop(reason: Reason, method: str = UNARY_UNARY, **kwargs) -> tuple[Replied, Reason]:
    error = error_for(reason, f'{reason} happened here', **kwargs)
    async with boundary_server(raising(error)) as channel:
        replied = await invoke(channel, method)
    return replied, reason


# -----------------------------------------------------------------------------------------------------
# THE WHOLE TABLE, OVER THE WIRE, BOTH CARDINALITIES


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [UNARY_UNARY, UNARY_STREAM], ids=['unary', 'server-streaming'])
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_the_reason_the_branch_the_detail_and_the_code_survive_the_hop(reason: Reason, method: str):
    """D6.4: THE CLIENT BRANCHES ON THE REASON. So the reason is what must come back, not merely a code.

    Several reasons share one code -- seven render as INVALID_ARGUMENT and five as FAILED_PRECONDITION --
    so a client that read the code alone could not tell RATE_BUDGET from DEADLINE. The reason is the only
    thing that distinguishes them, and it is carried in the ErrorInfo, not in the status.
    """
    sent = error_for(reason, f'{reason} happened here')
    async with boundary_server(raising(sent)) as channel:
        replied = await invoke(channel, method)

    assert replied.failed.code() is grpc.StatusCode[REASONS[reason].grpc_code]
    received = from_rpc_error(replied.failed)
    assert received.reason is reason
    assert received.detail == f'{reason} happened here'
    assert type(received) is REASONS[reason].branch, 'rebuilt on the branch, never on a leaf (architect, 15:11)'
    assert isinstance(received, REASONS[reason].branch)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_reset_at_and_the_derived_retry_after_survive_the_hop(reason: Reason):
    """A delay is carried, not invented: reset_at comes back as the same instant, and retry_after follows it."""
    sent = error_for(reason, 'with a window', with_reset_at=True)
    async with boundary_server(raising(sent)) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    received = from_rpc_error(replied.failed)

    assert (received.reset_at is None) == (sent.reset_at is None)
    if sent.reset_at is None:
        assert received.retry_after is None
        return
    assert received.reset_at == sent.reset_at, 'the instant, not a delay recomputed from an arrival time'
    assert received.reset_at.utcoffset().total_seconds() == 0, 'aware and UTC, never naive'
    assert received.retry_after == pytest.approx(RESET_AFTER_S, abs=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_no_reason_arrives_at_the_peer_as_unavailable(reason: Reason):
    """The rule checked where it finally matters: at the client, on what grpc.aio actually reported."""
    replied, _ = await _hop(reason)
    assert replied.failed.code() is not grpc.StatusCode.UNAVAILABLE
    assert from_rpc_error(replied.failed).reason is not Reason.PEER_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_the_allowlisted_metadata_survives_the_hop(reason: Reason):
    sent = error_for(reason, metadata={'vendor': 'the market-data vendor', 'granularity': '1Min'})
    async with boundary_server(raising(sent)) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    received = from_rpc_error(replied.failed)
    assert received.metadata['vendor'] == 'the market-data vendor'
    assert received.metadata['granularity'] == '1Min'


@pytest.mark.asyncio
async def test_a_sequence_metadata_value_arrives_as_one_string_and_is_never_split_back_into_a_tuple():
    """The one-way half of this module's stated encoding, over the hop, so a future re-split is a red.

    A comma inside an item is indistinguishable from the separator, so splitting would invent a tuple the
    sender never had. Nothing carries a sequence across this hop in PR 2; the day something does, changing
    this is a deliberate change, not a drift.
    """
    sent = error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': ('a,b', 'c')})
    async with boundary_server(raising(sent)) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    received = from_rpc_error(replied.failed)
    assert received.metadata['colliding_ids'] == 'a,b,c'
    assert isinstance(received.metadata['colliding_ids'], str), 'never re-split: that would invent a tuple'


# -----------------------------------------------------------------------------------------------------
# THE error_id, ACROSS THE HOP (ADR tj-fa1rpu addendum, 16:22 UTC 2026-10-02)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_every_typed_status_arrives_carrying_an_error_id(reason: Reason):
    """Rule 1, over the wire: there is no path by which a typed status reaches a peer without an id."""
    replied, _ = await _hop(reason)
    received = from_rpc_error(replied.failed)
    assert UUID4.fullmatch(str(received.metadata['error_id'])), 'the edge mints a uuid4 when the error has none'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_an_id_the_error_arrived_with_survives_the_hop_unchanged(reason: Reason):
    """Rule 2: a raise site that logged its chain under an id keeps that id, so both lines name the same one."""
    own = new_error_id()
    sent = error_for(reason, metadata={'error_id': own})
    async with boundary_server(raising(sent)) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    assert from_rpc_error(replied.failed).metadata['error_id'] == own


@pytest.mark.asyncio
async def test_the_id_the_caller_receives_is_the_one_in_the_servers_log(caplog: pytest.LogCaptureFixture):
    """The whole point of the id: a human quotes what they were given and an operator finds that line."""
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(error_for(Reason.VENDOR_AUTH, 'credentials rejected'))) as channel:
            replied = await invoke(channel, UNARY_UNARY)
    received = from_rpc_error(replied.failed)
    error_id = str(received.metadata['error_id'])
    named = [record for record in caplog.records if error_id in record.getMessage()]
    assert len(named) == 1, f'exactly one line per typed error sent, not {len(named)}'


# -----------------------------------------------------------------------------------------------------
# THE ONE LOG LINE, AND WHEN IT CARRIES THE CHAIN (rule 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_one_line_per_typed_error_sent_at_the_level_its_disposition_names(
    reason: Reason, caplog: pytest.LogCaptureFixture
):
    """The ADRs fix the line and are silent on its level, so the level is D4's disposition, as TE-3 does."""
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(error_for(reason, 'sent over the hop'))) as channel:
            await invoke(channel, UNARY_UNARY)
    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1, f'one line per typed error sent, not {len(records)}'
    assert records[0].levelno == LEVELS[REASONS[reason].disposition]
    assert reason.value in records[0].getMessage(), 'the line names the reason'
    assert path(UNARY_UNARY) in records[0].getMessage(), 'and the method, so the line is findable'


@pytest.mark.asyncio
@pytest.mark.parametrize('linked', ['from', 'context'], ids=['raise from e', 'raise inside except'])
async def test_a_minted_id_logs_the_cause_chain(linked: str, caplog: pytest.LogCaptureFixture):
    """Rule 3: the edge minted the id, so this line is the only place the chain is written down."""
    secret = 'postgresql://joe:hunter2@db.internal:5432/trader'
    cause = RuntimeError(f'the vendor client failed: {secret}')
    sent = error_for(Reason.VENDOR_UNAVAILABLE, 'the market-data vendor could not be reached')
    if linked == 'from':
        sent.__cause__ = cause
    else:
        sent.__context__ = cause

    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(sent)) as channel:
            replied = await invoke(channel, UNARY_UNARY)

    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1
    assert records[0].exc_info is not None, 'a minted id and a cause chain means the chain goes in the log'
    assert secret in caplog.text, 'the chain, with its secret, belongs to the operator'
    # And D8's other half, on the same error in the same breath: none of it reached the caller.
    error = replied.failed
    wire = repr((error.details(), list(error.trailing_metadata() or ())))
    assert secret not in wire and 'hunter2' not in wire and 'RuntimeError' not in wire


@pytest.mark.asyncio
async def test_a_suppressed_context_is_not_a_chain(caplog: pytest.LogCaptureFixture):
    """'raise ... from None' says the earlier failure is not the story, and the edge believes it."""
    sent = error_for(Reason.VENDOR_UNAVAILABLE, 'nothing to correlate')
    sent.__context__ = RuntimeError('deliberately suppressed')
    sent.__suppress_context__ = True
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(sent)) as channel:
            await invoke(channel, UNARY_UNARY)
    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1
    assert records[0].exc_info is None


@pytest.mark.asyncio
async def test_an_error_with_no_cause_at_all_logs_one_line_without_a_chain(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(error_for(Reason.NOT_FOUND, 'no such dataset'))) as channel:
            await invoke(channel, UNARY_UNARY)
    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1
    assert records[0].exc_info is None, 'there is no chain to carry'


@pytest.mark.asyncio
async def test_an_error_that_arrived_with_an_id_is_not_logged_with_its_chain_again(caplog: pytest.LogCaptureFixture):
    """Rule 3's second half: whoever set that id already wrote the chain down. Writing it twice is noise.

    This is the case that distinguishes 'always log the chain' from the rule as written, and the one a
    relayed error actually takes: the parser keeps the peer's id, so the next edge sees an error that
    already has one.
    """
    sent = error_for(Reason.VENDOR_UNAVAILABLE, 'relayed', metadata={'error_id': 'logged-by-the-raise-site'})
    sent.__cause__ = RuntimeError('a chain somebody else already wrote down')
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(sent)) as channel:
            replied = await invoke(channel, UNARY_UNARY)
    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1, 'the line is still written: it names the id that went out'
    assert records[0].exc_info is None, 'but not the chain, which is already in the log under this id'
    assert 'logged-by-the-raise-site' in records[0].getMessage()
    assert from_rpc_error(replied.failed).metadata['error_id'] == 'logged-by-the-raise-site'


# -----------------------------------------------------------------------------------------------------
# A RETURNED ERROR GETS WHAT A RAISED ONE GETS (the amendment's 'one function that sends a typed error')


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_a_returned_error_sent_by_the_servicer_is_indistinguishable_on_the_wire_from_a_raised_one(reason: Reason):
    """tj-3mk3u5.9's servicer converts a BarsFailure and calls abort_with_error; the boundary catches a raise.

    Both go through the same function, so the peer cannot tell which happened -- bar the error_id, which
    is minted fresh each time, and which is exactly what must differ.
    """
    detail = 'the same failure, two ways out'
    async with boundary_server(raising(error_for(reason, detail))) as channel:
        raised = from_rpc_error((await invoke(channel, UNARY_UNARY)).failed)
    async with boundary_server(aborting(error_for(reason, detail))) as channel:
        returned = from_rpc_error((await invoke(channel, UNARY_UNARY)).failed)

    assert returned.reason is raised.reason is reason
    assert returned.detail == raised.detail == detail
    assert type(returned) is type(raised)
    assert returned.metadata['error_id'] != raised.metadata['error_id'], 'two sends, two ids'


@pytest.mark.asyncio
async def test_a_returned_error_is_logged_exactly_as_a_raised_one_is(caplog: pytest.LogCaptureFixture):
    sent = error_for(Reason.VENDOR_RATE_LIMITED, 'the vendor throttled us')
    sent.__cause__ = RuntimeError('429 from the vendor')
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(aborting(sent, method=path(UNARY_UNARY))) as channel:
            await invoke(channel, UNARY_UNARY)
    records = [record for record in caplog.records if record.name == 'common.rpc.errors']
    assert len(records) == 1
    assert records[0].levelno == LEVELS[REASONS[Reason.VENDOR_RATE_LIMITED].disposition]
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_a_servicer_that_aborts_on_purpose_is_not_recaught_as_a_bug():
    """Abort raises AbortError to end the call; the boundary lets it through rather than answering INTERNAL."""
    async with boundary_server(aborting(error_for(Reason.NOT_FOUND, 'deliberate'))) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    assert replied.failed.code() is grpc.StatusCode.NOT_FOUND, 'an INTERNAL here means the abort was recaught'


# -----------------------------------------------------------------------------------------------------
# MID-STREAM, TYPED (the shape FetchDataset uses)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [UNARY_STREAM, STREAM_STREAM], ids=['unary-stream', 'stream-stream'])
async def test_a_typed_error_mid_stream_arrives_whole_after_the_messages_already_sent(method: str):
    """The pages a FetchDataset already delivered are kept; the reason explains why the rest never came."""
    sent = error_for(Reason.RATE_BUDGET, 'the budget could not admit the rest in time')
    async with boundary_server(raising(sent), before=4) as channel:
        replied = await invoke(channel, method)
    assert replied.messages == [b'0', b'1', b'2', b'3']
    received = from_rpc_error(replied.failed)
    assert received.reason is Reason.RATE_BUDGET
    assert received.reset_at is not None, 'the window survives an abort part way through a stream'
    assert received.metadata['error_id']


# -----------------------------------------------------------------------------------------------------
# THE TRAILER ITSELF
#
# The rich status rides grpc-status-details-bin, which is why no .proto changed for this commit. Pinned so
# that a grpcio-status upgrade spelling it differently is caught here as well as by the import-time check.


@pytest.mark.asyncio
async def test_the_rich_status_rides_the_grpc_status_details_bin_trailer_and_names_our_domain():
    async with boundary_server(raising(error_for(Reason.NOT_FOUND, 'no such dataset'))) as channel:
        replied = await invoke(channel, UNARY_UNARY)
    trailers = dict(replied.failed.trailing_metadata() or ())
    assert 'grpc-status-details-bin' in trailers, 'no trailer means no ErrorInfo and no reason to branch on'
    assert ERROR_DOMAIN.encode() in trailers['grpc-status-details-bin']


# -----------------------------------------------------------------------------------------------------
# THE IMPORT-TIME CHECK THAT THE TWO HALVES SPELL THE TRAILER THE SAME WAY
#
# The server half writes the trailer through grpcio-status' rpc_status.to_status; the client half reads it
# by name, because grpc_status.rpc_status.aio.from_call is async and takes an aio.Call, not the
# AioRpcError from_rpc_error() is handed. Two spellings of one name is a silent failure: every typed
# status would arrive as PEER_PROTOCOL_ERROR, and only an end-to-end test would notice. _check_module()
# turns that into a refusal at import, so no build can ship it. These tests pin that it is there, that it
# runs, and that it actually fails when the two disagree.


def test_the_module_checks_its_own_assumptions_at_import():
    """A guard defined but never called is no guard. This finds the call, in the module's own source."""
    tree = ast.parse(inspect.getsource(rpc_errors))
    calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
    ]
    assert '_check_module' in calls, 'the import-time check must run at import, not merely be defined'


def test_grpcio_status_writes_the_rich_status_under_the_name_the_client_half_reads():
    """The agreement itself, against the installed grpcio-status rather than against a remembered constant."""
    written = {key for key, _ in rpc_status.to_status(status_pb2.Status()).trailing_metadata}
    assert written == {'grpc-status-details-bin'}


def test_the_check_refuses_a_grpcio_status_that_spelled_the_trailer_differently(monkeypatch: pytest.MonkeyPatch):
    """The guard is only worth having if it fires. An upgrade that renamed the trailer must not import."""
    # grpc.Status is abstract, and _check_module reads only the one member, so a stand-in is enough.
    renamed = SimpleNamespace(trailing_metadata=(('grpc-status-details-v2-bin', b''),))
    monkeypatch.setattr(rpc_errors.rpc_status, 'to_status', lambda status: renamed)
    with pytest.raises(ValueError, match='grpc-status-details-bin'):
        rpc_errors._check_module()


def test_the_check_refuses_a_disposition_with_no_log_level(monkeypatch: pytest.MonkeyPatch):
    """A new Disposition without a level would otherwise KeyError at the moment an error is being sent."""
    monkeypatch.setattr(rpc_errors, '_LOG_LEVELS', {Disposition.PAGE: logging.ERROR})
    with pytest.raises(ValueError, match='no level'):
        rpc_errors._check_module()


def test_the_check_passes_as_the_module_stands():
    """Re-run it after the two negatives, so a monkeypatch that leaked would show up here."""
    rpc_errors._check_module()
