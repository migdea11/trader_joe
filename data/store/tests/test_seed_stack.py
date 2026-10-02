"""How the seed producer reaches the stack (data/store/seeds/stack.py): the environment, the driver, the POST.

WHY THIS FILE EXISTS (validator, tj-irhy0a.21; decision tj-vhboky.55 addendum S9). The producer runs
in test_client over the stack's networks, with no Docker access. Three safety properties live in
stack.py and nowhere else, so they are pinned here:
  * every setting comes from the environment, and a missing or empty one is NAMED, never its value;
  * INSTANCE_WRITE_SECRET is read at each POST and travels ONLY as the write-secret header -- never in
    a URL, a body, a message, a repr or anything printed;
  * a failed query names the step and the SQLSTATE only, never the driver's message (which can quote
    a row value before the rows are known to be synthetic), and a failed connect names the step and
    the exception class only (the driver's message can carry connection details).
Plus the negative: no subprocess, shell or docker import is left anywhere in data/store/seeds.

WHAT TIER THIS IS. Zero Docker, zero network: psycopg2.connect and httpx.post are replaced by
recording fakes through Stack's own injection seam. NOT PROVED HERE, and NOT RUN until the MCP sitting
tj-c4mosr.6: that psycopg2 accepts these connect arguments against the agent stack's Postgres, that
NORMALISE_SQL runs as one execute under autocommit, and that data_store accepts the header (tj-vhboky.65
runs the producer twice through the MCP).

The end-to-end pins (the whole producer through the real Stack, secret and password absent from
stdout, stderr and every SQL statement) are in test_seed_producer.py, which owns the scenario fake.
"""

import ast
from pathlib import Path

import httpx
import psycopg2
import pytest

from data.store.seeds import stack as stack_module
from data.store.seeds.manifest import SESSION_OPTIONS
from data.store.seeds.stack import (
    CONNECT_TIMEOUT_SECONDS,
    REPLY_BODY_LIMIT,
    REQUEST_TIMEOUT_SECONDS,
    Stack,
    StackError,
    StackSettings,
)


pytestmark = pytest.mark.data_store

SEEDS_DIR = Path(stack_module.__file__).resolve().parent

# Distinctive values, so an echo anywhere is found by a substring search.
PASSWORD = 'PWSENTINEL-9f3c'
SECRET = 'SECRETSENTINEL-41ad'
ROW_VALUE = 'ROWSENTINEL-AAPL-miguel'

ENV = {
    'DATABASE_NAME': 'store-db-host',
    'DATABASE_PORT': '5433',
    'POSTGRES_USER': 'seed-user',
    'POSTGRES_PASS': PASSWORD,
    'POSTGRES_DB_NAME': 'seed-db',
    'SYSTEM_TEST_DATA_STORE_URL': 'http://data-store:8000/',
    'INSTANCE_WRITE_SECRET': SECRET,
}
SENTINELS = (PASSWORD, SECRET)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False
        self.rows: list = []

    def execute(self, sql):
        self.connection.executed.append((sql, self.connection.autocommit))
        if self.connection.fail_with is not None:
            raise self.connection.fail_with
        self.rows = list(self.connection.rows)

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=(), fail_with=None):
        self.rows = rows
        self.fail_with = fail_with
        self.autocommit = False
        self.executed: list[tuple[str, bool]] = []
        self.cursors: list[FakeCursor] = []
        self.closed = 0

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed += 1


class FakeConnect:
    """Stands in for psycopg2.connect: records each call's keyword arguments."""

    def __init__(self, connection=None, fail_with=None):
        self.connection = connection or FakeConnection()
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        assert not args, 'every connect setting is passed by keyword'
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.connection


class FakeResponse:
    def __init__(self, status_code=200, text='{"data_points": 3}'):
        self.status_code = status_code
        self.text = text


