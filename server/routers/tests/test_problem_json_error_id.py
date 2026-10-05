"""Every typed problem+json answer carries an error_id (TE-3b tj-3mk3u5.37.13; ADR tj-fa1rpu D8).

D8 sends the cause chain to the log, correlated with the wire by an id carried on both. Its 16:22 UTC 2026-10-02
addendum reads that through for typed errors at the HTTP edge:
    (1) every body that carries a reason carries an error_id: each rendered TraderJoeError and the validation 422.
        A passed-through HTTPException is not ours and carries none.
    (2) the id is the error's own when it has one, set by a raise site that logged its chain under it or kept
        from a peer. Otherwise the edge mints one with new_error_id, the one id function in common/errors.
    (3) the edge's one log line names the id. It carries the cause chain (exc_info) only when the edge minted the
        id and the error has a chain: a __cause__, or a __context__ that 'raise ... from None' did not suppress.
        An error that arrived with an id had its chain logged where the id was set, so the edge does not repeat it.
render() stays free of side effects: it logs nothing, takes an id as an argument, and mints one itself when given
none, so every rendered body carries an id whoever calls it. The error's own id beats the argument.

An own id that names nothing is no id and is replaced: '', an empty sequence, or a sequence of nothing but ''
(builder decision 3, and its 387f169 reading of [''] and ['', '']). The body, the line and the chain decision ask
that one question once, so they never disagree. A list-shaped id with any non-empty part (TE-1 lets every carried
key hold a sequence of str) is the error's own: it stays a list on the wire and is named comma-joined in the line.

The validation 422 is built at the edge and never raised, so it has no chain, and the RequestValidationError behind
it can carry the request's input, which the log must never hold. Every value that must stay off the wire, or out
of a line that carries no chain, holds SENTINEL, so a leak is one substring check.
"""

import contextlib
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from common.errors.vocabulary import METADATA_SEQUENCE_SEPARATOR, REASONS, Disposition, Reason, TraderJoeError
from routers.common import errors
from routers.common.errors import render
from routers.tests.problem_app import ERRORS_LOGGER, answer_to, client_for, problem_app


pytestmark = pytest.mark.common

SENTINEL = 'SENTINEL-5c1e'

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
DETAIL = 'A human sentence about this one failure.'

# An id a raise site set, and an id an edge hands to render(). Neither is shaped like a minted id, so a body that
# carries one of them was not minted by mistake.
OWN_ID = 'raise-site-id-0001'
EDGE_ID = 'edge-id-0002'

# Any reason serves where the rule does not depend on one; this one is NOT_READY and pages.
ANY_REASON = Reason.PEER_INTERNAL

# The level each disposition logs at (TE-3 builder decision 5, accepted). A chained line keeps it.
LOG_LEVELS = {
    Disposition.PAGE: logging.ERROR,
    Disposition.RECORD: logging.WARNING,
    Disposition.CLIENT_FIX: logging.INFO,
}

# Request validation failures whose input carries SENTINEL in the body, the query and an unparseable document.
VALIDATION_SENDS = [
    pytest.param({'params': {'limit': SENTINEL}, 'json': {'count': SENTINEL, 'name': SENTINEL}}, id='wrong-types'),
    pytest.param({'json': {'count': 1, 'name': ['n', SENTINEL]}}, id='wrong-shape'),
    pytest.param(
        {'content': f'{{"count": "{SENTINEL}", "name": '.encode(), 'headers': {'content-type': 'application/json'}},
        id='malformed-json',
    ),
]


class Thing(BaseModel):
    count: int
    name: str


class VendorError(Exception):
    """Stands in for whatever a raise site caught. Its text is for the log alone."""


def build(reason: Reason, *, error_id: str | Sequence[str] | None = None) -> TraderJoeError:
    """An error as the reason is usually raised, reset_at exactly where its row requires one, on a fixed clock."""
    metadata = {} if error_id is None else {'error_id': error_id}
    reset_at = NOW + timedelta(seconds=5) if REASONS[reason].requires_reset_at else None
    return REASONS[reason].branch(reason, DETAIL, metadata=metadata, reset_at=reset_at, clock=lambda: NOW)


def a_cause() -> VendorError:
    return VendorError(f'the cause the raise site caught, {SENTINEL}')


def raised_from(error: TraderJoeError, cause: VendorError) -> TraderJoeError:
    """Raise error with 'raise error from cause' inside the except block that caught cause, and return it."""
    try:
        raise cause
    except VendorError:
        with contextlib.suppress(TraderJoeError):
            raise error from cause
    return error


