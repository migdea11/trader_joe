"""The handlers beside render(): request validation, a passed-through HTTPException, and a bug (TE-3 tj-3mk3u5.37.4).

ADR tj-fa1rpu D1(c), D5 and D8, as the bead's install_error_handlers items (b) to (d) state them:
    (b) RequestValidationError  422 INVALID_REQUEST, with errors listing loc, msg and type only: never the
                                input or ctx, because a request body is never echoed (D8).
    (c) HTTPException           about:blank, keeping its own status, detail string and headers: routing's 404
                                and 405 and require_instance_secret's 401, unconverted.
    (d) Exception               a bug (D5): 500 carrying ONLY an error_id. The traceback goes to the log under
                                the same id, never to the wire (D8).
D8 also names what must never reach the wire whatever escapes: str() of a vendor error, which is a raw vendor
body, or of a SQLAlchemyError, which carries SQL and bound parameters (TE-1 architect ruling (d)).

Every value a test must not see echoed carries SENTINEL, so a leak is one substring check on the raw text.
"""

import http
import logging
import uuid
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Query
from fastapi import HTTPException as FastAPIHTTPException
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, ExogenousError, Reason
from routers.common.instance_secret import (
    INSTANCE_SECRET_ENV_VAR,
    INSTANCE_SECRET_HEADER,
    INSTANCE_SECRET_REJECTION_DETAIL,
    require_instance_secret,
)
from routers.tests.problem_app import ABOUT_BLANK, ERRORS_LOGGER, PROBLEM_MEDIA_TYPE, answer_to, client_for, problem_app


pytestmark = pytest.mark.common

SENTINEL = 'SENTINEL-7f3a'

# What a bug's body may hold, and nothing more: the bead's 'ONLY an error_id'.
BUG_MEMBERS = {'type', 'title', 'status', 'error_id'}
# What a passed-through HTTPException's body holds: no reason and no domain, because it is not one of ours.
HTTP_EXCEPTION_MEMBERS = {'type', 'title', 'status', 'detail'}
VALIDATION_ISSUE_MEMBERS = {'loc', 'msg', 'type'}


class Thing(BaseModel):
    count: int
    name: str
    when: AwareDatetime


class ThingOut(BaseModel):
    count: int


class VendorAPIError(Exception):
    """Stands in for a vendor SDK's error, whose str() is the vendor's raw response body (alpaca-py's APIError)."""


def validation_app() -> FastAPI:
    app = problem_app()

    @app.post('/things')
    async def create_thing(thing: Thing, limit: Annotated[int, Query()] = 10) -> None:
        return None

    return app


def _problem(response) -> dict:
    assert response.headers['content-type'] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body['type'] == ABOUT_BLANK
    assert body['status'] == response.status_code
    assert body['title'] == http.HTTPStatus(response.status_code).phrase
    return body


# --- (b) request validation -------------------------------------------------------------------------------------


def test_a_validation_failure_is_422_invalid_request_listing_each_problem():
    """Every field that failed is listed by loc, and the body is the INVALID_REQUEST problem."""
    response = client_for(validation_app()).post(
        '/things', params={'limit': SENTINEL}, json={'count': SENTINEL, 'name': 7}
    )
    assert response.status_code == REASONS[Reason.INVALID_REQUEST].http_status
    body = _problem(response)
    assert body['reason'] == Reason.INVALID_REQUEST.value
    assert body['domain'] == ERROR_DOMAIN
    assert isinstance(body['detail'], str)
    assert body['detail']
    locs = {tuple(issue['loc']) for issue in body['errors']}
    assert {('body', 'count'), ('body', 'name'), ('body', 'when'), ('query', 'limit')} <= locs


