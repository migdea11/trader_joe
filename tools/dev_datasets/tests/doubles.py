"""Test doubles for tools/dev_datasets/request.py: a scripted HTTP transport and a scripted database (validator).

tj-grna9p.148: the database double also answers the reset's count SELECTs, TRUNCATE and SET LOCAL, and records the
transaction boundaries (events); the store double can answer as a TEST stack, filing rows under synthetic symbols.
tj-grna9p.150: it answers the reset's RUNNING count too, and its sleep() stands in for the wait after a reset, during
which work in flight may write (written_while_waiting); run_tool injects that wait into every run.
tj-grna9p.194: it answers the database rail's identity query, the pg_settings row of cluster_name as (setting, source):
stack and source by default, identity_rows to answer any rows at all (none, two). It records the query in statements
and events like any other statement, so a test sees what ran before and after it.
tj-grna9p.160: it accepts the reset's LOCK TABLE (answering nothing); writes() keeps it, so a lock taken by a run that
must change nothing shows.
tj-7zjov1.51: it answers the open-request count (PENDING or QUEUED ingest_request rows) from request_states, for
exactly the state literals the statement names.
tj-3kplhf (tj-7zjov1.293): eighteen tables (EIGHTEEN_TABLES), every table the store models declare.
tj-7zjov1.295: the export's reads (alembic_version, information_schema columns, the dry run's estimates) and the two
psycopg2 cursor calls it makes, mogrify and copy_expert; run_tool takes the seed root, so no test writes to output/.
tj-7zjov1.297: the RELOAD (decision tj-7zjov1.291 D1, D3). It answers COPY <table> FROM STDIN (reading the file the tool
streams, counting its rows into the transaction's uncommitted counts: a commit keeps them, a rollback drops them), the
setval of stock_market_activity.id (the uncommitted sequence, set to max(id) of the rows loaded) and the serial-column
query (serial_columns). The tables' uncommitted state is `pending`; TRUNCATE ... RESTART IDENTITY zeroes the tables it
names there and the sequence. run_tool ALWAYS passes a seed root: a fresh directory under SEED_BASE (set per test by
conftest.py to the test's tmp_path), so no test writes under output/ and two runs at the fixed clock never collide.

Nothing here reaches a network or a database. Every credential is a fake with a distinctive canary inside it, so a
leak shows up as a substring anywhere in the output, whatever the formatting around it.
"""

import io
import json
import re
import tempfile
import urllib.parse
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.dev_datasets import dev_seed
from tools.dev_datasets import request as tool


# The fakes. Each carries a canary that appears nowhere else in the repository.
SECRET = 'dummy-not-a-secret-SECRETCANARY-q7x9'  # nosec B105 -- a fake, by name
DB_PASSWORD = 'dummy-not-a-secret-DBPASSCANARY-k3z8'  # nosec B105 -- a fake, by name
CANARIES = ('SECRETCANARY', 'DBPASSCANARY', SECRET, DB_PASSWORD)

# A fixed clock: Tuesday 2026-10-20, 15:00 UTC. D is its UTC date unless --date says otherwise.
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
D = '2026-10-20'

GOOD_ENV = {
    'INSTANCE_WRITE_SECRET': SECRET,
    'POSTGRES_PASS': DB_PASSWORD,
    'DEV_DATASETS_STORE_URL': 'http://data_store.invalid',
}

# The modes that pass the rail before they write. reset is one too, but it is driven separately (test_reset.py),
# because it needs --yes-reset and writes only to the database.
WRITING_MODES = ('seed', 'reads', 'degrade', 'repair')
ALL_MODES = (*WRITING_MODES, 'check')
# seed resets first unless --no-reset; the tests of the plan's POST loop pass it, the reset's own tests do not.
NO_RESET = '--no-reset'

DEV_CONFIG = tool.Response(200, {'deploymentLabel': 'dev'})
TEST_CONFIG = tool.Response(200, {'deploymentLabel': 'test'})

