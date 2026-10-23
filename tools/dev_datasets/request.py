"""Generate the dev showcase datasets from plan.json (tj-grna9p.145, design on tj-grna9p.143).

Run from the repo root, in the agent container, against a DEV or TEST stack only (the venv synced through make,
for example .venv-devcontainer/bin/python):

    .venv-devcontainer/bin/python -m tools.dev_datasets.request [seed|reset|reads|degrade|repair|check]
        [--yes-reset|--no-reset] [--dry-run] [--date YYYY-MM-DD]

SEEDING ALWAYS STARTS CLEAN (owner ruling 2026-10-20). seed is a reset, then the plan. The reset empties exactly
seven store tables in ONE transaction (RESET_TABLES) and nothing else: never alembic_version, no other table, no DDL.
--yes-reset is REQUIRED to reset; without it seed and reset print the counts they would remove, change nothing and
exit 1. --no-reset skips the reset (incremental use). The two audit tables are append-only by CONVENTION only; the
reset is a deliberate environment reset and is not recorded anywhere.

THE RAIL. Every mode that writes (seed, reset, reads, degrade, repair) first asks GET /ui/v1/config and refuses
unless deploymentLabel is 'dev' or 'test'. A prod stack answers an empty label and is refused before any SQL or
request. The label also picks the backend, from the one plan: dev is real Alpaca bars under SPY, QQQ and DIA; test is
the fake broker under ZZDS-prefixed synthetic symbols, so the public repo never shows fake prices under a real ticker.

THE DATABASE RAIL. The HTTP label proves only the store that answered, not the database the tool writes to (they are
named separately). So every database connection the tool opens first reads, as its FIRST statement and on THAT
connection, the row of pg_settings named cluster_name (readable by any role), before any count, reset or degrade
statement. It refuses unless exactly one row came back whose source is 'command line', whose value is 'dev' or 'test',
and which equals the label the store reported. cluster_name is set only by docker-compose.override.yaml (dev) and
docker-compose.fake.yaml (test), as a postgres server option (-c cluster_name=dev); a prod database never has it from
the command line and is refused. The source is what makes it a proof: a client that sets the value itself (PGOPTIONS, a
session SET) shows source 'client' or 'session', and ALTER ROLE or ALTER DATABASE ... SET shows 'user' or 'database';
all are refused, as is no row, a NULL or empty value and any other source. What it does not prove: someone with
superuser rights or access to the server's command line can still set it. A refusal names the value and the source
only and changes nothing.

THE SECRETS. INSTANCE_WRITE_SECRET and POSTGRES_PASS come ONLY from the environment. Each is held in a Secret,
whose repr and str are masked, and is revealed at exactly one place: the header the HTTP call sends, the password
the DB connect call takes. Neither reaches stdout, stderr, a log, an argv, an exception message or a traceback:
every failure is reported by the exception's TYPE NAME only, because a library may quote what it was given
(http.client quotes a malformed header value). The placeholder the agent container carries when it was not
started through `make agent-up` is recognised and stops every mode except check and every dry run (a dry run sends
no secret and opens no database with a placeholder, so it skips the credential check).

WHAT IT TOUCHES. seed and repair POST to the store, as the plan's owners (dev-datasets, operator, dev-degraded),
never claude-seed. degrade is one SQL transaction that deletes bars of dev-degraded datasets named in the plan,
between New York midnights (whole sessions), and touches no audit, usage or run table. reset is the one SQL
transaction described above. Public repo: SPY, QQQ and DIA only (ZZDS-prefixed symbols in test).
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep as real_sleep
from typing import Any, Final
from zoneinfo import ZoneInfo


PLAN_PATH: Final = Path(__file__).with_name('plan.json')

# The environment variables, by name. The values are read once, in load_environment.
SECRET_VAR: Final = 'INSTANCE_WRITE_SECRET'  # nosec B105 -- the variable's name
DB_PASSWORD_VAR: Final = 'POSTGRES_PASS'  # nosec B105 -- the variable's name
STORE_URL_VAR: Final = 'DEV_DATASETS_STORE_URL'
DEFAULT_STORE_URL: Final = 'http://data_store'

# What the agent container carries for each credential when it was not started through `make agent-up`
# (.devcontainer/compose.yml). It cannot be mistaken for a credential, and nothing may be sent with it.
PLACEHOLDER: Final = 'UNSET-start-with-make-agent-up-from-the-repo-root'

SECRET_HEADER: Final = 'X-Instance-Secret'  # nosec B105 -- the header's name
CLIENT_ID_HEADER: Final = 'X-Client-Id'
UI_READER: Final = 'operator'
SDK_READER: Final = 'dev-datasets-sdk'

DEV_LABEL: Final = 'dev'
TEST_LABEL: Final = 'test'
RAIL_LABELS: Final = (DEV_LABEL, TEST_LABEL)

# The database's own identity: the built-in cluster_name, which the dev and test compose files put on the postgres
# command line (-c cluster_name=dev, -c cluster_name=test) and the prod files never set. A built-in is listed in
# pg_settings with its source; a custom placeholder setting is hidden from that view, so it could not be proven.
STACK_SETTING: Final = 'cluster_name'
STACK_SQL: Final = "SELECT setting, source FROM pg_settings WHERE name = 'cluster_name'"
STACK_SOURCE: Final = 'command line'

# The test backend's symbols are synthetic: an optional fake-broker scenario prefix (tests/fakes/market_data.py
# EMPTY_PREFIX, GAPS_PREFIX, FAILONCE_PREFIX), then 'ZZDS', then the dev symbol. Never a real ticker on its own.
TEST_SYMBOL_MARK: Final = 'ZZDS'
TEST_SCENARIO_PREFIXES: Final = ('EMPTY_', 'GAPS_', 'FAILONCE_')
FAILONCE_PREFIX: Final = 'FAILONCE_'
DEGRADED_OWNER: Final = 'dev-degraded'
FORBIDDEN_OWNER: Final = 'claude-seed'
ALLOWED_SYMBOLS: Final = frozenset({'SPY', 'QQQ', 'DIA'})
ALLOWED_OWNERS: Final = frozenset({'dev-datasets', 'operator', DEGRADED_OWNER})

# Granularity on the wire -> the label the database stores (the member name; migration 2b88043cd13c).
GRANULARITY_DB_LABELS: Final = {
    '1min': 'ONE_MINUTE',
    '5min': 'FIVE_MINUTES',
    '30min': 'THIRTY_MINUTES',
    '1hour': 'ONE_HOUR',
    '1day': 'ONE_DAY',
    '1week': 'ONE_WEEK',
    '1month': 'ONE_MONTH',
}
UPDATE_TYPES: Final = frozenset({'STATIC', 'DAILY'})
EXPIRY_TYPES: Final = frozenset({'BULK', 'BUFFER_1K', 'BUFFER_10K', 'BUFFER_100K', 'ROLLING'})
READ_KINDS: Final = frozenset({'ui', 'sdk'})
SOURCE: Final = 'ALPACA'

MARKET_TZ: Final = ZoneInfo('America/New_York')
# A range must end at least this long before now: the vendor serves a finished window only.
END_MARGIN: Final = timedelta(minutes=15)

# The store route is /store/{asset_type}/{data_type}/{asset_symbol}; this tool serves stock market activity only.
STORE_PATH: Final = '/store/stock/market-activity'
ASSET_DATA_PATH: Final = '/internal/asset-data/stock/market-activity'
OWN_OVERLAP_REASON: Final = 'OWN_OVERLAP_CONFLICT'
WAIT_STATUSES: Final = frozenset({429, 503, 504})
FAILONCE_STATUS: Final = 503  # the store's answer to the fake broker's VENDOR_UNAVAILABLE
FAILONCE_REASON: Final = 'VENDOR_UNAVAILABLE'  # any other 503 on a FAILONCE_ row is a real failure
SEED_TIMEOUT_SECONDS: Final = 600
READ_TIMEOUT_SECONDS: Final = 120
READ_PAGE_LIMIT: Final = 100

_RELATIVE = re.compile(r'^D(?:([+-])(\d+))?$')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_HEADER_SAFE = re.compile(r'^[\x21-\x7e]+$')

# Both statements bind every value. The delete repeats the owner test, so it can never reach a bar of a dataset
# that is not a dev-degraded entry, whatever id it is handed.
LOOKUP_SQL: Final = 'SELECT id FROM store_dataset_entry WHERE owner = %s AND asset_symbol = %s AND granularity = %s'
DELETE_SQL: Final = (
    'DELETE FROM stock_market_activity WHERE dataset_id = %s AND timestamp >= %s AND timestamp < %s '
    'AND EXISTS (SELECT 1 FROM store_dataset_entry e WHERE e.id = %s AND e.owner = %s)'
)


# THE RESET. Exactly these seven tables, in this order, and no others. The order is documentation, not a need:
# one TRUNCATE names them all, and the only foreign key among them (stock_market_activity.dataset_id ->
# store_dataset_entry.id) has both ends in the statement, so no CASCADE is used and nothing outside the list can be
# reached. alembic_version is never in it. Written out in full, never assembled from the names.
RESET_TABLES: Final = (
    'stock_market_activity',
    'store_dataset_entry',
    'ingest_run',
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
)
COUNT_SQL: Final = {
    'stock_market_activity': 'SELECT count(*) FROM stock_market_activity',
    'store_dataset_entry': 'SELECT count(*) FROM store_dataset_entry',
    'ingest_run': 'SELECT count(*) FROM ingest_run',
    'dataset_read': 'SELECT count(*) FROM dataset_read',
    'dataset_read_daily': 'SELECT count(*) FROM dataset_read_daily',
    'dataset_override_log': 'SELECT count(*) FROM dataset_override_log',
    'dataset_deletion': 'SELECT count(*) FROM dataset_deletion',
}
RESET_SQL: Final = (
    'TRUNCATE TABLE stock_market_activity, store_dataset_entry, ingest_run, dataset_read, dataset_read_daily, '
    'dataset_override_log, dataset_deletion RESTART IDENTITY'
)
# A TRUNCATE waits for every lock on the tables; fail fast instead of hanging behind a long reader.
LOCK_TIMEOUT_SQL: Final = "SET LOCAL lock_timeout = '10s'"
RUNNING_RUNS_SQL: Final = "SELECT count(*) FROM ingest_run WHERE state = 'RUNNING'"
# The store flushes buffered dataset reads every USAGE_FLUSH_INTERVAL_SECONDS (default 5; server/data/store/app/
# usage_recorder.py). After the reset commits, wait two intervals so a read buffered before it has been flushed (and
# so would show), then recount. A named constant: the stack's own setting is not visible from the generator.
# THIS MIRRORS DEFAULT_FLUSH_INTERVAL_SECONDS in server/data/store/app/usage_recorder.py (line 62) and MUST BE KEPT
# IN STEP with it by hand: nothing checks it, and a stack run with USAGE_FLUSH_INTERVAL_SECONDS set differs.
USAGE_FLUSH_INTERVAL_SECONDS: Final = 5
QUIESCE_WAIT_SECONDS: Final = 2 * USAGE_FLUSH_INTERVAL_SECONDS
NOT_CLEAN_MESSAGE: Final = 'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack'


class DevDatasetsError(Exception):
    """A refusal or a stop. The message is written for the operator and never holds a secret."""


class Secret:
    """A credential whose repr and str are masked. reveal() is the only way to the value."""

    __slots__ = ('_value',)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return 'Secret(***)'

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


def credential_state(secret: Secret) -> str:
    """The state of a credential, without its value: 'set', 'EMPTY' or 'PLACEHOLDER'."""
    value = secret.reveal()
    if not value:
        return 'EMPTY'
    if value.startswith('UNSET-') or value == PLACEHOLDER:
        return 'PLACEHOLDER'
    return 'set'


@dataclass(frozen=True)
class Environment:
    """The settings read from the process environment, once."""

    secret: Secret = field(repr=False)
    db_password: Secret = field(repr=False)
    store_url: str
    db_host: str
    db_user: str
    db_name: str


def load_environment(environ: Mapping[str, str]) -> Environment:
    """Read the settings; the credentials are wrapped at once.

    Args:
        environ: The process environment.

    Returns:
        Environment: The settings. Nothing here validates a credential; credential_state does.
    """
    return Environment(
        secret=Secret(environ.get(SECRET_VAR, '')),
        db_password=Secret(environ.get(DB_PASSWORD_VAR, '')),
        store_url=environ.get(STORE_URL_VAR, DEFAULT_STORE_URL).rstrip('/'),
        db_host=environ.get('DATABASE_NAME', 'db'),
        db_user=environ.get('POSTGRES_USER', 'postgres'),
        db_name=environ.get('POSTGRES_DB_NAME', 'postgres'),
    )


@dataclass(frozen=True)
class Degrade:
    """Why a dev-degraded row is damaged, and (usually) the whole sessions it loses.

    The sessions are local New York calendar days [first_day, last_day), half-open. Both are None for a row damaged
    by construction (its range starts before the vendor's history): nothing is deleted and there is nothing to repair.
    """

    first_day: str | None
    last_day: str | None
    why: str

    @property
    def drops_bars(self) -> bool:
        return self.first_day is not None


@dataclass(frozen=True)
class TestMapping:
    """A row's symbol on the test backend (the fake broker), and why. The default is ZZDS + the dev symbol."""

    __test__ = False  # not a pytest class

    symbol: str
    why: str


@dataclass(frozen=True)
class PlanRow:
    """One row of plan.json. start, end and expiry are ISO dates, D-relative strings or None."""

    id: str
    symbol: str
    granularity: str
    start: str
    end: str | None
    update_type: str
    expiry_type: str
    expiry: str | None
    owner: str
    degrade: Degrade | None
    reads: tuple[str, ...]
    purpose: str
    test: TestMapping | None = None
    collect: bool | None = None  # None: not sent, the server default (true) applies; subscriptions only

    def on_backend(self, label: str) -> 'PlanRow':
        """This row as the backend of a rail label sees it: the dev symbol on dev, the synthetic one on test."""
        if label != TEST_LABEL:
            return self
        return replace(self, symbol=self.test.symbol if self.test else f'{TEST_SYMBOL_MARK}{self.symbol}')

    @property
    def fails_first(self) -> bool:
        """True when the fake broker fails this row's first fetch (FAILONCE_): the failure is the point."""
        return self.symbol.startswith(FAILONCE_PREFIX)


def resolve_day(value: str, today: date) -> date:
    """A plan date as a calendar date: an ISO date, or 'D', 'D-8', 'D+2' relative to today.

    Args:
        value: The plan's string.
        today: D, the run's date.

    Returns:
        date: The calendar date.

    Raises:
        DevDatasetsError: When the string is neither form.
    """
    relative = _RELATIVE.match(value)
    if relative:
        sign, days = relative.groups()
        offset = int(days) if days else 0
        return today + timedelta(days=-offset if sign == '-' else offset)
    if _ISO_DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise DevDatasetsError(f'not a calendar date: {value!r}') from exc
    raise DevDatasetsError(f'not an ISO date or a D-relative date: {value!r}')


def utc_midnight(day: date) -> datetime:
    """The instant a UTC calendar day begins, aware."""
    return datetime.combine(day, time(0), tzinfo=UTC)


def market_midnight(day: date) -> datetime:
    """The instant a New York calendar day begins, aware. A whole session starts at or after it."""
    return datetime.combine(day, time(0), tzinfo=MARKET_TZ)


def wire_time(day: date) -> str:
    """A calendar day as the aware ISO-8601 UTC time the store requires."""
    return utc_midnight(day).strftime('%Y-%m-%dT%H:%M:%SZ')


def _require(condition: bool, row_id: str, message: str) -> None:
    if not condition:
        raise DevDatasetsError(f'plan row {row_id}: {message}')


def _parse_row(raw: Mapping[str, Any]) -> PlanRow:
    fields = ('id', 'symbol', 'granularity', 'start', 'end', 'update_type', 'expiry_type', 'expiry', 'owner')
    row_id = str(raw.get('id', '?'))
    missing = [name for name in (*fields, 'degrade', 'reads', 'purpose') if name not in raw]
    _require(not missing, row_id, f'missing {missing}')
    known = {*fields, 'degrade', 'reads', 'purpose', 'test', 'collect'}
    _require(set(raw) <= known, row_id, f'unknown fields {sorted(set(raw) - known)}')
    _require(raw['symbol'] in ALLOWED_SYMBOLS, row_id, 'symbol outside SPY, QQQ, DIA')
    _require(raw['granularity'] in GRANULARITY_DB_LABELS, row_id, 'unknown granularity')
    _require(raw['update_type'] in UPDATE_TYPES, row_id, 'update_type must be STATIC or DAILY')
    _require(raw['expiry_type'] in EXPIRY_TYPES, row_id, 'unknown expiry_type')
    _require(raw['owner'] != FORBIDDEN_OWNER and raw['owner'] in ALLOWED_OWNERS, row_id, 'owner is not allowed')
    _require(isinstance(raw['reads'], list) and set(raw['reads']) <= READ_KINDS, row_id, 'reads must be ui and sdk')
    if raw['update_type'] == 'DAILY':
        _require(raw['end'] is None, row_id, 'a DAILY row is open-ended: end must be null')
        _require(raw['expiry_type'] != 'BULK', row_id, 'a DAILY row may not use BULK')
    else:
        _require(raw['end'] is not None, row_id, 'a STATIC row needs an end')
    if 'collect' in raw:
        _require(isinstance(raw['collect'], bool), row_id, 'collect is a boolean when present')
        _require(raw['update_type'] == 'DAILY', row_id, 'collect is for a subscription: the store refuses it on STATIC')
    degrade = None
    if raw['degrade'] is not None:
        _require(raw['owner'] == DEGRADED_OWNER, row_id, f'only {DEGRADED_OWNER} rows may carry a degrade')
        days = raw['degrade'].get('drop_days')
        why = str(raw['degrade'].get('why', ''))
        _require(bool(why.strip()), row_id, 'a degrade must say why')
        if days is None:
            degrade = Degrade(None, None, why)
        else:
            _require(isinstance(days, list) and len(days) == 2, row_id, 'degrade.drop_days is [from, to] or null')
            degrade = Degrade(days[0], days[1], why)
    else:
        _require(raw['owner'] != DEGRADED_OWNER, row_id, f'a {DEGRADED_OWNER} row must name its degrade')
    test = _parse_test(raw, row_id)
    return PlanRow(
        id=raw['id'],
        symbol=raw['symbol'],
        granularity=raw['granularity'],
        start=raw['start'],
        end=raw['end'],
        update_type=raw['update_type'],
        expiry_type=raw['expiry_type'],
        expiry=raw['expiry'],
        owner=raw['owner'],
        degrade=degrade,
        reads=tuple(raw['reads']),
        purpose=str(raw['purpose']),
        test=test,
        collect=raw.get('collect'),
    )


def _parse_test(raw: Mapping[str, Any], row_id: str) -> TestMapping | None:
    """The optional test object: a synthetic symbol (never a bare real ticker) and the reason for it."""
    if raw.get('test') is None:
        return None
    given = raw['test']
    _require(isinstance(given, dict) and set(given) == {'symbol', 'why'}, row_id, 'test is {symbol, why}')
    prefixes = '|'.join(re.escape(prefix) for prefix in TEST_SCENARIO_PREFIXES)
    shape = rf'(?:{prefixes})?{TEST_SYMBOL_MARK}{raw["symbol"]}'
    _require(
        isinstance(given['symbol'], str) and re.fullmatch(shape, given['symbol']) is not None,
        row_id,
        f'test.symbol must be an optional scenario prefix, {TEST_SYMBOL_MARK}, then the symbol',
    )
    _require(isinstance(given['why'], str) and bool(given['why'].strip()), row_id, 'test.why must say why')
    return TestMapping(given['symbol'], given['why'])


def load_plan(path: Path = PLAN_PATH) -> list[PlanRow]:
    """Read and validate plan.json.

    Args:
        path: The plan file.

    Returns:
        list[PlanRow]: The rows, in file order.

    Raises:
        DevDatasetsError: When a row breaks the schema, or two rows share an id.
    """
    rows = [_parse_row(raw) for raw in json.loads(path.read_text(encoding='utf-8'))]
    ids = [row.id for row in rows]
    if len(set(ids)) != len(ids):
        raise DevDatasetsError('plan: duplicate row ids')
    return rows


def select_rows(rows: Sequence[PlanRow], wanted: Sequence[str]) -> list[PlanRow]:
    """The rows named by --row, or all of them."""
    if not wanted:
        return list(rows)
    unknown = sorted(set(wanted) - {row.id for row in rows})
    if unknown:
        raise DevDatasetsError(f'no such plan row: {", ".join(unknown)}')
    return [row for row in rows if row.id in wanted]


def render_body(row: PlanRow, today: date, now: datetime) -> dict[str, Any]:
    """The POST body for a row: no feed, aware UTC times, end null for a DAILY row.

    Args:
        row: The plan row.
        today: D.
        now: The instant a range must end before, less END_MARGIN.

    Returns:
        dict[str, Any]: A StoreAssetDatasetBody as JSON.

    Raises:
        DevDatasetsError: When the range is empty or ends too close to now.
    """
    start = resolve_day(row.start, today)
    end = resolve_day(row.end, today) if row.end is not None else None
    if end is not None:
        if end <= start:
            raise DevDatasetsError(f'row {row.id}: end {end} is not after start {start}')
        if utc_midnight(end) > now - END_MARGIN:
            raise DevDatasetsError(f'row {row.id}: the range must end in the past, but it ends {end}')
    body = {
        'owner': row.owner,
        'source': SOURCE,
        'granularity': row.granularity,
        'start': wire_time(start),
        'end': wire_time(end) if end is not None else None,
        'update_type': row.update_type,
        'expiry_type': row.expiry_type,
        'expiry': wire_time(resolve_day(row.expiry, today)) if row.expiry is not None else None,
    }
    if row.collect is not None:
        body['collect'] = row.collect
    return body


def degrade_window(degrade: Degrade, today: date) -> tuple[datetime, datetime]:
    """[from, to) as New York midnights, so only whole sessions fall inside.

    Raises:
        DevDatasetsError: When the window is empty.
    """
    if degrade.first_day is None or degrade.last_day is None:
        raise DevDatasetsError('this degrade deletes nothing: it has no window')
    first = resolve_day(degrade.first_day, today)
    last = resolve_day(degrade.last_day, today)
    if last <= first:
        raise DevDatasetsError(f'degrade window {first}..{last} is empty')
    return market_midnight(first), market_midnight(last)


@dataclass(frozen=True)
class Response:
    """An HTTP answer: the status and the decoded JSON body (None when there is none)."""

    status: int
    body: Any


class HttpClient:
    """The real transport: urllib, no proxy, no redirect, secret only in a request header."""

    def __init__(self, base_url: str):
        if urllib.parse.urlsplit(base_url).scheme not in ('http', 'https'):
            raise DevDatasetsError('the store URL must be http or https')
        self._base_url = base_url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        timeout: int = READ_TIMEOUT_SECONDS,
    ) -> Response:
        """Send one request.

        Raises:
            DevDatasetsError: When the call fails below HTTP; the message is the exception's type name only.
        """
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._base_url + path, data=data, method=method)
        for name, value in {**(headers or {}), **({'Content-Type': 'application/json'} if body else {})}.items():
            request.add_header(name, value)
        try:
            with self._opener.open(request, timeout=timeout) as answer:  # nosec B310 -- scheme checked in __init__
                return Response(answer.status, _decode(answer.read()))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _decode(exc.read()))
        except Exception as exc:
            raise DevDatasetsError(f'the request failed below HTTP ({type(exc).__name__})') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the secret header to wherever it points; an answer is the answer."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


