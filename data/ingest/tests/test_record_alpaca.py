"""The Alpaca recorder, offline: tests/fakes/record_alpaca.py (REC tj-3mk3u5.37.11).

WHY THIS FILE EXISTS. The recorder is run by hand at a credentialed sitting and never in CI, so a
mistake in it costs the user a sitting. The end-of-PR-2 sitting (tj-3mk3u5.16 U6) runs the Q-EMPTY
probes this bead added -- rulings 4 and 4b on tj-3mk3u5.37.1 -- and this file is the only check on
them before it does. FAKES-2 (tj-irhy0a.9) wrote the recorder without a test of its own, and
tests/fakes may hold no test module (test_fake_read.py), so its tests live here, beside the
fixtures it writes.

NOTHING HERE REACHES ALPACA. Every test replaces the recorder's `send` with a stub, and a socket
guard fails any test that opens a connection anyway. The one test that runs the real `send`
answers it from an in-process urllib handler.

EXPECTED REQUESTS ARE WRITTEN OUT -- host, path and every query parameter, as literals -- not
rebuilt from the recorder's helpers. Q_EMPTY is the --only list tj-3mk3u5.16 U6 hands the user.
"""

import email.message
import io
import json
import re
import socket
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from alpaca.trading.models import Asset

from tests.fakes import record_alpaca as recorder


pytestmark = pytest.mark.data_ingest

# Captured before the autouse fixture replaces it, for the one test that runs it.
REAL_SEND = recorder.send

# Stand-ins, not credentials: the credential scan looks for exactly these strings.
KEY_ID = 'stub-key-id-not-a-credential'
SECRET = 'stub-secret-not-a-credential'

Q_EMPTY = (
    'bars_unknown_symbol',
    'bars_mixed_known_unknown',
    'bars_malformed_character',
    'bars_malformed_length',
    'bars_malformed_lowercase',
    'bars_future_range',
    'asset_unknown_symbol',
    'asset_known_symbol',
)

DATA = 'data.alpaca.markets'
PAPER = 'paper-api.alpaca.markets'
BARS_PATH = '/v2/stocks/bars'
# bars_1Day's window and the parameters alpaca-py 0.44.0 sends with it.
CONTROL = {
    'timeframe': '1Day',
    'start': '2022-01-03T05:00:00Z',
    'end': '2022-01-05T05:00:00Z',
    'limit': '10000',
    'feed': 'iex',
}

# name -> (host, path, query): what each Q-EMPTY spec must ask, as the bead and the researcher's
# call list name it.
EXPECTED_REQUESTS = {
    'bars_unknown_symbol': (DATA, BARS_PATH, {'symbols': 'ZZZZZ', **CONTROL}),
    'bars_mixed_known_unknown': (DATA, BARS_PATH, {'symbols': 'AAPL,ZZZZZ', **CONTROL}),
    'bars_malformed_character': (DATA, BARS_PATH, {'symbols': 'AA$PL', **CONTROL}),
    'bars_malformed_length': (DATA, BARS_PATH, {'symbols': 'ABCDEFGHIJKLMNOP', **CONTROL}),
    'bars_malformed_lowercase': (DATA, BARS_PATH, {'symbols': 'aapl', **CONTROL}),
    'bars_future_range': (
        DATA,
        BARS_PATH,
        {
            'symbols': 'AAPL',
            'timeframe': '1Day',
            'start': '2030-01-03T05:00:00Z',
            'end': '2030-01-07T05:00:00Z',
            'limit': '10000',
            'feed': 'iex',
        },
    ),
    'asset_unknown_symbol': (PAPER, '/v2/assets/ZZZZZ', {}),
    'asset_known_symbol': (PAPER, '/v2/assets/AAPL', {}),
}
# The other request a test here routes: the existing weekend spec, for the null-bars fix.
ROUTES = {
    **EXPECTED_REQUESTS,
    'bars_empty': (
        DATA,
        BARS_PATH,
        {
            'symbols': 'AAPL',
            'timeframe': '1Day',
            'start': '2022-01-08T05:00:00Z',
            'end': '2022-01-09T05:00:00Z',
            'limit': '10000',
            'feed': 'iex',
        },
    ),
}
PROBE = '200|400|404|422'
EXPECTED_ACCEPTS = dict.fromkeys(Q_EMPTY, PROBE) | {'asset_known_symbol': '200'}

BARS_SOURCE = 'GET https://data.alpaca.markets/v2/stocks/bars (paper keys, read-only)'
ASSET_SOURCE = 'GET https://paper-api.alpaca.markets/v2/assets/{symbol} (paper keys, read-only)'

# Bodies. Prices and identifiers are illustrative; only the shapes matter.
AAPL_BARS = {
    'bars': {
        'AAPL': [
            {
                't': '2022-01-03T05:00:00Z',
                'o': 177.83,
                'h': 182.88,
                'l': 177.71,
                'c': 182.01,
                'v': 104487900,
                'n': 772932,
                'vw': 181.5,
            }
        ]
    },
    'next_page_token': None,
}
EMPTY_BARS = {'bars': {}, 'next_page_token': None}
NULL_BARS = {'bars': None, 'next_page_token': None}
# An asset body the SDK's own model reads (test_the_stub_asset_body_is_one_alpaca_py_reads).
AAPL_ASSET = {
    'id': 'b0b6dd9d-8b9b-48a9-ba46-b9d54906e415',
    'class': 'us_equity',
    'exchange': 'NASDAQ',
    'symbol': 'AAPL',
    'name': 'Apple Inc. Common Stock',
    'status': 'active',
    'tradable': True,
    'marginable': True,
    'shortable': True,
    'easy_to_borrow': True,
    'fractionable': True,
    'maintenance_margin_requirement': 30,
    'attributes': ['fractional_eh_enabled'],
}
ASSET_NOT_FOUND = {'code': 40410000, 'message': 'asset not found for ZZZZZ'}