# The eighteen tables a reset empties, written out here from the design, not read from the tool: the seven of the
# owner's ruling (tj-grna9p.148), every store data table then; tj-7zjov1.51's two request-record tables after
# ingest_run; and tj-3kplhf's nine (owner ruling 2026-11-03, no dev guard): the asset-list, corporate-action and panel
# tables, after dataset_deletion.
EIGHTEEN_TABLES = (
    'stock_market_activity',
    'store_dataset_entry',
    'ingest_run',
    'ingest_request',
    'ingest_instance',
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
    'asset_list_snapshot',
    'asset_list_row',
    'asset_list_event',
    'corporate_action',
    'panel',
    'panel_settings_version',
    'panel_source_settings_version',
    'panel_pass',
    'panel_member_transition',
)
# Distinct non-zero counts, so a count printed against the wrong table shows.
TABLE_COUNTS = dict(
    zip(EIGHTEEN_TABLES, (5000, 14, 31, 17, 4, 22, 9, 3, 2, 41, 1200, 57, 8, 6, 12, 13, 27, 19), strict=True)
)
# tj-7zjov1.297: what a dev export keeps of TABLE_COUNTS, distinct per table and fewer than the table holds; the five
# tables O1 and O3 wipe keep none, the three whole panel tables (every row) keep all. seeded_copy_data() is the COPY text a
# FakeDb streams for them, so a preserving reset reloads exactly these counts.
SEED_ROWS = dict(zip(EIGHTEEN_TABLES, (3, 2, 5, 4, 0, 0, 0, 0, 0, 7, 11, 9, 1, 6, 12, 13, 15, 10), strict=True))
# The bar ids the seed keeps, not contiguous and not in order, so max(id) is not the count or the last row.
SEED_BAR_IDS = (4001, 4100, 4007)


def seeded_copy_data() -> dict[str, bytes]:
    data = {}
    for table, rows in SEED_ROWS.items():
        if table == 'stock_market_activity':
            data[table] = b''.join(f'{bar}\t{table}\n'.encode() for bar in SEED_BAR_IDS)
        else:
            data[table] = b''.join(f'{table}-{n}\tv\\tx\n'.encode() for n in range(rows))
    assert data['stock_market_activity'].count(b'\n') == SEED_ROWS['stock_market_activity']
    return data