class Reporter:
    """The one place output leaves the tool."""

    def __init__(self, write: Callable[[str], object]):
        self._write = write

    def line(self, text: str) -> None:
        self._write(text)


def _reason(response: Response) -> str:
    """A refusal's typed reason and error id; never the detail, which can hold ids and values."""
    body = response.body if isinstance(response.body, dict) else {}
    return f'reason={body.get("reason")} error_id={body.get("error_id")}'


def _reason_code(response: Response) -> str | None:
    body = response.body if isinstance(response.body, dict) else {}
    reason = body.get('reason')
    return reason if isinstance(reason, str) else None


def check_rail(http: HttpClient, out: Reporter) -> str:
    """The rail: refuse unless GET /ui/v1/config says deploymentLabel is 'dev' or 'test'.

    Returns:
        str: The label, which picks the backend: DEV_LABEL (real Alpaca) or TEST_LABEL (the fake broker).

    Raises:
        DevDatasetsError: When the label is anything else (an empty one, prod's, included), or the config cannot be
            read. Nothing has been sent to the database or the dataset routes by then.
    """
    answer = http.request('GET', '/ui/v1/config')
    label = answer.body.get('deploymentLabel') if isinstance(answer.body, dict) else None
    if answer.status != 200 or label not in RAIL_LABELS:
        shown = label if answer.status == 200 and isinstance(label, str) and label else 'none'
        raise DevDatasetsError(
            f'refused: this is not a dev or test stack (HTTP {answer.status}, deploymentLabel {shown!r})'
        )
    out.line(f'rail: deploymentLabel is {label!r} ({"real Alpaca" if label == DEV_LABEL else "fake broker"} backend)')
    return label


