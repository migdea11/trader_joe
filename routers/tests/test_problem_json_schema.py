"""The declared problem+json body and its OpenAPI declaration (TE-3 tj-3mk3u5.37.4; ADR tj-fa1rpu D8, D10, U3).

ProblemDetails is what the typed SDK generates from (docs/API.md G6), so what it declares is a published contract:
    - its members are RFC 9457's type, title, status and detail, the reason and domain extensions, the errors
      member of request validation, and the METADATA_KEYS allowlist (D8) -- nothing else can reach the wire;
    - an import refuses a build whose body could not carry an allowlisted key;
    - reason and type are open strings, not an enum or a constant, so a client survives a reason, or a type URI,
      that it has never heard of (D10; the U3 addendum keeps a derived type URI reversible);
    - each metadata member admits both shapes TE-1 lets it carry, a string and an array of strings, so a client
      generated from the document accepts every body the edge sends;
    - PROBLEM_RESPONSES, passed as FastAPI(responses=...), declares every non-2xx an app can answer as
      ProblemDetails under application/problem+json, and every body the edge sends validates against it.
"""

import importlib.util
import json
from enum import StrEnum
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from common.errors import vocabulary
from common.errors.vocabulary import METADATA_KEYS, REASONS, Outcome, Reason
from routers.common import errors
from routers.common.errors import PROBLEM_RESPONSES, ProblemDetails
from routers.tests.problem_app import (
    METADATA_SHAPE_CASES,
    METADATA_SHAPES,
    PROBLEM_MEDIA_TYPE,
    answer_to,
    client_for,
    problem_app,
)


pytestmark = pytest.mark.common

PROBLEM_DETAILS_REF = {'$ref': '#/components/schemas/ProblemDetails'}

# RFC 9457's members, the two extensions every one of our errors carries, and request validation's errors member.
NAMED_MEMBERS = {'type', 'title', 'status', 'detail', 'reason', 'domain', 'errors'}

# Every status a rendered error can answer with: each reason's row, and 500 for a bug.
ERROR_STATUSES = {spec.http_status for spec in REASONS.values()} | {500}


class Thing(BaseModel):
    count: int


def documented_app() -> FastAPI:
    app = problem_app()

    @app.get('/things/{thing_id}')
    async def read_thing(thing_id: int) -> Thing:
        return Thing(count=thing_id)

    @app.post('/things')
    async def create_thing(thing: Thing) -> Thing:
        return thing

    return app


def load_a_fresh_copy() -> ModuleType:
    """Execute routers/common/errors.py again, as a module of its own, so its import-time checks run now.

    The copy is never put in sys.modules, so the routers.common.errors every other test imports is untouched.
    """
    spec = importlib.util.spec_from_file_location('routers.common.errors_fresh_copy', Path(errors.__file__))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the body ---------------------------------------------------------------------------------------------------


def test_the_body_declares_exactly_the_named_members_and_the_metadata_allowlist():
    """D8: METADATA_KEYS is what may reach the wire beyond the named members, so the body declares nothing else."""
    assert set(ProblemDetails.model_fields) == NAMED_MEMBERS | METADATA_KEYS


def test_type_defaults_to_about_blank():
    """U3 as amended: about:blank for every error, so a body that names no type is about:blank."""
    assert ProblemDetails(title='Not Found', status=404).type == 'about:blank'


def test_an_import_refuses_a_body_that_cannot_carry_an_allowlisted_key(monkeypatch: pytest.MonkeyPatch):
    """Grow METADATA_KEYS without a member to match, and importing the renderer fails, naming the key."""
    load_a_fresh_copy()
    monkeypatch.setattr(vocabulary, 'METADATA_KEYS', METADATA_KEYS | {'a_key_with_no_member'})
    with pytest.raises(ValueError, match='a_key_with_no_member'):
        load_a_fresh_copy()


def test_an_import_refuses_a_disposition_with_no_log_level(monkeypatch: pytest.MonkeyPatch):
    """Grow Disposition without a level to match, and importing the renderer fails, naming the disposition."""

    class WiderDisposition(StrEnum):
        PAGE = 'PAGE'
        RECORD = 'RECORD'
        CLIENT_FIX = 'CLIENT_FIX'
        A_NEW_DISPOSITION = 'A_NEW_DISPOSITION'

    monkeypatch.setattr(vocabulary, 'Disposition', WiderDisposition)
    with pytest.raises(ValueError, match='A_NEW_DISPOSITION'):
        load_a_fresh_copy()


def test_a_body_from_a_later_release_still_validates():
    """D10: a reason, a type and a member this build has never seen are carried, not refused."""
    body = {
        'type': 'https://example.invalid/a-type-uri-from-a-later-release',
        'title': 'Conflict',
        'status': 409,
        'detail': 'Something new.',
        'reason': 'A_REASON_FROM_A_LATER_RELEASE',
        'domain': 'trader-joe',
        'a_member_from_a_later_release': 1,
    }
    problem = ProblemDetails.model_validate(body)
    assert problem.reason == 'A_REASON_FROM_A_LATER_RELEASE'
    assert problem.type == body['type']