_COUNT = re.compile(r'^SELECT count\(\*\) FROM ([a-z_]+)$', re.IGNORECASE)
_TRUNCATED = re.compile(r'^TRUNCATE TABLE (?P<tables>[a-z_, ]+) RESTART IDENTITY$', re.IGNORECASE)
# tj-grna9p.150 (5a): the reset's RUNNING count, written out from the design (the store writes the enum's value,
# 'RUNNING', into ingest_run.state; test_reset.py checks that against the store model's source).
RUNNING_COUNT_TEXT = "SELECT count(*) FROM ingest_run WHERE state = 'RUNNING'"
# tj-7zjov1.51 (2): the open-request count, right after the RUNNING count, written out from the design: the requests
# ingest may still answer (PENDING) or deliver (QUEUED). NOT_DELIVERED and the finished states do not block.
OPEN_REQUEST_STATES = ('PENDING', 'QUEUED')
OPEN_REQUESTS_TEXT = "SELECT count(*) FROM ingest_request WHERE state IN ('PENDING', 'QUEUED')"
# Any count of ingest_request rows by a state list: the fake answers it from FakeDb.request_states for the literals the
# statement itself names, so a statement that drops a state really lets that state through.
_STATE_COUNT = re.compile(
    r"^SELECT count\(\*\) FROM ingest_request WHERE state IN \((?P<states>'[A-Z_]+'(?:, '[A-Z_]+')*)\)$"
)
# tj-grna9p.194, THE DATABASE RAIL (architect's option 3): the identity query every connection runs first, written out
# from the design. The built-in cluster_name, which the dev and test compose files put on the postgres command line;
# pg_settings lists it with its source, and only 'command line' proves the server was started that way.
STACK_NAME = 'cluster_name'
STACK_TEXT = "SELECT setting, source FROM pg_settings WHERE name = 'cluster_name'"
COMMAND_LINE = 'command line'
# tj-grna9p.150 (5b): the wait after a reset is two usage flush intervals of 5 s each.
QUIESCE_SECONDS = 10
# tj-7zjov1.295, THE EXPORT's reads, written out from the design (decision tj-7zjov1.291 D3): the revision from
# alembic_version, a table's columns from information_schema in ordinal order, and the dry run's count-and-size wrap.
ALEMBIC_TEXT = 'SELECT version_num FROM alembic_version'
COLUMNS_TEXT = (
    'SELECT column_name, data_type, udt_name FROM information_schema.columns '
    "WHERE table_schema = 'public' AND table_name = %(table)s ORDER BY ordinal_position"
)
ESTIMATE_PREFIX = 'SELECT count(*), coalesce(sum(pg_column_size(k)), 0) FROM ('
DEFAULT_COLUMNS = [('id', 'uuid', 'uuid')]
# tj-7zjov1.297, THE RELOAD's statements, written out from the design (D1 and R3): one COPY FROM STDIN per table, the
# setval of the one serial (stock_market_activity.id) to max(id), true only when a row was loaded, and the query that
# proves the live schema has no other serial or identity column.
LOAD_TEXT = re.compile(r'^COPY (?P<table>[a-z_]+) FROM STDIN$')
SETVAL_TEXT = (
    "SELECT setval(pg_get_serial_sequence('stock_market_activity', 'id'), "
    'coalesce((SELECT max(id) FROM stock_market_activity), 1), '
    '(SELECT count(*) > 0 FROM stock_market_activity))'
)
SERIAL_TEXT = (
    'SELECT table_name, column_name FROM information_schema.columns '
    "WHERE table_schema = 'public' AND table_name = ANY(%(tables)s) "
    "AND (column_default LIKE 'nextval(%%' OR is_identity = 'YES') ORDER BY table_name, column_name"
)
SESSION_TEXTS = ("SET LOCAL TimeZone = 'UTC'", 'SET LOCAL extra_float_digits = 1')
# The seed root run_tool hands the tool: conftest.py points it at each test's tmp_path. None outside a test.
SEED_BASE: Path | None = None


def fresh_seed_root() -> Path:
    """A new, empty directory under SEED_BASE, for one run's export."""
    assert SEED_BASE is not None, 'SEED_BASE is unset: conftest.py sets it for every test'
    return Path(tempfile.mkdtemp(prefix='seed-', dir=SEED_BASE))


def _ids(data: bytes) -> list[int]:
    """The integer first column of COPY text rows (the bar ids a reload of stock_market_activity carries)."""
    return [int(line.split(b'\t', 1)[0]) for line in data.splitlines() if line]


def _table_of_trim(sql: str) -> str:
    """The table whose trim statement this is: the double knows the statements only by identity, never by parsing."""
    for table, statement in dev_seed.TRIM_SQL.items():
        if statement == sql:
            return table
    raise AssertionError(f'not a trim statement: {sql[:80]}')


def _table_of_estimate(sql: str) -> str:
    for table, statement in dev_seed.ESTIMATE_SQL.items():
        if statement == sql:
            return table
    raise AssertionError(f'not an estimate statement: {sql[:80]}')


@dataclass
class Call:
    """One request the tool made."""

    method: str
    path: str
    headers: dict[str, str]
    body: Any
    timeout: int

    @property
    def route(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    @property
    def query(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query, keep_blank_values=True))


Handler = Callable[[Call], tool.Response]


def dataset_id(row_id: str) -> str:
    """A stable fake dataset id for a plan row."""
    number = int(row_id[1:])
    return f'00000000-0000-4000-8000-{number:012d}'


