"""auth.py: the bearer-token gate in front of the whole MCP app, and the token file.

tj-c4mosr.5 body (missing token, wrong token, token in the wrong header -> refused, the app never
reached, the token never in a log line or response) and item (10) (exactly one Authorization header
equal to 'Bearer <token>'; two headers, a lowercase scheme, X-Token and ?token= refused; a non-http
scope closed; MIN_TOKEN_LENGTH enforced by the middleware and by the reader; the token file created
0600 with O_EXCL, refused when group/other bits are set, when a symlink, when short).

The DNS-rebinding Host check (421) lives in server.py, which imports the MCP SDK (the agent-mcp uv
group the dev venv deliberately omits); it is proven at the host sitting, tj-c4mosr.6 H1.
"""

import asyncio
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp.auth import MIN_TOKEN_LENGTH, BearerTokenMiddleware, TokenFileError, load_or_create_token


pytestmark = pytest.mark.build_infra

# A sentinel token: if it appears in a response, a log line or a message, the test sees it.
TOKEN = 'SENTINEL_TOKEN_' + 'x' * 40


class _App:
    """The app behind the gate: records whether any request reached it."""

    def __init__(self) -> None:
        self.reached: list[dict[str, Any]] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.reached.append(scope)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})


def _request(headers: list[tuple[bytes, bytes]], *, scope_type: str = 'http', query: bytes = b'') -> tuple[_App, list]:
    app = _App()
    gate = BearerTokenMiddleware(app, TOKEN)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict:
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {'type': scope_type, 'path': '/mcp', 'headers': headers, 'query_string': query}
    asyncio.run(gate(scope, receive, send))
    return app, sent


def _status(sent: list[dict]) -> int | None:
    return next((message['status'] for message in sent if message['type'] == 'http.response.start'), None)


def test_the_right_token_reaches_the_app():
    app, sent = _request([(b'authorization', f'Bearer {TOKEN}'.encode())])
    assert len(app.reached) == 1 and _status(sent) == 200


_REFUSED = {
    'no header': ([], b''),
    'wrong token': ([(b'authorization', b'Bearer ' + b'y' * 55)], b''),
    'token with a suffix': ([(b'authorization', f'Bearer {TOKEN}x'.encode())], b''),
    'lowercase scheme': ([(b'authorization', f'bearer {TOKEN}'.encode())], b''),
    'no scheme': ([(b'authorization', TOKEN.encode())], b''),
    'two spaces': ([(b'authorization', f'Bearer  {TOKEN}'.encode())], b''),
    'two authorization headers, both right': (
        [(b'authorization', f'Bearer {TOKEN}'.encode()), (b'authorization', f'Bearer {TOKEN}'.encode())],
        b'',
    ),
    'X-Token header': ([(b'x-token', TOKEN.encode())], b''),
    'Bearer in another header': ([(b'x-authorization', f'Bearer {TOKEN}'.encode())], b''),
    'query parameter': ([], f'token={TOKEN}'.encode()),
    'query Authorization': ([], f'authorization=Bearer%20{TOKEN}'.encode()),
}


@pytest.mark.parametrize(('headers', 'query'), list(_REFUSED.values()), ids=list(_REFUSED))
def test_a_request_without_exactly_the_right_header_is_refused_before_the_app(headers: list, query: bytes):
    app, sent = _request(headers, query=query)
    assert app.reached == [], 'a refused request reached the app behind the gate'
    assert _status(sent) == 401
    start = next(message for message in sent if message['type'] == 'http.response.start')
    assert (b'www-authenticate', b'Bearer') in start['headers']
    assert TOKEN not in repr(sent), 'the 401 response carries the token'


def test_a_non_http_scope_is_closed_and_lifespan_passes_through():
    app, sent = _request([(b'authorization', f'Bearer {TOKEN}'.encode())], scope_type='websocket')
    assert app.reached == [] and sent == [{'type': 'websocket.close', 'code': 1008}]
    app, _ = _request([], scope_type='lifespan')
    assert [scope['type'] for scope in app.reached] == ['lifespan']