@pytest.mark.parametrize(
    'send',
    [
        pytest.param({'params': {'limit': SENTINEL}, 'json': {'count': SENTINEL, 'name': SENTINEL}}, id='wrong-types'),
        pytest.param(
            {'json': {'count': 1, 'name': 'n', 'when': f'2030-01-01T00:00:00 {SENTINEL}'}}, id='unparseable-datetime'
        ),
        pytest.param({'json': {'count': 1, 'name': 'n', 'when': '2030-01-01T00:00:00'}}, id='naive-datetime'),
        pytest.param(
            {'content': f'{{"count": "{SENTINEL}", "name": '.encode(), 'headers': {'content-type': 'application/json'}},
            id='malformed-json',
        ),
    ],
)
def test_a_validation_failure_never_echoes_the_input_or_its_context(send: dict):
    """D8: each issue is exactly loc, msg and type. No input, no ctx, and nothing the caller sent comes back."""
    response = client_for(validation_app()).post('/things', **send)
    assert response.status_code == REASONS[Reason.INVALID_REQUEST].http_status
    body = _problem(response)
    assert body['errors']
    for issue in body['errors']:
        assert set(issue) == VALIDATION_ISSUE_MEMBERS
    assert SENTINEL not in response.text
    assert '2030-01-01T00:00:00' not in response.text


# --- (c) HTTPException, passed through ---------------------------------------------------------------------------


def test_a_route_that_does_not_exist_is_a_404_problem_with_no_reason():
    """Routing's own 404 keeps its status and Starlette's detail, under about:blank, with no reason or domain."""
    response = client_for(problem_app()).get('/nowhere')
    assert response.status_code == 404
    body = _problem(response)
    assert set(body) == HTTP_EXCEPTION_MEMBERS
    assert body['detail'] == http.HTTPStatus(404).phrase


def test_a_wrong_method_is_a_405_problem_that_keeps_the_allow_header():
    """Routing's 405 keeps the Allow header Starlette sets, which is what tells a client the methods that work."""
    app = problem_app()

    @app.get('/only-get')
    async def only_get() -> None:
        return None

    response = client_for(app).post('/only-get')
    assert response.status_code == 405
    assert {method.strip() for method in response.headers['allow'].split(',')} == {'GET'}
    assert set(_problem(response)) == HTTP_EXCEPTION_MEMBERS


@pytest.mark.parametrize('configured', [True, False], ids=['secret-set', 'secret-unset'])
def test_the_write_secret_401_passes_through_unconverted(configured: bool, monkeypatch: pytest.MonkeyPatch):
    """require_instance_secret's fixed 401 keeps its one detail; no secret, sent or configured, reaches the body."""
    if configured:
        monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, f'configured-{SENTINEL}')
    else:
        monkeypatch.delenv(INSTANCE_SECRET_ENV_VAR, raising=False)
    app = problem_app()

    @app.post('/write', dependencies=[Depends(require_instance_secret)])
    async def write() -> None:
        return None

    response = client_for(app).post('/write', headers={INSTANCE_SECRET_HEADER: f'guess-{SENTINEL}'})
    assert response.status_code == 401
    body = _problem(response)
    assert body == {
        'type': ABOUT_BLANK,
        'title': http.HTTPStatus(401).phrase,
        'status': 401,
        'detail': INSTANCE_SECRET_REJECTION_DETAIL,
    }
    assert SENTINEL not in response.text


@pytest.mark.parametrize(
    'exception_class', [StarletteHTTPException, FastAPIHTTPException], ids=['starlette', 'fastapi']
)
def test_an_http_exception_keeps_its_status_detail_and_headers(exception_class: type[StarletteHTTPException]):
    """The bead: about:blank, keeping the exception's own status, detail string and headers."""
    error = exception_class(status_code=401, detail='Present a bearer token.', headers={'WWW-Authenticate': 'Bearer'})
    response = answer_to(error)
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == 'Bearer'
    body = _problem(response)
    assert set(body) == HTTP_EXCEPTION_MEMBERS
    assert body['detail'] == 'Present a bearer token.'


def test_a_structured_http_exception_detail_never_reaches_the_wire():
    """A detail that is not a str could hold anything, so it is dropped rather than rendered (builder decision 7)."""
    response = answer_to(FastAPIHTTPException(status_code=409, detail={'colliding_ids': ['x'], 'note': SENTINEL}))
    assert response.status_code == 409
    assert set(_problem(response)) == {'type', 'title', 'status'}
    assert SENTINEL not in response.text