def entry_for(body: dict, row_id: str, **overrides: Any) -> dict:
    """A GET /store entry that matches a rendered body, as the store would answer it."""
    entry = {
        'id': dataset_id(row_id),
        'owner': body['owner'],
        'source': 'ALPACA',
        'granularity': body['granularity'],
        'start': body['start'].replace('Z', '+00:00'),
        'end': body['end'].replace('Z', '+00:00') if body['end'] else None,
        'expiry_type': body['expiry_type'],
        'update_type': body['update_type'],
        'expiry': body['expiry'],
    }
    entry.update(overrides)
    return entry


@dataclass
class FakeStore:
    """A scripted store. Every call is recorded; answers come from the fields below.

    present: row ids the lookup finds (with the body's exact range). post_answers: row id -> Response for its POST
    (default 200). label: what GET /ui/v1/config answers.
    """

    bodies: dict[str, dict] = field(default_factory=dict)
    present: set[str] = field(default_factory=set)
    lookup_extra: dict[str, list[dict]] = field(default_factory=dict)
    post_answers: dict[str, tool.Response] = field(default_factory=dict)
    lookup_status: int = 200
    config: tool.Response = field(default_factory=lambda: DEV_CONFIG)
    calls: list[Call] = field(default_factory=list)
    # row id -> the symbol this store files the row under: the dev symbols unless a test-backend store says so.
    symbols: dict[str, str] | None = None

    def request(self, method, path, *, headers=None, body=None, timeout=tool.READ_TIMEOUT_SECONDS):
        call = Call(method, path, dict(headers or {}), body, timeout)
        self.calls.append(call)
        return self._answer(call)

    def _row_for(self, call: Call) -> str | None:
        symbol = call.route.rsplit('/', 1)[-1]
        for row_id, body in self.bodies.items():
            if (
                symbol == (self.symbols[row_id] if self.symbols else _symbol_of(row_id))
                and call.query.get('granularity', body['granularity']) == body['granularity']
            ):
                if call.method == 'POST' and call.body != body:
                    continue
                if call.method == 'GET' and call.query.get('owner') != body['owner']:
                    continue
                return row_id
        return None

    def _answer(self, call: Call) -> tool.Response:
        if call.route == '/ui/v1/config':
            return self.config
        if call.route == '/ping':
            return tool.Response(200, {'ping': 'pong'})
        if call.method == 'GET' and call.route.startswith(tool.STORE_PATH + '/'):
            if self.lookup_status != 200:
                return tool.Response(self.lookup_status, {'reason': 'DATABASE_UNAVAILABLE', 'error_id': 'e1'})
            row_id = self._row_for(call)
            found = list(self.lookup_extra.get(row_id, []))
            if row_id in self.present:
                found.append(entry_for(self.bodies[row_id], row_id))
            return tool.Response(200, found)
        if call.method == 'POST' and call.route.startswith(tool.STORE_PATH + '/'):
            row_id = self._row_for(call)
            return self.post_answers.get(row_id, tool.Response(200, {'message': 'ok', 'data_points': 42}))
        if call.route.startswith('/ui/v1/datasets/'):
            return tool.Response(200, {'bars': [{}, {}, {}]})
        if call.route == tool.ASSET_DATA_PATH:
            return tool.Response(200, [{}, {}])
        return tool.Response(404, {'reason': 'NOT_FOUND', 'error_id': 'e404'})

    # Views over the record.
    def posts(self) -> list[Call]:
        return [c for c in self.calls if c.method == 'POST']

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != 'GET']


_PLAN_SYMBOLS: dict[str, str] = {}


def _symbol_of(row_id: str) -> str:
    if not _PLAN_SYMBOLS:
        _PLAN_SYMBOLS.update({row.id: row.symbol for row in tool.load_plan()})
    return _PLAN_SYMBOLS[row_id]


class ExplodingHttp:
    """A transport that fails the test on any call: for --dry-run, check --dry-run and every refusal."""

    def __init__(self):
        self.calls: list[tuple] = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError(f'the tool made a network call it must not make: {args}')


def exploding_connect():
    raise AssertionError('the tool opened a database connection it must not open')


