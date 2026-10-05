"""Record Alpaca's responses into the fixtures the recorded-response tests replay.

RUN BY HAND ONLY, on the host, at a credentialed sitting: tj-irhy0a.3 and FAKES-4 tj-irhy0a.13 for
the bars bodies, and the end-of-PR-2 sitting (tj-3mk3u5.16 U6) for the Q-EMPTY probes that REC
tj-3mk3u5.37.11 added. Never in CI, never by an agent: it needs a live credential, which no
branch-triggered workflow may hold (tj-59cce6).

    ALPACA_API_KEY=... ALPACA_API_SECRET=... uv run python tests/fakes/record_alpaca.py --dry-run
    ALPACA_API_KEY=... ALPACA_API_SECRET=... uv run python tests/fakes/record_alpaca.py --only NAME ...

--dry-run prints every request it would send -- host, path and query -- and sends nothing, reads
no credential and writes nothing. --only records the named fixtures alone, so a sitting rewrites no
fixture it did not mean to (a page-2 spec brings its page 1 along).

Use PAPER keys. Every call is a read-only GET of public data: stock bars from the market-data host,
GET https://data.alpaca.markets/v2/stocks/bars, and one asset's reference record from the PAPER
trading host, GET https://paper-api.alpaca.markets/v2/assets/{symbol}, which returns no account
data. Nothing is placed, changed or read from an account. A request to any other host -- the live
trading host above all -- is refused before it is sent, and a redirect is never followed: urllib
would resend the credential headers to wherever it pointed.

WHAT IS WRITTEN, AND WHAT NEVER IS: each fixture holds a provenance block, the response status and
the response body -- nothing else. No request or response header (the credential travels in the
APCA-API-KEY-ID / APCA-API-SECRET-KEY request headers), no URL, no query string. Before a file is
written the body's top-level keys are checked against its own endpoint's list -- a bars body
against the bars list, an asset body against the asset list -- and the serialised text is searched
for both credential values; either check failing aborts the whole run with nothing written.

WHAT A REFUSAL PRINTS (tj-3mk3u5.37.18): the spec, the reason and -- once a response has arrived --
its HTTP status, so a refused body still leaves its status as evidence even though nothing is
written. It may name top-level key names. It never prints a value from the body or a header, and
the credential refusal names neither the credential nor where it appeared.

RATE-LIMIT HEADERS ARE PRINTED, NEVER WRITTEN (tj-3mk3u5.37.11, the architect's optional suggestion
from the TE-4 gate). Four response headers are read and no other: X-RateLimit-Limit,
X-RateLimit-Remaining, X-RateLimit-Reset and Retry-After. The run's summary shows each one only as
a whole number, or as a UTC instant this module renders itself; any other value is withheld. Which
ones Alpaca sends tells TE-4's 429 classifier (tj-3mk3u5.37.5) where reset_at can come from, and
Remaining across the two hosts shows whether they draw on one budget.

IMPORTS: the standard library only. tests/fakes is imported by the fake-mode launcher inside the
prod image, so this module takes nothing from pytest, the testing group or any package data_ingest
does not already depend on (decision tj-j4wknb R4; FAKES-1's import scan checks it).

Recordable: the paging pair, one body per timeframe, the range boundary, the empty range, a 400, and
the Q-EMPTY probes -- an unknown symbol, that symbol beside AAPL, three malformed symbols, a range
in the future, and the asset lookup for the unknown symbol and for AAPL. Not recordable, so they
stay documented: 429, 504 and 500 -- provoking them is not a test step.
"""