def raised_while_handling(error: TraderJoeError, cause: VendorError) -> TraderJoeError:
    """Raise error with a bare 'raise error' inside the except block that caught cause: an implicit __context__."""
    try:
        raise cause
    except VendorError:
        with contextlib.suppress(TraderJoeError):
            raise error  # noqa: B904 -- the implicit chain is what this builds
    return error


def raised_from_none(error: TraderJoeError, cause: VendorError) -> TraderJoeError:
    """Raise error with 'raise error from None' inside the except block that caught cause: a suppressed context."""
    try:
        raise cause
    except VendorError:
        with contextlib.suppress(TraderJoeError):
            raise error from None
    return error


def is_minted(value: object) -> bool:
    """Whether value is an id new_error_id could have returned: the canonical text of a uuid4."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def the_one_line(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The edge's one log line for the answer just sent."""
    records = [record for record in caplog.records if record.name == ERRORS_LOGGER]
    assert len(records) == 1, [record.getMessage() for record in records]
    return records[0]


def as_written(record: logging.LogRecord) -> str:
    """The record as a handler writes it, the traceback of its exc_info included."""
    return logging.Formatter().format(record)


def answer_and_line(error: BaseException, caplog: pytest.LogCaptureFixture) -> tuple[dict, logging.LogRecord]:
    """The body a caller receives for error, and the one line the edge logged for it."""
    with caplog.at_level(logging.DEBUG, logger=ERRORS_LOGGER):
        body = answer_to(error).json()
    return body, the_one_line(caplog)


def validation_app() -> FastAPI:
    app = problem_app()

    @app.post('/things')
    async def create_thing(thing: Thing, limit: Annotated[int, Query()] = 10) -> None:
        return None

    return app


# --- (1) every typed body carries one; HTTPException carries none ---------------------------------------------------


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_every_typed_answer_carries_a_minted_error_id_named_in_its_one_line(
    reason: Reason, caplog: pytest.LogCaptureFixture
):
    """No id of its own and no chain: the edge mints one, sends it, names it in the line and logs no traceback."""
    body, record = answer_and_line(build(reason), caplog)
    assert body['reason'] == reason.value
    assert is_minted(body['error_id'])
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.exc_info is None


@pytest.mark.parametrize('send', VALIDATION_SENDS)
def test_the_validation_422_carries_an_error_id_and_its_line_holds_no_chain_and_no_input(
    send: dict, caplog: pytest.LogCaptureFixture
):
    """The 422 is typed, so it carries an id like any other, and its line holds neither a chain nor the input.

    It is built at the edge and never raised, so it has no chain to log. The RequestValidationError behind it,
    whose text can hold the request's input, never reaches the line either.
    """
    with caplog.at_level(logging.DEBUG, logger=ERRORS_LOGGER):
        response = client_for(validation_app()).post('/things', **send)
    body = response.json()
    assert response.status_code == REASONS[Reason.INVALID_REQUEST].http_status
    assert body['reason'] == Reason.INVALID_REQUEST.value
    assert is_minted(body['error_id'])
    record = the_one_line(caplog)
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.exc_info is None
    assert SENTINEL not in as_written(record)


@pytest.mark.parametrize('status', [401, 404, 409, 503])
def test_a_passed_through_http_exception_carries_no_error_id(status: int):
    """(1): an HTTPException is not ours. Its body stays {type, title, status, detail}, with no error_id."""
    body = answer_to(HTTPException(status_code=status, detail='Not one of ours.')).json()
    assert 'error_id' not in body
    assert 'reason' not in body


def test_no_two_answers_share_a_minted_error_id():
    """An id two failures share correlates nothing: every reason twice, one error answered twice, and two 422s."""
    once = build(ANY_REASON)
    ids = [answer_to(build(reason)).json()['error_id'] for reason in Reason for _ in range(2)]
    ids += [answer_to(once).json()['error_id'] for _ in range(2)]
    ids += [client_for(validation_app()).post('/things', json={}).json()['error_id'] for _ in range(2)]
    assert all(is_minted(error_id) for error_id in ids)
    assert len(set(ids)) == len(ids)


def test_every_id_the_edge_mints_comes_from_the_one_id_function(monkeypatch: pytest.MonkeyPatch):
    """Item 1: new_error_id is the only spelling an id is minted in: a typed answer, the 422, a bug's 500, render()."""
    issued: list[str] = []

    def counting_new_error_id() -> str:
        issued.append(f'issued-{len(issued)}')
        return issued[-1]

    monkeypatch.setattr(errors, 'new_error_id', counting_new_error_id)
    typed = answer_to(build(ANY_REASON)).json()['error_id']
    validation = client_for(validation_app()).post('/things', json={}).json()['error_id']
    bug = answer_to(RuntimeError('a bug')).json()['error_id']
    rendered = json.loads(render(build(ANY_REASON)).body)['error_id']
    assert {typed, validation, bug, rendered} <= set(issued)
    assert len({typed, validation, bug, rendered}) == 4