@pytest.mark.parametrize('length', [0, 1, MIN_TOKEN_LENGTH - 1])
def test_the_gate_refuses_to_guard_with_a_short_token(length: int):
    with pytest.raises(ValueError, match='short token'):
        BearerTokenMiddleware(_App(), 'x' * length)
    BearerTokenMiddleware(_App(), 'x' * MIN_TOKEN_LENGTH)


def test_min_token_length_is_the_documented_floor():
    assert MIN_TOKEN_LENGTH == 32


# --- the token file ---------------------------------------------------------------------------


def test_the_first_start_writes_a_fresh_0600_token_and_the_next_reads_it_back(tmp_path: Path):
    path = tmp_path / 'agent_mcp_token'
    previous = os.umask(0)
    try:
        token = load_or_create_token(path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600, 'the token file is not 0600 even under umask 0'
    assert len(token) >= MIN_TOKEN_LENGTH and path.read_text() == token + '\n'
    assert load_or_create_token(path) == token, 'a second start regenerated (overwrote) an existing token'
    other = tmp_path / 'other_token'
    assert load_or_create_token(other) != token, 'two generations produced the same token'


@pytest.mark.parametrize('mode', [0o640, 0o604, 0o620, 0o602, 0o660, 0o644])
def test_a_token_file_readable_or_writable_by_others_is_refused(tmp_path: Path, mode: int):
    path = tmp_path / 'agent_mcp_token'
    path.write_text(TOKEN + '\n')
    path.chmod(mode)
    with pytest.raises(TokenFileError, match='0600'):
        load_or_create_token(path)


def test_a_symlinked_token_file_is_refused_and_not_followed(tmp_path: Path):
    real = tmp_path / 'real_token'
    real.write_text(TOKEN + '\n')
    real.chmod(0o600)
    link = tmp_path / 'agent_mcp_token'
    link.symlink_to(real)
    with pytest.raises(TokenFileError, match='not a regular file'):
        load_or_create_token(link)


def test_a_dangling_symlink_is_refused_and_its_target_never_created(tmp_path: Path):
    target = tmp_path / 'planted_target'
    link = tmp_path / 'agent_mcp_token'
    link.symlink_to(target)
    with pytest.raises(TokenFileError, match='not a regular file'):
        load_or_create_token(link)
    assert not target.exists(), 'the token was written through a planted symlink'


def test_a_directory_at_the_token_path_is_refused(tmp_path: Path):
    (tmp_path / 'agent_mcp_token').mkdir(mode=0o700)
    with pytest.raises(TokenFileError, match='not a regular file'):
        load_or_create_token(tmp_path / 'agent_mcp_token')


@pytest.mark.parametrize('content', ['', '\n', 'short\n', 'x' * (MIN_TOKEN_LENGTH - 1) + '\n'])
def test_a_token_file_holding_no_plausible_token_is_refused(tmp_path: Path, content: str):
    path = tmp_path / 'agent_mcp_token'
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(TokenFileError, match='does not hold a token'):
        load_or_create_token(path)


def test_the_token_never_reaches_a_log_line(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Neither the gate nor the token file logs the token, whatever happens."""
    caplog.set_level(logging.DEBUG)
    path = tmp_path / 'agent_mcp_token'
    path.write_text(TOKEN + '\n')
    path.chmod(0o600)
    assert load_or_create_token(path) == TOKEN
    for headers, query in _REFUSED.values():
        _request(headers, query=query)
    _request([(b'authorization', f'Bearer {TOKEN}'.encode())])
    path.chmod(0o644)
    with pytest.raises(TokenFileError) as refused:
        load_or_create_token(path)
    assert TOKEN not in str(refused.value)
    assert TOKEN not in caplog.text
