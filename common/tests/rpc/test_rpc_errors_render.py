"""render(): a TraderJoeError as the google.rpc.Status its REASONS row names (ADR tj-fa1rpu Tier 4, D4, D8).

EVERY CASE HERE IS DRIVEN OFF THE REASONS TABLE. Not one reason is named in a list: RENDERABLE and
CODELESS are computed from REASONS in errors_harness.py, so a row added to the table joins this gate
without anyone remembering to add it. A hand-written list of reasons would pass forever while the table
grew past it -- the hole the architect found in tj-3mk3u5.29's _NAIVE_CASES.

render() IS PURE. It logs nothing and sends nothing, exactly like TE-3's render() at the HTTP edge; the
log line and the abort belong to abort_with_error(). That is pinned here too, because a renderer that
quietly logged would double every line the edge already writes.

WHAT THE RECORDS FIX, AND WHAT THIS COMMIT FIXED ITSELF. The code, the (reason, domain) pair, the
RetryInfo and the ever-present error_id are ADR tj-fa1rpu (D4, D8 and its 2026-09-30 and 16:22 UTC
2026-10-02 addenda) and tj-8konfu D6.4. Three things the records leave open, which this module decided
and which are therefore pinned HERE so a future change to them is a deliberate one, not a drift:

    retry_after is NOT written into ErrorInfo.metadata.  RetryInfo.retry_delay is its gRPC-native
                                                         carrier, and a second copy can only go stale.
    a sequence metadata value is comma-joined, one way.  An ErrorInfo metadata value is a string; a comma
                                                         inside an item is indistinguishable from the
                                                         separator, so nothing ever splits it back.
    reset_at is written with datetime.isoformat().       Which is RFC 3339 for an aware UTC instant.
"""

import logging
from datetime import UTC, datetime, timedelta

import grpc
import pytest
from google.rpc import error_details_pb2, status_pb2

from common.errors.vocabulary import ERROR_DOMAIN, METADATA_KEYS, REASONS, Reason, new_error_id
from common.rpc.errors import render

from .errors_harness import CODELESS, RENDERABLE, RESET_AFTER_S, error_for


pytestmark = pytest.mark.common


def _detail(status: status_pb2.Status, prototype):
    for packed in status.details:
        if packed.Is(prototype.DESCRIPTOR):
            packed.Unpack(prototype)
            return prototype
    return None


def _info(status: status_pb2.Status) -> error_details_pb2.ErrorInfo:
    found = _detail(status, error_details_pb2.ErrorInfo())
    assert found is not None, 'every typed status carries exactly one ErrorInfo'
    return found


# -----------------------------------------------------------------------------------------------------
# THE CODE AND THE (reason, domain) PAIR, FOR EVERY ROW THE TABLE SAYS RENDERS


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_the_code_is_the_one_its_row_names_and_the_message_is_the_detail(reason: Reason):
    status = render(error_for(reason, 'a human sentence'))
    assert status.code == grpc.StatusCode[REASONS[reason].grpc_code].value[0]
    assert status.message == 'a human sentence'


@pytest.mark.parametrize('reason', RENDERABLE)
def test_the_error_info_carries_the_reason_and_the_domain_as_aip_193_requires(reason: Reason):
    """D6.4: every ErrorInfo carries a domain as well as a reason; AIP-193 identifies an error by the pair."""
    info = _info(render(error_for(reason)))
    assert info.reason == reason.value
    assert info.domain == ERROR_DOMAIN


@pytest.mark.parametrize('reason', RENDERABLE)
def test_exactly_one_error_info_is_packed(reason: Reason):
    """A second one would make the pair ambiguous, and from_rpc_error reads only the first."""
    status = render(error_for(reason))
    packed = [d for d in status.details if d.Is(error_details_pb2.ErrorInfo.DESCRIPTOR)]
    assert len(packed) == 1


@pytest.mark.parametrize('reason', RENDERABLE)
def test_no_reason_renders_as_unavailable(reason: Reason):
    """ADR tj-8konfu D6.4, absolutely: UNAVAILABLE is the transport saying the peer is down, nothing else.

    A server that sends it deliberately makes PEER_UNAVAILABLE a lie on the other side of the hop, and
    every retry policy anyone writes retries a request that may never succeed.
    """
    assert render(error_for(reason)).code != grpc.StatusCode.UNAVAILABLE.value[0]


