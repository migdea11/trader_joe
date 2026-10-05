"""Every Reason, rendered as problem+json at the HTTP edge (TE-3 tj-3mk3u5.37.4; ADR tj-fa1rpu D1(c), D4, D8, U3).

The bead's gate, enumerated over Reason and never a hand list. Each reason answers the status its REASONS row
names, as application/problem+json, with type "about:blank", the HTTP status phrase as title (U3 as amended
2026-10-02, RFC 9457 s4.2.1), its reason, the domain "trader-joe" and its detail. Retry-After, with the
retry_after and reset_at members, appears exactly when the error knows reset_at. Only a NOT_READY reason can
(TE-1 architect ruling (b)), so no refusal ever carries a retry signal.

The title is DERIVED from http.HTTPStatus and never written as a literal: Python 3.13 renames 422's phrase from
"Unprocessable Entity" to "Unprocessable Content", and the design names the phrase, not a spelling of it.

Every clock is fixed in 2030, years from the real one, so a renderer that reads real time cannot pass by
accident.
"""

import http
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Disposition, Outcome, Reason, TraderJoeError
from routers.common.errors import render
from routers.tests.problem_app import (
    ABOUT_BLANK,
    ERRORS_LOGGER,
    METADATA_SHAPE_CASES,
    METADATA_SHAPES,
    PROBLEM_MEDIA_TYPE,
    answer_to,
)


pytestmark = pytest.mark.common

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
UNTIL_RESET = timedelta(seconds=4, microseconds=1)
# UNTIL_RESET in whole seconds, rounded up (TE-1).
RETRY_AFTER = 5
DETAIL = 'A human sentence about this one failure.'

REFUSED = [reason for reason in Reason if REASONS[reason].outcome is Outcome.REFUSED]

# Every (reason, reset_at known) pair TE-1 lets an error take: a REFUSED reason never carries reset_at, and a
# reason whose row requires it always does.
RESET_AT_CASES = [
    pytest.param(reason, known, id=f'{reason}-{"reset_at" if known else "no_reset_at"}')
    for reason in Reason
    for known in (False, True)
    if not (known and REASONS[reason].outcome is Outcome.REFUSED)
    and not (not known and REASONS[reason].requires_reset_at)
]

# The level each disposition logs at: the builder's decision (4), pinned so that changing it is a reviewed diff.
LOG_LEVELS = {
    Disposition.PAGE: logging.ERROR,
    Disposition.RECORD: logging.WARNING,
    Disposition.CLIENT_FIX: logging.INFO,
}