@dataclass(frozen=True)
class Answer:
    """One stubbed vendor response."""

    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    def raw(self) -> bytes:
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


# One answer per Q-EMPTY spec: a plausible sitting, with every status class a probe may record.
SITTING = {
    'bars_unknown_symbol': Answer(200, EMPTY_BARS),
    'bars_mixed_known_unknown': Answer(200, AAPL_BARS),
    'bars_malformed_character': Answer(400, {'message': 'invalid symbol: AA$PL'}),
    'bars_malformed_length': Answer(422, {'message': 'invalid symbol: ABCDEFGHIJKLMNOP'}),
    'bars_malformed_lowercase': Answer(200, EMPTY_BARS),
    'bars_future_range': Answer(200, EMPTY_BARS),
    'asset_unknown_symbol': Answer(404, ASSET_NOT_FOUND),
    'asset_known_symbol': Answer(200, AAPL_ASSET),
}


def message_headers(headers: Mapping[str, str]) -> email.message.Message:
    """Headers as urllib hands them over: an email Message, read case-insensitively."""
    message = email.message.Message()
    for name, value in headers.items():
        message[name] = value
    return message


def url_parts(url: str) -> tuple[str, str, dict[str, str]]:
    """A URL as (host with any port or user, path, query parameters)."""
    split = urlsplit(url)
    return split.netloc, split.path, dict(parse_qsl(split.query, keep_blank_values=True))