# --- (2) and (3) whose id, and when the chain is logged -------------------------------------------------------------


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_an_error_that_carries_its_own_id_is_sent_and_named_under_it_without_its_chain(
    reason: Reason, caplog: pytest.LogCaptureFixture
):
    """The raise site logged the chain under its id, so the edge sends exactly that id, names it, and logs no chain."""
    error = raised_from(build(reason, error_id=OWN_ID), a_cause())
    body, record = answer_and_line(error, caplog)
    assert body['error_id'] == OWN_ID
    assert f'error_id {OWN_ID}' in record.getMessage()
    assert record.exc_info is None
    assert SENTINEL not in as_written(record)


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_a_chained_error_with_no_id_logs_its_chain_under_the_id_its_body_carries(
    reason: Reason, caplog: pytest.LogCaptureFixture
):
    """The edge minted the id, so its line carries the chain: exc_info is the error, and the cause hangs off it."""
    cause = a_cause()
    error = raised_from(build(reason), cause)
    body, record = answer_and_line(error, caplog)
    assert is_minted(body['error_id'])
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.levelno == LOG_LEVELS[REASONS[reason].disposition]
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert record.exc_info[1].__cause__ is cause
    assert SENTINEL in as_written(record)
    # The chain is for the log. The wire still carries only the raiser's detail.
    assert SENTINEL not in json.dumps(body)


def test_an_error_raised_while_handling_another_logs_that_one_as_its_chain(caplog: pytest.LogCaptureFixture):
    """An unsuppressed __context__ is a chain too: a bare raise in an except block keeps what it was handling."""
    cause = a_cause()
    error = raised_while_handling(build(ANY_REASON), cause)
    assert error.__cause__ is None
    assert error.__context__ is cause
    body, record = answer_and_line(error, caplog)
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert SENTINEL in as_written(record)


def test_raise_from_none_logs_no_chain(caplog: pytest.LogCaptureFixture):
    """'raise ... from None' says the context is not a cause, so the edge mints and names an id but logs no chain."""
    error = raised_from_none(build(ANY_REASON), a_cause())
    assert error.__context__ is not None
    assert error.__suppress_context__
    body, record = answer_and_line(error, caplog)
    assert is_minted(body['error_id'])
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.exc_info is None
    assert SENTINEL not in as_written(record)


@pytest.mark.parametrize(
    'empty',
    [
        pytest.param('', id='empty-str'),
        pytest.param([], id='empty-sequence'),
        pytest.param([''], id='sequence-of-one-empty-str'),
        pytest.param(['', ''], id='sequence-of-empty-strs'),
    ],
)
def test_an_empty_own_id_is_no_id(empty: str | list[str], caplog: pytest.LogCaptureFixture):
    """An id that names nothing is no id, so the edge mints one, and as the minter it also logs the chain."""
    error = raised_from(build(ANY_REASON, error_id=empty), a_cause())
    body, record = answer_and_line(error, caplog)
    assert is_minted(body['error_id'])
    assert f'error_id {body["error_id"]}' in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is error


@pytest.mark.parametrize(
    'own',
    [
        pytest.param([OWN_ID, 'peer-id-0003'], id='every-part-names-one'),
        # Not every part need name something: one that does makes the list the error's own (387f169).
        pytest.param(['', OWN_ID], id='one-empty-part'),
    ],
)
def test_a_list_shaped_own_id_stays_a_list_on_the_wire_and_is_named_joined_in_the_line(
    own: list[str], caplog: pytest.LogCaptureFixture
):
    """TE-1 lets error_id be a sequence. The wire keeps the list, the line names it joined, and no chain is logged.

    THE JOIN IS NOW ',' AND NOT ', ' (validator, re-pinned at the tj-zxqn4r gate). This edge had its own private
    copy of the id reader, and that copy joined with ', ' while common/rpc/errors.py's joined with ',' -- two
    copies that had diverged on their fourth line within two days of the second being written, under a comment
    claiming they read an id identically. tj-zxqn4r's Part B ruled the comma join, because it is the WIRE-VISIBLE
    one: the gRPC hop writes every sequence-valued metadata value that way. So the same error now names one
    string in data_store's log, in ingest's log and on the wire between them, which is the correlation D8 exists
    to give. THE BODY IS UNCHANGED -- it still carries the list -- and the one-empty-part case is what shows that
    only the log line moved.

    The separator is read from the shared constant rather than written out, so this case says "joined the way
    both edges join" rather than making an independent claim about a comma; the literal has exactly one witness,
    in common/tests/errors/test_errors_error_id.py.
    """
    error = raised_from(build(ANY_REASON, error_id=own), a_cause())
    body, record = answer_and_line(error, caplog)
    assert body['error_id'] == own
    assert record.getMessage().endswith(f'; error_id {METADATA_SEQUENCE_SEPARATOR.join(own)}')
    assert record.exc_info is None