class FakeClock:
    """A clock that reads NOW until a test moves it."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def build(
    reason: Reason,
    *,
    reset_at: datetime | None = None,
    metadata: dict[str, str | Sequence[str]] | None = None,
    clock: FakeClock | None = None,
) -> TraderJoeError:
    """Build an error of the branch REASONS names for reason, on a fixed clock."""
    return REASONS[reason].branch(reason, DETAIL, metadata=metadata, reset_at=reset_at, clock=clock or FakeClock())


def typical(reason: Reason) -> TraderJoeError:
    """The error as a reason is usually raised: with reset_at exactly where its row requires one."""
    return build(reason, reset_at=NOW + UNTIL_RESET if REASONS[reason].requires_reset_at else None)


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_every_reason_answers_its_table_status_as_problem_json(reason: Reason):
    """Status from REASONS, type about:blank, the status phrase as title, and reason, domain and detail members."""
    status = REASONS[reason].http_status
    response = answer_to(typical(reason))
    body = response.json()
    assert response.status_code == status
    assert response.headers['content-type'] == PROBLEM_MEDIA_TYPE
    assert body['type'] == ABOUT_BLANK
    assert body['title'] == http.HTTPStatus(status).phrase
    assert body['status'] == status
    assert body['detail'] == DETAIL
    assert body['reason'] == reason.value
    assert body['domain'] == ERROR_DOMAIN == 'trader-joe'


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_render_is_the_answer_the_handler_sends(reason: Reason):
    """render() itself returns the same status, media type, headers and body the installed handler sends.

    The handler mints the error_id it logs under, and render() mints its own unless given one (TE-3b
    tj-3mk3u5.37.13), so render() is given the handler's id here. Every other member must match without help.
    """
    error = typical(reason)
    answered = answer_to(error)
    rendered = render(error, error_id=answered.json()['error_id'])
    assert rendered.status_code == answered.status_code
    assert rendered.media_type == PROBLEM_MEDIA_TYPE
    assert json.loads(rendered.body) == answered.json()
    assert rendered.headers.get('retry-after') == answered.headers.get('retry-after')


@pytest.mark.parametrize(('reason', 'reset_at_known'), RESET_AT_CASES)
def test_retry_after_appears_exactly_when_reset_at_is_known(reason: Reason, reset_at_known: bool):
    """Retry-After in delta-seconds, and the retry_after and reset_at members, if and only if reset_at is known."""
    reset_at = NOW + UNTIL_RESET if reset_at_known else None
    response = answer_to(build(reason, reset_at=reset_at))
    body = response.json()
    if reset_at_known:
        assert response.headers['retry-after'] == str(RETRY_AFTER)
        assert body['retry_after'] == RETRY_AFTER
        rendered_reset_at = datetime.fromisoformat(body['reset_at'])
        assert rendered_reset_at.utcoffset() is not None
        assert rendered_reset_at == reset_at
    else:
        assert 'retry-after' not in response.headers
        assert 'retry_after' not in body
        assert 'reset_at' not in body


@pytest.mark.parametrize('reason', REFUSED, ids=str)
def test_a_refusal_never_carries_a_retry_signal(reason: Reason):
    """D3/D4: REFUSED means do not retry unchanged, so no Retry-After header and no retry members, ever."""
    response = answer_to(build(reason))
    assert 'retry-after' not in response.headers
    assert not {'retry_after', 'reset_at'} & set(response.json())


def test_retry_after_is_the_wait_left_when_the_answer_is_sent():
    """The header and the member are derived when rendered, so an error read late reports what is left."""
    clock = FakeClock()
    error = build(Reason.VENDOR_RATE_LIMITED, reset_at=NOW + timedelta(seconds=10), clock=clock)
    clock.advance(timedelta(seconds=3, microseconds=500_000))
    response = answer_to(error)
    assert response.headers['retry-after'] == '7'
    assert response.json()['retry_after'] == 7


def test_the_header_and_the_member_name_the_same_wait_while_the_clock_moves():
    """Each read of retry_after is derived afresh, so both must come from ONE read: here every read is 1s later."""

    class TickingClock(FakeClock):
        def __call__(self) -> datetime:
            self.advance(timedelta(seconds=1))
            return self.now

    error = build(Reason.RATE_BUDGET, reset_at=NOW + timedelta(seconds=30), clock=TickingClock())
    response = answer_to(error)
    assert response.headers['retry-after'] == str(response.json()['retry_after'])


def test_a_reset_at_already_past_says_retry_now_never_a_negative_wait():
    """Once reset_at has passed, Retry-After is 0: delta-seconds is never negative (RFC 9110 s10.2.3)."""
    response = answer_to(build(Reason.RATE_BUDGET, reset_at=NOW - timedelta(seconds=30)))
    assert response.headers['retry-after'] == '0'
    assert response.json()['retry_after'] == 0


@pytest.mark.parametrize(('key', 'shape'), METADATA_SHAPE_CASES)
def test_every_metadata_shape_the_vocabulary_accepts_is_rendered(key: str, shape: str):
    """Every allowlisted key, in each shape TE-1 lets it hold, reaches the wire under its own name, value unchanged.

    render(error: TraderJoeError) must answer for every error TE-1 lets be built, never turn one into a 500 (the
    defect tj-3mk3u5.37.4's first gate found). D8's allowlist is what may reach the wire: a str stays that string,
    and a sequence becomes a JSON array of the same items, never one comma-joined string or a wrapped list.
    """
    carried, on_the_wire = METADATA_SHAPES[shape]
    reason = Reason.OWN_OVERLAP_CONFLICT
    response = answer_to(build(reason, metadata={key: carried}))
    assert response.status_code == REASONS[reason].http_status
    assert response.json()['reason'] == reason.value
    assert response.json()[key] == on_the_wire


def test_no_rendered_error_carries_the_validation_errors_member():
    """The errors member belongs to request validation alone; a raised error never carries it."""
    for reason in Reason:
        assert 'errors' not in answer_to(typical(reason)).json(), reason


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_rendered_error_logs_one_line_at_its_dispositions_level(reason: Reason, caplog: pytest.LogCaptureFixture):
    """One line per rendered error, at the level its disposition names (D4), with no traceback attached."""
    with caplog.at_level(logging.DEBUG, logger=ERRORS_LOGGER):
        answer_to(typical(reason))
    records = [record for record in caplog.records if record.name == ERRORS_LOGGER]
    assert len(records) == 1
    (record,) = records
    assert record.levelno == LOG_LEVELS[REASONS[reason].disposition]
    assert record.exc_info is None
    assert reason.value in record.getMessage()
    assert str(REASONS[reason].http_status) in record.getMessage()