class StubResponse:
    """What urlopen returns for a 2xx: status, body and headers, usable in a with block."""

    def __init__(self, answer: Answer) -> None:
        self.status = answer.status
        self.headers = message_headers(answer.headers)
        self._raw = answer.raw()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> 'StubResponse':
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Vendor:
    """Stands in for recorder.send: answers each request by the spec it matches; keeps them all.

    A non-2xx answer is raised as urllib's HTTPError, exactly as the real send raises it.
    """

    def __init__(self, answers: Mapping[str, Answer], default: Answer | None = None) -> None:
        self.answers = answers
        self.default = default
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> StubResponse:
        self.requests.append(request)
        name = next((name for name, parts in ROUTES.items() if parts == url_parts(request.full_url)), None)
        answer = self.answers.get(name, self.default) if name else self.default
        if answer is None:
            raise AssertionError(f'unscripted request: {request.full_url}')
        if 200 <= answer.status < 300:
            return StubResponse(answer)
        raise urllib.error.HTTPError(
            request.full_url, answer.status, 'stub', message_headers(answer.headers), io.BytesIO(answer.raw())
        )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test here reaches the network, the real fixture directory or a real credential."""

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('a recorder test opened a network connection')

    def no_send(request: urllib.request.Request) -> None:
        raise AssertionError(f'unscripted send: {request.full_url}')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(recorder, 'send', no_send)
    monkeypatch.setattr(recorder, 'DEFAULT_OUT', tmp_path / 'default-out')
    for name in recorder.CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ALPACA_API_KEY', KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', SECRET)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / 'out'


def run(monkeypatch: pytest.MonkeyPatch, vendor: Vendor, *argv: str, out: Path) -> int:
    monkeypatch.setattr(recorder, 'send', vendor)
    return recorder.main([*argv, '--out', str(out)])


def written(out: Path) -> dict[str, dict]:
    """Every fixture the run wrote, by file stem; empty when it wrote nothing."""
    return {path.stem: json.loads(path.read_text()) for path in sorted(out.glob('*.json'))}


def specs_by_name() -> dict[str, recorder.Spec]:
    return {spec.name: spec for spec in recorder.SPECS}


# ---------------------------------------------------------------------------------------------
# The requests the sitting sends
# ---------------------------------------------------------------------------------------------


def test_each_q_empty_spec_sends_its_intended_host_path_and_query(monkeypatch, keys, out):
    vendor = Vendor(SITTING)

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 0

    assert [url_parts(request.full_url) for request in vendor.requests] == [EXPECTED_REQUESTS[n] for n in Q_EMPTY]


def test_every_request_is_a_get_with_the_key_pair_in_its_headers_and_nowhere_else(monkeypatch, keys, out):
    vendor = Vendor(SITTING)

    run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out)

    assert len(vendor.requests) == len(Q_EMPTY)
    for request in vendor.requests:
        assert request.get_method() == 'GET'
        assert request.get_header('Apca-api-key-id') == KEY_ID
        assert request.get_header('Apca-api-secret-key') == SECRET
        assert KEY_ID not in request.full_url
        assert SECRET not in request.full_url


def test_the_unknown_symbol_is_well_formed_and_is_the_one_the_asset_lookup_asks_for():
    specs = specs_by_name()

    assert re.fullmatch(r'[A-Z]{1,5}', recorder.UNKNOWN_SYMBOL)
    assert specs['bars_unknown_symbol'].params['symbols'] == recorder.UNKNOWN_SYMBOL
    assert specs['asset_unknown_symbol'].path_symbol == recorder.UNKNOWN_SYMBOL
    # The bead: name the choice in the provenance 'why'.
    assert recorder.UNKNOWN_SYMBOL in specs['bars_unknown_symbol'].why


@pytest.mark.parametrize(
    'name',
    [
        'bars_unknown_symbol',
        'bars_mixed_known_unknown',
        'bars_malformed_character',
        'bars_malformed_length',
        'bars_malformed_lowercase',
    ],
)
def test_a_symbol_probe_is_bars_1Day_with_only_its_symbols_changed(name):
    specs = specs_by_name()
    probe, control = specs[name].params, specs['bars_1Day'].params

    assert {k: v for k, v in probe.items() if k != 'symbols'} == {k: v for k, v in control.items() if k != 'symbols'}
    assert probe['symbols'] != control['symbols']


def test_each_malformed_symbol_breaks_exactly_the_rule_its_name_says():
    specs = specs_by_name()
    character = specs['bars_malformed_character'].params['symbols']
    length = specs['bars_malformed_length'].params['symbols']
    lowercase = specs['bars_malformed_lowercase'].params['symbols']

    # A character outside [A-Z0-9.], and nothing else wrong with it.
    assert re.search(r'[^A-Z0-9.]', character)
    assert len(character) <= 5
    # Too long, and nothing else wrong with it.
    assert len(length) > 5
    assert re.fullmatch(r'[A-Z]+', length)
    # Lower case, and otherwise the control symbol.
    assert lowercase == lowercase.lower()
    assert lowercase.upper() == 'AAPL'


def test_the_future_range_is_static_and_lies_wholly_after_today():
    params = specs_by_name()['bars_future_range'].params
    start, end = datetime.fromisoformat(params['start']), datetime.fromisoformat(params['end'])

    assert datetime.now(UTC) < start < end


def test_every_spec_goes_to_an_allowed_host_and_the_live_trading_host_is_not_one():
    assert {'https://data.alpaca.markets', 'https://paper-api.alpaca.markets'} == recorder.ALLOWED_HOSTS
    for spec in recorder.SPECS:
        split = urlsplit(recorder.request_url(spec, spec.params))
        assert f'{split.scheme}://{split.netloc}' in recorder.ALLOWED_HOSTS, spec.name


@pytest.mark.parametrize(
    'url',
    [
        'https://api.alpaca.markets/v2/assets/AAPL',
        'http://data.alpaca.markets/v2/stocks/bars',
        'https://data.alpaca.markets.example.com/v2/stocks/bars',
        'https://someone@data.alpaca.markets/v2/stocks/bars',
        'https://data.alpaca.markets:8443/v2/stocks/bars',
    ],
    ids=['live-trading-host', 'plain-http', 'lookalike-host', 'userinfo', 'other-port'],
)
def test_a_request_to_any_other_origin_is_refused_before_it_is_sent(monkeypatch, url):
    vendor = Vendor({}, default=Answer(200, EMPTY_BARS))
    monkeypatch.setattr(recorder, 'send', vendor)

    with pytest.raises(recorder.RecordingRefused, match='is not an allowed host'):
        recorder.fetch(url, KEY_ID, SECRET)

    assert vendor.requests == []


def test_a_spec_pointed_at_the_live_trading_host_aborts_the_run_with_nothing_sent(monkeypatch, keys, out, capsys):
    live = recorder.Endpoint('https://api.alpaca.markets', '/v2/assets/{symbol}', recorder.ASSET_TOP_LEVEL_KEYS)
    monkeypatch.setattr(
        recorder, 'SPECS', (recorder.Spec('asset_live', {}, 'never', endpoint=live, path_symbol='AAPL'),)
    )
    vendor = Vendor({}, default=Answer(200, AAPL_ASSET))

    assert run(monkeypatch, vendor, out=out) == 1

    assert vendor.requests == []
    assert not out.exists()
    assert 'asset_live: https://api.alpaca.markets is not an allowed host' in capsys.readouterr().err


class InProcessHTTPS(urllib.request.BaseHandler):
    """Answers every https request in-process with a 302 to another host, and keeps each one."""

    handler_order = 100  # ahead of urllib's own HTTPSHandler, so nothing reaches a socket

    def __init__(self) -> None:
        self.seen: list[tuple[str, object]] = []

    def https_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.seen.append((request.full_url, request.timeout))
        headers = message_headers({'Location': 'https://elsewhere.example/v2/stocks/bars'})
        response = urllib.response.addinfourl(io.BytesIO(b'{}'), headers, request.full_url, 302)
        response.msg = 'Found'
        return response


def test_a_redirect_is_never_followed_so_the_key_pair_goes_nowhere_else(monkeypatch):
    for proxy in ('https_proxy', 'HTTPS_PROXY', 'http_proxy', 'HTTP_PROXY', 'all_proxy', 'ALL_PROXY'):
        monkeypatch.delenv(proxy, raising=False)
    transport = InProcessHTTPS()
    build_opener = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: build_opener(*handlers, transport))
    monkeypatch.setattr(recorder, 'send', REAL_SEND)
    url = 'https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL'

    status, _body, _limits = recorder.fetch(url, KEY_ID, SECRET)

    assert status == 302
    assert transport.seen == [(url, recorder.REQUEST_TIMEOUT_SECONDS)]


# ---------------------------------------------------------------------------------------------
# The allowlists
# ---------------------------------------------------------------------------------------------


def wire_names(model: type) -> frozenset[str]:
    """A pydantic model's field names as they appear on the wire: each alias, else the name."""
    return frozenset(info.alias or name for name, info in model.model_fields.items())