@dataclass
class FakeCursor:
    db: 'FakeDb'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.db.statements.append((sql, tuple(params)))
        self.db.events.append(('execute', sql))
        text = ' '.join(sql.split())
        if text == STACK_TEXT:
            # The identity query fails only when told to by name (identity_error, or fail_on naming it), so the
            # failure tests of the work statements still reach the statement they target.
            if self.db.identity_error is not None:
                raise self.db.identity_error
            if self.db.execute_error is not None and self.db.fail_on is not None and self.db.fail_on in sql:
                raise self.db.execute_error
            rows = self.db.identity_rows
            self._result = list(rows) if rows is not None else [(self.db.stack, self.db.source)]
            return
        if self.db.execute_error is not None and (self.db.fail_on is None or self.db.fail_on in sql):
            raise self.db.execute_error
        count = _COUNT.match(text)
        state_count = _STATE_COUNT.match(text)
        if text == RUNNING_COUNT_TEXT:
            self._result = [(self.db.running,)]
        elif state_count:
            named = re.findall(r"'([A-Z_]+)'", state_count.group('states'))
            emptied = 'ingest_request' in self.db.truncated_tables
            self._result = [(0 if emptied else sum(self.db.request_states.get(s, 0) for s in named),)]
        elif count:
            self._result = [(self.db.current(count.group(1)),)]
        elif text.upper().startswith('TRUNCATE'):
            # Only the tables the statement names are emptied, so a table dropped from it keeps its rows.
            self.db.truncated = True
            self.db.truncated_tables = set(_TRUNCATED.match(text).group('tables').replace(' ', '').split(','))
            self.db.pending.update(dict.fromkeys(self.db.truncated_tables, 0))
            for table in self.db.truncated_tables:
                self.db.loaded.pop(table, None)
            if 'stock_market_activity' in self.db.truncated_tables:
                # RESTART IDENTITY: the sequence starts again at 1, not yet called.
                self.db.pending_sequence = (1, False)
            self._result = []
        elif text == SETVAL_TEXT:
            ids = _ids(self.db.loaded.get('stock_market_activity', b''))
            value = (max(ids), True) if ids else (1, False)
            self.db.pending_sequence = value
            self.db.setvals.append(value)
            self._result = [(value[0],)]
        elif text == SERIAL_TEXT:
            self.db.serial_queries.append(dict(params))
            self._result = list(self.db.serial_columns)
        elif text.upper().startswith(('SET ', 'LOCK TABLE ')):
            self._result = []
        elif text == ALEMBIC_TEXT:
            self._result = list(self.db.alembic_rows) if self.db.alembic_rows is not None else [(self.db.revision,)]
        elif text == COLUMNS_TEXT:
            table = params['table']
            self._result = list(self.db.columns.get(table, DEFAULT_COLUMNS))
        elif text.startswith(ESTIMATE_PREFIX):
            table = _table_of_estimate(sql)
            self._result = [self.db.estimates.get(table, (0, 0))]
        elif text.upper().startswith('SELECT'):
            owner, symbol, granularity = params
            self._result = list(self.db.lookups.get((owner, symbol, granularity), []))
            self.rowcount = len(self._result)
        else:
            self.rowcount = self.db.delete_counts.get(params[0], 7)
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]

    # tj-7zjov1.295: the export's two psycopg2 cursor calls. mogrify binds the cutoffs into a trim statement (here as
    # quoted ISO literals, recorded with the parameters); copy_expert streams the table's scripted COPY text into the
    # sink, or raises copy_error for the table named by copy_error_on.
    def mogrify(self, sql, params):
        self.db.mogrified.append((sql, dict(params)))
        self.db.events.append(('mogrify', sql))
        self._mogrified_from = sql
        text = sql
        for name, value in params.items():
            text = text.replace(f'%({name})s', f"'{value.isoformat()}'::timestamptz")
        return text.encode('utf-8')

    def copy_expert(self, sql, sink):
        load = LOAD_TEXT.match(' '.join(sql.split()))
        if load:
            self._copy_in(load.group('table'), sink)
            return
        table = _table_of_trim(self._mogrified_from)
        self.db.copies.append((table, sql))
        self.db.events.append(('copy', table))
        if self.db.copy_error is not None and self.db.copy_error_on == table:
            raise self.db.copy_error
        data = self.db.copy_data.get(table, b'')
        # In two writes when there is more than one row, as a driver's buffer may split it.
        middle = len(data) // 2
        for chunk in (data[:middle], data[middle:]):
            if chunk:
                sink.write(chunk)

    # tj-7zjov1.297: COPY <table> FROM STDIN. The server reads the tool's source to its end, in small reads as psycopg2
    # does; before the first read, before_load(table) may change the file on disk (a file edited after verification).
    # The rows read become the table's uncommitted count, less load_shortfall (a server that loaded fewer rows).
    def _copy_in(self, table, source):
        self.db.events.append(('copy_in', table))
        if self.db.before_load is not None:
            self.db.before_load(table)
        if self.db.load_error is not None and self.db.load_error_on == table:
            raise self.db.load_error
        chunks = []
        while chunk := source.read(8192):
            chunks.append(chunk)
        data = b''.join(chunks)
        self.db.loads.append((table, data))
        self.db.loaded[table] = self.db.loaded.get(table, b'') + data
        rows = data.count(b'\n') - self.db.load_shortfall.get(table, 0)
        self.db.pending[table] = self.db.current(table) + rows


