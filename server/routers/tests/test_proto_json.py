"""The protobuf canonical JSON edge for /ui/v1 (tj-grna9p.15; ADR tj-grna9p.4 section 4).

Pinned here, through a throwaway app built the way a /ui/v1 route adopts the helpers:
    rendering   ProtoJSONResponse and send_proto_json carry exactly json_format.MessageToJson of the message, and
                that text holds the canonical rules: lowerCamelCase names, enums by name, int64 as a string,
                Timestamp as RFC 3339 'Z'. The response is application/json; anything but a Message is a TypeError.
    parsing     ProtoBody hands the handler the parsed message, and turns every body that is not canonical JSON of
                the type -- empty, malformed, not an object, not UTF-8, the wrong type for a field, an unknown
                field -- into a 422 problem+json with reason INVALID_REQUEST and an error_id, never a 500.
    no echo     no part of a refused body reaches the answer or the log line (ADR tj-fa1rpu D8). Every refused
                body carries SENTINEL, so a leak is one substring check.
    OpenAPI     proto_route puts the message's full proto name under x-proto-message, and no field schema.

The manifest side of x-proto-message, the enumerator recording it, is pinned in test_interface_surface.py.
"""

import json
import logging
from typing import Annotated, Final

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from google.protobuf import json_format

from common.errors.vocabulary import Reason
from routers.common.proto_json import ProtoBody, ProtoJSONResponse, proto_route, send_proto_json, to_proto_json
from routers.tests.problem_app import ERRORS_LOGGER, PROBLEM_MEDIA_TYPE, client_for, problem_app
from routers.tests.proto_fixtures import BAR_COUNT, SUMMARY_FULL_NAME, Summary, summary


pytestmark = pytest.mark.common

READ_PATH: Final = '/ui/v1/fixture'
ECHO_PATH: Final = '/ui/v1/fixture/echo'
SOCKET_PATH: Final = '/ui/v1/fixture/socket'

SENTINEL: Final = 'SENTINEL-proto-body-7f3a'

# Each body ProtoBody must refuse, holding SENTINEL wherever the shape lets it. The parser's own message quotes
# the offending name or value for most of these, which is exactly what must not travel.
REFUSED_BODIES = [
    pytest.param(b'', id='empty'),
    pytest.param(f'{{"assetSymbol": "{SENTINEL}"'.encode(), id='malformed-json'),
    pytest.param(f'["{SENTINEL}"]'.encode(), id='array-not-object'),
    pytest.param(f'"{SENTINEL}"'.encode(), id='string-not-object'),
    pytest.param(b'null', id='null-not-object'),
    pytest.param(f'{{"assetSymbol": "{SENTINEL}\xff"}}'.encode('latin-1'), id='invalid-utf8'),
    pytest.param(f'{{"barCount": "{SENTINEL}"}}'.encode(), id='wrong-field-type'),
    pytest.param(f'{{"assetSymbol": "AAPL", "{SENTINEL}": 1}}'.encode(), id='unknown-field'),
    pytest.param(b' \t\r\n ', id='whitespace-only'),
    pytest.param(f'{{"assetSymbol": ["{SENTINEL}"]}}'.encode(), id='nested-array-in-a-string-field'),
]

# The two non-object bodies json_format.Parse itself accepts, as an empty message, whatever ignore_unknown_fields
# says (protobuf 6.33.6, tj-y8mc2l): only ProtoBody's own object check refuses them. Neither can carry SENTINEL,
# so the no-echo pin for them is the detail being the fixed sentence and nothing else.
PARSE_ACCEPTED_NON_OBJECTS = [pytest.param(b'[]', id='empty-array'), pytest.param(b'""', id='empty-string')]
REFUSED_BODIES += PARSE_ACCEPTED_NON_OBJECTS

# The refusal's detail, written out: it names the message type and nothing of the body.
REFUSAL_DETAIL: Final = f'The request body is not valid protobuf JSON for {SUMMARY_FULL_NAME}.'


def fixture_app() -> FastAPI:
    """Build an app with the problem+json edge and one route of each kind the helpers serve.

    Returns:
        FastAPI: The app.
    """
    app = problem_app()

    @app.get(READ_PATH, **proto_route(Summary))
    async def read_summary() -> ProtoJSONResponse:
        return ProtoJSONResponse(summary())

    @app.post(ECHO_PATH, **proto_route(Summary))
    async def echo_summary(body: Annotated[Summary, Depends(ProtoBody(Summary))]) -> ProtoJSONResponse:
        return ProtoJSONResponse(body)

    @app.websocket(SOCKET_PATH)
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await send_proto_json(websocket, summary())
        await websocket.close()

    return app


@pytest.fixture
def client() -> TestClient:
    """A client for the fixture app.

    Returns:
        TestClient: The client.
    """
    return client_for(fixture_app())


# --- rendering -------------------------------------------------------------------------------------------------


def test_to_proto_json_is_message_to_json():
    assert to_proto_json(summary()) == json_format.MessageToJson(summary())


def test_the_rendering_follows_the_canonical_json_rules():
    rendered = json.loads(to_proto_json(summary()))
    assert rendered == {
        'id': 'ds-1',
        'assetSymbol': 'AAPL',
        'assetType': 'ASSET_TYPE_STOCK',
        'barCount': str(BAR_COUNT),
        'start': '2026-01-02T14:30:00Z',
    }