def test_the_asset_allowlist_is_alpaca_py_asset_model_and_an_error_body():
    assert wire_names(Asset) == recorder.ASSET_MODEL_KEYS
    assert wire_names(Asset) | {'code', 'message'} == recorder.ASSET_TOP_LEVEL_KEYS
    assert recorder.ASSET.allowed_keys == recorder.ASSET_TOP_LEVEL_KEYS


def test_the_bars_allowlist_is_unchanged():
    assert {'bars', 'next_page_token', 'currency', 'code', 'message'} == recorder.ALLOWED_TOP_LEVEL_KEYS
    assert recorder.BARS.allowed_keys == recorder.ALLOWED_TOP_LEVEL_KEYS


def test_only_the_two_asset_lookups_use_the_asset_endpoint():
    by_endpoint = {spec.name: spec.endpoint for spec in recorder.SPECS}

    assert {name for name, endpoint in by_endpoint.items() if endpoint == recorder.ASSET} == {
        'asset_unknown_symbol',
        'asset_known_symbol',
    }
    assert all(endpoint in (recorder.ASSET, recorder.BARS) for endpoint in by_endpoint.values())


def test_the_stub_asset_body_is_one_alpaca_py_reads():
    assert Asset.model_validate(AAPL_ASSET).symbol == 'AAPL'


def test_an_asset_shaped_body_is_recorded_as_it_came(monkeypatch, keys, out):
    vendor = Vendor({'asset_known_symbol': Answer(200, AAPL_ASSET)})

    assert run(monkeypatch, vendor, '--only', 'asset_known_symbol', out=out) == 0

    document = written(out)['asset_known_symbol']
    assert document['status'] == 200
    assert document['body'] == AAPL_ASSET


@pytest.mark.parametrize('body', [ASSET_NOT_FOUND, {'message': 'not found'}], ids=['code-and-message', 'message-only'])
def test_the_unknown_asset_lookup_records_its_404_error_body(monkeypatch, keys, out, body):
    vendor = Vendor({'asset_unknown_symbol': Answer(404, body)})

    assert run(monkeypatch, vendor, '--only', 'asset_unknown_symbol', out=out) == 0

    document = written(out)['asset_unknown_symbol']
    assert (document['status'], document['body']) == (404, body)


def test_an_asset_body_with_a_key_outside_the_model_aborts_the_whole_run(monkeypatch, keys, out, capsys):
    # asset_known_symbol is the last spec: seven bodies are staged by then, and none is written.
    vendor = Vendor(SITTING | {'asset_known_symbol': Answer(200, {**AAPL_ASSET, 'account_number': 'PA0000'})})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    assert len(vendor.requests) == len(Q_EMPTY)
    assert not out.exists()
    assert "asset_known_symbol: unexpected top-level keys ['account_number']" in capsys.readouterr().err


@pytest.mark.parametrize(
    ('name', 'body', 'refused_keys'),
    [
        (
            'bars_unknown_symbol',
            AAPL_ASSET,
            [
                'attributes',
                'class',
                'easy_to_borrow',
                'exchange',
                'fractionable',
                'id',
                'maintenance_margin_requirement',
                'marginable',
                'name',
                'shortable',
                'status',
                'symbol',
                'tradable',
            ],
        ),
        ('asset_known_symbol', AAPL_BARS, ['bars', 'next_page_token']),
    ],
    ids=['asset-body-on-the-bars-host', 'bars-body-on-the-asset-host'],
)
def test_each_endpoint_refuses_the_other_endpoints_body(monkeypatch, keys, out, capsys, name, body, refused_keys):
    vendor = Vendor({name: Answer(200, body)})

    assert run(monkeypatch, vendor, '--only', name, out=out) == 1

    assert not out.exists()
    assert f'{name}: unexpected top-level keys {refused_keys}' in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------
# The credential scan, on both hosts
# ---------------------------------------------------------------------------------------------


PLANTS: dict[str, tuple[int, Callable[[str], dict]]] = {
    'bars_mixed_known_unknown': (200, lambda value: {'bars': {}, 'next_page_token': value}),
    'asset_unknown_symbol': (404, lambda value: {'code': 40410000, 'message': f'asset not found: {value}'}),
    'asset_known_symbol': (200, lambda value: {**AAPL_ASSET, 'name': value}),
}


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
@pytest.mark.parametrize('name', list(PLANTS), ids=['data-host', 'paper-host-error-body', 'paper-host-asset-body'])
def test_a_credential_planted_in_a_body_aborts_the_run_with_nothing_written(
    monkeypatch, keys, out, capsys, name, credential
):
    status, plant = PLANTS[name]
    vendor = Vendor(SITTING | {name: Answer(status, plant(credential))})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert f'{name}: a credential value appears in the output' in captured.err
    assert credential not in captured.out + captured.err


# ---------------------------------------------------------------------------------------------
# A null 'bars'
# ---------------------------------------------------------------------------------------------