import argparse
import email.utils
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DATA_HOST = 'https://data.alpaca.markets'
PAPER_TRADING_HOST = 'https://paper-api.alpaca.markets'
# Every origin a request may go to. The live trading host, https://api.alpaca.markets, is not one.
ALLOWED_HOSTS = frozenset({DATA_HOST, PAPER_TRADING_HOST})
BARS_PATH = '/v2/stocks/bars'
ASSET_PATH = '/v2/assets/{symbol}'
DATA_URL = f'{DATA_HOST}{BARS_PATH}'
CREDENTIAL_VARS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')
DEFAULT_OUT = Path(__file__).resolve().parents[2] / 'data' / 'ingest' / 'tests' / 'fixtures' / 'alpaca'
# What a stock-bars body or an error body may contain at the top level. Anything else is refused:
# it would be a shape the tests do not know, and possibly data that is not market data.
ALLOWED_TOP_LEVEL_KEYS = frozenset({'bars', 'next_page_token', 'currency', 'code', 'message'})
# What an asset body may contain: the wire names of alpaca-py 0.44.0's Asset model
# (alpaca/trading/models.py; 'id' comes from ModelWithID, 'class' is asset_class's alias), and an
# error body's keys. A list of its own: the bars list above is not widened for it.
ASSET_MODEL_KEYS = frozenset(
    {
        'id',
        'class',
        'exchange',
        'symbol',
        'name',
        'status',
        'tradable',
        'marginable',
        'shortable',
        'easy_to_borrow',
        'fractionable',
        'min_order_size',
        'min_trade_increment',
        'price_increment',
        'maintenance_margin_requirement',
        'attributes',
    }
)
ERROR_KEYS = frozenset({'code', 'message'})
ASSET_TOP_LEVEL_KEYS = ASSET_MODEL_KEYS | ERROR_KEYS
REQUEST_TIMEOUT_SECONDS = 30
SYMBOL = 'AAPL'
FEED = 'iex'  # the tape paper keys are entitled to
SDK_LIMIT = '10000'  # what alpaca-py 0.44.0 sends on every bars request (tj-vhboky.57)

# THE Q-EMPTY PROBES (rulings 4 and 4b on tj-3mk3u5.37.1; REC tj-3mk3u5.37.11). Ingest cannot yet
# tell an unknown symbol from an empty window; what these record decides how follow-up tj-lldllr
# refuses one.
# A well-formed symbol with no Alpaca asset: upper-case letters, at most five, the shape of a real
# listing. asset_unknown_symbol records the lookup that shows it has none.
UNKNOWN_SYMBOL = 'ZZZZZ'
# bars_1Day's window, so a probe's symbol is the only difference from a recorded populated body.
CONTROL_START = '2022-01-03T05:00:00Z'
CONTROL_END = '2022-01-05T05:00:00Z'
# What a probe may answer. The answer IS the question, so every status that speaks about the
# request is recorded: served (200), invalid (400, 422) or not found (404). 401, 403, 429 and 5xx
# speak about the sitting -- its keys, its entitlement, its rate, the vendor's health -- and abort.
PROBE_STATUSES = frozenset({200, 400, 404, 422})

# Read from every response and printed, never written (module docstring).
RATE_LIMIT_HEADERS = ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset', 'Retry-After')
WHOLE_NUMBER = re.compile(r'\d{1,12}')
WITHHELD = '<withheld: not a whole number or an HTTP date>'


@dataclass(frozen=True)
class Endpoint:
    """One GET this recorder may send, and what its body may hold.

    Attributes:
        host (str): Scheme and host, one of ALLOWED_HOSTS.
        path (str): Path; '{symbol}' in it is filled from the Spec's path_symbol.
        allowed_keys (frozenset[str]): The body's permitted top-level keys.
    """

    host: str
    path: str
    allowed_keys: frozenset[str]

    @property
    def source(self) -> str:
        """The provenance 'source' line: the endpoint actually called, without query or symbol."""
        return f'GET {self.host}{self.path} (paper keys, read-only)'


BARS = Endpoint(DATA_HOST, BARS_PATH, ALLOWED_TOP_LEVEL_KEYS)
ASSET = Endpoint(PAPER_TRADING_HOST, ASSET_PATH, ASSET_TOP_LEVEL_KEYS)