def test_a_route_answers_with_message_to_json_byte_for_byte(client: TestClient):
    response = client.get(READ_PATH)
    assert response.status_code == 200
    assert response.content == json_format.MessageToJson(summary()).encode('utf-8')


def test_a_route_answers_as_application_json(client: TestClient):
    assert client.get(READ_PATH).headers['content-type'] == 'application/json'


@pytest.mark.parametrize('content', [{'assetSymbol': 'AAPL'}, 'text', None], ids=['dict', 'str', 'none'])
def test_the_response_refuses_anything_but_a_message(content: object):
    with pytest.raises(TypeError, match='protobuf Message'):
        ProtoJSONResponse(content)


def test_the_socket_sends_one_text_frame_of_message_to_json(client: TestClient):
    with client.websocket_connect(SOCKET_PATH) as websocket:
        frame = websocket.receive()
    assert frame['type'] == 'websocket.send'
    assert frame.get('text') == json_format.MessageToJson(summary())


# --- parsing ---------------------------------------------------------------------------------------------------


def test_a_valid_body_reaches_the_handler_as_the_message(client: TestClient):
    response = client.post(ECHO_PATH, content=to_proto_json(summary()))
    assert response.status_code == 200
    assert json_format.Parse(response.text, Summary()) == summary()


def test_the_unknown_field_alone_decides_the_refusal(client: TestClient):
    # The control: the same body without the unknown field is accepted, so the 422 below is that field's.
    assert client.post(ECHO_PATH, content=b'{"assetSymbol": "AAPL"}').status_code == 200
    assert client.post(ECHO_PATH, content=b'{"assetSymbol": "AAPL", "notAField": 1}').status_code == 422


@pytest.mark.parametrize('body', REFUSED_BODIES)
def test_a_refused_body_is_a_422_invalid_request_problem(client: TestClient, body: bytes):
    response = client.post(ECHO_PATH, content=body)
    assert response.status_code == 422
    assert response.headers['content-type'] == PROBLEM_MEDIA_TYPE
    problem = response.json()
    assert problem['reason'] == Reason.INVALID_REQUEST.value
    assert isinstance(problem['error_id'], str)
    assert problem['error_id']
    assert SUMMARY_FULL_NAME in problem['detail']


@pytest.mark.parametrize('body', REFUSED_BODIES)
def test_a_refused_body_carries_only_the_fixed_detail(client: TestClient, body: bytes):
    assert client.post(ECHO_PATH, content=body).json()['detail'] == REFUSAL_DETAIL


@pytest.mark.parametrize('body', PARSE_ACCEPTED_NON_OBJECTS)
def test_parse_alone_reads_these_non_objects_as_an_empty_message(body: bytes):
    # The control for the 422 above: Parse, with the flag ProtoBody passes, accepts these, so the refusal is
    # ProtoBody's own object check and not the parser's.
    assert json_format.Parse(body.decode(), Summary(), ignore_unknown_fields=False) == Summary()


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(f'["{SENTINEL}"]', id='array-not-object'),
        pytest.param(f'"{SENTINEL}"', id='string-not-object'),
        pytest.param('null', id='null-not-object'),
    ],
)
def test_parse_alone_refuses_the_other_non_objects(body: str):
    # These were refused before the object check existed, and still are by Parse itself: a non-empty array or
    # string because its items read as unknown field names, null whatever the flag. The object check refuses
    # them first now, with the same answer, so their 422 above does not depend on which of the two runs.
    with pytest.raises(json_format.ParseError):
        json_format.Parse(body, Summary(), ignore_unknown_fields=False)


def test_whitespace_around_an_object_is_accepted(client: TestClient):
    response = client.post(ECHO_PATH, content=b' \n {"assetSymbol": "AAPL"} \n')
    assert response.status_code == 200
    assert json_format.Parse(response.text, Summary()) == Summary(asset_symbol='AAPL')


def test_an_object_with_a_nested_array_the_message_declares_is_accepted(client: TestClient):
    # siblings is a repeated message field: an array is its canonical JSON, so the object check must not reach
    # inside the body.
    sent = Summary(asset_symbol='AAPL')
    sent.siblings.add(id='ds-2')
    sent.siblings.add(id='ds-3')
    response = client.post(ECHO_PATH, content=to_proto_json(sent))
    assert response.status_code == 200
    assert json_format.Parse(response.text, Summary()) == sent


@pytest.mark.parametrize('body', REFUSED_BODIES)
def test_a_refused_body_is_never_echoed(client: TestClient, caplog: pytest.LogCaptureFixture, body: bytes):
    with caplog.at_level(logging.DEBUG, logger=ERRORS_LOGGER):
        response = client.post(ECHO_PATH, content=body)
    assert response.status_code == 422
    assert SENTINEL not in response.text
    assert caplog.records, 'the edge logs every refusal it renders, so an empty capture proves nothing'
    assert SENTINEL not in caplog.text


# --- OpenAPI ---------------------------------------------------------------------------------------------------


def test_the_route_names_its_message_under_x_proto_message():
    operation = fixture_app().openapi()['paths'][READ_PATH]['get']
    assert operation['x-proto-message'] == SUMMARY_FULL_NAME


def test_the_route_carries_no_field_schema_for_its_message():
    # The .proto is the schema (ADR tj-grna9p.4 section 4): no component schema is generated for the message.
    document = fixture_app().openapi()
    success = document['paths'][READ_PATH]['get']['responses']['200']
    assert '$ref' not in json.dumps(success)
    assert 'DatasetSummary' not in json.dumps(document.get('components', {}))