def test_a_null_bars_on_the_empty_range_is_recorded_as_bars_null(monkeypatch, keys, out, capsys):
    vendor = Vendor({'bars_empty': Answer(200, NULL_BARS)})

    assert run(monkeypatch, vendor, '--only', 'bars_empty', out=out) == 0

    documents = written(out)
    assert list(documents) == ['bars_null']
    assert documents['bars_null']['body'] == NULL_BARS
    assert capsys.readouterr().out.startswith('bars_null: HTTP 200; ')


@pytest.mark.parametrize(
    ('body', 'name'),
    [(EMPTY_BARS, 'bars_empty_absent_symbol'), ({'bars': {'AAPL': []}}, 'bars_empty_list'), (NULL_BARS, 'bars_null')],
    ids=['absent-symbol', 'empty-list', 'null'],
)
def test_the_empty_range_fixture_is_named_after_the_shape_that_came(body, name):
    assert recorder.empty_name(body, 200) == name


@pytest.mark.parametrize(
    'body',
    [{'bars': [], 'next_page_token': None}, {'next_page_token': None}, {'bars': {'AAPL': None}}, AAPL_BARS],
    ids=['bars-a-list', 'no-bars-key', 'symbol-null', 'populated'],
)
def test_an_empty_range_shape_nobody_expects_is_refused_not_crashed_on(body):
    with pytest.raises(recorder.RecordingRefused, match=r'^bars_empty: '):
        recorder.empty_name(body, 200)


def test_a_null_bars_on_a_probe_is_recorded_under_the_probes_own_name(monkeypatch, keys, out):
    vendor = Vendor({'bars_unknown_symbol': Answer(200, NULL_BARS)})

    assert run(monkeypatch, vendor, '--only', 'bars_unknown_symbol', out=out) == 0

    assert {name: document['body'] for name, document in written(out).items()} == {'bars_unknown_symbol': NULL_BARS}


# ---------------------------------------------------------------------------------------------
# The statuses each spec records
# ---------------------------------------------------------------------------------------------


def test_each_q_empty_spec_accepts_the_statuses_the_design_names():
    specs = specs_by_name()

    assert {name: specs[name].accepted_text for name in Q_EMPTY} == EXPECTED_ACCEPTS


@pytest.mark.parametrize('status', [200, 400, 404, 422])
def test_a_probe_records_every_status_that_speaks_about_the_request(monkeypatch, keys, out, status):
    body = EMPTY_BARS if status == 200 else {'message': 'invalid symbol: AA$PL'}
    vendor = Vendor({'bars_malformed_character': Answer(status, body)})

    assert run(monkeypatch, vendor, '--only', 'bars_malformed_character', out=out) == 0

    assert written(out)['bars_malformed_character']['status'] == status


@pytest.mark.parametrize('status', [302, 401, 403, 429, 500, 504])
def test_a_probe_aborts_on_a_status_about_the_sitting(monkeypatch, keys, out, capsys, status):
    vendor = Vendor({'bars_malformed_character': Answer(status, {'message': 'not about the request'})})

    assert run(monkeypatch, vendor, '--only', 'bars_malformed_character', out=out) == 1

    assert not out.exists()
    assert f'bars_malformed_character: expected HTTP {PROBE}, got {status}' in capsys.readouterr().err


def test_the_asset_control_records_only_a_200(monkeypatch, keys, out, capsys):
    vendor = Vendor({'asset_known_symbol': Answer(404, ASSET_NOT_FOUND)})

    assert run(monkeypatch, vendor, '--only', 'asset_known_symbol', out=out) == 1

    assert not out.exists()
    assert 'asset_known_symbol: expected HTTP 200, got 404' in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------
# What a refusal names (tj-3mk3u5.37.18)
#
# A spec's 'HTTP <status>' summary line is printed only once its body is staged, so when a body is
# refused the refusal line is the only place its status survives -- and for asset_unknown_symbol
# that status is the evidence (tj-3mk3u5.16 C3). Each expected line is written out in full, so it
# also shows that nothing from the body is printed but a top-level key name.
# ---------------------------------------------------------------------------------------------


REFUSED = 'record_alpaca: REFUSED, nothing written: '
# Planted in each refused body below; no refusal may print it.
BODY_VALUE = 'body-value-never-printed'


