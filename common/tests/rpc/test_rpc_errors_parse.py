"""from_rpc_error(): a failed gRPC call as the TraderJoeError it stands for (bead item 3; ADR tj-fa1rpu C8).

THE HOP TESTS COVER WHAT OUR OWN SERVER SENDS. This file covers everything else -- the replies a peer
could send that our renderer never would. They are built by hand as grpc.aio.AioRpcError objects, because
a well-behaved server cannot be made to emit them: a status with a foreign domain, a reason this release
has never heard of, a trailer that is not a protobuf at all.

THE RULE THEY ALL ANSWER TO IS C8. In the Kafka layer this replaces, two identical `except Exception`
blocks turned 'the handler failed', 'the reply would not parse' and 'the service is not running' into one
client-side timeout, and an operator could not tell them apart. So: a reply that will not map is its OWN
reason, PEER_PROTOCOL_ERROR, never a hang and never an escaping ValueError or TypeError out of
common/errors' constructor. from_rpc_error raises nothing, and the last test here sweeps every status
code gRPC defines to say so.

THE THREE TRANSPORT OUTCOMES are read from the CODE, before any detail is unpacked, because they are the
transport speaking rather than a peer's vocabulary (ADR tj-8konfu D6.4).
"""

from datetime import UTC, datetime, timedelta

import grpc
import pytest
from google.protobuf import duration_pb2
from google.rpc import error_details_pb2, status_pb2

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, RESERVED_REASONS, ExogenousError, Reason, new_error_id
from common.rpc.errors import from_rpc_error

from .errors_harness import RENDERABLE


pytestmark = pytest.mark.common

TRAILER = 'grpc-status-details-bin'
# Any code that is not one of the three transport outcomes, so the rich status is what decides.
TYPED_CODE = grpc.StatusCode.FAILED_PRECONDITION


def _status(
    reason: str = Reason.VENDOR_UNAVAILABLE.value,
    *,
    domain: str = ERROR_DOMAIN,
    metadata: dict[str, str] | None = None,
    with_error_info: bool = True,
    retry_delay_s: int | None = None,
    message: str = 'the peer said so',
) -> status_pb2.Status:
    status = status_pb2.Status(code=TYPED_CODE.value[0], message=message)
    if with_error_info:
        status.details.add().Pack(error_details_pb2.ErrorInfo(reason=reason, domain=domain, metadata=metadata or {}))
    if retry_delay_s is not None:
        status.details.add().Pack(error_details_pb2.RetryInfo(retry_delay=duration_pb2.Duration(seconds=retry_delay_s)))
    return status


def _reply(
    code: grpc.StatusCode = TYPED_CODE,
    *,
    status: status_pb2.Status | None = None,
    raw_trailer: bytes | None = None,
    details: str = 'the peer said so',
) -> grpc.aio.AioRpcError:
    trailing = grpc.aio.Metadata()
    if status is not None:
        trailing.add(TRAILER, status.SerializeToString())
    if raw_trailer is not None:
        trailing.add(TRAILER, raw_trailer)
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), trailing, details)


# -----------------------------------------------------------------------------------------------------
# THE THREE TRANSPORT OUTCOMES, READ FROM THE CODE


def test_unavailable_is_the_peer_being_down_not_a_reason_the_peer_chose():
    """The server never sends UNAVAILABLE deliberately (D6.4), so one that arrives means the peer is gone."""
    received = from_rpc_error(_reply(grpc.StatusCode.UNAVAILABLE, details='failed to connect'))
    assert received.reason is Reason.PEER_UNAVAILABLE
    assert type(received) is ExogenousError
    assert received.detail == 'failed to connect'


def test_deadline_exceeded_is_deadline():
    assert from_rpc_error(_reply(grpc.StatusCode.DEADLINE_EXCEEDED)).reason is Reason.DEADLINE


def test_internal_is_peer_internal_and_keeps_the_id_out_of_the_peers_fixed_bug_message():
    """A bug carries no ErrorInfo, so the fixed message is the only machine-readable thing it has.

    Reading the id back out of it is not parsing a human detail (D8): it is reading OUR OWN one-line
    format, written by the same constant that reads it. The payoff is that data_store's HTTP edge renders
    the id from INGEST's log, and the id a caller quotes is the one an operator searches for.
    """
    peer_id = new_error_id()
    received = from_rpc_error(_reply(grpc.StatusCode.INTERNAL, details=f'internal error; error_id {peer_id}'))
    assert received.reason is Reason.PEER_INTERNAL
    assert received.metadata['error_id'] == peer_id