@dataclass
class FakeDb:
    """A scripted psycopg2 connection. lookups: (owner, symbol, db granularity) -> rows of (id,)."""

    lookups: dict[tuple, list[tuple]] = field(default_factory=dict)
    delete_counts: dict[str, int] = field(default_factory=dict)
    execute_error: Exception | None = None
    # When set, execute_error is raised only by a statement containing this text.
    fail_on: str | None = None
    connect_error: Exception | None = None
    statements: list[tuple[str, tuple]] = field(default_factory=list)
    # connect, execute, commit, rollback and close, in order: the transaction boundaries.
    events: list[tuple[str, ...]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: dict(TABLE_COUNTS))
    truncated: bool = False
    # The tables the uncommitted TRUNCATE named (tj-7zjov1.51): a commit empties exactly these, a rollback none.
    truncated_tables: set[str] = field(default_factory=set)
    # ingest_run rows in state RUNNING, as the reset's first count sees them (tj-grna9p.150 (5a)).
    running: int = 0
    # ingest_request rows by state (tj-7zjov1.51 (2)): state -> count. The open-request count sums the states its
    # statement names; a committed TRUNCATE empties them. The ingest_request entry of counts is a test's to keep in step.
    request_states: dict[str, int] = field(default_factory=dict)
    # Rows that work in flight writes while the tool waits after a reset (tj-grna9p.150 (5b)): table -> count, applied
    # by sleep(). None: the stack stays quiet.
    written_while_waiting: dict[str, int] | None = None
    # Every wait the tool asked for, in seconds; never a real one.
    sleeps: list[float] = field(default_factory=list)
    autocommit: bool = False
    # The pg_settings row of cluster_name on this server: setting 'dev' as the dev override sets it, 'test' as the fake
    # overlay does, '' from source 'default' on a server started without it (prod). identity_rows, when set, is the
    # whole answer instead (no row, two rows). identity_error: the query itself fails.
    stack: str | None = 'dev'
    source: str | None = COMMAND_LINE
    identity_rows: list[tuple] | None = None
    identity_error: Exception | None = None
    connects: int = 0
    commits: int = 0
    rollbacks: int = 0
    closes: int = 0
    # tj-7zjov1.295, the export. revision: what alembic_version holds (the checkout head by default); alembic_rows, when
    # set, is the whole answer instead (none, two). columns: table -> information_schema rows (name, data_type, udt).
    # copy_data: table -> the COPY text the server streams. estimates: table -> (rows, bytes) for a dry run.
    revision: str = field(default_factory=lambda: dev_seed.head_revision())
    alembic_rows: list[tuple] | None = None
    columns: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    copy_data: dict[str, bytes] = field(default_factory=dict)
    copy_error: Exception | None = None
    copy_error_on: str | None = None
    estimates: dict[str, tuple[int, int]] = field(default_factory=dict)
    mogrified: list[tuple[str, dict]] = field(default_factory=list)
    copies: list[tuple[str, str]] = field(default_factory=list)
    # tj-7zjov1.297, the reload. pending: the open transaction's table counts (a TRUNCATE zeroes the tables it names, a
    # COPY FROM adds the rows it read); commit keeps them, rollback drops them. loaded: the bytes COPY FROM read per table
    # in the open transaction; loads: every COPY FROM, (table, bytes), across transactions. sequence: the committed state
    # of stock_market_activity's id sequence, (value, is_called), None when no test has moved it; pending_sequence the
    # open transaction's. serial_columns: what the serial-column query answers (the design's one serial by default).
    pending: dict[str, int] = field(default_factory=dict)
    loaded: dict[str, bytes] = field(default_factory=dict)
    loads: list[tuple[str, bytes]] = field(default_factory=list)
    load_shortfall: dict[str, int] = field(default_factory=dict)
    load_error: Exception | None = None
    load_error_on: str | None = None
    before_load: Callable[[str], object] | None = None
    sequence: tuple[int, bool] | None = None
    pending_sequence: tuple[int, bool] | None = None
    setvals: list[tuple[int, bool]] = field(default_factory=list)
    serial_columns: list[tuple[str, str]] = field(default_factory=lambda: [('stock_market_activity', 'id')])
    serial_queries: list[dict] = field(default_factory=list)

    def current(self, table: str) -> int:
        """A table's row count as the open transaction sees it."""
        return self.pending[table] if table in self.pending else self.counts.get(table, 0)

    @classmethod
    def with_plan_entries(cls, label: str = 'dev') -> 'FakeDb':
        """One entry for each dev-degraded row of the plan, as the real store would hold it on that backend."""
        lookups = {}
        for row in tool.load_plan():
            if row.owner == tool.DEGRADED_OWNER:
                symbol = TESTMODE_SYMBOLS[row.id] if label == 'test' else row.symbol
                key = (row.owner, symbol, tool.GRANULARITY_DB_LABELS[row.granularity])
                lookups[key] = [(dataset_id(row.id),)]
        return cls(lookups=lookups, stack=label)

    def connect(self):
        self.connects += 1
        self.events.append(('connect',))
        if self.connect_error is not None:
            raise self.connect_error
        return self

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.events.append(('commit',))
        if self.truncated and 'ingest_request' in self.truncated_tables:
            self.request_states = {}
        self.counts = {table: self.current(table) for table in self.counts}
        if self.pending_sequence is not None:
            self.sequence = self.pending_sequence
        self._end()

    def _end(self):
        self.truncated = False
        self.truncated_tables = set()
        self.pending = {}
        self.loaded = {}
        self.pending_sequence = None

    def rollback(self):
        self.rollbacks += 1
        self.events.append(('rollback',))
        self._end()

    def close(self):
        self.closes += 1
        self.events.append(('close',))

    def sleep(self, seconds):
        """The injected wait: recorded in events, never slept; what work in flight writes lands here."""
        self.sleeps.append(seconds)
        self.events.append(('sleep', seconds))
        if self.written_while_waiting:
            self.counts.update(self.written_while_waiting)

    def sql(self) -> list[str]:
        """Every statement's text, whitespace collapsed."""
        return [' '.join(sql.split()) for sql, _ in self.statements]

    def per_connection(self) -> list[list[str]]:
        """The statements of each connection, in order: events split at every connect, whitespace collapsed."""
        groups: list[list[str]] = []
        for event in self.events:
            if event[0] == 'connect':
                groups.append([])
            elif event[0] == 'execute':
                groups[-1].append(' '.join(event[1].split()))
        return groups

    def writes(self) -> list[str]:
        """Every statement that is not a read or a session setting."""
        return [sql for sql in self.sql() if not re.match(r'^(SELECT|SET LOCAL lock_timeout)\b', sql, re.IGNORECASE)]


