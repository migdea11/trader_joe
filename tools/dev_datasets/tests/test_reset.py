"""The clean start: reset, and seed resetting first (validator, tj-grna9p.148 (1)-(2); owner ruling 2026-10-20).

THE OWNER'S RULING (bd memory seeding-always-starts-clean): seeding always starts clean. reset empties, in ONE
transaction on the store database, exactly seven tables, by one TRUNCATE ... RESTART IDENTITY without CASCADE, and
never alembic_version. It sets a lock timeout first, prints the counts before and after, never the password. A
destructive run needs --yes-reset; without it the counts are printed, nothing changes and the exit is 1. --no-reset
skips the reset (seed only); --yes-reset with --no-reset is a usage error (2); --row with a resetting run is refused.

THE SEVEN, written out (doubles.SEVEN_TABLES), and checked here against the store's REAL models and migrations, in a
child interpreter (the models live under server/, which a tools-scoped run does not import; see
test_store_contract.py for why a subprocess). A new store table turns this red until someone decides whether a clean
start empties it.

The database is doubles.FakeDb: it records every statement and the transaction boundaries (events), and answers the
count SELECTs with distinct numbers per table.

tj-grna9p.150 (5), THE QUIET STACK: the reset's first read in its transaction counts RUNNING ingest runs and refuses
on any; after the commit it waits two usage flush intervals (injected, never slept) and recounts ingest_run and
dataset_read on a fresh read-only connection, NOT CLEAN on any row.
"""

import ast
import inspect
import json
import os
import re
import subprocess
import sys
import time

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    DB_PASSWORD,
    GOOD_ENV,
    NO_RESET,
    QUIESCE_SECONDS,
    RUNNING_COUNT_TEXT,
    SEVEN_TABLES,
    TABLE_COUNTS,
    TEST_CONFIG,
    ExplodingHttp,
    FakeDb,
    assert_no_canary,
    exploding_connect,
    run_tool,
    store_for,
)
from tools.dev_datasets.tests.test_store_contract import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.build_infra

MIGRATIONS = SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'
PLACEHOLDER = 'UNSET-start-with-make-agent-up-from-the-repo-root'
COUNT_TEXT = re.compile(r'^SELECT count\(\*\) FROM ([a-z_]+)$')
TRUNCATE_TEXT = re.compile(r'^TRUNCATE TABLE (?P<tables>[a-z_, ]+) RESTART IDENTITY$')


def counts_block(heading: str, counts: dict[str, int]) -> list[str]:
    return [f'{heading}:', *(f'  {table}: {counts[table]}' for table in SEVEN_TABLES)]


def reset(db: FakeDb, *extra: str, store=None, environ=None):
    return run_tool(['reset', *extra], environ, http=store or store_for(), connect=db.connect)


# ---- the statement itself ---------------------------------------------------------------------------------------


def test_the_reset_is_one_truncate_of_exactly_the_seven_with_restart_identity_and_no_cascade():
    text = ' '.join(tool.RESET_SQL.split())
    match = TRUNCATE_TEXT.match(text)
    assert match, text
    named = [name.strip() for name in match.group('tables').split(',')]
    assert sorted(named) == sorted(SEVEN_TABLES)
    assert len(named) == len(set(named)) == 7
    assert 'CASCADE' not in text.upper()
    assert 'alembic_version' not in text
    assert ';' not in text


def test_the_lock_timeout_is_a_set_local_so_it_ends_with_the_transaction():
    assert re.fullmatch(r"SET LOCAL lock_timeout = '\d+s'", tool.LOCK_TIMEOUT_SQL)


def test_the_counts_read_exactly_the_seven_and_nothing_else():
    assert set(tool.COUNT_SQL) == set(SEVEN_TABLES)
    for table, sql in tool.COUNT_SQL.items():
        assert COUNT_TEXT.match(sql).group(1) == table


# ---- the seven against the store's real models and migrations ----------------------------------------------------