@pytest.mark.parametrize(
    'details',
    [
        'Exception calling application: boom',
        '',
        'internal error; error_id ',
        'internal error, error_id 1234',
        'prefixed internal error; error_id abc',
    ],
    ids=['a foreign peer', 'no message', 'the template with no id', 'a near miss', 'not at the start'],
)
def test_an_internal_that_is_not_our_fixed_template_invents_no_id(details: str):
    """Better no id than an id that names no log line anywhere. A foreign INTERNAL is still PEER_INTERNAL."""
    received = from_rpc_error(_reply(grpc.StatusCode.INTERNAL, details=details))
    assert received.reason is Reason.PEER_INTERNAL
    assert 'error_id' not in received.metadata


@pytest.mark.parametrize(
    ('code', 'reason'),
    [
        (grpc.StatusCode.UNAVAILABLE, Reason.PEER_UNAVAILABLE),
        (grpc.StatusCode.DEADLINE_EXCEEDED, Reason.DEADLINE),
        (grpc.StatusCode.INTERNAL, Reason.PEER_INTERNAL),
    ],
)
def test_the_transport_outcomes_are_decided_before_any_detail_is_unpacked(code: grpc.StatusCode, reason: Reason):
    """A peer that attaches an ErrorInfo to one of these cannot talk the transport out of what it means."""
    received = from_rpc_error(_reply(code, status=_status(Reason.NOT_FOUND.value)))
    assert received.reason is reason


@pytest.mark.parametrize(
    'code', [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.INTERNAL]
)
def test_a_transport_outcome_with_no_message_still_gets_a_human_detail(code: grpc.StatusCode):
    """TE-1 requires a detail; an empty one from the transport would otherwise reach a human as blank."""
    assert from_rpc_error(_reply(code, details='')).detail


# -----------------------------------------------------------------------------------------------------
# A REPLY THAT WILL NOT MAP IS ITS OWN REASON (C8)

_MALFORMED = {
    'no rich status at all': _reply(),
    'a trailer that is not a protobuf': _reply(raw_trailer=b'\xff\xff\xff\xff not a message'),
    'a status with no details': _reply(status=_status(with_error_info=False)),
    'details but no ErrorInfo': _reply(status=_status(with_error_info=False, retry_delay_s=30)),
    'a foreign domain': _reply(status=_status(domain='some.other.service')),
    'an empty domain': _reply(status=_status(domain='')),
    'a reason this release does not know': _reply(status=_status('WHO_KNOWS')),
    'an empty reason': _reply(status=_status('')),
    'a reason reserved for a later release': _reply(status=_status(RESERVED_REASONS[0])),
    'a lower-case reason': _reply(status=_status('not_found')),
    'a metadata key outside the allowlist': _reply(status=_status(metadata={'account_id': '904837e3'})),
    'a REFUSED reason carrying a reset_at': _reply(
        status=_status(Reason.NOT_FOUND.value, metadata={'reset_at': '2026-10-03T14:00:00+00:00'})
    ),
    'a reset_at that is not a timestamp': _reply(
        status=_status(Reason.RATE_BUDGET.value, metadata={'reset_at': 'soon'})
    ),
    'a naive reset_at': _reply(status=_status(Reason.RATE_BUDGET.value, metadata={'reset_at': '2026-10-03T14:00:00'})),
    'a reason that requires a window, with none': _reply(status=_status(Reason.RATE_BUDGET.value)),
    'a REFUSED reason carrying a RetryInfo': _reply(status=_status(Reason.NOT_FOUND.value, retry_delay_s=30)),
}


@pytest.mark.parametrize('reply', _MALFORMED.values(), ids=_MALFORMED.keys())
def test_every_reply_that_will_not_map_becomes_peer_protocol_error(reply: grpc.aio.AioRpcError):
    """C8: its own reason, so an operator can tell 'we disagree about the schema' from 'the peer is down'.

    Never an escaping ValueError or TypeError out of common/errors' constructor, which is the shape the
    architect's 15:11 ruling is about: TE-1 refuses things a decoded wire error could carry, and a refusal
    WHILE DECODING is a reply that will not map.
    """
    received = from_rpc_error(reply)
    assert received.reason is Reason.PEER_PROTOCOL_ERROR
    assert type(received) is ExogenousError


@pytest.mark.parametrize('reply', _MALFORMED.values(), ids=_MALFORMED.keys())
def test_a_protocol_error_says_what_arrived_so_a_human_can_see_the_disagreement(reply: grpc.aio.AioRpcError):
    """The raw text goes in the detail, where a human reads it and nothing parses it (RFC 9457 s3.1.4)."""
    detail = from_rpc_error(reply).detail
    assert reply.code().name in detail, 'the code the peer answered'
    assert repr(reply.details()) in detail, "and the peer's own message, quoted"