@pytest.mark.parametrize(
    ('name', 'answer', 'line'),
    [
        pytest.param(
            'asset_unknown_symbol',
            Answer(404, {**ASSET_NOT_FOUND, 'request_id': BODY_VALUE}),
            "asset_unknown_symbol: unexpected top-level keys ['request_id'] (HTTP 404)",
            id='paper-unexpected-key-404',
        ),
        pytest.param(
            'asset_known_symbol',
            Answer(200, {**AAPL_ASSET, 'margin_requirement_long': BODY_VALUE}),
            "asset_known_symbol: unexpected top-level keys ['margin_requirement_long'] (HTTP 200)",
            id='paper-unexpected-key-200',
        ),
        pytest.param(
            'bars_unknown_symbol',
            Answer(200, {**EMPTY_BARS, 'symbol': BODY_VALUE}),
            "bars_unknown_symbol: unexpected top-level keys ['symbol'] (HTTP 200)",
            id='data-unexpected-key-200',
        ),
        pytest.param(
            'bars_mixed_known_unknown',
            Answer(200, [BODY_VALUE]),
            'bars_mixed_known_unknown: body is not a JSON object (HTTP 200)',
            id='data-not-an-object-200',
        ),
        pytest.param(
            'bars_malformed_length',
            Answer(422, BODY_VALUE),
            'bars_malformed_length: body is not a JSON object (HTTP 422)',
            id='data-not-an-object-422',
        ),
        pytest.param(
            'asset_unknown_symbol',
            Answer(404, BODY_VALUE),
            'asset_unknown_symbol: body is not a JSON object (HTTP 404)',
            id='paper-not-an-object-404',
        ),
        pytest.param(
            'asset_unknown_symbol',
            Answer(404, b''),
            'asset_unknown_symbol: body is not a JSON object (HTTP 404)',
            id='paper-empty-body-404',
        ),
        pytest.param(
            'asset_known_symbol',
            Answer(200, None),
            'asset_known_symbol: body is not a JSON object (HTTP 200)',
            id='paper-null-body-200',
        ),
        # The two refusals that named the status already keep their text.
        pytest.param(
            'bars_malformed_character',
            Answer(400, f'<html>{BODY_VALUE}</html>'.encode()),
            'bars_malformed_character: HTTP 400 with a body that is not JSON',
            id='data-not-json-400',
        ),
        pytest.param(
            'asset_unknown_symbol',
            Answer(404, f'<html>{BODY_VALUE}</html>'.encode()),
            'asset_unknown_symbol: HTTP 404 with a body that is not JSON',
            id='paper-not-json-404',
        ),
        pytest.param(
            'asset_known_symbol',
            Answer(403, {'message': BODY_VALUE}),
            'asset_known_symbol: expected HTTP 200, got 403',
            id='paper-status-not-accepted-403',
        ),
    ],
)
def test_a_body_refused_after_its_response_names_the_spec_and_its_http_status(
    monkeypatch, keys, out, capsys, name, answer, line
):
    vendor = Vendor(SITTING | {name: answer})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert captured.err == f'{REFUSED}{line}\n'
    assert BODY_VALUE not in captured.out + captured.err


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
@pytest.mark.parametrize('name', list(PLANTS), ids=['data-host', 'paper-host-error-body', 'paper-host-asset-body'])
def test_the_credential_refusal_names_the_status_and_neither_the_credential_nor_where_it_was(
    monkeypatch, keys, out, capsys, name, credential
):
    status, plant = PLANTS[name]
    vendor = Vendor(SITTING | {name: Answer(status, plant(credential))})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    # The whole line: no credential, and no key the credential was planted under.
    assert captured.err == f'{REFUSED}{name}: a credential value appears in the output (HTTP {status})\n'
    assert credential not in captured.out + captured.err