MODELS_CHILD = r"""
import importlib, json, pkgutil
import data.store.app.database.models as models
for info in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f'{models.__name__}.{info.name}')
from common.database.sql_alchemy_table import AppBase
metadata = AppBase.DATA_STORE_BASE.metadata
print(json.dumps({
    'tables': sorted(metadata.tables),
    'fks': sorted([fk.parent.table.name, fk.column.table.name, fk.ondelete] for t in metadata.tables.values()
                  for fk in t.foreign_keys),
}))
"""


@pytest.fixture(scope='module')
def store_schema() -> dict:
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(SERVER_ROOT), str(REPO_ROOT)])}
    done = subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', MODELS_CHILD],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_seven_are_every_table_the_store_models_declare(store_schema):
    # Complete (no table the store writes dataset data to is missed) and exact (nothing else is emptied).
    assert sorted(store_schema['tables']) == sorted(SEVEN_TABLES)
    assert 'alembic_version' not in store_schema['tables']


def test_the_only_foreign_key_has_both_ends_inside_the_one_statement(store_schema):
    # TRUNCATE without CASCADE refuses a table referenced from outside the statement; with both ends inside, it runs.
    assert store_schema['fks'] == [['stock_market_activity', 'store_dataset_entry', 'CASCADE']]
    for child_table, parent_table, _ in store_schema['fks']:
        assert {child_table, parent_table} <= set(SEVEN_TABLES)


def _created_tables() -> set[str]:
    """Every table a migration's upgrade() creates, its name resolved through the module's string constants."""
    created = set()
    for path in sorted(MIGRATIONS.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for function in tree.body:
            if not (isinstance(function, ast.FunctionDef) and function.name == 'upgrade'):
                continue
            for call in ast.walk(function):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    assert call.func.attr != 'rename_table', f'{path.name} renames a table'
                    if call.func.attr == 'create_table':
                        first = call.args[0]
                        name = first.value if isinstance(first, ast.Constant) else constants[first.id]
                        created.add(name)
    return created


def test_the_seven_are_every_table_the_migrations_create():
    assert _created_tables() == set(SEVEN_TABLES)


def test_no_migration_protects_the_audit_tables_so_the_reset_is_permitted():
    # The design's FACT (tj-grna9p.148 (1)): append-only by code path only; no trigger, rule or REVOKE.
    for path in MIGRATIONS.glob('*.py'):
        text = path.read_text(encoding='utf-8').upper()
        for word in ('CREATE TRIGGER', 'CREATE RULE', 'REVOKE'):
            assert word not in text, (path.name, word)


# ---- a confirmed reset: one transaction ----------------------------------------------------------------------------


COUNTS = [f'SELECT count(*) FROM {table}' for table in SEVEN_TABLES]
LOCK = ' '.join(tool.LOCK_TIMEOUT_SQL.split())
TRUNCATE = ' '.join(tool.RESET_SQL.split())
CLEAN_TAIL = [
    f'reset: waiting {QUIESCE_SECONDS}s (two usage flush intervals) to verify the stack stayed quiet',
    'reset: verified clean (ingest_run 0, dataset_read 0)',
]


def test_a_confirmed_reset_is_one_transaction_lock_timeout_then_the_running_count_first():
    # tj-grna9p.150 (5): the transaction's first read is the RUNNING count; after it commits and closes, the wait, then
    # a second, read-only connection recounts and rolls back.
    db = FakeDb()
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert db.sql() == [LOCK, RUNNING_COUNT_TEXT, *COUNTS, TRUNCATE, *COUNTS, *COUNTS]
    kinds = [event[0] for event in db.events]
    assert kinds == [
        *['connect', *['execute'] * 17, 'commit', 'close'],
        'sleep',
        *['connect', *['execute'] * 7, 'rollback', 'close'],
    ]
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (2, 1, 1, 2)
    assert db.autocommit is False


def test_a_confirmed_reset_prints_the_counts_before_and_after_then_the_verification_and_nothing_else():
    db = FakeDb()
    outcome = reset(db, '--yes-reset')
    assert outcome.lines == [
        "rail: deploymentLabel is 'dev' (real Alpaca backend)",
        *counts_block('reset: rows before', TABLE_COUNTS),
        *counts_block('reset: rows after', dict.fromkeys(SEVEN_TABLES, 0)),
        *CLEAN_TAIL,
    ]
    assert outcome.stdout == outcome.stderr == ''
    assert_no_canary(outcome.text)


def test_reset_sends_no_http_but_the_rail():
    store = store_for()
    assert reset(FakeDb(), '--yes-reset', store=store).status == 0
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]


