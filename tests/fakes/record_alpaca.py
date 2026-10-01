"""Record Alpaca's stock-bars responses into the fixtures the recorded-response tests replay.

RUN BY HAND ONLY, on the host, at the sitting that replaces the documented fixtures (tj-irhy0a.3,
FAKES-4 tj-irhy0a.13). Never in CI, never by an agent: it needs a live credential, which no
branch-triggered workflow may hold (tj-59cce6).

    ALPACA_API_KEY=... ALPACA_API_SECRET=... uv run python tests/fakes/record_alpaca.py --dry-run
    ALPACA_API_KEY=... ALPACA_API_SECRET=... uv run python tests/fakes/record_alpaca.py

Use PAPER keys. Every call is a read-only GET to Alpaca's market-data host, /v2/stocks/bars; nothing
is placed, changed or read from an account.

WHAT IS WRITTEN, AND WHAT NEVER IS: each fixture holds a provenance block, the response status and
the response body -- nothing else. No request or response header (the credential travels in the
APCA-API-KEY-ID / APCA-API-SECRET-KEY request headers), no URL, no query string. Stock bars carry
market data only: symbol, timestamps, prices, volumes, trade counts, a page token. Before a file is
written the body's top-level keys are checked against that list and the serialised text is searched
for both credential values; either check failing aborts the whole run with nothing written.

IMPORTS: the standard library only. tests/fakes is imported by the fake-mode launcher inside the
prod image, so this module takes nothing from pytest, the testing group or any package data_ingest
does not already depend on (decision tj-j4wknb R4; FAKES-1's import scan checks it).

Recordable: the paging pair, one body per timeframe, the range boundary, the empty range and a 400.
Not recordable, so they stay documented: 429, 504 and 500 -- provoking them is not a test step.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DATA_URL = 'https://data.alpaca.markets/v2/stocks/bars'
CREDENTIAL_VARS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')
DEFAULT_OUT = Path(__file__).resolve().parents[2] / 'data' / 'ingest' / 'tests' / 'fixtures' / 'alpaca'
# What a stock-bars body or an error body may contain at the top level. Anything else is refused:
# it would be a shape the tests do not know, and possibly data that is not market data.
ALLOWED_TOP_LEVEL_KEYS = frozenset({'bars', 'next_page_token', 'currency', 'code', 'message'})
REQUEST_TIMEOUT_SECONDS = 30
SYMBOL = 'AAPL'
FEED = 'iex'  # the tape paper keys are entitled to
SDK_LIMIT = '10000'  # what alpaca-py 0.44.0 sends on every bars request (tj-vhboky.57)


@dataclass(frozen=True)
class Spec:
    """One fixture to record.

    Attributes:
        name (str): Fixture file stem.
        params (dict[str, str]): Query parameters, credentials never among them.
        why (str): What the recorded body settles, written into its provenance.
        follows (str | None): A fixture whose next_page_token this request sends as page_token.
        expect_status (int): The status that counts as success; anything else aborts the run.
    """

    name: str
    params: dict[str, str]
    why: str
    follows: str | None = None
    expect_status: int = 200


def bars(timeframe: str, start: str, end: str, limit: str = SDK_LIMIT) -> dict[str, str]:
    return {'symbols': SYMBOL, 'timeframe': timeframe, 'start': start, 'end': end, 'limit': limit, 'feed': FEED}


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
)


class RecordingRefused(Exception):
    """A response failed a safety check; nothing from this run is written."""


def credentials() -> tuple[str, str]:
    """Read the key pair from the environment, at run time only."""
    values = tuple(os.environ.get(name, '') for name in CREDENTIAL_VARS)
    missing = [name for name, value in zip(CREDENTIAL_VARS, values, strict=True) if not value]
    if missing:
        raise SystemExit(f'record_alpaca: set {", ".join(missing)} in the environment (paper keys)')
    return values[0], values[1]


def fetch(params: dict[str, str], key_id: str, secret: str) -> tuple[int, object]:
    """GET one bars request and return its status and decoded body. Headers are never returned."""
    request = urllib.request.Request(
        f'{DATA_URL}?{urllib.parse.urlencode(params)}',
        headers={'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret, 'Accept': 'application/json'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw or b'null')
    except ValueError as undecodable:
        raise RecordingRefused(f'HTTP {status} with a body that is not JSON') from undecodable


def fixture_text(spec: Spec, status: int, body: object, secrets: tuple[str, str]) -> str:
    """Serialise one fixture, refusing anything that is not plainly market data."""
    if not isinstance(body, dict):
        raise RecordingRefused(f'{spec.name}: body is not a JSON object')
    unexpected = set(body) - ALLOWED_TOP_LEVEL_KEYS
    if unexpected:
        raise RecordingRefused(f'{spec.name}: unexpected top-level keys {sorted(unexpected)}')
    document = {
        'provenance': {
            'kind': 'recorded',
            'recorded_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'source': 'GET https://data.alpaca.markets/v2/stocks/bars (paper keys, read-only)',
            'note': f'Recorded by tests/fakes/record_alpaca.py: {spec.why}.',
        },
        'status': status,
        'body': body,
    }
    text = json.dumps(document, indent=2) + '\n'
    if any(secret and secret in text for secret in secrets):
        raise RecordingRefused(f'{spec.name}: a credential value appears in the output')
    return text


def empty_name(body: dict) -> str:
    """Name the empty-range fixture after the shape the vendor actually sent."""
    symbol_bars = (body.get('bars') or {}).get(SYMBOL)
    if symbol_bars is None:
        return 'bars_empty_absent_symbol'
    if symbol_bars == []:
        return 'bars_empty_list'
    raise RecordingRefused(f'empty-range request returned bars: {symbol_bars!r}')


def record(specs: tuple[Spec, ...], out: Path, dry_run: bool) -> int:
    """Record every spec, then write them all, or write nothing if any check fails."""
    if dry_run:
        for spec in specs:
            after = f' (page_token from {spec.follows})' if spec.follows else ''
            print(f'{spec.name}: GET {DATA_URL}?{urllib.parse.urlencode(spec.params)}{after}')
        return 0

    key_id, secret = credentials()
    bodies: dict[str, dict] = {}
    staged: dict[str, str] = {}
    for spec in specs:
        params = dict(spec.params)
        if spec.follows is not None:
            token = bodies[spec.follows].get('next_page_token')
            if not token:
                raise RecordingRefused(
                    f'{spec.follows} returned no next_page_token, so {spec.name} cannot be asked for'
                )
            params['page_token'] = token
        status, body = fetch(params, key_id, secret)
        if status != spec.expect_status:
            raise RecordingRefused(f'{spec.name}: expected HTTP {spec.expect_status}, got {status}')
        if isinstance(body, dict):
            bodies[spec.name] = body
        name = empty_name(body) if spec.name == 'bars_empty' and isinstance(body, dict) else spec.name
        staged[name] = fixture_text(spec, status, body, (key_id, secret))
        print(f'{name}: HTTP {status}')

    out.mkdir(parents=True, exist_ok=True)
    for name, text in staged.items():
        (out / f'{name}.json').write_text(text)
    print(f'wrote {len(staged)} fixtures to {out}; update fixtures/alpaca/README.md to say which are now recorded')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dry-run', action='store_true', help='print the requests; no network, no credentials')
    parser.add_argument('--only', nargs='+', metavar='NAME', help='record only these fixture stems')
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