def test_every_reason_in_the_table_is_in_exactly_one_of_the_two_groups():
    """The tripwire on the sweep itself: if RENDERABLE and CODELESS stop covering Reason, this is the red.

    Without it, a bug in how the two groups are derived could silently empty one of them and every
    parametrised test above would pass by running over nothing.
    """
    assert set(RENDERABLE) | set(CODELESS) == set(Reason)
    assert not set(RENDERABLE) & set(CODELESS)
    assert len(RENDERABLE) == 17 and len(CODELESS) == 7, 'the table changed; check that the new row is gated here'


# -----------------------------------------------------------------------------------------------------
# A ROW WITH NO CODE IS REFUSED (bead item 1; FEED_NOT_AVAILABLE travels in the ack, tj-3mk3u5.22 Q5)


@pytest.mark.parametrize('reason', CODELESS)
def test_a_reason_whose_row_has_no_grpc_code_is_refused(reason: Reason):
    """Asking for the status of a reason that has none is a programming error, and render() says so."""
    with pytest.raises(ValueError, match='never rendered as a gRPC status'):
        render(error_for(reason))


def test_the_refusal_names_the_reason_so_the_programmer_knows_which_call_site_is_wrong():
    with pytest.raises(ValueError, match='FEED_NOT_AVAILABLE'):
        render(error_for(Reason.FEED_NOT_AVAILABLE))


# -----------------------------------------------------------------------------------------------------
# THE METADATA: ALLOWLISTED KEYS, STRING VALUES, AND reset_at FROM THE ATTRIBUTE


def test_every_allowlisted_metadata_key_the_error_carries_reaches_the_wire_as_a_string():
    """Driven off METADATA_KEYS minus the two the constructor derives, so a new allowlisted key is covered."""
    passable = sorted(METADATA_KEYS - {'reset_at', 'retry_after'})
    assert passable, 'METADATA_KEYS lost every passable key; the sweep below would assert nothing'
    error = error_for(Reason.NOT_FOUND, metadata={key: f'value-of-{key}' for key in passable})
    info = _info(render(error))
    for key in passable:
        assert info.metadata[key] == f'value-of-{key}', f'{key} did not survive the render'


def test_reset_at_is_written_from_the_attribute_in_rfc_3339():
    """The bead's item 1: the metadata includes reset_at, in RFC 3339. TE-1 refuses it as an argument."""
    reset_at = datetime(2026, 10, 3, 14, 30, 15, tzinfo=UTC)
    info = _info(render(error_for(Reason.RATE_BUDGET, reset_at=reset_at)))
    assert info.metadata['reset_at'] == '2026-10-03T14:30:15+00:00'
    assert datetime.fromisoformat(info.metadata['reset_at']) == reset_at


@pytest.mark.parametrize('reason', RENDERABLE)
def test_reset_at_appears_in_the_metadata_exactly_when_the_error_has_one(reason: Reason):
    error = error_for(reason, with_reset_at=True)
    info = _info(render(error))
    assert ('reset_at' in info.metadata) == (error.reset_at is not None)


# -----------------------------------------------------------------------------------------------------
# retry_after IS NOT A METADATA MEMBER (this module's choice; the records are silent)
#
# D8 allowlists retry_after as a metadata key and D4 says the gRPC rendering of a delay is
# RetryInfo.retry_delay. Neither record says whether BOTH may be written. This module writes only
# RetryInfo, because the two are derived from one reset_at and the metadata copy is a fixed number that
# goes stale while the reply is in flight, while RetryInfo is read relative to arrival. The tests below
# are what makes writing the second copy a red rather than an unnoticed addition.


@pytest.mark.parametrize('reason', RENDERABLE)
def test_retry_after_is_never_written_into_the_error_info_metadata(reason: Reason):
    assert 'retry_after' not in _info(render(error_for(reason, with_reset_at=True))).metadata