def _every_kind_of_answer() -> list:
    answers = []
    for reason in Reason:
        branch = REASONS[reason].branch
        # reset_at wherever TE-1 allows one, so the retry members are validated too.
        if REASONS[reason].outcome is Outcome.NOT_READY:
            error = branch.from_retry_after(reason, 'detail', 5, metadata={'vendor': 'a vendor'})
        else:
            error = branch(reason, 'detail', metadata={'colliding_ids': ['an-id'], 'error_id': 'an-error-id'})
        answers.append(pytest.param(error, id=str(reason)))
    answers.append(pytest.param(RuntimeError('a bug'), id='bug'))
    return answers


@pytest.mark.parametrize('error', _every_kind_of_answer())
def test_every_body_the_edge_sends_validates_against_the_declared_model(error: Exception):
    """The OpenAPI document is honest about the wire: each rendered body is a ProblemDetails, member for member."""
    body = answer_to(error).json()
    assert ProblemDetails.model_validate(body).model_dump(mode='json', exclude_none=True) == body


@pytest.mark.parametrize(
    'request_args',
    [
        pytest.param(('get', '/nowhere', {}), id='404'),
        pytest.param(('delete', '/things', {}), id='405'),
        pytest.param(('post', '/things', {'json': {'count': 'many'}}), id='422'),
    ],
)
def test_every_framework_body_validates_against_the_declared_model(request_args: tuple[str, str, dict]):
    """Routing's 404 and 405 and request validation's 422 send ProblemDetails too."""
    method, path, kwargs = request_args
    response: httpx.Response = getattr(client_for(documented_app()), method)(path, **kwargs)
    assert response.headers['content-type'] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert ProblemDetails.model_validate(body).model_dump(mode='json', exclude_none=True) == body


# --- the OpenAPI declaration --------------------------------------------------------------------------------------


def test_problem_responses_names_every_status_an_error_can_answer_and_a_default():
    """Each reason's status, 500 for a bug, and 'default' for an HTTPException's own status, such as 401 or 405."""
    assert ERROR_STATUSES | {'default'} <= set(PROBLEM_RESPONSES)


def test_every_operation_declares_every_error_status_as_problem_details():
    """The bead: every non-2xx is declared in OpenAPI, as ProblemDetails under application/problem+json."""
    document = documented_app().openapi()
    operations = [operation for path in document['paths'].values() for operation in path.values()]
    assert len(operations) == 2
    for operation in operations:
        for status in [*sorted(str(status) for status in ERROR_STATUSES), 'default']:
            content = operation['responses'][status]['content']
            assert content[PROBLEM_MEDIA_TYPE]['schema'] == PROBLEM_DETAILS_REF, status


def test_the_document_never_advertises_fastapis_own_validation_body():
    """The 422 a client gets is ProblemDetails, so the document must not promise FastAPI's HTTPValidationError."""
    document = documented_app().openapi()
    assert {'ProblemDetails', 'ValidationIssue'} <= set(document['components']['schemas'])
    assert 'HTTPValidationError' not in json.dumps(document)


def _admits(declared: dict, value: object) -> bool:
    """Whether a member's declared schema admits value: a string, or an array whose items are strings.

    Only the forms a metadata member is declared in are understood. Any other form admits nothing, so a declaration
    this cannot read turns the test red instead of passing unread.
    """
    for alternative in declared.get('anyOf', [declared]):
        if alternative.get('type') == 'string' and isinstance(value, str):
            return True
        if (
            alternative.get('type') == 'array'
            and alternative.get('items', {}).get('type') == 'string'
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        ):
            return True
    return False


@pytest.mark.parametrize(('key', 'shape'), METADATA_SHAPE_CASES)
def test_the_document_admits_every_metadata_shape_the_edge_sends(key: str, shape: str):
    """The SDK is generated from the document (docs/API.md G6), so each member must admit what the wire carries.

    TE-1 lets every allowlisted key hold a str or a sequence of str, and the edge sends a string or an array. A
    member declared narrower than that would make a generated client refuse a body the server sent correctly.
    The body is checked against the published document here, not against the Pydantic model, which can differ.
    """
    carried, _ = METADATA_SHAPES[shape]
    reason = Reason.OWN_OVERLAP_CONFLICT
    body = answer_to(REASONS[reason].branch(reason, 'detail', metadata={key: carried})).json()
    declared = documented_app().openapi()['components']['schemas']['ProblemDetails']['properties'][key]
    assert body['reason'] == reason.value
    assert _admits(declared, body[key]), declared


@pytest.mark.parametrize('member', ['reason', 'type'])
def test_reason_and_type_are_open_strings_in_the_document(member: str):
    """D10: an SDK generated from the document must survive a reason added after it was built, so no enum, no const."""
    schema = documented_app().openapi()['components']['schemas']['ProblemDetails']
    declared = json.dumps(schema['properties'][member])
    assert '"string"' in declared
    assert '"enum"' not in declared
    assert '"const"' not in declared
    assert schema.get('additionalProperties', True) is not False