@dataclass(frozen=True)
class Spec:
    """One fixture to record.

    Attributes:
        name (str): Fixture file stem.
        params (dict[str, str]): Query parameters, credentials never among them.
        why (str): What the recorded body settles, written into its provenance.
        follows (str | None): A fixture whose next_page_token this request sends as page_token.
        expect_status (int | frozenset[int]): The status, or statuses, that count as success;
            anything else aborts the run.
        endpoint (Endpoint): Where the request goes, and which allowlist its body meets.
        path_symbol (str | None): The symbol an endpoint path's '{symbol}' is filled with.
    """

    name: str
    params: dict[str, str]
    why: str
    follows: str | None = None
    expect_status: int | frozenset[int] = 200
    endpoint: Endpoint = BARS
    path_symbol: str | None = None

    @property
    def accepted(self) -> frozenset[int]:
        """Every status this spec records; any other aborts the run."""
        expected = self.expect_status
        return expected if isinstance(expected, frozenset) else frozenset({expected})

    @property
    def accepted_text(self) -> str:
        return '|'.join(str(status) for status in sorted(self.accepted))


def bars(timeframe: str, start: str, end: str, limit: str = SDK_LIMIT, symbols: str = SYMBOL) -> dict[str, str]:
    return {'symbols': symbols, 'timeframe': timeframe, 'start': start, 'end': end, 'limit': limit, 'feed': FEED}


SPECS = (
    # limit=3 over five trading days forces a second page; the body shape is the same as at 10000.
    Spec(
        'bars_1Day_page1',
        bars('1Day', '2022-01-03T05:00:00Z', '2022-01-07T05:00:00Z', limit='3'),
        'page 1 of 2 (limit=3 forces paging)',
    ),
    Spec(
        'bars_1Day_page2',
        bars('1Day', '2022-01-03T05:00:00Z', '2022-01-07T05:00:00Z', limit='3'),
        'page 2 of 2, requested with page 1 token',
        follows='bars_1Day_page1',
    ),
    Spec('bars_1Min', bars('1Min', '2022-01-03T14:30:00Z', '2022-01-03T14:32:00Z'), 'one body per timeframe'),
    Spec('bars_5Min', bars('5Min', '2022-01-03T14:30:00Z', '2022-01-03T14:40:00Z'), 'one body per timeframe'),
    Spec('bars_30Min', bars('30Min', '2022-01-03T14:30:00Z', '2022-01-03T15:30:00Z'), 'one body per timeframe'),
    Spec('bars_1Hour', bars('1Hour', '2022-01-03T15:00:00Z', '2022-01-03T17:00:00Z'), 'one body per timeframe'),
    Spec('bars_1Day', bars('1Day', '2022-01-03T05:00:00Z', '2022-01-05T05:00:00Z'), 'one body per timeframe'),
    Spec(
        'bars_1Week',
        bars('1Week', '2022-01-01T00:00:00Z', '2022-01-21T00:00:00Z'),
        'the instant a weekly bar is stamped with',
    ),
    Spec(
        'bars_1Month',
        bars('1Month', '2022-01-01T00:00:00Z', '2022-03-31T00:00:00Z'),
        'the instant a monthly bar is stamped with',
    ),
    Spec(
        'bars_range_boundary',
        bars('1Hour', '2022-01-03T15:00:00Z', '2022-01-03T17:00:00Z'),
        "Alpaca's inclusive end: a bar stamped exactly at start and one exactly at end",
    ),
    # A weekend: no trading. Which empty shape comes back decides the file name (see record()).
    Spec('bars_empty', bars('1Day', '2022-01-08T05:00:00Z', '2022-01-09T05:00:00Z'), 'the real empty-range body shape'),
    Spec(
        'error_400',
        bars('1Day', '2022-01-05T05:00:00Z', '2022-01-03T05:00:00Z'),
        'a 400 error body (start after end)',
        expect_status=400,
    ),
    # The Q-EMPTY probes. Each bars probe is bars_1Day's request with one thing changed.
    Spec(
        'bars_unknown_symbol',
        bars('1Day', CONTROL_START, CONTROL_END, symbols=UNKNOWN_SYMBOL),
        f'an unknown well-formed symbol, {UNKNOWN_SYMBOL} (upper-case letters, at most five, chosen to have no '
        f'Alpaca asset; asset_unknown_symbol records that lookup), over the bars_1Day window, so the symbol is '
        f'the only difference from a recorded populated body',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'bars_mixed_known_unknown',
        bars('1Day', CONTROL_START, CONTROL_END, symbols=f'{SYMBOL},{UNKNOWN_SYMBOL}'),
        f'{SYMBOL} and the unknown {UNKNOWN_SYMBOL} in one request, over the bars_1Day window: whether Alpaca '
        f'omits {UNKNOWN_SYMBOL} and serves {SYMBOL}, or refuses the whole request',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'bars_malformed_character',
        bars('1Day', CONTROL_START, CONTROL_END, symbols='AA$PL'),
        'a malformed symbol, AA$PL: a character outside [A-Z0-9.], which nothing upstream checks',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'bars_malformed_length',
        bars('1Day', CONTROL_START, CONTROL_END, symbols='ABCDEFGHIJKLMNOP'),
        'a malformed symbol, ABCDEFGHIJKLMNOP: sixteen letters, over any listing length, which nothing upstream checks',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'bars_malformed_lowercase',
        bars('1Day', CONTROL_START, CONTROL_END, symbols='aapl'),
        'a malformed symbol, aapl: lower case, which the schemas upper-case but an Instrument built by hand does not',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'bars_future_range',
        bars('1Day', '2030-01-03T05:00:00Z', '2030-01-07T05:00:00Z'),
        f'{SYMBOL} 1Day over a range wholly in the future, 2030-01-03 to 2030-01-07: what Alpaca says to a '
        f'request TE-4 refuses before the vendor (RANGE_IN_FUTURE)',
        expect_status=PROBE_STATUSES,
    ),
    Spec(
        'asset_unknown_symbol',
        {},
        f'the asset lookup for the unknown {UNKNOWN_SYMBOL}, the symbol the bars probes ask for: whether it has '
        f'an asset (Alpaca documents 404 for none)',
        expect_status=PROBE_STATUSES,
        endpoint=ASSET,
        path_symbol=UNKNOWN_SYMBOL,
    ),
    Spec(
        'asset_known_symbol',
        {},
        f'the asset lookup for {SYMBOL}, the control beside asset_unknown_symbol',
        endpoint=ASSET,
        path_symbol=SYMBOL,
    ),
)