# Each shape TE-1 lets an own error_id take, None for none at all. Whatever the shape, the id the line names is the
# one the body carries, and it names something.
OWN_ID_SHAPES = [
    pytest.param(None, id='none'),
    pytest.param('', id='empty-str'),
    pytest.param([], id='empty-sequence'),
    pytest.param(OWN_ID, id='str'),
    pytest.param([OWN_ID, 'peer-id-0003'], id='sequence'),
    pytest.param([''], id='sequence-of-one-empty-str'),
    pytest.param(['', ''], id='sequence-of-empty-strs'),
    pytest.param(['', OWN_ID], id='sequence-with-one-empty-part'),
]


@pytest.mark.parametrize('own', OWN_ID_SHAPES)
def test_the_line_names_exactly_the_id_the_body_carries(own: str | list[str] | None, caplog: pytest.LogCaptureFixture):
    """D8's correlation: the id an operator is quoted from the body is the id the line ends by naming."""
    body, record = answer_and_line(build(ANY_REASON, error_id=own), caplog)
    sent = body['error_id']
    # ',' since tj-zxqn4r, and read from the shared constant: see the case above for the ruling.
    named = sent if isinstance(sent, str) else METADATA_SEQUENCE_SEPARATOR.join(sent)
    assert named, f'the body carries {sent!r}, which names nothing'
    assert record.getMessage().endswith(f'; error_id {named}')


# --- render() ----------------------------------------------------------------------------------------------------

# Stands for 'an id render() minted', where no literal can be expected.
MINTED = object()

RENDER_CASES = [
    pytest.param(None, None, MINTED, id='neither-so-minted'),
    pytest.param(None, EDGE_ID, EDGE_ID, id='the-argument'),
    pytest.param(None, '', MINTED, id='an-empty-argument-so-minted'),
    pytest.param(OWN_ID, None, OWN_ID, id='its-own'),
    pytest.param(OWN_ID, EDGE_ID, OWN_ID, id='its-own-beats-the-argument'),
    pytest.param('', EDGE_ID, EDGE_ID, id='an-empty-own-yields-to-the-argument'),
    pytest.param([], EDGE_ID, EDGE_ID, id='an-empty-sequence-yields-to-the-argument'),
    pytest.param('', None, MINTED, id='an-empty-own-and-no-argument-so-minted'),
    pytest.param([''], EDGE_ID, EDGE_ID, id='a-sequence-of-one-empty-str-yields-to-the-argument'),
    pytest.param(['', ''], EDGE_ID, EDGE_ID, id='a-sequence-of-empty-strs-yields-to-the-argument'),
    pytest.param(['', ''], None, MINTED, id='a-sequence-of-empty-strs-and-no-argument-so-minted'),
    pytest.param([OWN_ID, 'peer-id-0003'], EDGE_ID, [OWN_ID, 'peer-id-0003'], id='a-list-own-stays-a-list'),
    pytest.param(['', OWN_ID], EDGE_ID, ['', OWN_ID], id='a-list-own-with-an-empty-part-stays-a-list'),
]


@pytest.mark.parametrize(('own', 'argument', 'expected'), RENDER_CASES)
def test_render_carries_the_errors_own_id_else_the_argument_else_a_minted_one_and_logs_nothing(
    own: str | list[str] | None, argument: str | None, expected: object, caplog: pytest.LogCaptureFixture
):
    """render() alone: the precedence of the three sources, and no log line, even for a chained error."""
    error = raised_from(build(ANY_REASON, error_id=own), a_cause())
    with caplog.at_level(logging.DEBUG):
        response = render(error, error_id=argument)
    sent = json.loads(response.body)['error_id']
    if expected is MINTED:
        assert is_minted(sent)
    else:
        assert sent == expected
    assert caplog.records == []


def test_render_mints_a_fresh_id_on_every_call():
    """Two renders of one error with no id each mint their own: render() keeps no id between calls."""
    error = build(ANY_REASON)
    first = json.loads(render(error).body)['error_id']
    second = json.loads(render(error).body)['error_id']
    assert is_minted(first)
    assert is_minted(second)
    assert first != second