@pytest.mark.parametrize(
    ('body', 'line'),
    [
        pytest.param(
            {'next_page_token': None},
            "bars_empty: the response has no bars key, only ['next_page_token'] (HTTP 200)",
            id='no-bars-key',
        ),
        pytest.param(
            {'bars': [], 'next_page_token': None},
            'bars_empty: the response has bars as list, not an object (HTTP 200)',
            id='bars-a-list',
        ),
        pytest.param(
            AAPL_BARS,
            'bars_empty: an empty-range request returned AAPL bars as a list of 1, not an empty list (HTTP 200)',
            id='populated',
        ),
        pytest.param(
            {'bars': {'AAPL': None}, 'next_page_token': None},
            'bars_empty: an empty-range request returned AAPL bars as NoneType, not an empty list (HTTP 200)',
            id='symbol-null',
        ),
    ],
)
def test_an_empty_range_refusal_names_its_http_status_and_the_shape_alone(monkeypatch, keys, out, capsys, body, line):
    vendor = Vendor({'bars_empty': Answer(200, body)})

    assert run(monkeypatch, vendor, '--only', 'bars_empty', out=out) == 1

    assert not out.exists()
    assert capsys.readouterr().err == f'{REFUSED}{line}\n'


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
def test_a_populated_empty_range_body_is_refused_without_echoing_it(monkeypatch, keys, out, capsys, credential):
    # record() scans a body for the credentials before empty_name sees it (tj-3mk3u5.37.19), so the
    # scan refuses this one; empty_name's populated refusal prints a count, never the entry, either
    # way (the 'populated' row above pins its whole line).
    weekend = {'bars': {'AAPL': [{**AAPL_BARS['bars']['AAPL'][0], 't': credential}]}, 'next_page_token': None}
    vendor = Vendor({'bars_empty': Answer(200, weekend)})

    assert run(monkeypatch, vendor, '--only', 'bars_empty', out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert credential not in captured.out + captured.err
    assert '177.83' not in captured.err


def test_a_page_one_with_no_token_is_refused_with_page_ones_status(monkeypatch, keys, out, capsys):
    vendor = Vendor({}, default=Answer(200, {'bars': {'AAPL': []}, 'next_page_token': None}))

    assert run(monkeypatch, vendor, '--only', 'bars_1Day_page2', out=out) == 1

    assert len(vendor.requests) == 1
    assert not out.exists()
    assert capsys.readouterr().err == (
        f'{REFUSED}bars_1Day_page1 returned no next_page_token (HTTP 200), so bars_1Day_page2 cannot be asked for\n'
    )


# ---------------------------------------------------------------------------------------------
# The credential scan comes before any refusal that prints a key name (tj-3mk3u5.37.19)
#
# The unexpected-keys refusal and empty_name's no-bars-key refusal print a body's top-level key
# names. A credential the vendor sent back AS a key would ride out on either one, so every decoded
# body -- an object or not -- is scanned first, and such a body is refused as a credential: the
# whole stderr line is the credential refusal, naming neither the credential nor the key.
# ---------------------------------------------------------------------------------------------


# name -> (status, the body with the credential planted as a top-level key)
KEY_PLANTS: dict[str, tuple[int, Callable[[str], dict]]] = {
    'asset_known_symbol': (200, lambda credential: {**AAPL_ASSET, credential: True}),
    'asset_unknown_symbol': (404, lambda credential: {**ASSET_NOT_FOUND, credential: None}),
    'bars_unknown_symbol': (200, lambda credential: {**EMPTY_BARS, credential: {}}),
}


def credential_line(name: str, status: int) -> str:
    return f'{REFUSED}{name}: a credential value appears in the output (HTTP {status})\n'


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
@pytest.mark.parametrize(
    'name', list(KEY_PLANTS), ids=['paper-host-asset-body-200', 'paper-host-error-body-404', 'data-host-bars-body-200']
)
def test_a_credential_sent_back_as_a_top_level_key_is_refused_as_a_credential_not_as_a_key(
    monkeypatch, keys, out, capsys, name, credential
):
    status, plant = KEY_PLANTS[name]
    vendor = Vendor(SITTING | {name: Answer(status, plant(credential))})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert captured.err == credential_line(name, status)
    assert credential not in captured.out + captured.err


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
def test_a_credential_as_a_key_of_an_empty_range_body_with_no_bars_key_is_refused_as_a_credential(
    monkeypatch, keys, out, capsys, credential
):
    vendor = Vendor({'bars_empty': Answer(200, {'next_page_token': None, credential: []})})

    assert run(monkeypatch, vendor, '--only', 'bars_empty', out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert captured.err == credential_line('bars_empty', 200)
    assert credential not in captured.out + captured.err


@pytest.mark.parametrize('credential', [KEY_ID, SECRET], ids=['key-id', 'secret'])
@pytest.mark.parametrize(
    ('name', 'status', 'plant'),
    [
        pytest.param('bars_mixed_known_unknown', 200, lambda credential: [credential], id='data-host-list-200'),
        pytest.param('asset_unknown_symbol', 404, lambda credential: credential, id='paper-host-string-404'),
    ],
)
def test_a_body_that_is_not_an_object_is_scanned_too(monkeypatch, keys, out, capsys, name, status, plant, credential):
    vendor = Vendor(SITTING | {name: Answer(status, plant(credential))})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert captured.err == credential_line(name, status)
    assert credential not in captured.out + captured.err


def test_a_non_ascii_credential_sent_back_as_a_key_is_found_in_the_characters_a_refusal_prints(
    monkeypatch, out, capsys
):
    # The unexpected-keys refusal prints a key's repr, which keeps 'é' as it is; a scan of the
    # ASCII-escaped serialisation would look for it and miss it.
    secret = 'stub-sécret-not-a-credential'
    monkeypatch.setenv('ALPACA_API_KEY', KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', secret)
    vendor = Vendor(SITTING | {'asset_known_symbol': Answer(200, {**AAPL_ASSET, secret: True})})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 1

    captured = capsys.readouterr()
    assert not out.exists()
    assert captured.err == credential_line('asset_known_symbol', 200)
    assert secret not in captured.out + captured.err


# ---------------------------------------------------------------------------------------------
# What is written, and what is only printed
# ---------------------------------------------------------------------------------------------


LIMIT_HEADERS = {
    'X-RateLimit-Limit': '200',
    'x-ratelimit-remaining': '199',
    'X-RateLimit-Reset': '1767225600',
    'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT',
    'X-Request-ID': 'request-identifier-0123',
    'Set-Cookie': 'session=cookie-value',
}
LIMITS_PRINTED = (
    'rate-limit headers: X-RateLimit-Limit=200, X-RateLimit-Remaining=199, X-RateLimit-Reset=1767225600, '
    'Retry-After=2015-10-21T07:28:00+00:00'
)


@pytest.mark.parametrize(
    ('name', 'answer'),
    [
        ('asset_known_symbol', Answer(200, AAPL_ASSET, LIMIT_HEADERS)),
        ('asset_unknown_symbol', Answer(404, ASSET_NOT_FOUND, LIMIT_HEADERS)),
    ],
    ids=['2xx', 'error-status'],
)
def test_the_summary_prints_the_four_rate_limit_headers_and_no_other(monkeypatch, keys, out, capsys, name, answer):
    vendor = Vendor({name: answer})

    assert run(monkeypatch, vendor, '--only', name, out=out) == 0

    printed = capsys.readouterr().out
    assert printed.splitlines()[0] == f'{name}: HTTP {answer.status}; {LIMITS_PRINTED}'
    assert 'request-identifier-0123' not in printed
    assert 'cookie-value' not in printed


def test_a_response_with_no_rate_limit_header_says_so(monkeypatch, keys, out, capsys):
    vendor = Vendor({'asset_known_symbol': Answer(200, AAPL_ASSET)})

    run(monkeypatch, vendor, '--only', 'asset_known_symbol', out=out)

    assert capsys.readouterr().out.splitlines()[0] == 'asset_known_symbol: HTTP 200; rate-limit headers: none'


@pytest.mark.parametrize(
    'raw', ['abc', '12.5', '-1', '1' * 13, KEY_ID, 'Wed, 21 Oct 2015 07:28:00 -0000', ''], ids=repr
)
def test_a_rate_limit_value_that_is_not_a_whole_number_or_an_http_date_is_withheld(raw):
    assert recorder.rate_limit_value(raw) == recorder.WITHHELD


def test_no_header_and_no_query_reaches_a_written_fixture(monkeypatch, keys, out):
    vendor = Vendor({name: Answer(a.status, a.body, LIMIT_HEADERS) for name, a in SITTING.items()})

    assert run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out) == 0

    for path in sorted(out.glob('*.json')):
        document = json.loads(path.read_text())
        assert set(document) == {'provenance', 'status', 'body'}, path.name
        assert set(document['provenance']) == {'kind', 'recorded_at', 'source', 'note'}, path.name
        text = path.read_text().lower()
        for forbidden in ('ratelimit', 'retry-after', 'x-request-id', 'set-cookie', 'apca', 'symbols=', '?'):
            assert forbidden not in text, (path.name, forbidden)


def test_each_fixture_keeps_the_provenance_block_and_names_the_host_actually_called(monkeypatch, keys, out, capsys):
    specs = specs_by_name()

    assert run(monkeypatch, Vendor(SITTING), '--only', *Q_EMPTY, out=out) == 0

    documents = written(out)
    assert set(documents) == set(Q_EMPTY)
    for name, document in documents.items():
        provenance = document['provenance']
        assert provenance['kind'] == 'recorded'
        assert provenance['source'] == (ASSET_SOURCE if name.startswith('asset_') else BARS_SOURCE)
        assert provenance['note'] == f'Recorded by tests/fakes/record_alpaca.py: {specs[name].why}.'
        assert datetime.fromisoformat(provenance['recorded_at']).utcoffset() is not None
        assert (document['status'], document['body']) == (SITTING[name].status, SITTING[name].body)
    assert capsys.readouterr().out.splitlines()[-1].startswith(f'wrote 8 fixtures to {out}')


# ---------------------------------------------------------------------------------------------
# --dry-run and --only
# ---------------------------------------------------------------------------------------------


DRY_RUN_LINE = re.compile(
    r'(?P<name>\w+): GET (?P<url>\S+)(?P<after> \(page_token from \w+\))?  \[accepts HTTP (?P<accepts>[\d|]+)\]'
)


def test_dry_run_prints_every_q_empty_request_and_reads_no_credential(monkeypatch, out, capsys):
    monkeypatch.setenv('ALPACA_API_KEY', KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', SECRET)

    def no_credentials() -> tuple[str, str]:
        raise AssertionError('the dry run read a credential')

    monkeypatch.setattr(recorder, 'credentials', no_credentials)

    assert recorder.main(['--dry-run', '--only', *Q_EMPTY, '--out', str(out)]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == 'DRY RUN: 8 requests; nothing is sent, no credential is read, nothing is written.'
    parsed = [DRY_RUN_LINE.fullmatch(line) for line in lines[1:]]
    assert all(parsed), lines
    assert [(match['name'], url_parts(match['url'])) for match in parsed] == [
        (name, EXPECTED_REQUESTS[name]) for name in Q_EMPTY
    ]
    assert {match['name']: match['accepts'] for match in parsed} == EXPECTED_ACCEPTS
    assert KEY_ID not in captured.out + captured.err
    assert SECRET not in captured.out + captured.err
    assert not out.exists()


def test_dry_run_needs_no_credential_in_the_environment(out):
    assert recorder.main(['--dry-run', '--only', *Q_EMPTY, '--out', str(out)]) == 0


def test_a_real_run_without_the_key_pair_stops_before_any_request(monkeypatch, out):
    vendor = Vendor(SITTING)

    with pytest.raises(SystemExit, match='ALPACA_API_KEY, ALPACA_API_SECRET'):
        run(monkeypatch, vendor, '--only', *Q_EMPTY, out=out)

    assert vendor.requests == []


def test_a_full_dry_run_lists_every_spec_once_in_order(out, capsys):
    assert recorder.main(['--dry-run', '--out', str(out)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f'DRY RUN: {len(recorder.SPECS)} requests; ')
    assert [DRY_RUN_LINE.fullmatch(line)['name'] for line in lines[1:]] == [spec.name for spec in recorder.SPECS]


def test_only_refuses_a_name_no_spec_has(out, capsys):
    with pytest.raises(SystemExit) as stopped:
        recorder.main(['--dry-run', '--only', 'bars_unknown', '--out', str(out)])

    assert stopped.value.code == 2
    assert "unknown fixtures: ['bars_unknown']" in capsys.readouterr().err


def test_only_brings_a_page_two_spec_its_page_one(out, capsys):
    assert recorder.main(['--dry-run', '--only', 'bars_1Day_page2', '--out', str(out)]) == 0

    matches = [DRY_RUN_LINE.fullmatch(line) for line in capsys.readouterr().out.splitlines()[1:]]
    assert [match['name'] for match in matches] == ['bars_1Day_page1', 'bars_1Day_page2']
    assert matches[1]['after'] == ' (page_token from bars_1Day_page1)'