def test_reset_never_names_alembic_version_or_any_other_table():
    db = FakeDb()
    assert reset(db, '--yes-reset').status == 0
    for sql in db.sql():
        assert 'alembic_version' not in sql
        named = set(re.findall(r'\b(?:FROM|TABLE)\s+([a-z_]+)', sql))
        named |= (
            {name.strip() for name in sql.split('TABLE', 1)[1].split('RESTART')[0].split(',')}
            if 'TRUNCATE' in sql
            else set()
        )
        assert named <= set(SEVEN_TABLES), sql
        assert not re.search(r'\b(DROP|ALTER|CREATE|DELETE|INSERT|UPDATE|GRANT|REVOKE|CASCADE)\b', sql), sql


def test_the_reset_is_not_recorded_anywhere():
    # A clean start means empty (tj-grna9p.148 (1)): no INSERT into dataset_deletion or any table.
    db = FakeDb()
    assert reset(db, '--yes-reset').status == 0
    assert db.writes() == [' '.join(tool.RESET_SQL.split())]


@pytest.mark.parametrize(
    'failing',
    ['lock_timeout', "state = 'RUNNING'", 'count(*) FROM ingest_run', 'count(*) FROM dataset_read', 'TRUNCATE'],
)
def test_a_failure_anywhere_rolls_the_whole_reset_back_and_commits_nothing(failing):
    db = FakeDb()
    db.execute_error = RuntimeError('canceling statement due to lock timeout DETAILTEXT')
    db.fail_on = failing
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert 'DETAILTEXT' not in outcome.text
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == TABLE_COUNTS
    assert 'reset: rows after:' not in outcome.lines
    # A failed reset neither waits nor claims anything about the stack.
    assert outcome.sleeps == []
    assert not any('verified clean' in line or 'NOT CLEAN' in line for line in outcome.lines)


def test_a_failing_commit_is_rolled_back_and_reported():
    db = FakeDb()

    def refuse():
        raise RuntimeError('could not serialize DETAILTEXT')

    db.commit = refuse
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert (db.rollbacks, db.closes) == (1, 1)
    assert outcome.sleeps == []
    assert db.connects == 1


def test_a_connection_failure_names_the_type_only():
    db = FakeDb(connect_error=ConnectionError(f'could not connect password={DB_PASSWORD}'))
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the database connection failed (ConnectionError)'
    assert_no_canary(outcome.text)


# ---- --yes-reset is required ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('argv', 'hint'),
    [(['reset'], ''), (['seed'], ' or --no-reset to keep the data'), ([], ' or --no-reset to keep the data')],
)
def test_without_yes_reset_the_counts_are_printed_nothing_changes_and_the_exit_is_1(argv, hint):
    db = FakeDb()
    store = store_for()
    outcome = run_tool(argv, http=store, connect=db.connect)
    assert outcome.status == 1
    mode = argv[0] if argv else 'seed'
    assert outcome.lines == [
        "rail: deploymentLabel is 'dev' (real Alpaca backend)",
        *counts_block('a reset would remove these rows', TABLE_COUNTS),
        'kept: alembic_version, the schema and every other table',
        f'refused: nothing was changed. {mode} resets first; pass --yes-reset to confirm{hint}',
    ]
    assert db.writes() == []
    assert all(COUNT_TEXT.match(sql) for sql in db.sql())
    assert db.commits == 0
    assert db.counts == TABLE_COUNTS
    assert store.writes() == []
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]
    assert outcome.sleeps == []


# ---- seed resets first ------------------------------------------------------------------------------------------