@pytest.mark.parametrize('reason', RENDERABLE)
def test_a_retry_info_detail_appears_exactly_when_the_error_knows_when_it_clears(reason: Reason):
    error = error_for(reason, with_reset_at=True)
    status = render(error)
    retry_info = _detail(status, error_details_pb2.RetryInfo())
    assert (retry_info is not None) == (error.retry_after is not None)


def test_the_retry_delay_is_the_derived_retry_after_in_whole_seconds():
    """D4: the delay is rendered as RetryInfo.retry_delay, and it is derived, never a stored copy."""
    error = error_for(Reason.RATE_BUDGET)
    status = render(error)
    retry_info = _detail(status, error_details_pb2.RetryInfo())
    assert retry_info is not None
    seconds = retry_info.retry_delay.ToTimedelta().total_seconds()
    assert seconds == pytest.approx(RESET_AFTER_S, abs=1)
    assert seconds == error.retry_after or seconds == error.retry_after + 1


def test_a_delay_already_past_renders_as_zero_rather_than_a_negative_one():
    """An error that crossed its own window is 'try now', never a negative delay a client might subtract."""
    error = error_for(Reason.RATE_BUDGET, reset_at=datetime.now(UTC) - timedelta(seconds=90))
    retry_info = _detail(render(error), error_details_pb2.RetryInfo())
    assert retry_info is not None
    assert retry_info.retry_delay.ToTimedelta() == timedelta(0)


# -----------------------------------------------------------------------------------------------------
# THE error_id IS ALWAYS THERE (ADR tj-fa1rpu addendum, 16:22 UTC 2026-10-02, rules 1 and 2)


@pytest.mark.parametrize('reason', RENDERABLE)
def test_every_typed_status_carries_an_error_id_even_when_nobody_passed_one(reason: Reason):
    """Rule 1: every status that carries a reason also carries an error_id. There is no path without one."""
    assert _info(render(error_for(reason))).metadata['error_id']


@pytest.mark.parametrize('reason', RENDERABLE)
def test_the_errors_own_id_wins_over_the_argument(reason: Reason):
    """Rule 2: the id is the error's own when it has one -- set by a raise site that logged its chain."""
    own = new_error_id()
    info = _info(render(error_for(reason, metadata={'error_id': own}), error_id='minted-by-the-edge'))
    assert info.metadata['error_id'] == own


def test_the_argument_is_used_when_the_error_carries_none():
    info = _info(render(error_for(Reason.NOT_FOUND), error_id='settled-by-the-edge'))
    assert info.metadata['error_id'] == 'settled-by-the-edge'


@pytest.mark.parametrize('empty', ['', (), ('', '')], ids=['blank', 'no items', 'blank items'])
def test_an_id_that_names_nothing_is_no_id_and_one_is_minted_instead(empty):
    """TE-1 accepts all three, and all three identify no log line, so the edge treats them as absent.

    Read exactly as routers/common/errors.py reads it, so an error relayed from one transport to the
    other is judged the same way on both.
    """
    info = _info(render(error_for(Reason.NOT_FOUND, metadata={'error_id': empty}), error_id='the-real-one'))
    assert info.metadata['error_id'] == 'the-real-one'


def test_two_renders_of_an_error_with_no_id_mint_two_different_ids():
    first = _info(render(error_for(Reason.NOT_FOUND))).metadata['error_id']
    second = _info(render(error_for(Reason.NOT_FOUND))).metadata['error_id']
    assert first != second


# -----------------------------------------------------------------------------------------------------
# A SEQUENCE METADATA VALUE: ONE STATED ENCODING, ONE WAY (this module's choice; the records are silent)