class RecordingRefused(Exception):
    """A response failed a safety check; nothing from this run is written."""


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the credential headers to wherever it points.

    Declining here leaves the 3xx to surface as an HTTPError, and no Spec accepts a 3xx.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials() -> tuple[str, str]:
    """Read the key pair from the environment, at run time only."""
    values = tuple(os.environ.get(name, '') for name in CREDENTIAL_VARS)
    missing = [name for name, value in zip(CREDENTIAL_VARS, values, strict=True) if not value]
    if missing:
        raise SystemExit(f'record_alpaca: set {", ".join(missing)} in the environment (paper keys)')
    return values[0], values[1]


def request_url(spec: Spec, params: Mapping[str, str]) -> str:
    """The URL a spec's request goes to: its endpoint's host and path, then the query, if any.

    Raises:
        ValueError: The endpoint's path names a symbol and the spec gives none (a bug in SPECS).
    """
    path = spec.endpoint.path
    if '{symbol}' in path:
        if not spec.path_symbol:
            raise ValueError(f'{spec.name}: {path} needs a path_symbol')
        path = path.replace('{symbol}', urllib.parse.quote(spec.path_symbol, safe=''))
    query = urllib.parse.urlencode(params)
    return f'{spec.endpoint.host}{path}?{query}' if query else f'{spec.endpoint.host}{path}'