class Timeline:
    """One record of the HTTP calls and the database events, in the order they happened."""

    def __init__(self, store, db):
        self.events: list[str] = []
        self._store, self._db = store, db

    def request(self, method, path, **kwargs):
        self.events.append(f'{method} {path.split("?")[0]}')
        return self._store.request(method, path, **kwargs)

    def connect(self):
        connection = self._db.connect()
        timeline = self

        class Connection:
            def cursor(self):
                cursor = connection.cursor()
                execute = cursor.execute

                def recorded(sql, params=()):
                    timeline.events.append(' '.join(sql.split()).split()[0])
                    return execute(sql, params)

                cursor.execute = recorded
                return cursor

            def commit(self):
                timeline.events.append('COMMIT')
                connection.commit()

            def rollback(self):
                timeline.events.append('ROLLBACK')
                connection.rollback()

            def close(self):
                connection.close()

        return Connection()


def test_seed_resets_and_commits_before_its_first_lookup_or_post():
    store, db = store_for(), FakeDb()
    timeline = Timeline(store, db)
    outcome = run_tool(
        ['seed', '--yes-reset'],
        http=timeline,
        connect=timeline.connect,
        sleep=lambda _: timeline.events.append('SLEEP'),
    )
    assert outcome.status == 0, outcome.text
    first_store_call = next(i for i, event in enumerate(timeline.events) if event.startswith(('GET /store', 'POST')))
    assert timeline.events[0] == 'GET /ui/v1/config'
    assert 'TRUNCATE' in timeline.events[:first_store_call]
    assert timeline.events.index('COMMIT') < first_store_call
    # tj-grna9p.150 (5b): the plan goes out only after the wait and the clean recount.
    sleep_at = timeline.events.index('SLEEP')
    assert timeline.events.index('COMMIT') < sleep_at < first_store_call
    assert timeline.events[sleep_at + 1 : first_store_call] == ['SELECT'] * 7 + ['ROLLBACK']
    assert len(store.posts()) == 14
    assert db.commits == 1
    assert outcome.sleeps == [QUIESCE_SECONDS]


def test_seed_after_a_failed_reset_posts_nothing():
    store, db = store_for(), FakeDb(execute_error=RuntimeError('x'), fail_on='TRUNCATE')
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect)
    assert outcome.status == 1
    assert store.posts() == []
    assert [c.route for c in store.calls] == ['/ui/v1/config']


@pytest.mark.parametrize('argv', [['seed', NO_RESET], [NO_RESET]])
def test_no_reset_skips_the_reset_and_never_opens_the_database(argv):
    store = store_for()
    outcome = run_tool(argv, http=store, connect=exploding_connect)
    assert outcome.status == 0, outcome.text
    assert len(store.posts()) == 14
    assert not any('reset' in line for line in outcome.lines)


def test_seed_with_no_reset_needs_no_database_password():
    store = store_for()
    outcome = run_tool(['seed', NO_RESET], {**GOOD_ENV, 'POSTGRES_PASS': ''}, http=store, connect=exploding_connect)
    assert outcome.status == 0, outcome.text


# ---- the usage errors and --row ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('argv', 'message'),
    [
        (['seed', '--yes-reset', NO_RESET], '--yes-reset and --no-reset contradict each other'),
        (['reset', '--yes-reset', NO_RESET], '--yes-reset and --no-reset contradict each other'),
        ([NO_RESET, '--yes-reset'], '--yes-reset and --no-reset contradict each other'),
        (['reset', NO_RESET], '--no-reset only means something for seed'),
        (['degrade', NO_RESET], '--no-reset only means something for seed'),
        (['reads', '--yes-reset'], '--yes-reset only means something for seed and reset'),
        (['repair', '--yes-reset'], '--yes-reset only means something for seed and reset'),
        (['check', '--yes-reset'], '--yes-reset only means something for seed and reset'),
    ],
)
def test_contradicting_reset_flags_are_a_usage_error_before_anything_is_sent(argv, message):
    http = ExplodingHttp()
    outcome = run_tool(argv, http=http, connect=exploding_connect)
    assert outcome.status == 2
    assert outcome.lines == [message]
    assert http.calls == []