def test_a_sequence_metadata_value_is_written_as_its_items_joined_by_a_comma():
    """colliding_ids is the only sequence key today, and it does not cross this hop in PR 2.

    The encoding is stated now rather than chosen under pressure later, and pinned so that the day
    something does carry a sequence here, changing it is a deliberate change.
    """
    info = _info(render(error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': ('a1', 'b2', 'c3')})))
    assert info.metadata['colliding_ids'] == 'a1,b2,c3'


def test_a_one_item_sequence_is_indistinguishable_from_the_bare_string_which_is_why_it_is_never_resplit():
    """The reason the encoding is one-way, made concrete: the wire cannot tell these two apart."""
    sequence = _info(render(error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': ('solo',)})))
    plain = _info(render(error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': 'solo'})))
    assert sequence.metadata['colliding_ids'] == plain.metadata['colliding_ids'] == 'solo'


def test_an_item_containing_a_comma_is_written_through_unescaped_which_is_the_other_reason():
    """Splitting this back would invent a three-item tuple from a two-item one. So nothing splits it."""
    info = _info(render(error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': ('a,b', 'c')})))
    assert info.metadata['colliding_ids'] == 'a,b,c'


def test_an_empty_sequence_is_written_as_the_empty_string():
    info = _info(render(error_for(Reason.RANGE_COLLISION, metadata={'colliding_ids': ()})))
    assert info.metadata['colliding_ids'] == ''


# -----------------------------------------------------------------------------------------------------
# D8: THE CAUSE CHAIN GOES TO THE LOG, NOT TO THE WIRE
#
# The two categories D8 names that this hop could actually leak are a raw vendor response body and a DSN
# or credential -- str(an alpaca APIError) is the first and str(a SQLAlchemyError) is often both. render()
# never reads __cause__ at all, and the test below proves it on the SERIALISED bytes rather than on the
# message object, because that is what the peer receives.

_SECRETS = {
    'a vendor response body': 'APIError: {"code":40110000,"message":"account 904837e3 forbidden","key":"PKTEST"}',
    'a database url': 'postgresql+asyncpg://joe:hunter2@db.internal:5432/trader',
    'a sqlalchemy statement': '(psycopg.errors.UniqueViolation) duplicate key: INSERT INTO bars (account_id) ...',
}


@pytest.mark.parametrize('secret', _SECRETS.values(), ids=_SECRETS.keys())
def test_nothing_from_the_cause_chain_reaches_the_serialised_status(secret: str):
    """Construct the error with that cause, render it, and search the BYTES that would go on the wire."""
    error = error_for(Reason.VENDOR_UNAVAILABLE, 'the market-data vendor could not be reached')
    error.__cause__ = RuntimeError(secret)
    wire = render(error).SerializeToString()
    assert secret.encode() not in wire
    for fragment in (b'hunter2', b'904837e3', b'PKTEST', b'UniqueViolation', b'RuntimeError', b'Traceback'):
        assert fragment not in wire, f'{fragment!r} reached the wire'


def test_only_the_detail_the_raiser_wrote_becomes_the_message():
    """The message is the detail and nothing else: no reason prefix, no exception text, no str(error)."""
    error = error_for(Reason.VENDOR_AUTH, 'the market-data vendor rejected our credentials')
    error.__cause__ = RuntimeError('key=PKTEST secret=abc123')
    status = render(error)
    assert status.message == 'the market-data vendor rejected our credentials'
    assert str(error) not in status.message, 'str(error) prefixes the reason; the message is the detail alone'


@pytest.mark.parametrize('reason', RENDERABLE)
def test_no_rendered_status_carries_a_metadata_key_outside_the_allowlist(reason: Reason):
    """D8's allowlist is closed. reset_at is derived rather than passed, so it joins the permitted set."""
    error = error_for(reason, with_reset_at=True, metadata={'vendor': 'the market-data vendor'})
    assert set(_info(render(error)).metadata) <= METADATA_KEYS


# -----------------------------------------------------------------------------------------------------
# PURITY: render() LOGS NOTHING AND SENDS NOTHING


@pytest.mark.parametrize('reason', RENDERABLE)
def test_render_logs_nothing(reason: Reason, caplog: pytest.LogCaptureFixture):
    """The one log line per typed error belongs to abort_with_error. A renderer that logged would double it."""
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        render(error_for(reason, with_reset_at=True))
    assert caplog.records == []


def test_render_does_not_mutate_the_error_it_was_given():
    """The minted id goes on the wire, not onto the error: a second render must mint a second id."""
    error = error_for(Reason.NOT_FOUND)
    render(error)
    assert 'error_id' not in error.metadata