def checked_origin(url: str) -> str:
    """Refuse a URL whose scheme and host are not in ALLOWED_HOSTS, before anything is sent."""
    parts = urllib.parse.urlsplit(url)
    origin = f'{parts.scheme}://{parts.netloc}'
    if origin not in ALLOWED_HOSTS:
        raise RecordingRefused(f'{origin} is not an allowed host ({", ".join(sorted(ALLOWED_HOSTS))}); nothing sent')
    return origin


def send(request: urllib.request.Request):
    """Open one request, refusing redirects. The offline tests stand a stub in for this."""
    return urllib.request.build_opener(RefuseRedirects).open(request, timeout=REQUEST_TIMEOUT_SECONDS)


def rate_limit_value(raw: str) -> str:
    """A rate-limit header's value as printed: a whole number, or a UTC instant rendered here.

    Anything else -- text, an identifier, a fraction, a sign -- is withheld, so nothing the vendor
    wrote is echoed unless it is digits.
    """
    text = raw.strip()
    if WHOLE_NUMBER.fullmatch(text):
        return text
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return WITHHELD
    if when.tzinfo is None:
        return WITHHELD
    return when.astimezone(UTC).isoformat()


def rate_limits(headers) -> dict[str, str]:
    """The RATE_LIMIT_HEADERS a response carried, made safe to print. No other header is read."""
    if headers is None:
        return {}
    found = {}
    for name in RATE_LIMIT_HEADERS:
        raw = headers.get(name)
        if raw is not None:
            found[name] = rate_limit_value(raw)
    return found


def rate_limit_text(limits: Mapping[str, str]) -> str:
    if not limits:
        return 'rate-limit headers: none'
    return 'rate-limit headers: ' + ', '.join(f'{name}={value}' for name, value in limits.items())