@dataclass
class Outcome:
    status: int
    lines: list[str]
    stdout: str
    stderr: str
    # Every wait the tool asked for (seconds), in order.
    sleeps: list[float] = field(default_factory=list)
    # The seed root the run was given (tj-7zjov1.297): where a dev reset's export landed.
    seed_root: Path | None = None

    @property
    def text(self) -> str:
        return '\n'.join([*self.lines, self.stdout, self.stderr])


def run_tool(
    argv, environ=None, *, http=None, connect=None, now=NOW, plan_path=None, sleep=None, seed_root=None
) -> Outcome:
    """Run the tool's entry point with every side channel captured.

    Output goes through a Reporter that records each line; stdout and stderr are captured too, so a print or an
    argparse message that bypasses the Reporter is still seen.

    The wait after a reset is ALWAYS injected (the tool's default is a real 10 s sleep): every wait is recorded in
    Outcome.sleeps, handed to the FakeDb behind connect when there is one (so work in flight can write during it), and
    then to sleep when a test passes its own.
    """
    lines: list[str] = []
    sleeps: list[float] = []
    out, err = io.StringIO(), io.StringIO()
    kwargs = {'plan_path': plan_path} if plan_path is not None else {}
    # Never the tool's default (output/dev_seed): a fresh directory per run unless the test names one.
    seed_root = fresh_seed_root() if seed_root is None else seed_root
    kwargs['seed_root'] = seed_root
    db = getattr(connect, '__self__', None)

    def injected_sleep(seconds):
        sleeps.append(seconds)
        if isinstance(db, FakeDb):
            db.sleep(seconds)
        if sleep is not None:
            sleep(seconds)

    with redirect_stdout(out), redirect_stderr(err):
        status = tool.run(
            list(argv),
            GOOD_ENV if environ is None else environ,
            out=tool.Reporter(lines.append),
            http=http,
            connect=connect,
            now=now,
            sleep=injected_sleep,
            **kwargs,
        )
    return Outcome(status, lines, out.getvalue(), err.getvalue(), sleeps, seed_root)