@pytest.mark.parametrize(
    'argv',
    [
        ['seed', '--yes-reset', '--row', 'r1'],
        ['seed', '--row', 'r1'],
        ['reset', '--yes-reset', '--row', 'r1'],
        ['seed', '--dry-run', '--row', 'r1'],
    ],
)
def test_row_with_a_resetting_run_is_refused_before_anything_is_sent(argv):
    http = ExplodingHttp()
    outcome = run_tool(argv, http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert outcome.lines == ['--row limits the plan but the reset is not row-bound: add --no-reset']
    assert http.calls == []


# ---- the rail and the credentials come before any SQL --------------------------------------------------------------


@pytest.mark.parametrize('label', ['', 'prod', 'staging', 'Test'])
@pytest.mark.parametrize('argv', [['reset', '--yes-reset'], ['reset'], ['seed', '--yes-reset'], ['reset', '--dry-run']])
def test_the_rail_refuses_before_any_sql(argv, label):
    db = FakeDb()
    store = store_for(config=tool.Response(200, {'deploymentLabel': label}))
    outcome = run_tool(argv, http=store, connect=db.connect)
    assert outcome.status == 1
    assert db.events == []
    assert 'refused: this is not a dev or test stack' in outcome.lines[-1]


def test_the_reset_passes_the_test_label():
    db = FakeDb()
    outcome = reset(db, '--yes-reset', store=store_for(config=TEST_CONFIG))
    assert outcome.status == 0
    assert outcome.lines[0] == "rail: deploymentLabel is 'test' (fake broker backend)"
    assert db.commits == 1


@pytest.mark.parametrize('variable', ['INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'])
def test_the_placeholder_stops_a_confirmed_reset_before_any_call(variable):
    http = ExplodingHttp()
    outcome = run_tool(
        ['reset', '--yes-reset'], {**GOOD_ENV, variable: PLACEHOLDER}, http=http, connect=exploding_connect
    )
    assert outcome.status == 1
    assert http.calls == []
    assert PLACEHOLDER not in outcome.text


# ---- --dry-run of seed and reset: the rail GET and the count SELECTs, and nothing else -----------------------------


@pytest.mark.parametrize('label', ['dev', 'test'])
@pytest.mark.parametrize('mode', ['seed', 'reset'])
def test_a_dry_run_reads_the_rail_and_the_counts_and_writes_nothing(mode, label):
    db = FakeDb()
    store = store_for(config=tool.Response(200, {'deploymentLabel': label}))
    outcome = run_tool([mode, '--dry-run'], http=store, connect=db.connect)
    assert outcome.status == 0, outcome.text
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]
    assert store.writes() == []
    assert [COUNT_TEXT.match(sql).group(1) for sql in db.sql()] == list(SEVEN_TABLES)
    assert db.writes() == []
    assert db.commits == 0
    assert db.counts == TABLE_COUNTS
    assert outcome.lines[1] == f'dry run: {mode} would reset first; nothing is changed'
    assert outcome.lines[2:11] == [
        *counts_block('a reset would remove these rows', TABLE_COUNTS),
        'kept: alembic_version, the schema and every other table',
    ]
    bodies = [line for line in outcome.lines if line.startswith('{')]
    assert len(bodies) == (14 if mode == 'seed' else 0)
    assert_no_canary(outcome.text)
    # A dry run changes nothing, so it has nothing to verify: no RUNNING count, no wait, no recount.
    assert outcome.sleeps == []
    assert RUNNING_COUNT_TEXT not in db.sql()
    assert db.connects == 1


def test_a_dry_run_with_the_placeholder_password_opens_no_database():
    store = store_for()
    outcome = run_tool(
        ['seed', '--dry-run'], {**GOOD_ENV, 'POSTGRES_PASS': PLACEHOLDER}, http=store, connect=exploding_connect
    )
    assert outcome.status == 0, outcome.text
    assert 'reset counts unavailable: the database password is not set' in outcome.lines
    assert PLACEHOLDER not in outcome.text
    assert store.writes() == []


