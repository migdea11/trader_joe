"""The real transport, HttpClient, against loopback servers started by the test (validator, tj-grna9p.145).

It must not follow a redirect (the secret header would travel to wherever the redirect points) and must not use a
proxy from the environment (the proxy would see the secret header). Each server here binds 127.0.0.1 on an ephemeral
port and records what reaches it; nothing leaves the machine.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import SECRET


pytestmark = pytest.mark.build_infra


class Recorder:
    def __init__(self):
        self.requests: list[dict] = []


@contextmanager
def server(answer) -> Iterator[tuple[str, Recorder]]:
    """A loopback HTTP server; answer(handler) writes the response. Yields its base URL and its record."""
    record = Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get('Content-Length') or 0)
            record.requests.append(
                {
                    'method': self.command,
                    'path': self.path,
                    'headers': {k.lower(): v for k, v in self.headers.items()},
                    'body': self.rfile.read(length) if length else b'',
                }
            )
            answer(self)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_address[1]}', record
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def reply(status: int, body: bytes = b'', headers: dict | None = None):
    def answer(handler):
        handler.send_response(status)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return answer


@pytest.fixture(autouse=True)
def no_proxy_bypass(monkeypatch):
    # Without these, urllib would bypass a proxy for loopback anyway and the proxy test would prove nothing.
    for name in (
        'no_proxy',
        'NO_PROXY',
        'http_proxy',
        'HTTP_PROXY',
        'https_proxy',
        'HTTPS_PROXY',
        'all_proxy',
        'ALL_PROXY',
    ):
        monkeypatch.delenv(name, raising=False)


def test_a_post_carries_the_secret_header_and_a_json_body():
    with server(reply(200, b'{"data_points": 3}')) as (url, record):
        answer = tool.HttpClient(url).request(
            'POST', '/store/stock/market-activity/SPY', headers={'X-Instance-Secret': SECRET}, body={'owner': 'o'}
        )
    assert (answer.status, answer.body) == (200, {'data_points': 3})
    [seen] = record.requests
    assert seen['method'] == 'POST'
    assert seen['path'] == '/store/stock/market-activity/SPY'
    assert seen['headers']['x-instance-secret'] == SECRET
    assert seen['headers']['content-type'] == 'application/json'
    assert json.loads(seen['body']) == {'owner': 'o'}


def test_a_get_sends_no_body_and_no_content_type():
    with server(reply(200, b'[]')) as (url, record):
        answer = tool.HttpClient(url).request('GET', '/ui/v1/config')
    assert answer == tool.Response(200, [])
    assert 'content-type' not in record.requests[0]['headers']
    assert record.requests[0]['body'] == b''


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_a_redirect_is_never_followed_so_the_secret_header_stays_put(status):
    with (
        server(reply(200, b'{}')) as (elsewhere, landed),
        server(reply(status, b'', {'Location': f'{elsewhere}/steal'})) as (url, record),
    ):
        answer = tool.HttpClient(url).request(
            'POST', '/store/stock/market-activity/SPY', headers={'X-Instance-Secret': SECRET}, body={'a': 1}
        )
    assert answer.status == status
    assert len(record.requests) == 1
    assert landed.requests == []


@pytest.mark.parametrize('variable', ['http_proxy', 'HTTP_PROXY'])
def test_a_proxy_in_the_environment_is_never_used(monkeypatch, variable):
    with server(reply(200, b'{}')) as (proxy, proxied), server(reply(200, b'{"ok": true}')) as (url, direct):
        monkeypatch.setenv(variable, proxy)
        answer = tool.HttpClient(url).request('GET', '/ping', headers={'X-Instance-Secret': SECRET})
    assert answer.body == {'ok': True}
    assert proxied.requests == []
    assert len(direct.requests) == 1


def test_an_http_error_is_an_answer_with_its_decoded_body():
    with server(reply(409, b'{"reason": "OWN_OVERLAP_CONFLICT"}')) as (url, _):
        answer = tool.HttpClient(url).request('POST', '/x', body={'a': 1})
    assert answer == tool.Response(409, {'reason': 'OWN_OVERLAP_CONFLICT'})


def test_a_body_that_is_not_json_decodes_to_none():
    with server(reply(502, b'<html>bad gateway</html>')) as (url, _):
        assert tool.HttpClient(url).request('GET', '/x') == tool.Response(502, None)


@pytest.mark.parametrize('base', ['file:///etc', 'ftp://x', 'data_store', 'gopher://x'])
def test_only_http_and_https_store_urls_are_accepted(base):
    with pytest.raises(tool.DevDatasetsError, match='must be http or https'):
        tool.HttpClient(base)