def rendered_bodies(day=None, now=NOW) -> dict[str, dict]:
    """The bodies the tool renders for every plan row on day D."""
    from datetime import date

    today = date.fromisoformat(day or D)
    return {row.id: tool.render_body(row, today, now) for row in tool.load_plan()}


def store_for(day=None, **kwargs) -> FakeStore:
    return FakeStore(bodies=rendered_bodies(day), **kwargs)


# Row id -> the symbol the TEST backend files it under, written out here from the design (tj-grna9p.148 (4)), not
# computed by the tool's on_backend: ZZDS + the dev symbol, and the two scenario rows.
TESTMODE_SYMBOLS = {
    'r1': 'ZZDSSPY',
    'r2': 'ZZDSSPY',
    'r3': 'ZZDSSPY',
    'r4': 'ZZDSSPY',
    'r5': 'ZZDSSPY',
    'r6': 'ZZDSSPY',
    'r7': 'ZZDSDIA',
    'r8': 'ZZDSQQQ',
    'r9': 'ZZDSQQQ',
    'r10': 'ZZDSDIA',
    'r11': 'ZZDSQQQ',
    'r12': 'ZZDSDIA',
    'r13': 'FAILONCE_ZZDSQQQ',
    'r14': 'GAPS_ZZDSDIA',
}


def fake_broker_store(day=None, **kwargs) -> FakeStore:
    """A store whose rail answers 'test' and which files every row under its synthetic symbol."""
    return FakeStore(bodies=rendered_bodies(day), config=TEST_CONFIG, symbols=dict(TESTMODE_SYMBOLS), **kwargs)


def assert_no_canary(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, f'a credential canary reached the output: {canary!r}'


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