def test_a_dry_run_of_reset_with_the_placeholder_password_opens_no_database():
    store = store_for()
    outcome = run_tool(
        ['reset', '--dry-run'], {**GOOD_ENV, 'POSTGRES_PASS': PLACEHOLDER}, http=store, connect=exploding_connect
    )
    assert outcome.status == 0, outcome.text
    assert outcome.lines == [
        "rail: deploymentLabel is 'dev' (real Alpaca backend)",
        'dry run: reset would reset first; nothing is changed',
        'reset counts unavailable: the database password is not set',
    ]
    assert store.writes() == []
    assert outcome.sleeps == []


def test_a_dry_run_with_the_placeholder_secret_sends_nothing_with_it():
    store = store_for()
    outcome = run_tool(
        ['seed', '--dry-run'], {**GOOD_ENV, 'INSTANCE_WRITE_SECRET': PLACEHOLDER}, http=store, connect=FakeDb().connect
    )
    assert outcome.status == 0, outcome.text
    assert store.writes() == []
    assert all(call.headers == {} for call in store.calls)
    assert PLACEHOLDER not in outcome.text


# ---- the quiet stack (tj-grna9p.150 (5)) ---------------------------------------------------------------------------
#
# (a) Inside the reset transaction, before the TRUNCATE, any ingest_run row in state RUNNING refuses the reset: exit 1,
# the count named, nothing changed. (b) After the commit, wait two usage flush intervals, then recount ingest_run and
# dataset_read; a non-zero count is NOT CLEAN and exits 1. The wait is injected (doubles.run_tool); FakeDb.sleep is
# where work in flight writes.

RAIL_DEV = "rail: deploymentLabel is 'dev' (real Alpaca backend)"
STORE_APP = SERVER_ROOT / 'data' / 'store' / 'app'


def refused_line(running: int) -> str:
    return f'refused: {running} ingest run(s) are RUNNING; nothing was changed. Wait for them, then reset a quiet stack'


def _module_constants(path) -> dict[str, object]:
    """Every module-level NAME = <literal> (or NAME: Final = <literal>) in a source file, without importing it."""
    constants = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            constants[node.target.id] = node.value.value
    return constants


def test_the_running_count_reads_ingest_run_for_the_value_the_store_writes():
    assert ' '.join(tool.RUNNING_RUNS_SQL.split()) == RUNNING_COUNT_TEXT
    # The store writes the enum's VALUE (values_callable), and the value is 'RUNNING'; the migration's enum type
    # carries the same literal. Read from the source, so a renamed member turns this red.
    model = (STORE_APP / 'database' / 'models' / 'ingest_run.py').read_text(encoding='utf-8')
    tree = ast.parse(model)
    state_enum = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'IngestRunState')
    members = {
        node.targets[0].id: node.value.value
        for node in state_enum.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    assert members['RUNNING'] == 'RUNNING'
    assert re.search(
        r"state = Column\(\s*Enum\(IngestRunState, name='ingest_run_state', values_callable=_values\)", model
    )
    assert 'return [member.value for member in enum_cls]' in model
    [migration] = MIGRATIONS.glob('6f5a798746a1_*.py')
    assert "STATE_MEMBERS = ('RUNNING'," in migration.read_text(encoding='utf-8')


@pytest.mark.parametrize('running', [1, 3])
def test_a_running_ingest_run_refuses_the_reset_before_the_truncate_and_changes_nothing(running):
    db = FakeDb(running=running)
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines == [RAIL_DEV, refused_line(running)]
    # Counted after the lock timeout, in the transaction, and nothing after it: no TRUNCATE, no other count.
    assert db.sql() == [LOCK, RUNNING_COUNT_TEXT]
    assert db.writes() == []
    assert [event[0] for event in db.events] == ['connect', 'execute', 'execute', 'rollback', 'close']
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == TABLE_COUNTS
    assert outcome.sleeps == []
    assert_no_canary(outcome.text)


def test_the_running_count_is_in_the_same_transaction_as_the_truncate_and_before_it():
    db = FakeDb()
    assert reset(db, '--yes-reset').status == 0
    first_commit = db.events.index(('commit',))
    transaction = db.events[:first_commit]
    # One connection, no boundary inside it: the RUNNING count and the TRUNCATE share the transaction.
    assert transaction[0] == ('connect',)
    assert all(event[0] == 'execute' for event in transaction[1:])
    statements = [' '.join(event[1].split()) for event in transaction[1:]]
    assert statements.index(RUNNING_COUNT_TEXT) == 1
    assert statements.index(RUNNING_COUNT_TEXT) < statements.index(TRUNCATE)


def test_seed_with_a_running_ingest_run_posts_nothing():
    store, db = store_for(), FakeDb(running=2)
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1] == refused_line(2)
    assert store.posts() == []
    assert [c.route for c in store.calls] == ['/ui/v1/config']