def test_a_protocol_error_is_never_mistaken_for_the_peer_being_down():
    """The distinction C8 exists for. Both recoveries differ, and so does the alert."""
    received = from_rpc_error(_reply(status=_status('WHO_KNOWS')))
    assert received.reason is not Reason.PEER_UNAVAILABLE
    assert received.reason is not Reason.PEER_INTERNAL


def test_an_unknown_reason_here_is_deploy_skew_and_the_detail_names_it():
    """Both services ship from one release, so an unknown reason is skew, not D10's SDK case."""
    assert 'WHO_KNOWS' in from_rpc_error(_reply(status=_status('WHO_KNOWS'))).detail


# -----------------------------------------------------------------------------------------------------
# WHAT A WELL-FORMED REPLY FROM A PEER YIELDS


@pytest.mark.parametrize('reason', RENDERABLE)
def test_a_well_formed_error_info_rebuilds_on_its_own_branch_with_its_reason_preserved(reason: Reason):
    """D6.4: the reason is what the client branches on, so it is preserved rather than collapsed to a code."""
    metadata = {'reset_at': (datetime.now(UTC) + timedelta(seconds=60)).isoformat()}
    needs_window = REASONS[reason].requires_reset_at
    received = from_rpc_error(_reply(status=_status(reason.value, metadata=metadata if needs_window else None)))
    assert received.reason is reason
    assert type(received) is REASONS[reason].branch


def test_a_reset_at_in_the_metadata_is_preferred_over_a_retry_info():
    """An instant does not go stale on the way; a delay measured at the sender does."""
    reset_at = datetime.now(UTC) + timedelta(seconds=600)
    received = from_rpc_error(
        _reply(status=_status(Reason.RATE_BUDGET.value, metadata={'reset_at': reset_at.isoformat()}, retry_delay_s=5))
    )
    assert received.reset_at == reset_at
    assert received.retry_after > 5


def test_a_retry_info_with_no_reset_at_is_read_relative_to_now():
    """RetryInfo means 'wait this long from when you got it', so the instant is computed on arrival."""
    received = from_rpc_error(_reply(status=_status(Reason.VENDOR_RATE_LIMITED.value, retry_delay_s=120)))
    assert received.reason is Reason.VENDOR_RATE_LIMITED
    assert received.retry_after == pytest.approx(120, abs=2)


def test_the_derived_keys_never_land_back_in_the_metadata():
    """TE-1 refuses reset_at and retry_after as metadata; a parser that passed them through would raise."""
    reset_at = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    received = from_rpc_error(_reply(status=_status(Reason.RATE_BUDGET.value, metadata={'reset_at': reset_at})))
    assert 'reset_at' not in received.metadata
    assert 'retry_after' not in received.metadata
    assert received.reset_at is not None, 'lifted into the attribute, not dropped'


def test_a_retry_after_a_peer_wrote_into_the_metadata_is_dropped_rather_than_honoured():
    """Architect's ruling of 15:11, item 1: RetryInfo is the carrier here; a metadata copy is not read."""
    received = from_rpc_error(_reply(status=_status(Reason.VENDOR_UNAVAILABLE.value, metadata={'retry_after': '3600'})))
    assert received.reason is Reason.VENDOR_UNAVAILABLE, 'dropped, not a protocol error'
    assert 'retry_after' not in received.metadata
    assert received.retry_after is None, 'no RetryInfo and no reset_at means no delay was named'


def test_a_sequence_valued_metadata_item_arrives_as_one_string():
    """The one-way encoding, seen from the receiving side: a comma-joined value is never split back."""
    received = from_rpc_error(_reply(status=_status(Reason.RANGE_COLLISION.value, metadata={'colliding_ids': 'a,b'})))
    assert received.metadata['colliding_ids'] == 'a,b'


# -----------------------------------------------------------------------------------------------------
# THE TOTALITY OF THE PARSER


@pytest.mark.parametrize('code', list(grpc.StatusCode), ids=lambda code: code.name)
def test_from_rpc_error_answers_for_every_status_code_grpc_defines_and_raises_nothing(code: grpc.StatusCode):
    """Including OK and CANCELLED, and including codes no row in REASONS renders as.

    'This function raises nothing: a decoding failure is a reason, not an exception.' A parser that raised
    would turn a confusing reply into a bug in the caller, which is the Kafka layer's failure again.
    """
    received = from_rpc_error(_reply(code))
    assert isinstance(received, ExogenousError)
    assert received.detail, 'a human always gets something to read'


@pytest.mark.parametrize('code', list(grpc.StatusCode), ids=lambda code: code.name)
def test_from_rpc_error_answers_for_every_code_even_with_a_corrupt_trailer(code: grpc.StatusCode):
    received = from_rpc_error(_reply(code, raw_trailer=b'\x08\xff\xff\xff'))
    assert isinstance(received, ExogenousError)