def _instant(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def find_entry(http: HttpClient, row: PlanRow, body: Mapping[str, Any]) -> dict[str, Any] | None:
    """The store entry with this row's exact identity, or None. Resolved through the store, never guessed.

    Raises:
        DevDatasetsError: When the lookup is refused.
    """
    query = urllib.parse.urlencode(
        {
            'owner': row.owner,
            'source': SOURCE,
            'granularity': row.granularity,
            'update_type': row.update_type,
            'expiry_type': row.expiry_type,
        }
    )
    answer = http.request('GET', f'{STORE_PATH}/{row.symbol}?{query}')
    if answer.status != 200 or not isinstance(answer.body, list):
        raise DevDatasetsError(f'{row.id}: the dataset lookup failed (HTTP {answer.status})')
    want_start, want_end = _instant(body['start']), _instant(body['end'])
    for entry in answer.body:
        if _instant(entry.get('start')) == want_start and _instant(entry.get('end')) == want_end:
            return entry
    return None


def _post(http: HttpClient, secret: Secret, row: PlanRow, body: Mapping[str, Any]) -> Response:
    value = secret.reveal()
    if not _HEADER_SAFE.match(value):
        raise DevDatasetsError('the instance secret holds characters a header cannot carry')
    return http.request(
        'POST', f'{STORE_PATH}/{row.symbol}', headers={SECRET_HEADER: value}, body=body, timeout=SEED_TIMEOUT_SECONDS
    )


def _report_post(out: Reporter, row: PlanRow, answer: Response, *, tolerate_overlap: bool) -> None:
    """One line for the answer; raise to stop the run when it is not usable."""
    if 200 <= answer.status < 300:
        points = answer.body.get('data_points') if isinstance(answer.body, dict) else None
        out.line(f'{row.id} {answer.status} data_points={points}')
        return
    if answer.status in WAIT_STATUSES:
        raise DevDatasetsError(
            f'{row.id} {answer.status} {_reason(answer)}: the store or the vendor is busy; wait, then rerun'
        )
    body = answer.body if isinstance(answer.body, dict) else {}
    if tolerate_overlap and answer.status == 409 and body.get('reason') == OWN_OVERLAP_REASON:
        out.line(f'{row.id} 409 already there ({OWN_OVERLAP_REASON})')
        return
    raise DevDatasetsError(f'{row.id} {answer.status} {_reason(answer)}: stopped')


def run_seed(
    http: HttpClient, env: Environment, rows: Sequence[PlanRow], bodies: Mapping[str, dict], out: Reporter
) -> None:
    """POST each row. A present row is skipped, so a rerun never rewrites an expiry; a 409 overlap is 'already there'."""
    for row in rows:
        if find_entry(http, row, bodies[row.id]) is not None:
            out.line(f'{row.id} present (not re-requested)')
            continue
        answer = _post(http, env.secret, row, bodies[row.id])
        if row.fails_first and answer.status == FAILONCE_STATUS and _reason_code(answer) == FAILONCE_REASON:
            # The fake broker fails this symbol's first fetch on purpose: a FAILED run, no entry, no bars.
            out.line(
                f'{row.id} {answer.status} {_reason(answer)} failed once as designed (FAILONCE_): '
                f'run repair --row {row.id} to repeat it'
            )
            continue
        _report_post(out, row, answer, tolerate_overlap=True)


def run_repair(
    http: HttpClient, env: Environment, rows: Sequence[PlanRow], bodies: Mapping[str, dict], out: Reporter
) -> None:
    """Re-POST each degraded row's identical seed body: an exact repeat refetches the whole declared range.

    A row damaged by construction (no window: its range starts before the vendor's history) is left alone.
    """
    for row in rows:
        if row.degrade is None or not row.degrade.drops_bars:
            continue
        _report_post(out, row, _post(http, env.secret, row, bodies[row.id]), tolerate_overlap=False)


def run_reads(http: HttpClient, rows: Sequence[PlanRow], bodies: Mapping[str, dict], out: Reporter) -> None:
    """One UI read (X-Client-Id operator, no cursor) and/or one SDK read per row that asks for it."""
    for row in rows:
        if not row.reads:
            continue
        entry = find_entry(http, row, bodies[row.id])
        if entry is None or not entry.get('id'):
            out.line(f'{row.id} missing (seed it first)')
            continue
        dataset_id = entry['id']
        if 'ui' in row.reads:
            answer = http.request(
                'GET',
                f'/ui/v1/datasets/{dataset_id}/bars?limit={READ_PAGE_LIMIT}',
                headers={CLIENT_ID_HEADER: UI_READER},
            )
            bars = len(answer.body.get('bars', [])) if isinstance(answer.body, dict) else None
            out.line(f'{row.id} ui {answer.status} bars={bars}')
        if 'sdk' in row.reads:
            params = {'dataset_id': dataset_id, 'asset_symbol': row.symbol, 'granularity': row.granularity}
            params['start'] = bodies[row.id]['start']
            if bodies[row.id]['end'] is not None:
                params['end'] = bodies[row.id]['end']
            answer = http.request(
                'GET', f'{ASSET_DATA_PATH}?{urllib.parse.urlencode(params)}', headers={CLIENT_ID_HEADER: SDK_READER}
            )
            bars = len(answer.body) if isinstance(answer.body, list) else None
            out.line(f'{row.id} sdk {answer.status} bars={bars}')


def _connect(env: Environment) -> Any:
    """Open the dev postgres. The password is revealed here and nowhere else."""
    import psycopg2  # imported only by the one mode that needs a driver

    return psycopg2.connect(
        host=env.db_host,
        port=5432,
        user=env.db_user,
        dbname=env.db_name,
        password=env.db_password.reveal(),
        connect_timeout=10,
    )


def check_database_stack(connection: Any, label: str) -> None:
    """The database rail: refuse unless this server's cluster_name came from its command line and is the store's label.

    The one statement run on a fresh connection, before anything else touches it. Fails closed.

    Raises:
        DevDatasetsError: Unless exactly one pg_settings row came back with source 'command line' and a value of 'dev'
            or 'test' equal to label. The connection is rolled back and closed first, and only the value and the
            source are named.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(STACK_SQL)
            rows = cursor.fetchall()
    except Exception as exc:
        connection.close()
        raise DevDatasetsError(f'the database identity check failed ({type(exc).__name__})') from None
    stack, source = rows[0] if len(rows) == 1 else (None, None)
    if source == STACK_SOURCE and stack in RAIL_LABELS and stack == label:
        return
    connection.rollback()
    connection.close()
    shown = stack[:40] if isinstance(stack, str) and stack else 'none'
    shown_source = source[:40] if isinstance(source, str) and source else 'none'
    raise DevDatasetsError(
        f'refused: the database is not this dev or test stack ({STACK_SETTING} {shown!r} from source {shown_source!r}, '
        f'the store says {label!r}); nothing was changed'
    )


def checked_connect(connect: Callable[[], Any], label: str) -> Callable[[], Any]:
    """connect, but every connection it returns has passed check_database_stack(label) first."""

    def open_checked() -> Any:
        connection = connect()
        check_database_stack(connection, label)
        return connection

    return open_checked


def run_degrade(connect: Callable[[], Any], rows: Sequence[PlanRow], today: date, out: Reporter) -> None:
    """Delete whole sessions of bars from the plan's dev-degraded datasets, in ONE transaction.

    Every row is checked first; a refusal rolls the whole transaction back. Only store_dataset_entry is read and
    only stock_market_activity is written.

    Raises:
        DevDatasetsError: On a row that is not dev-degraded, a lookup that finds other than one entry, or a
            database failure (reported by type name only).
    """
    targets = []
    for row in rows:
        if row.degrade is None or not row.degrade.drops_bars:
            continue
        if row.owner != DEGRADED_OWNER:
            raise DevDatasetsError(f'{row.id}: refused, the owner is not {DEGRADED_OWNER}')
        if row.fails_first:
            out.line(f'{row.id} skipped: its first fetch fails by design (FAILONCE_), so it holds no bars to delete')
            continue
        targets.append(row)
    if not targets:
        return
    connection = _open(connect)
    try:
        with connection.cursor() as cursor:
            for row in targets:
                first, last = degrade_window(row.degrade, today)
                cursor.execute(LOOKUP_SQL, (row.owner, row.symbol, GRANULARITY_DB_LABELS[row.granularity]))
                found = cursor.fetchall()
                if len(found) != 1:
                    raise DevDatasetsError(f'{row.id}: refused, expected exactly one entry and found {len(found)}')
                dataset_id = found[0][0]
                cursor.execute(DELETE_SQL, (dataset_id, first, last, dataset_id, DEGRADED_OWNER))
                out.line(f'{row.id} deleted={cursor.rowcount} window={first.isoformat()}..{last.isoformat()}')
        connection.commit()
    except DevDatasetsError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        raise DevDatasetsError(f'the degrade transaction failed and was rolled back ({type(exc).__name__})') from None
    finally:
        connection.close()


def _open(connect: Callable[[], Any]) -> Any:
    try:
        return connect()
    except DevDatasetsError:
        raise
    except Exception as exc:
        raise DevDatasetsError(f'the database connection failed ({type(exc).__name__})') from None


def _count_rows(cursor: Any) -> dict[str, int]:
    counts = {}
    for table in RESET_TABLES:
        cursor.execute(COUNT_SQL[table])
        counts[table] = int(cursor.fetchone()[0])
    return counts


def _show_counts(out: Reporter, heading: str, counts: Mapping[str, int]) -> None:
    out.line(f'{heading}:')
    for table in RESET_TABLES:
        out.line(f'  {table}: {counts[table]}')


def count_reset_rows(connect: Callable[[], Any]) -> dict[str, int]:
    """Row counts of the seven reset tables, read only. Writes nothing.

    Raises:
        DevDatasetsError: On a database failure (reported by type name only).
    """
    connection = _open(connect)
    try:
        with connection.cursor() as cursor:
            return _count_rows(cursor)
    except Exception as exc:
        raise DevDatasetsError(f'counting the reset tables failed ({type(exc).__name__})') from None
    finally:
        connection.rollback()
        connection.close()


def _recount_after(connect: Callable[[], Any]) -> tuple[int, int]:
    """(ingest_run rows, dataset_read rows) now, read only, on a fresh connection."""
    connection = _open(connect)
    try:
        with connection.cursor() as cursor:
            counts = _count_rows(cursor)
        return counts['ingest_run'], counts['dataset_read']
    except Exception as exc:
        raise DevDatasetsError(f'the post-reset recount failed ({type(exc).__name__})') from None
    finally:
        connection.rollback()
        connection.close()


def run_reset(connect: Callable[[], Any], out: Reporter, sleep: Callable[[float], object] = real_sleep) -> None:
    """Empty exactly the seven RESET_TABLES in ONE transaction. The caller has checked the rail and --yes-reset.

    Refuses, changing nothing, while any ingest_run row is RUNNING (counted in the same transaction, before the
    TRUNCATE). The counts before are printed first; the counts after are read inside the same transaction and printed
    once it has committed. Then it waits QUIESCE_WAIT_SECONDS (two usage flush intervals) and recounts ingest_run and
    dataset_read: any row means work in flight wrote after the reset (NOT CLEAN). The guarantee is clean at commit,
    verified after one flush interval. Any failure rolls the whole transaction back, so nothing is half-removed.
    alembic_version, every other table and the schema are not touched. This is a deliberate environment reset and is
    not recorded anywhere (the audit tables are append-only by convention only; nothing in the database stops this).

    Raises:
        DevDatasetsError: On a database failure (reported by type name only), a RUNNING run, or NOT CLEAN.
    """
    connection = _open(connect)
    running = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(LOCK_TIMEOUT_SQL)
            cursor.execute(RUNNING_RUNS_SQL)
            running = int(cursor.fetchone()[0])
            if running:
                connection.rollback()
            else:
                before = _count_rows(cursor)
                _show_counts(out, 'reset: rows before', before)
                cursor.execute(RESET_SQL)
                after = _count_rows(cursor)
        if not running:
            connection.commit()
    except Exception as exc:
        connection.rollback()
        raise DevDatasetsError(f'the reset transaction failed and was rolled back ({type(exc).__name__})') from None
    finally:
        connection.close()
    if running:
        raise DevDatasetsError(
            f'refused: {running} ingest run(s) are RUNNING; nothing was changed. Wait for them, then reset a quiet stack'
        )
    _show_counts(out, 'reset: rows after', after)
    out.line(f'reset: waiting {QUIESCE_WAIT_SECONDS}s (two usage flush intervals) to verify the stack stayed quiet')
    sleep(QUIESCE_WAIT_SECONDS)
    runs, reads = _recount_after(connect)
    if runs or reads:
        raise DevDatasetsError(f'{NOT_CLEAN_MESSAGE} (ingest_run {runs}, dataset_read {reads})')
    out.line('reset: verified clean (ingest_run 0, dataset_read 0)')


def explain_reset(counts: Mapping[str, int], out: Reporter) -> None:
    """What a reset would remove: the counts, the tables, and what stays."""
    _show_counts(out, 'a reset would remove these rows', counts)
    out.line('kept: alembic_version, the schema and every other table')


def dry_run(mode: str, rows: Sequence[PlanRow], bodies: Mapping[str, dict], today: date, out: Reporter) -> None:
    """Say what would be sent or deleted. Makes no write and never shows a credential."""
    out.line(f'dry run: {mode}; nothing is sent')
    for row in rows:
        if mode in ('seed', 'repair'):
            if mode == 'repair' and (row.degrade is None or not row.degrade.drops_bars):
                continue
            out.line(f'{row.id} POST {STORE_PATH}/{row.symbol} header {SECRET_HEADER}: <from {SECRET_VAR}>')
            out.line(json.dumps(bodies[row.id], sort_keys=True))
        elif mode == 'reads':
            for kind in row.reads:
                target = 'bars page, no cursor' if kind == 'ui' else ASSET_DATA_PATH
                reader = UI_READER if kind == 'ui' else SDK_READER
                out.line(f'{row.id} GET {target} header {CLIENT_ID_HEADER}: {reader}')
        elif mode == 'degrade' and row.degrade is not None and row.degrade.drops_bars:
            first, last = degrade_window(row.degrade, today)
            out.line(f'{row.id} owner={row.owner} {row.symbol} {row.granularity}: {row.degrade.why}')
            out.line(f'  {LOOKUP_SQL}   -- one entry exactly')
            out.line(f'  {DELETE_SQL}   -- window {first.isoformat()} .. {last.isoformat()}')


def _dry_run_reset(
    env: Environment,
    mode: str,
    rows: Sequence[PlanRow],
    bodies: Mapping[str, dict],
    today: date,
    open_db: Callable[[], Any],
    out: Reporter,
) -> int:
    """A dry run of seed or reset, after the rail: the counts a reset would remove (read only) and the bodies."""
    out.line(f'dry run: {mode} would reset first; nothing is changed')
    if credential_state(env.db_password) == 'set':
        explain_reset(count_reset_rows(open_db), out)
    else:
        out.line('reset counts unavailable: the database password is not set')
    if mode == 'seed':
        dry_run(mode, rows, bodies, today, out)
    return 0


def run_check(env: Environment, http: HttpClient | None, out: Reporter) -> None:
    """Report the environment and the stack, without a value. Reads only."""
    out.line(f'secret: {credential_state(env.secret)}')
    out.line(f'db password: {credential_state(env.db_password)}')
    if http is None:
        return
    ping = http.request('GET', '/ping')
    out.line(f'ping: HTTP {ping.status}')
    config = http.request('GET', '/ui/v1/config')
    label = config.body.get('deploymentLabel') if isinstance(config.body, dict) else None
    out.line(f'config: HTTP {config.status} deploymentLabel={label if isinstance(label, str) and label else "none"}')


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='python -m tools.dev_datasets.request', description=__doc__.split('\n\n')[0])
    parser.add_argument(
        'mode', nargs='?', choices=['seed', 'reset', 'reads', 'degrade', 'repair', 'check'], default=None
    )
    for name in ('reset', 'reads', 'degrade', 'repair', 'check'):
        parser.add_argument(f'--{name}', dest='flag_mode', action='append_const', const=name, help=f'same as {name}')
    parser.add_argument('--yes-reset', action='store_true', help='REQUIRED to reset: without it nothing is changed')
    parser.add_argument('--no-reset', action='store_true', help='seed without the reset (incremental use)')
    parser.add_argument('--dry-run', action='store_true', help='print what would be sent or deleted; change nothing')
    parser.add_argument('--date', help='D, the run date (YYYY-MM-DD); default today in UTC')
    parser.add_argument('--row', action='append', default=[], help='limit to a plan row id; repeatable')
    return parser.parse_args(argv)


def _resolve_mode(arguments: argparse.Namespace) -> str:
    chosen = [*(arguments.flag_mode or []), *([arguments.mode] if arguments.mode else [])]
    if len(set(chosen)) > 1:
        raise DevDatasetsError(f'one mode at a time, not {", ".join(sorted(set(chosen)))}')
    return chosen[0] if chosen else 'seed'


def _usage_error(arguments: argparse.Namespace, mode: str) -> str | None:
    """The reason the reset flags contradict the mode, or None."""
    if arguments.yes_reset and arguments.no_reset:
        return '--yes-reset and --no-reset contradict each other'
    if arguments.no_reset and mode != 'seed':
        return '--no-reset only means something for seed'
    if arguments.yes_reset and mode not in ('seed', 'reset'):
        return '--yes-reset only means something for seed and reset'
    return None


def _check_credentials(env: Environment, mode: str, resets: bool) -> None:
    """Stop on a placeholder or empty credential the mode needs."""
    states = {credential_state(env.secret), credential_state(env.db_password)}
    if 'PLACEHOLDER' in states:
        raise DevDatasetsError(
            'a credential holds the placeholder: start the agent with make agent-up from the repo root'
        )
    if mode in ('seed', 'repair') and not env.secret:
        raise DevDatasetsError(f'{SECRET_VAR} is empty')
    if (mode in ('degrade', 'reset') or resets) and not env.db_password:
        raise DevDatasetsError(f'{DB_PASSWORD_VAR} is empty')


def run(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    out: Reporter,
    http: HttpClient | None = None,
    connect: Callable[[], Any] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], object] = real_sleep,
    plan_path: Path = PLAN_PATH,
) -> int:
    """Run one mode. Returns the exit status: 0 done, 1 refused or stopped, 2 a usage error.

    seed resets first (RESET_TABLES, one transaction) and then loads the plan, unless --no-reset. A reset needs
    --yes-reset: without it the counts that would go are printed, nothing changes and the status is 1.

    Args:
        argv: The command line after the program name.
        environ: The process environment.
        out: Where output goes.
        http: The transport; the real one when None. check --dry-run and the dry runs of reads, degrade and repair
            never build one; a dry run of seed or reset asks the rail (a read) to learn the backend.
        connect: Opens the database; the real driver when None.
        now: The clock; the real one when None.
        sleep: The wait after a reset (QUIESCE_WAIT_SECONDS); the real one by default.
        plan_path: The plan file.

    Returns:
        int: The exit status.
    """
    try:
        arguments = parse_arguments(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        mode = _resolve_mode(arguments)
        problem = _usage_error(arguments, mode)
        if problem:
            out.line(problem)
            return 2
        env = load_environment(environ)
        moment = now or datetime.now(UTC)
        try:
            today = date.fromisoformat(arguments.date) if arguments.date else moment.date()
        except ValueError:
            out.line('--date must be YYYY-MM-DD')
            return 2
        rows = select_rows(load_plan(plan_path), arguments.row)
        if mode == 'check':
            run_check(env, None if arguments.dry_run else http or HttpClient(env.store_url), out)
            return 0
        bodies = {row.id: render_body(row, today, moment) for row in rows}
        resets = mode == 'reset' or (mode == 'seed' and not arguments.no_reset)
        if resets and arguments.row:
            raise DevDatasetsError('--row limits the plan but the reset is not row-bound: add --no-reset')
        if arguments.dry_run and not resets:
            dry_run(mode, rows, bodies, today, out)
            return 0
        if not arguments.dry_run:
            _check_credentials(env, mode, resets)
        client = http or HttpClient(env.store_url)
        label = check_rail(client, out)
        rows = [row.on_backend(label) for row in rows]
        open_db = checked_connect(connect or (lambda: _connect(env)), label)
        if resets:
            if arguments.dry_run:
                return _dry_run_reset(env, mode, rows, bodies, today, open_db, out)
            if not arguments.yes_reset:
                explain_reset(count_reset_rows(open_db), out)
                hint = ' or --no-reset to keep the data' if mode == 'seed' else ''
                out.line(f'refused: nothing was changed. {mode} resets first; pass --yes-reset to confirm{hint}')
                return 1
            run_reset(open_db, out, sleep)
        if mode == 'seed':
            run_seed(client, env, rows, bodies, out)
        elif mode == 'repair':
            run_repair(client, env, rows, bodies, out)
        elif mode == 'reads':
            run_reads(client, rows, bodies, out)
        elif mode == 'degrade':
            run_degrade(open_db, rows, today, out)
        return 0
    except DevDatasetsError as exc:
        out.line(str(exc))
        return 1
    except Exception as exc:
        # Type name only: a library may quote what it was given.
        out.line(f'failed ({type(exc).__name__})')
        return 1


def main() -> int:
    return run(sys.argv[1:], os.environ, out=Reporter(print))


if __name__ == '__main__':
    sys.exit(main())