@pytest.mark.parametrize('status', [204, 205, 304])
def test_a_status_that_cannot_carry_a_body_is_answered_with_none(status: int):
    """No problem body on a status HTTP forbids a body for, as Starlette's own handler does (builder decision 7)."""
    response = answer_to(StarletteHTTPException(status_code=status))
    assert response.status_code == status
    assert response.content == b''
    assert response.headers.get('content-type') != PROBLEM_MEDIA_TYPE


# --- (d) a bug ----------------------------------------------------------------------------------------------------


def test_a_bug_answers_500_with_only_an_error_id():
    """D5: anything not a TraderJoeError is a bug. The caller gets an id and nothing else: no type name, no text."""
    response = answer_to(RuntimeError(f'postgres://admin:{SENTINEL}@db:5432/trader'))
    assert response.status_code == 500
    body = _problem(response)
    assert set(body) == BUG_MEMBERS
    uuid.UUID(body['error_id'])
    for leak in (SENTINEL, 'RuntimeError', 'Traceback', 'postgres://'):
        assert leak not in response.text


def test_the_bug_is_logged_with_its_traceback_under_the_same_error_id(caplog: pytest.LogCaptureFixture):
    """D8: the cause goes to the log, correlated with the wire by the one id both carry."""
    error = RuntimeError(f'a bug {SENTINEL}')
    with caplog.at_level(logging.DEBUG, logger=ERRORS_LOGGER):
        response = answer_to(error)
    error_id = response.json()['error_id']
    records = [record for record in caplog.records if record.name == ERRORS_LOGGER]
    assert len(records) == 1
    (record,) = records
    assert record.levelno == logging.ERROR
    assert error_id in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is error


def test_each_bug_gets_its_own_error_id():
    """An id that two failures share correlates nothing."""
    first = answer_to(RuntimeError('one')).json()['error_id']
    second = answer_to(RuntimeError('two')).json()['error_id']
    assert first != second


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(
            IntegrityError(
                'INSERT INTO store_dataset_entry (owner, note) VALUES (%(owner)s, %(note)s)',
                {'owner': f'account-{SENTINEL}', 'note': 'x'},
                Exception(f'connection to postgresql://admin:{SENTINEL}@db:5432/trader refused'),
            ),
            id='sqlalchemy-error',
        ),
        pytest.param(
            VendorAPIError(f'{{"code": 40310000, "message": "forbidden for key PK{SENTINEL}"}}'), id='vendor-error'
        ),
    ],
)
def test_a_vendor_or_database_error_that_escapes_never_reaches_the_wire(error: Exception):
    """D8: never str() of a vendor error or a SQLAlchemyError on the wire. Escaping, either one is a bug: 500, an id."""
    assert SENTINEL in str(error)
    response = answer_to(error)
    assert response.status_code == 500
    assert set(_problem(response)) == BUG_MEMBERS
    assert SENTINEL not in response.text
    assert 'INSERT INTO' not in response.text


def test_a_typed_error_raised_from_a_vendor_error_renders_its_own_detail_only():
    """D8: the cause chain is for the log. The wire carries the raiser's detail, never the vendor body under it."""
    try:
        raise VendorAPIError(f'{{"message": "upstream body {SENTINEL}"}}')
    except VendorAPIError as vendor:
        try:
            raise ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The vendor did not answer.') from vendor
        except ExogenousError as typed:
            error = typed
    response = answer_to(error)
    assert response.status_code == REASONS[Reason.VENDOR_UNAVAILABLE].http_status
    assert _problem(response)['detail'] == 'The vendor did not answer.'
    assert SENTINEL not in response.text


def test_a_response_our_own_route_got_wrong_is_a_bug_and_is_not_echoed():
    """FastAPI's ResponseValidationError carries the route's own output; it is a bug, so 500 and only an id."""
    app = problem_app()

    @app.get('/broken', response_model=ThingOut)
    async def broken() -> dict:
        return {'count': SENTINEL}

    response = client_for(app).get('/broken')
    assert response.status_code == 500
    assert set(_problem(response)) == BUG_MEMBERS
    assert SENTINEL not in response.text