class FakePost:
    """Stands in for httpx.post: records each call."""

    def __init__(self, response=None, fail_with=None):
        self.response = response or FakeResponse()
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({'args': args, **kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        return self.response


def _stack(environ=None, connect=None, http_post=None) -> Stack:
    return Stack(dict(ENV) if environ is None else environ, connect or FakeConnect(), http_post or FakePost())


def _assert_no_sentinel(text: str) -> None:
    for value in (*SENTINELS, ROW_VALUE):
        assert value not in text, value


class PgError(psycopg2.Error):
    """A driver error carrying a SQLSTATE (pgcode is read-only on psycopg2.Error itself)."""

    pgcode = '23505'


class PgErrorNoCode(psycopg2.Error):
    pgcode = None


# ---------------------------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------------------------


def test_every_setting_is_read_from_the_environment():
    settings = StackSettings.from_environment(ENV)
    assert settings == StackSettings(
        host='store-db-host',
        port=5433,
        user='seed-user',
        database='seed-db',
        store_url='http://data-store:8000',
        password=PASSWORD,
    )


def test_the_settings_repr_does_not_show_the_password():
    assert PASSWORD not in repr(StackSettings.from_environment(ENV))


@pytest.mark.parametrize('name', sorted(ENV))
@pytest.mark.parametrize('value', [None, ''], ids=['missing', 'empty'])
def test_a_missing_or_empty_variable_is_named_without_any_value(name, value):
    environ = dict(ENV)
    if value is None:
        del environ[name]
    else:
        environ[name] = value
    connect, post = FakeConnect(), FakePost()
    with pytest.raises(StackError) as raised:
        Stack(environ, connect, post)
    assert str(raised.value) == f'the environment variable {name} is missing or empty'
    for other in ENV.values():
        assert other not in str(raised.value)
    assert connect.calls == [] and post.calls == []


@pytest.mark.parametrize('port', ['54x2', '-5432', '+5432', '5432 ', '5.4', chr(0x0661) + chr(0x0662), chr(0x00B2)])
def test_a_port_that_is_not_an_ascii_integer_is_named_without_its_value(port):
    with pytest.raises(StackError) as raised:
        _stack({**ENV, 'DATABASE_PORT': port})
    assert str(raised.value) == 'the environment variable DATABASE_PORT is not an integer'


def test_without_an_environment_the_process_environment_is_read(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    connect = FakeConnect()
    Stack(connect=connect, http_post=FakePost()).query('SELECT 1;', 'probe')
    assert connect.calls[0]['host'] == 'store-db-host'


def test_the_default_transport_is_the_driver_and_httpx():
    """No subprocess and no docker: the injected defaults are psycopg2.connect and httpx.post."""
    defaults = Stack.__init__.__defaults__
    assert defaults == (None, psycopg2.connect, httpx.post)


# ---------------------------------------------------------------------------------------------
# SQL through the driver
# ---------------------------------------------------------------------------------------------


def test_one_connection_at_the_session_options_with_autocommit():
    connection = FakeConnection(rows=[(1,)])
    connect = FakeConnect(connection)
    stack = _stack(connect=connect)
    stack.query('SELECT 1;', 'a')
    stack.script('SELECT 2;', 'b')
    stack.query('SELECT 3;', 'c')
    assert connect.calls == [
        {
            'host': 'store-db-host',
            'port': 5433,
            'user': 'seed-user',
            'password': PASSWORD,
            'dbname': 'seed-db',
            'options': SESSION_OPTIONS,
            'connect_timeout': CONNECT_TIMEOUT_SECONDS,
        }
    ]
    # autocommit is on before the first statement: NORMALISE_SQL carries its own BEGIN and COMMIT.
    assert connection.executed == [('SELECT 1;', True), ('SELECT 2;', True), ('SELECT 3;', True)]


def test_the_session_options_are_the_digests_settings():
    assert SESSION_OPTIONS == '-c TimeZone=UTC -c extra_float_digits=1'


def test_nothing_connects_until_the_first_statement():
    connect = FakeConnect()
    _stack(connect=connect)
    assert connect.calls == []


def test_a_query_returns_rows_as_tuples_and_closes_its_cursor():
    connection = FakeConnection(rows=[['a', 1], ['b', 2]])
    stack = _stack(connect=FakeConnect(connection))
    assert stack.query('SELECT x, y;', 'probe') == [('a', 1), ('b', 2)]
    assert all(cursor.closed for cursor in connection.cursors)


def test_a_script_is_one_execute_and_returns_nothing():
    connection = FakeConnection()
    script = 'BEGIN;\nSELECT 1;\nCOMMIT;\n'
    assert _stack(connect=FakeConnect(connection)).script(script, 'normalise') is None
    assert connection.executed == [(script, True)]
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize('method', ['query', 'script'])
@pytest.mark.parametrize(('error', 'code'), [(PgError, '23505'), (PgErrorNoCode, 'unknown')])
def test_a_failed_statement_names_the_step_and_sqlstate_only(method, error, code):
    """The driver's message can quote a row value; it never reaches StackError, not even as a cause."""
    connection = FakeConnection(fail_with=error(f'duplicate key: Key (asset_symbol)=({ROW_VALUE}) password={PASSWORD}'))
    with pytest.raises(StackError) as raised:
        getattr(_stack(connect=FakeConnect(connection)), method)('SELECT 1;', 'synthetic check')
    assert str(raised.value) == f'synthetic check failed (SQLSTATE {code})'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert all(cursor.closed for cursor in connection.cursors)


def test_a_failed_connect_names_the_step_and_exception_class_only():
    failure = psycopg2.OperationalError(f'connection to "store-db-host" failed: password {PASSWORD} rejected')
    with pytest.raises(StackError) as raised:
        _stack(connect=FakeConnect(fail_with=failure)).query('SELECT 1;', 'alembic_version read')
    assert str(raised.value) == 'alembic_version read: could not connect (OperationalError)'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


class CursorFails(FakeConnection):
    """A connection whose cursor() raises, as psycopg2 does on a connection the server dropped."""

    def cursor(self):
        raise psycopg2.InterfaceError(f'connection already closed; password={PASSWORD} row {ROW_VALUE}')


class AutocommitFails(FakeConnection):
    """A connection that refuses autocommit (psycopg2 raises inside a transaction or on a dead link)."""

    def __setattr__(self, name, value):
        if name == 'autocommit' and value is True:
            raise psycopg2.ProgrammingError(f'set_session cannot be used; password={PASSWORD}')
        super().__setattr__(name, value)


@pytest.mark.parametrize('method', ['query', 'script'])
def test_a_failed_cursor_is_a_stack_error_naming_the_step_and_class_only(method):
    """tj-irhy0a.23 N4: cursor() is inside the try, so the driver's message never escapes."""
    with pytest.raises(StackError) as raised:
        getattr(_stack(connect=FakeConnect(CursorFails())), method)('SELECT 1;', 'synthetic check')
    assert str(raised.value) == 'synthetic check: could not open a cursor (InterfaceError)'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    _assert_no_sentinel(str(raised.value))


@pytest.mark.parametrize('method', ['query', 'script'])
def test_a_failed_autocommit_closes_the_new_connection_and_names_the_step_and_class_only(method):
    """tj-irhy0a.23 N4: the connection is closed before the StackError, and is not kept for reuse."""
    connection = AutocommitFails()
    connect = FakeConnect(connection)
    stack = _stack(connect=connect)
    with pytest.raises(StackError) as raised:
        getattr(stack, method)('SELECT 1;', 'alembic_version read')
    assert str(raised.value) == 'alembic_version read: could not set autocommit (ProgrammingError)'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    _assert_no_sentinel(str(raised.value))
    assert connection.closed == 1, 'the connection is closed before raising'
    assert connection.executed == [] and connection.cursors == [], 'nothing runs without autocommit'
    stack.close()
    assert connection.closed == 1, 'a connection that failed setup is not the stack connection'
    with pytest.raises(StackError):
        stack.query('SELECT 1;', 'retry')
    assert len(connect.calls) == 2, 'the next statement connects afresh'


class AutocommitAndCloseFail(AutocommitFails):
    """Refuses autocommit, and then close() raises too, carrying a driver message."""

    def close(self):
        super().close()
        raise psycopg2.InterfaceError(f'connection already closed; password={PASSWORD} row {ROW_VALUE}')


@pytest.mark.parametrize('method', ['query', 'script'])
def test_a_failing_close_after_a_failed_autocommit_still_raises_the_stack_error(method):
    """tj-irhy0a.23 F3: a raising close() never replaces the StackError.

    close() in the autocommit-failure path is suppressed, so its exception and driver message
    never escape.
    """
    connection = AutocommitAndCloseFail()
    with pytest.raises(StackError) as raised:
        getattr(_stack(connect=FakeConnect(connection)), method)('SELECT 1;', 'alembic_version read')
    assert str(raised.value) == 'alembic_version read: could not set autocommit (ProgrammingError)'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    _assert_no_sentinel(str(raised.value))
    assert connection.closed == 1, 'close was attempted'


def test_close_closes_the_connection_once_and_the_context_manager_calls_it():
    connection = FakeConnection()
    with _stack(connect=FakeConnect(connection)) as stack:
        stack.query('SELECT 1;', 'probe')
    assert connection.closed == 1
    stack.close()
    assert connection.closed == 1


def test_closing_an_unopened_stack_is_harmless():
    _stack().close()


# ---------------------------------------------------------------------------------------------
# The POST and the secret
# ---------------------------------------------------------------------------------------------


def test_the_post_goes_to_the_store_url_with_the_secret_as_the_header_only():
    post = FakePost(FakeResponse(201, '{"data_points": 4}'))
    reply = _stack(http_post=post).post('/store/stock/market-activity/ZZSEEDAA', {'owner': 'seed-owner-a'}, 'X-H')
    assert reply == {'status': 201, 'body': '{"data_points": 4}'}
    assert post.calls == [
        {
            'args': ('http://data-store:8000/store/stock/market-activity/ZZSEEDAA',),
            'json': {'owner': 'seed-owner-a'},
            'headers': {'X-H': SECRET},
            'timeout': REQUEST_TIMEOUT_SECONDS,
        }
    ]
    assert REQUEST_TIMEOUT_SECONDS == 120


def test_the_secret_appears_in_no_part_of_the_call_but_the_header_value():
    post = FakePost()
    _stack(http_post=post).post('/p', {'a': 'b'}, 'X-H')
    (call,) = post.calls
    headers = call.pop('headers')
    assert headers == {'X-H': SECRET}
    assert SECRET not in repr(call)


def test_the_secret_is_read_at_each_post():
    environ = dict(ENV)
    post = FakePost()
    stack = _stack(environ, http_post=post)
    environ['INSTANCE_WRITE_SECRET'] = 'rotated'
    stack.post('/p', {}, 'X-H')
    assert post.calls[0]['headers'] == {'X-H': 'rotated'}


def test_the_secret_is_not_held_by_the_stack_or_its_settings():
    stack = _stack()
    for name, value in vars(stack).items():
        if name.endswith('__environ'):
            continue  # the environment itself, read at each call
        assert SECRET not in repr(value), name


def test_a_secret_gone_by_the_post_is_named_and_nothing_is_sent():
    environ = dict(ENV)
    post = FakePost()
    stack = _stack(environ, http_post=post)
    del environ['INSTANCE_WRITE_SECRET']
    with pytest.raises(StackError, match=r'^the environment variable INSTANCE_WRITE_SECRET is missing or empty$'):
        stack.post('/p', {}, 'X-H')
    assert post.calls == []


@pytest.mark.parametrize(
    'failure',
    [
        httpx.ConnectError(f'cannot reach data-store with {SECRET}'),
        httpx.ReadTimeout(f'timed out; headers X-H: {SECRET}'),
        ValueError(f'{PASSWORD} {ROW_VALUE}'),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_failed_post_names_the_path_and_exception_class_only(failure):
    with pytest.raises(StackError) as raised:
        _stack(http_post=FakePost(fail_with=failure)).post('/p', {}, 'X-H')
    assert str(raised.value) == f'POST /p failed ({type(failure).__name__})'
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_reply_body_is_truncated():
    post = FakePost(FakeResponse(200, 'x' * (REPLY_BODY_LIMIT + 50)))
    assert _stack(http_post=post).post('/p', {}, 'X-H')['body'] == 'x' * REPLY_BODY_LIMIT
    assert REPLY_BODY_LIMIT == 400


# ---------------------------------------------------------------------------------------------
# No process, shell or docker anywhere in the producer
# ---------------------------------------------------------------------------------------------


FORBIDDEN_MODULES = {'subprocess', 'shlex', 'pty', 'docker', 'multiprocessing', 'asyncio.subprocess'}
FORBIDDEN_OS_CALLS = {'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'posix_spawnp'}


def _seed_sources() -> list[Path]:
    sources = sorted(SEEDS_DIR.glob('*.py'))
    assert {path.name for path in sources} >= {'stack.py', 'dump.py', 'producer.py', 'bundle.py', '__main__.py'}
    return sources


@pytest.mark.parametrize('path', _seed_sources(), ids=lambda path: path.name)
def test_no_seed_module_imports_a_process_shell_or_docker_module(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f'{node.module}.{alias.name}' for alias in node.names)
    hits = {name for name in imported if name in FORBIDDEN_MODULES or name.split('.')[0] in FORBIDDEN_MODULES}
    assert hits == set()


@pytest.mark.parametrize('path', _seed_sources(), ids=lambda path: path.name)
def test_no_seed_module_calls_an_os_process_function(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == 'os'
        and (node.attr in FORBIDDEN_OS_CALLS or node.attr.startswith(('exec', 'spawn')))
    }
    assert calls == set()


def test_no_compose_target_or_exec_script_is_left():
    for gone in ('ComposeTarget', 'POST_SCRIPT'):
        assert not hasattr(stack_module, gone)