def test_the_wait_is_two_usage_flush_intervals():
    assert tool.QUIESCE_WAIT_SECONDS == 2 * tool.USAGE_FLUSH_INTERVAL_SECONDS == QUIESCE_SECONDS


def test_the_flush_interval_mirrors_the_stores_default():
    # request.py says it MUST be kept in step by hand; this is what notices when it is not.
    constants = _module_constants(STORE_APP / 'usage_recorder.py')
    assert constants['FLUSH_INTERVAL_ENV'] == 'USAGE_FLUSH_INTERVAL_SECONDS'
    assert constants['DEFAULT_FLUSH_INTERVAL_SECONDS'] == tool.USAGE_FLUSH_INTERVAL_SECONDS
    source = (STORE_APP / 'usage_recorder.py').read_text(encoding='utf-8')
    assert '_positive_int(FLUSH_INTERVAL_ENV, DEFAULT_FLUSH_INTERVAL_SECONDS)' in source


def test_a_confirmed_reset_waits_once_after_its_commit_and_close_and_before_the_recount():
    db = FakeDb()
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert outcome.sleeps == db.sleeps == [QUIESCE_SECONDS]
    at = db.events.index(('sleep', QUIESCE_SECONDS))
    assert db.events[at - 2 : at] == [('commit',), ('close',)]
    assert db.events[at + 1] == ('connect',)
    # The recount reads, and only reads.
    assert db.writes() == [TRUNCATE]


def test_the_real_wait_is_the_default():
    assert inspect.signature(tool.run_reset).parameters['sleep'].default is time.sleep
    assert inspect.signature(tool.run).parameters['sleep'].default is time.sleep


def test_the_not_clean_message_is_the_design():
    assert tool.NOT_CLEAN_MESSAGE == 'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack'


@pytest.mark.parametrize(
    'written',
    [{'ingest_run': 1}, {'dataset_read': 4}, {'ingest_run': 2, 'dataset_read': 6}],
    ids=['a-run', 'a-read', 'both'],
)
def test_work_that_writes_during_the_wait_is_not_clean_and_exits_1(written):
    db = FakeDb(written_while_waiting=written)
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    runs, reads = written.get('ingest_run', 0), written.get('dataset_read', 0)
    assert outcome.lines[-1] == (
        'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack '
        f'(ingest_run {runs}, dataset_read {reads})'
    )
    assert outcome.lines[-2] == CLEAN_TAIL[0]
    assert not any('verified clean' in line for line in outcome.lines)
    assert outcome.sleeps == [QUIESCE_SECONDS]
    assert db.commits == 1
    assert_no_canary(outcome.text)


def test_seed_after_a_not_clean_reset_posts_nothing():
    store, db = store_for(), FakeDb(written_while_waiting={'dataset_read': 1})
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1].startswith('NOT CLEAN: ')
    assert store.posts() == []


def test_a_clean_recount_exits_0_and_says_so():
    db = FakeDb()
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 0
    assert outcome.lines[-2:] == CLEAN_TAIL
