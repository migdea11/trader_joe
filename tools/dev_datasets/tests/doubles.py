"""Test doubles for tools/dev_datasets/request.py: a scripted HTTP transport and a scripted database (validator).

tj-grna9p.148: the database double also answers the reset's count SELECTs, TRUNCATE and SET LOCAL, and records the
transaction boundaries (events); the store double can answer as a TEST stack, filing rows under synthetic symbols.
tj-grna9p.150: it answers the reset's RUNNING count too, and its sleep() stands in for the wait after a reset, during
which work in flight may write (written_while_waiting); run_tool injects that wait into every run.

Nothing here reaches a network or a database. Every credential is a fake with a distinctive canary inside it, so a
leak shows up as a substring anywhere in the output, whatever the formatting around it.
"""

import io
import json
import re
import urllib.parse
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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

# The seven tables a reset empties, written out here from the owner's ruling (tj-grna9p.148), not read from the tool.
SEVEN_TABLES = (
    'stock_market_activity',
    'store_dataset_entry',
    'ingest_run',
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
)
# Distinct non-zero counts, so a count printed against the wrong table shows.
TABLE_COUNTS = dict(zip(SEVEN_TABLES, (5000, 14, 31, 22, 9, 3, 2), strict=True))
_COUNT = re.compile(r'^SELECT count\(\*\) FROM ([a-z_]+)$', re.IGNORECASE)
# tj-grna9p.150 (5a): the reset's RUNNING count, written out from the design (the store writes the enum's value,
# 'RUNNING', into ingest_run.state; test_reset.py checks that against the store model's source).
RUNNING_COUNT_TEXT = "SELECT count(*) FROM ingest_run WHERE state = 'RUNNING'"
# tj-grna9p.150 (5b): the wait after a reset is two usage flush intervals of 5 s each.
QUIESCE_SECONDS = 10


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
        if self.db.execute_error is not None and (self.db.fail_on is None or self.db.fail_on in sql):
            raise self.db.execute_error
        text = ' '.join(sql.split())
        count = _COUNT.match(text)
        if text == RUNNING_COUNT_TEXT:
            self._result = [(self.db.running,)]
        elif count:
            table = count.group(1)
            self._result = [(0 if self.db.truncated else self.db.counts.get(table, 0),)]
        elif text.upper().startswith('TRUNCATE'):
            self.db.truncated = True
            self._result = []
        elif text.upper().startswith('SET '):
            self._result = []
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
    # ingest_run rows in state RUNNING, as the reset's first count sees them (tj-grna9p.150 (5a)).
    running: int = 0
    # Rows that work in flight writes while the tool waits after a reset (tj-grna9p.150 (5b)): table -> count, applied
    # by sleep(). None: the stack stays quiet.
    written_while_waiting: dict[str, int] | None = None
    # Every wait the tool asked for, in seconds; never a real one.
    sleeps: list[float] = field(default_factory=list)
    autocommit: bool = False
    connects: int = 0
    commits: int = 0
    rollbacks: int = 0
    closes: int = 0

    @classmethod
    def with_plan_entries(cls, label: str = 'dev') -> 'FakeDb':
        """One entry for each dev-degraded row of the plan, as the real store would hold it on that backend."""
        lookups = {}
        for row in tool.load_plan():
            if row.owner == tool.DEGRADED_OWNER:
                symbol = TESTMODE_SYMBOLS[row.id] if label == 'test' else row.symbol
                key = (row.owner, symbol, tool.GRANULARITY_DB_LABELS[row.granularity])
                lookups[key] = [(dataset_id(row.id),)]
        return cls(lookups=lookups)

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
        if self.truncated:
            self.counts = dict.fromkeys(self.counts, 0)
            self.truncated = False

    def rollback(self):
        self.rollbacks += 1
        self.events.append(('rollback',))
        self.truncated = False

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

    @property
    def text(self) -> str:
        return '\n'.join([*self.lines, self.stdout, self.stderr])


def run_tool(argv, environ=None, *, http=None, connect=None, now=NOW, plan_path=None, sleep=None) -> Outcome:
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
    return Outcome(status, lines, out.getvalue(), err.getvalue(), sleeps)


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