def fetch(url: str, key_id: str, secret: str) -> tuple[int, object, dict[str, str]]:
    """GET one URL on an allowed host; return its status, decoded body and rate-limit headers.

    No other header is returned, and the rate-limit ones only as rate_limit_value renders them.
    """
    checked_origin(url)
    request = urllib.request.Request(
        url,
        headers={'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret, 'Accept': 'application/json'},
        method='GET',
    )
    try:
        with send(request) as response:
            status, raw, headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        status, raw, headers = error.code, error.read(), error.headers
    limits = rate_limits(headers)
    try:
        return status, json.loads(raw or b'null'), limits
    except ValueError as undecodable:
        raise RecordingRefused(f'HTTP {status} with a body that is not JSON') from undecodable


def fixture_text(spec: Spec, status: int, body: object, secrets: tuple[str, str]) -> str:
    """Serialise one fixture, refusing anything that is not plainly market or reference data.

    Each refusal names the spec and the response's HTTP status, and nothing from the body but its
    top-level key names.
    """
    if not isinstance(body, dict):
        raise RecordingRefused(f'{spec.name}: body is not a JSON object (HTTP {status})')
    unexpected = set(body) - spec.endpoint.allowed_keys
    if unexpected:
        raise RecordingRefused(f'{spec.name}: unexpected top-level keys {sorted(unexpected)} (HTTP {status})')
    document = {
        'provenance': {
            'kind': 'recorded',
            'recorded_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'source': spec.endpoint.source,
            'note': f'Recorded by tests/fakes/record_alpaca.py: {spec.why}.',
        },
        'status': status,
        'body': body,
    }
    text = json.dumps(document, indent=2) + '\n'
    if any(secret and secret in text for secret in secrets):
        raise RecordingRefused(f'{spec.name}: a credential value appears in the output (HTTP {status})')
    return text


def empty_name(body: dict, status: int) -> str:
    """Name the empty-range fixture after the shape the vendor actually sent.

    A JSON null 'bars' is recorded as it came, as bars_null. It is never read as an object, and
    never filed as the absent-symbol shape it is not (null was community-reported on the old
    single-symbol endpoint, forum thread 8954; alpaca-py 0.44.0 raises on it, common/rest.py:395).

    This runs before fixture_text's credential scan, so a refusal here names the response's HTTP
    status and the body's shape -- key names, a type, a count -- and never a value from it.
    """
    if 'bars' not in body:
        raise RecordingRefused(f'bars_empty: the response has no bars key, only {sorted(body)} (HTTP {status})')
    by_symbol = body['bars']
    if by_symbol is None:
        return 'bars_null'
    if not isinstance(by_symbol, dict):
        raise RecordingRefused(
            f'bars_empty: the response has bars as {type(by_symbol).__name__}, not an object (HTTP {status})'
        )
    if SYMBOL not in by_symbol:
        return 'bars_empty_absent_symbol'
    entry = by_symbol[SYMBOL]
    if entry == []:
        return 'bars_empty_list'
    found = f'a list of {len(entry)}' if isinstance(entry, list) else type(entry).__name__
    raise RecordingRefused(
        f'bars_empty: an empty-range request returned {SYMBOL} bars as {found}, not an empty list (HTTP {status})'
    )


def describe(spec: Spec) -> str:
    """One dry-run line: the spec, the request it would send, and the statuses it records."""
    after = f' (page_token from {spec.follows})' if spec.follows else ''
    return f'{spec.name}: GET {request_url(spec, spec.params)}{after}  [accepts HTTP {spec.accepted_text}]'


def record(specs: tuple[Spec, ...], out: Path, dry_run: bool) -> int:
    """Record every spec, then write them all, or write nothing if any check fails."""
    if dry_run:
        print(f'DRY RUN: {len(specs)} requests; nothing is sent, no credential is read, nothing is written.')
        for spec in specs:
            print(describe(spec))
        return 0

    key_id, secret = credentials()
    bodies: dict[str, dict] = {}
    statuses: dict[str, int] = {}
    staged: dict[str, str] = {}
    for spec in specs:
        params = dict(spec.params)
        if spec.follows is not None:
            token = bodies[spec.follows].get('next_page_token')
            if not token:
                raise RecordingRefused(
                    f'{spec.follows} returned no next_page_token (HTTP {statuses[spec.follows]}), '
                    f'so {spec.name} cannot be asked for'
                )
            params['page_token'] = token
        try:
            status, body, limits = fetch(request_url(spec, params), key_id, secret)
        except RecordingRefused as refused:
            raise RecordingRefused(f'{spec.name}: {refused}') from refused
        if status not in spec.accepted:
            raise RecordingRefused(f'{spec.name}: expected HTTP {spec.accepted_text}, got {status}')
        if isinstance(body, dict):
            bodies[spec.name] = body
            statuses[spec.name] = status
        name = empty_name(body, status) if spec.name == 'bars_empty' and isinstance(body, dict) else spec.name
        staged[name] = fixture_text(spec, status, body, (key_id, secret))
        print(f'{name}: HTTP {status}; {rate_limit_text(limits)}')

    out.mkdir(parents=True, exist_ok=True)
    for name, text in staged.items():
        (out / f'{name}.json').write_text(text)
    print(f'wrote {len(staged)} fixtures to {out}; update fixtures/alpaca/README.md to say which are now recorded')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--dry-run', action='store_true', help='print every request; send nothing, read no credential, write nothing'
    )
    parser.add_argument(
        '--only', nargs='+', metavar='NAME', help='record only these fixture stems (page 2 brings its page 1)'
    )
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT, help='fixture directory')
    args = parser.parse_args(argv)

    specs = SPECS
    if args.only:
        unknown = set(args.only) - {spec.name for spec in SPECS}
        if unknown:
            parser.error(f'unknown fixtures: {sorted(unknown)}')
        wanted = set(args.only) | {spec.follows for spec in SPECS if spec.name in args.only and spec.follows}
        specs = tuple(spec for spec in SPECS if spec.name in wanted)
    try:
        return record(specs, args.out, args.dry_run)
    except RecordingRefused as refused:
        print(f'record_alpaca: REFUSED, nothing written: {refused}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
