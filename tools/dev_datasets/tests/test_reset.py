"""The clean start: reset, and seed resetting first (validator, tj-grna9p.148 (1)-(2); owner ruling 2026-10-20).

THE OWNER'S RULING (bd memory seeding-always-starts-clean): seeding always starts clean. reset empties, in ONE
transaction on the store database, exactly eighteen tables, by one TRUNCATE ... RESTART IDENTITY without CASCADE,
and never alembic_version. It sets a lock timeout first, prints the counts before and after, never the password. A
destructive run needs --yes-reset; without it the counts are printed, nothing changes and the exit is 1. --no-reset
skips the reset (seed only); --yes-reset with --no-reset is a usage error (2); --row with a resetting run is refused.

THE EIGHTEEN, written out (doubles.EIGHTEEN_TABLES), and checked here against the store's REAL models and migrations,
in a child interpreter (the models live under server/, which a tools-scoped run does not import; see
test_store_contract.py for why a subprocess), and against the models' TABLE_NAME lines read as text, independently of
the tool's constant (tj-7zjov1.51). A new store table turns this red until someone decides whether a clean start
empties it. The seven of tj-grna9p.148 were simply every store data table then; tj-7zjov1.51 added ingest_request and
ingest_instance after ingest_run; tj-3kplhf (owner ruling 2026-11-03) added the asset-list, corporate-action and
panel tables after dataset_deletion. Every foreign key among them has both ends inside the one statement, so no
CASCADE is needed and none can reach outside it. There is NO dev guard (owner ruling 2026-11-03): a dev reset with
rows in the panel tables goes ahead and empties them.

The database is doubles.FakeDb: it records every statement and the transaction boundaries (events), and answers the
count SELECTs with distinct numbers per table.

tj-grna9p.150 (5), THE QUIET STACK: the reset's first read in its transaction counts RUNNING ingest runs and refuses
on any; after the commit it waits two usage flush intervals (injected, never slept) and recounts on a fresh read-only
connection, NOT CLEAN on any row.

tj-grna9p.160, THE LOCK FIRST: after the lock timeout, the transaction takes ACCESS EXCLUSIVE on exactly the eighteen,
and only then counts RUNNING runs and truncates, so no run can start in between. The recount sets the lock timeout
too and reads all eighteen tables; a non-zero count in any one is NOT CLEAN, naming that table.

tj-7zjov1.51, THE OPEN REQUESTS: right after the RUNNING count, in the same locked transaction, the reset counts the
ingest_request rows in state PENDING or QUEUED. Either count non-zero refuses with exit 1, one rollback, no TRUNCATE,
naming both counts. NOT_DELIVERED, SUCCEEDED, EMPTY and FAILED do not block; they are emptied with the table.

tj-7zjov1.297 (P56v), THE PRESERVING DEV RESET (decision tj-7zjov1.291 D1; built by tj-7zjov1.296). On the DEV label the
same transaction, after the in-flight counts, exports the trimmed seed, TRUNCATEs, COPYs the files back and setvals the
one serial, so a dev reset LEAVES the seed's counts (SEED_ROWS here), not 0, and the recount after the wait expects
them. On the TEST label the reset is still the plain wipe. Every test below that a label changes runs on both (LABELS),
each against what its label must leave; the dev reset's own order, rollback and file rules are pinned in
test_preserving_reset.py, and load in test_load.py.
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
    ALEMBIC_TEXT,
    COLUMNS_TEXT,
    DB_PASSWORD,
    EIGHTEEN_TABLES,
    GOOD_ENV,
    NO_RESET,
    OPEN_REQUEST_STATES,
    OPEN_REQUESTS_TEXT,
    QUIESCE_SECONDS,
    RUNNING_COUNT_TEXT,
    SEED_ROWS,
    SESSION_TEXTS,
    SETVAL_TEXT,
    STACK_TEXT,
    TABLE_COUNTS,
    TEST_CONFIG,
    ExplodingHttp,
    FakeDb,
    assert_no_canary,
    exploding_connect,
    run_tool,
    seeded_copy_data,
    store_for,
)
from tools.dev_datasets.tests.test_store_contract import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.build_infra

MIGRATIONS = SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'
PLACEHOLDER = 'UNSET-start-with-make-agent-up-from-the-repo-root'
COUNT_TEXT = re.compile(r'^SELECT count\(\*\) FROM ([a-z_]+)$')
TRUNCATE_TEXT = re.compile(r'^TRUNCATE TABLE (?P<tables>[a-z_, ]+) RESTART IDENTITY$')
# tj-grna9p.160 (1), written out from the design: one LOCK TABLE of the eighteen, in ACCESS EXCLUSIVE MODE.
TABLE_LOCK_TEXT = re.compile(r'^LOCK TABLE (?P<tables>[a-z_, ]+) IN ACCESS EXCLUSIVE MODE$')


def counts_block(heading: str, counts: dict[str, int]) -> list[str]:
    return [f'{heading}:', *(f'  {table}: {counts[table]}' for table in EIGHTEEN_TABLES)]


def reset(db: FakeDb, *extra: str, store=None, environ=None):
    return run_tool(['reset', *extra], environ, http=store or store_for(), connect=db.connect)


# ---- the statement itself ---------------------------------------------------------------------------------------


def _truncated_names() -> list[str]:
    """The tables RESET_SQL names, in order, read from the statement itself."""
    match = TRUNCATE_TEXT.match(' '.join(tool.RESET_SQL.split()))
    assert match, tool.RESET_SQL
    return [name.strip() for name in match.group('tables').split(',')]


def test_the_reset_is_one_truncate_of_exactly_the_eighteen_with_restart_identity_and_no_cascade():
    text = ' '.join(tool.RESET_SQL.split())
    match = TRUNCATE_TEXT.match(text)
    assert match, text
    named = _truncated_names()
    assert sorted(named) == sorted(EIGHTEEN_TABLES)
    assert len(named) == len(set(named)) == 18
    # tj-7zjov1.51 (1) and tj-3kplhf: all eighteen in the design's order, the request-record tables after ingest_run,
    # the asset-list, corporate-action and panel tables after dataset_deletion.
    assert named == list(EIGHTEEN_TABLES)
    assert text.count('TRUNCATE') == 1
    assert 'CASCADE' not in text.upper()
    assert 'alembic_version' not in text
    assert ';' not in text


def test_the_lock_timeout_is_a_set_local_so_it_ends_with_the_transaction():
    assert re.fullmatch(r"SET LOCAL lock_timeout = '\d+s'", tool.LOCK_TIMEOUT_SQL)


def test_the_table_lock_names_exactly_the_eighteen_in_access_exclusive_mode():
    text = ' '.join(tool.LOCK_SQL.split())
    match = TABLE_LOCK_TEXT.match(text)
    assert match, text
    named = [name.strip() for name in match.group('tables').split(',')]
    assert sorted(named) == sorted(EIGHTEEN_TABLES)
    assert len(named) == len(set(named)) == 18
    # tj-7zjov1.51 (1) and tj-3kplhf: all eighteen in the same order as the TRUNCATE.
    assert named == list(EIGHTEEN_TABLES) == _truncated_names()
    assert 'alembic_version' not in text
    assert ';' not in text


def test_the_counts_read_exactly_the_eighteen_and_nothing_else():
    assert set(tool.COUNT_SQL) == set(EIGHTEEN_TABLES)
    assert len(tool.COUNT_SQL) == 18
    # Printed in the design's order (the counts blocks below rely on it).
    assert list(tool.COUNT_SQL) == list(EIGHTEEN_TABLES)
    for table, sql in tool.COUNT_SQL.items():
        assert COUNT_TEXT.match(sql).group(1) == table


# ---- the eighteen against the store's real models and migrations -------------------------------------------------

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


def test_the_eighteen_are_every_table_the_store_models_declare(store_schema):
    # Complete (no store table is missed) and exact (nothing else is emptied); against the statement itself as well
    # as the design's list, so a table dropped from RESET_SQL alone turns this red too.
    assert sorted(store_schema['tables']) == sorted(EIGHTEEN_TABLES) == sorted(_truncated_names())
    assert len(store_schema['tables']) == 18
    assert 'alembic_version' not in store_schema['tables']


STORE_MODELS = SERVER_ROOT / 'data' / 'store' / 'app' / 'database' / 'models'


def _declared_table_names() -> set[str]:
    """Every TABLE_NAME = '<name>' line in the store models directory, read as text: nothing imported."""
    declared = set()
    for path in sorted(STORE_MODELS.glob('*.py')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('TABLE_NAME = '):
                declared.add(line.split('=', 1)[1].strip().strip('\'"'))
    return declared


def test_reset_tables_are_the_store_models_table_names_read_independently_of_the_constant():
    # tj-7zjov1.51: the gap existed because no pin compared the tool's own constant with the models directory. Read
    # the way tools/agent_mcp/tests/test_dev_exec.py test_the_tables_are_the_store_models_tables does, so the next
    # revision's new table turns this red at once, whatever EIGHTEEN_TABLES says.
    declared = _declared_table_names()
    assert len(declared) == 18, sorted(declared)
    assert set(tool.RESET_TABLES) == declared, sorted(declared)
    assert set(_truncated_names()) == declared, sorted(declared)
    assert set(tool.COUNT_SQL) == declared, sorted(declared)
    assert len(tool.RESET_TABLES) == len(set(tool.RESET_TABLES)) == 18
    assert 'alembic_version' not in tool.RESET_TABLES
    # And the design's written order: the request-record tables right after ingest_run, tj-3kplhf's nine at the end.
    assert tuple(tool.RESET_TABLES) == EIGHTEEN_TABLES


# Every foreign key among the store tables, written out from the store's models and revision b6d1f93a7c42 (tj-3kplhf):
# (child, parent, ON DELETE). The bar -> entry key, and the panel keys with no ON DELETE.
STORE_FOREIGN_KEYS = [
    ['panel_member_transition', 'panel', None],
    ['panel_member_transition', 'panel_pass', None],
    ['panel_pass', 'panel', None],
    ['panel_pass', 'panel_settings_version', None],
    ['panel_pass', 'panel_source_settings_version', None],
    ['panel_settings_version', 'panel', None],
    ['stock_market_activity', 'store_dataset_entry', 'CASCADE'],
]


def test_every_foreign_key_has_both_ends_inside_the_one_statement(store_schema):
    # TRUNCATE without CASCADE refuses a table referenced from outside the statement; with both ends inside, it runs.
    # A new foreign key turns the exact list red, so someone re-reads whether its ends are inside.
    assert sorted(store_schema['fks'], key=str) == sorted(STORE_FOREIGN_KEYS, key=str)
    truncated = set(_truncated_names())
    locked = {name.strip() for name in TABLE_LOCK_TEXT.match(TABLE_LOCK).group('tables').split(',')}
    for child_table, parent_table, _ in store_schema['fks']:
        assert {child_table, parent_table} <= truncated, (child_table, parent_table)
        assert {child_table, parent_table} <= locked, (child_table, parent_table)
    # And the statement is a plain TRUNCATE: the bar -> entry key's ON DELETE CASCADE is the schema's, never the reset's.
    assert 'CASCADE' not in TRUNCATE.upper()


def _migration_foreign_keys() -> list[list]:
    """Every ForeignKeyConstraint a migration's create_table declares, as (child, parent, ondelete), read by ast."""
    keys = []
    for path in sorted(MIGRATIONS.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for function in tree.body:
            if not (isinstance(function, ast.FunctionDef) and function.name == 'upgrade'):
                continue
            for call in ast.walk(function):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                    continue
                # A key added after its table exists would be missed below: make it visible instead.
                assert call.func.attr != 'create_foreign_key', f'{path.name} adds a foreign key outside create_table'
                if call.func.attr != 'create_table':
                    continue
                child = _created_name(call, tree)
                for inner in ast.walk(call):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr in ('ForeignKeyConstraint', 'ForeignKey')
                    ):
                        target = inner.args[1].elts[0] if inner.func.attr == 'ForeignKeyConstraint' else inner.args[0]
                        ondelete = next((k.value.value for k in inner.keywords if k.arg == 'ondelete'), None)
                        keys.append([child, ast.literal_eval(target).split('.')[0], ondelete])
    return keys


def test_every_foreign_key_the_migrations_create_has_both_ends_inside_the_one_statement():
    # The same claim read from the schema's other source, so a key the models do not declare still shows.
    keys = _migration_foreign_keys()
    assert sorted(keys, key=str) == sorted(STORE_FOREIGN_KEYS, key=str)
    truncated = set(_truncated_names())
    for child_table, parent_table, _ in keys:
        assert {child_table, parent_table} <= truncated, (child_table, parent_table)


def _string_constants(tree: ast.Module) -> dict[str, str]:
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _created_name(call: ast.Call, tree: ast.Module) -> str:
    """A create_table call's table name, resolved through the module's string constants."""
    first = call.args[0]
    return first.value if isinstance(first, ast.Constant) else _string_constants(tree)[first.id]


def _created_tables() -> set[str]:
    """Every table a migration's upgrade() creates, its name resolved through the module's string constants."""
    created = set()
    for path in sorted(MIGRATIONS.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for function in tree.body:
            if not (isinstance(function, ast.FunctionDef) and function.name == 'upgrade'):
                continue
            for call in ast.walk(function):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    assert call.func.attr != 'rename_table', f'{path.name} renames a table'
                    if call.func.attr == 'create_table':
                        created.add(_created_name(call, tree))
    return created


def test_the_eighteen_are_every_table_the_migrations_create():
    created = _created_tables()
    assert len(created) == 18, sorted(created)
    assert created == set(EIGHTEEN_TABLES) == set(_truncated_names())


def test_no_migration_protects_the_audit_tables_so_the_reset_is_permitted():
    # The design's FACT (tj-grna9p.148 (1)): append-only by code path only; no trigger, rule or REVOKE.
    for path in MIGRATIONS.glob('*.py'):
        text = path.read_text(encoding='utf-8').upper()
        for word in ('CREATE TRIGGER', 'CREATE RULE', 'REVOKE'):
            assert word not in text, (path.name, word)


# ---- a confirmed reset: one transaction ----------------------------------------------------------------------------


COUNTS = [f'SELECT count(*) FROM {table}' for table in EIGHTEEN_TABLES]
LOCK_TIMEOUT = ' '.join(tool.LOCK_TIMEOUT_SQL.split())
TABLE_LOCK = ' '.join(tool.LOCK_SQL.split())
TRUNCATE = ' '.join(tool.RESET_SQL.split())
CLEAN_TAIL = [
    f'reset: waiting {QUIESCE_SECONDS}s (two usage flush intervals) to verify the stack stayed quiet',
    'reset: verified clean (all eighteen tables 0)',
]

# tj-7zjov1.297: the two labels and what each reset must leave. dev reloads the seed the FakeDb streams
# (doubles.seeded_copy_data, SEED_ROWS rows); test wipes.
LABELS = ('dev', 'test')
DEV_TAIL = [
    CLEAN_TAIL[0],
    f'reset: verified clean (all eighteen tables as loaded, {sum(SEED_ROWS.values())} rows in all)',
]


RAIL_LINES = {
    'dev': "rail: deploymentLabel is 'dev' (real Alpaca backend)",
    'test': "rail: deploymentLabel is 'test' (fake broker backend)",
}
# What a dev reset says it keeps, before it is confirmed (the design: the 90 days, reloaded in the same transaction).
KEPT_SEED_LINE = (
    'kept: the trimmed seed (the last 90 days, see export --dry-run for its size) is exported and reloaded in the same '
    'transaction'
)


def labelled(label: str, **kwargs) -> FakeDb:
    """A database behind a stack of this label; on dev it streams the seed's COPY text, so the reload is not empty."""
    if label == 'dev':
        kwargs.setdefault('copy_data', seeded_copy_data())
    return FakeDb(stack=label, **kwargs)


def store_on(label: str):
    return store_for(config=TEST_CONFIG) if label == 'test' else store_for()


def reset_on(label: str, db: FakeDb, *extra: str):
    return run_tool(['reset', *extra], http=store_on(label), connect=db.connect)


def left(label: str) -> dict[str, int]:
    """What a committed reset leaves in each table: the seed on dev, nothing on test."""
    return dict(SEED_ROWS) if label == 'dev' else dict.fromkeys(EIGHTEEN_TABLES, 0)


def tail(label: str) -> list[str]:
    return DEV_TAIL if label == 'dev' else CLEAN_TAIL


def transaction_sql(label: str) -> list[str]:
    """The reset transaction's statements, written out from the design (D1 on dev; tj-grna9p.148 on test)."""
    guard = [STACK_TEXT, LOCK_TIMEOUT, TABLE_LOCK, RUNNING_COUNT_TEXT, OPEN_REQUESTS_TEXT, *COUNTS]
    if label == 'test':
        return [*guard, TRUNCATE, *COUNTS]
    # The export's session settings and revision read, one column read per table written, then the one TRUNCATE, the
    # setval after the COPYs (which are not execute()d), and the recount against the manifest.
    return [*guard, *SESSION_TEXTS, ALEMBIC_TEXT, *[COLUMNS_TEXT] * 18, TRUNCATE, SETVAL_TEXT, *COUNTS]


# The non-read statements of a committed reset (FakeDb.writes keeps SET LOCAL other than the lock timeout).
def reset_writes(label: str) -> list[str]:
    return [TABLE_LOCK, TRUNCATE] if label == 'test' else [TABLE_LOCK, *SESSION_TEXTS, TRUNCATE]


@pytest.mark.parametrize('label', LABELS)
def test_a_confirmed_reset_is_one_transaction_lock_timeout_table_lock_then_the_running_count(label):
    # tj-grna9p.150 (5) and .160: the lock timeout, the table lock, then the RUNNING count; after the commit and close,
    # the wait, then a second, read-only connection sets the lock timeout, recounts all eighteen and rolls back.
    db = labelled(label)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    # tj-grna9p.194: each of the two connections asks the database rail's identity query first.
    assert db.sql() == [*transaction_sql(label), *[STACK_TEXT, LOCK_TIMEOUT, *COUNTS]]
    # The COPYs out and in (dev) sit inside the first transaction; no boundary anywhere else.
    kinds = [event[0] for event in db.events if event[0] not in ('mogrify', 'copy', 'copy_in')]
    assert kinds == [
        *['connect', *['execute'] * len(transaction_sql(label)), 'commit', 'close'],
        'sleep',
        # The rail, the timeout, 18 counts.
        *['connect', *['execute'] * 20, 'rollback', 'close'],
    ]
    first_commit = db.events.index(('commit',))
    copies = [event for event in db.events if event[0] in ('mogrify', 'copy', 'copy_in')]
    assert len(copies) == (54 if label == 'dev' else 0)
    assert all(db.events.index(event) < first_commit for event in copies)
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (2, 1, 1, 2)
    assert db.autocommit is False
    assert db.counts == left(label)


def test_a_confirmed_test_reset_prints_the_counts_before_and_after_then_the_verification_and_nothing_else():
    # The dev reset's exact lines (the export's, the reload's) are test_preserving_reset.py's.
    db = FakeDb(stack='test')
    outcome = reset_on('test', db, '--yes-reset')
    assert outcome.lines == [
        "rail: deploymentLabel is 'test' (fake broker backend)",
        *counts_block('reset: rows before', TABLE_COUNTS),
        *counts_block('reset: rows after', dict.fromkeys(EIGHTEEN_TABLES, 0)),
        *CLEAN_TAIL,
    ]
    assert outcome.stdout == outcome.stderr == ''
    assert_no_canary(outcome.text)


def test_a_confirmed_dev_reset_prints_the_counts_before_and_the_manifest_counts_after():
    db = labelled('dev')
    outcome = reset_on('dev', db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert outcome.lines[:20] == [
        "rail: deploymentLabel is 'dev' (real Alpaca backend)",
        *counts_block('reset: rows before', TABLE_COUNTS),
    ]
    after = outcome.lines.index('reset: rows after:')
    assert outcome.lines[after:] == [*counts_block('reset: rows after', SEED_ROWS), *DEV_TAIL]
    assert outcome.stdout == outcome.stderr == ''
    assert_no_canary(outcome.text)


TJ_3KPLHF_TABLES = (
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


@pytest.mark.parametrize('label', LABELS)
def test_a_reset_with_panel_and_asset_list_rows_present_goes_ahead(label):
    # Owner ruling 2026-11-03: NO dev guard. Rows in the nine tables tj-3kplhf added never refuse a confirmed reset, on
    # the dev label as on the test one: one transaction, one TRUNCATE naming them, verified clean. On test they are
    # emptied; on dev they hold exactly what the seed kept of them (tj-7zjov1.297).
    db = labelled(label)
    assert all(db.counts[table] > 0 for table in TJ_3KPLHF_TABLES)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert not any(line.startswith('refused') for line in outcome.lines)
    assert db.writes() == reset_writes(label)
    assert set(TJ_3KPLHF_TABLES) <= set(_truncated_names())
    assert db.commits == 1
    assert db.counts == left(label)
    assert outcome.lines[-2:] == tail(label)


@pytest.mark.parametrize('label', LABELS)
def test_reset_sends_no_http_but_the_rail(label):
    store = store_on(label)
    db = labelled(label)
    assert run_tool(['reset', '--yes-reset'], http=store, connect=db.connect).status == 0
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]


def test_a_test_reset_never_names_alembic_version_or_any_other_table():
    db = FakeDb(stack='test')
    assert reset_on('test', db, '--yes-reset').status == 0
    # tj-grna9p.194: the one read outside the eighteen is the database rail's identity query, exactly, once per
    # connection.
    assert [sql for sql in db.sql() if 'pg_settings' in sql] == [STACK_TEXT, STACK_TEXT]
    _only_the_eighteen(db, allowed=(STACK_TEXT,))
    assert db.copies == db.loads == db.mogrified == []


def test_a_dev_reset_reads_alembic_version_and_the_column_layout_and_writes_only_the_eighteen():
    # tj-7zjov1.297, D3: the dev reset's export reads the revision (once, never writes alembic_version) and each table's
    # columns from information_schema; every other statement names only the eighteen, and every COPY in and out too.
    db = labelled('dev')
    assert reset_on('dev', db, '--yes-reset').status == 0
    assert [sql for sql in db.sql() if 'alembic_version' in sql] == [ALEMBIC_TEXT]
    assert db.sql().count(COLUMNS_TEXT) == 18
    _only_the_eighteen(db, allowed=(STACK_TEXT, ALEMBIC_TEXT, COLUMNS_TEXT))
    assert [table for table, _ in db.copies] == [table for table, _ in db.loads]
    assert {table for table, _ in db.loads} == set(EIGHTEEN_TABLES)


def _only_the_eighteen(db: FakeDb, *, allowed: tuple[str, ...]) -> None:
    for sql in db.sql():
        if sql in allowed:
            continue
        assert 'alembic_version' not in sql
        named = set(re.findall(r'\b(?:FROM|TABLE)\s+([a-z_]+)', sql))
        named |= (
            {name.strip() for name in sql.split('TABLE', 1)[1].split('RESTART')[0].split(',')}
            if 'TRUNCATE' in sql
            else set()
        )
        assert named <= set(EIGHTEEN_TABLES), sql
        assert not re.search(r'\b(DROP|ALTER|CREATE|DELETE|INSERT|UPDATE|GRANT|REVOKE|CASCADE)\b', sql), sql


@pytest.mark.parametrize('label', LABELS)
def test_the_reset_is_not_recorded_anywhere(label):
    # A clean start means empty (tj-grna9p.148 (1)): no INSERT into dataset_deletion or any table. The table lock is
    # the one other non-read statement (tj-grna9p.160), and it ends with the transaction. On dev the two transaction-
    # local session settings are the export's, and the reload writes the two audit tables back EMPTY (O3).
    db = labelled(label)
    assert reset_on(label, db, '--yes-reset').status == 0
    assert db.writes() == reset_writes(label)
    loaded = dict(db.loads)
    for audit in ('dataset_override_log', 'dataset_deletion'):
        assert loaded.get(audit, b'') == b''
        assert db.counts[audit] == 0


@pytest.mark.parametrize(
    'failing',
    [
        'lock_timeout',
        'LOCK TABLE',
        "state = 'RUNNING'",
        'count(*) FROM ingest_run',
        "state IN ('PENDING', 'QUEUED')",
        'count(*) FROM ingest_instance',
        'count(*) FROM dataset_read',
        'TRUNCATE',
    ],
)
@pytest.mark.parametrize('label', LABELS)
def test_a_failure_anywhere_rolls_the_whole_reset_back_and_commits_nothing(failing, label):
    db = labelled(label)
    db.execute_error = RuntimeError('canceling statement due to lock timeout DETAILTEXT')
    db.fail_on = failing
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert 'DETAILTEXT' not in outcome.text
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == TABLE_COUNTS
    assert 'reset: rows after:' not in outcome.lines
    # A failed reset neither waits nor claims anything about the stack.
    assert outcome.sleeps == []
    assert not any('verified clean' in line or 'NOT CLEAN' in line for line in outcome.lines)
    # tj-7zjov1.297: on dev the TRUNCATE comes after the export, which stays on disk; before it nothing was written.
    assert len(complete_stamps(outcome.seed_root)) == (1 if (label, failing) == ('dev', 'TRUNCATE') else 0)
    assert db.loads == []


def complete_stamps(root) -> list:
    return sorted(path.parent for path in root.glob('*/*/manifest.json'))


@pytest.mark.parametrize('label', LABELS)
def test_a_failing_commit_is_rolled_back_and_reported(label):
    db = labelled(label)

    def refuse():
        raise RuntimeError('could not serialize DETAILTEXT')

    db.commit = refuse
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert (db.rollbacks, db.closes) == (1, 1)
    assert outcome.sleeps == []
    assert db.connects == 1
    assert db.counts == TABLE_COUNTS
    assert len(complete_stamps(outcome.seed_root)) == (1 if label == 'dev' else 0)


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
@pytest.mark.parametrize('label', LABELS)
def test_without_yes_reset_the_counts_are_printed_nothing_changes_and_the_exit_is_1(argv, hint, label):
    db = labelled(label)
    store = store_on(label)
    outcome = run_tool(argv, http=store, connect=db.connect)
    assert outcome.status == 1
    mode = argv[0] if argv else 'seed'
    assert outcome.lines == [
        RAIL_LINES[label],
        *counts_block('a reset would remove these rows', TABLE_COUNTS),
        *([KEPT_SEED_LINE] if label == 'dev' else []),
        'kept: alembic_version, the schema and every other table',
        f'refused: nothing was changed. {mode} resets first; pass --yes-reset to confirm{hint}',
    ]
    assert not any(outcome.seed_root.iterdir())
    assert db.writes() == []
    assert db.sql()[0] == STACK_TEXT
    assert all(COUNT_TEXT.match(sql) for sql in db.sql()[1:])
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
    # The recount's connection: the identity query (a SELECT), the lock timeout, the eighteen counts, the rollback.
    assert timeline.events[sleep_at + 1 : first_store_call] == ['SELECT', 'SET', *['SELECT'] * 18, 'ROLLBACK']
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
    db = FakeDb(stack='test')
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
    db = FakeDb(stack=label)
    store = store_for(config=tool.Response(200, {'deploymentLabel': label}))
    outcome = run_tool([mode, '--dry-run'], http=store, connect=db.connect)
    assert outcome.status == 0, outcome.text
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]
    assert store.writes() == []
    assert db.sql()[0] == STACK_TEXT
    assert [COUNT_TEXT.match(sql).group(1) for sql in db.sql()[1:]] == list(EIGHTEEN_TABLES)
    assert db.writes() == []
    assert db.commits == 0
    assert db.counts == TABLE_COUNTS
    assert outcome.lines[1] == f'dry run: {mode} would reset first; nothing is changed'
    # The heading, the eighteen counts and the kept line(s): on dev, the seed that would be exported and reloaded.
    kept = [*([KEPT_SEED_LINE] if label == 'dev' else []), 'kept: alembic_version, the schema and every other table']
    assert outcome.lines[2 : 21 + len(kept)] == [*counts_block('a reset would remove these rows', TABLE_COUNTS), *kept]
    # tj-7zjov1.297: a dry run writes no export and reads no revision.
    assert not any(outcome.seed_root.iterdir())
    assert db.copies == db.loads == db.mogrified == []
    bodies = [line for line in outcome.lines if line.startswith('{')]
    assert len(bodies) == (14 if mode == 'seed' else 0)
    assert_no_canary(outcome.text)
    # A dry run changes nothing, so it has nothing to verify: no RUNNING count, no wait, no recount.
    assert outcome.sleeps == []
    assert RUNNING_COUNT_TEXT not in db.sql()
    assert OPEN_REQUESTS_TEXT not in db.sql()
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
# (a) Inside the reset transaction, after the table lock (tj-grna9p.160) and before the TRUNCATE, any ingest_run row in
# state RUNNING refuses the reset: exit 1, the count named, nothing changed. (b) After the commit, wait two usage flush
# intervals, then recount all eighteen tables (tj-grna9p.160; ingest_run and dataset_read before it); a non-zero count in
# any is NOT CLEAN and exits 1. The wait is injected (doubles.run_tool); FakeDb.sleep is where work in flight writes.

RAIL_DEV = "rail: deploymentLabel is 'dev' (real Alpaca backend)"
STORE_APP = SERVER_ROOT / 'data' / 'store' / 'app'


def refused_line(running: int, open_requests: int = 0) -> str:
    # tj-7zjov1.51 (2): the refusal names both counts, whichever is non-zero.
    return (
        f'refused: {running} ingest run(s) are RUNNING and {open_requests} ingest request(s) are PENDING or QUEUED; '
        'nothing was changed. Wait for them, or restart data_ingest so its queued work is marked NOT_DELIVERED, '
        'then reset a quiet stack'
    )


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
    # Counted after the lock timeout and the table lock, in the transaction, with the open-request count right after
    # it (tj-7zjov1.51), and nothing after them: no TRUNCATE, no table count. The lock is the only non-read, and the
    # rollback that follows it releases it (tj-grna9p.160).
    assert db.sql() == [STACK_TEXT, LOCK_TIMEOUT, TABLE_LOCK, RUNNING_COUNT_TEXT, OPEN_REQUESTS_TEXT]
    assert db.writes() == [TABLE_LOCK]
    assert [event[0] for event in db.events] == ['connect', *['execute'] * 5, 'rollback', 'close']
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == TABLE_COUNTS
    assert outcome.sleeps == []
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('label', LABELS)
def test_the_table_lock_precedes_the_running_count_and_the_truncate_in_one_transaction(label):
    # tj-grna9p.160 (1): no run can start between the count and the TRUNCATE, because the lock is already held.
    db = labelled(label)
    assert reset_on(label, db, '--yes-reset').status == 0
    first_commit = db.events.index(('commit',))
    transaction = db.events[:first_commit]
    # One connection, no boundary inside it: the lock, the RUNNING count and the TRUNCATE share the transaction (on dev
    # the export's COPYs out and the reload's COPYs in too).
    assert transaction[0] == ('connect',)
    assert all(event[0] in ('execute', 'mogrify', 'copy', 'copy_in') for event in transaction[1:])
    statements = [' '.join(event[1].split()) for event in transaction[1:] if event[0] == 'execute']
    assert statements[0] == STACK_TEXT
    assert statements.count(TABLE_LOCK) == 1
    at = {
        sql: statements.index(sql)
        for sql in (LOCK_TIMEOUT, TABLE_LOCK, RUNNING_COUNT_TEXT, OPEN_REQUESTS_TEXT, TRUNCATE)
    }
    assert at[LOCK_TIMEOUT] < at[TABLE_LOCK] < at[RUNNING_COUNT_TEXT] < at[OPEN_REQUESTS_TEXT] < at[TRUNCATE]
    # tj-7zjov1.51 (2): the open-request count follows the RUNNING count directly.
    assert at[OPEN_REQUESTS_TEXT] == at[RUNNING_COUNT_TEXT] + 1
    # Nothing reads a table before the lock is held: the only statements ahead of it are the rail and the timeout, and
    # every COPY (the export's reads of the eighteen, the reload) comes after it.
    assert statements[: at[TABLE_LOCK]] == [STACK_TEXT, LOCK_TIMEOUT]
    lock_event = transaction.index(('execute', tool.LOCK_SQL))
    assert all(i > lock_event for i, event in enumerate(transaction) if event[0] in ('mogrify', 'copy', 'copy_in'))


@pytest.mark.parametrize('label', LABELS)
def test_the_recount_sets_the_lock_timeout_before_it_reads_all_eighteen(label):
    # tj-grna9p.160 (4): the recount must fail fast too, rather than hang behind a long lock.
    db = labelled(label)
    assert reset_on(label, db, '--yes-reset').status == 0
    _, recount = db.per_connection()
    assert recount == [STACK_TEXT, LOCK_TIMEOUT, *COUNTS]
    assert TABLE_LOCK not in recount


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


@pytest.mark.parametrize('label', LABELS)
def test_a_confirmed_reset_waits_once_after_its_commit_and_close_and_before_the_recount(label):
    db = labelled(label)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert outcome.sleeps == db.sleeps == [QUIESCE_SECONDS]
    at = db.events.index(('sleep', QUIESCE_SECONDS))
    assert db.events[at - 2 : at] == [('commit',), ('close',)]
    assert db.events[at + 1] == ('connect',)
    # The recount reads, and only reads: the lock and the TRUNCATE are the reset transaction's.
    assert db.writes() == reset_writes(label)
    assert not any(event[0] in ('copy', 'copy_in') for event in db.events[at:])


def test_the_real_wait_is_the_default():
    assert inspect.signature(tool.run_reset).parameters['sleep'].default is time.sleep
    assert inspect.signature(tool.run).parameters['sleep'].default is time.sleep


def test_the_not_clean_message_is_the_design():
    assert tool.NOT_CLEAN_MESSAGE == 'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack'


def written_in(label: str, extra: dict[str, int]) -> dict[str, int]:
    """Absolute counts after work in flight adds `extra` rows to what the reset left."""
    return {table: left(label)[table] + rows for table, rows in extra.items()}


def dirty(label: str, extra: dict[str, int]) -> str:
    """How NOT CLEAN names the tables: the count found, and on dev the manifest's count it expected."""
    return ', '.join(
        f'{table} {left(label)[table] + rows}' + (f' expected {left(label)[table]}' if label == 'dev' else '')
        for table, rows in sorted(extra.items(), key=lambda item: EIGHTEEN_TABLES.index(item[0]))
    )


@pytest.mark.parametrize('label', LABELS)
@pytest.mark.parametrize('table', EIGHTEEN_TABLES)
def test_a_row_in_any_one_of_the_eighteen_after_the_wait_is_not_clean_naming_that_table(table, label):
    # tj-grna9p.160 (1): the recount reads all eighteen, and names each table that differs (and only those). On dev the
    # recount expects the manifest's counts (tj-7zjov1.297), so a row added on top of the seed is the same NOT CLEAN.
    db = labelled(label, written_while_waiting=written_in(label, {table: 3}))
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == (
        f'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack ({dirty(label, {table: 3})})'
    )
    assert outcome.lines[-2] == CLEAN_TAIL[0]
    assert not any('verified clean' in line for line in outcome.lines)
    assert db.commits == 1


@pytest.mark.parametrize('table', [table for table in EIGHTEEN_TABLES if SEED_ROWS[table]])
def test_a_dev_table_that_lost_rows_during_the_wait_is_not_clean_too(table):
    # The dev recount compares for equality, not for "no more than": a seed table emptied by something in flight (a
    # reaper, an operator delete) is a change after the reset as well.
    db = labelled('dev', written_while_waiting={table: SEED_ROWS[table] - 1})
    outcome = reset_on('dev', db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == (
        'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack '
        f'({table} {SEED_ROWS[table] - 1} expected {SEED_ROWS[table]})'
    )


@pytest.mark.parametrize(
    'written',
    [
        {'ingest_run': 1},
        {'dataset_read': 4},
        {'ingest_run': 2, 'dataset_read': 6},
        {'dataset_deletion': 1, 'stock_market_activity': 9},
    ],
    ids=['a-run', 'a-read', 'both', 'bars-and-a-deletion'],
)
@pytest.mark.parametrize('label', LABELS)
def test_work_that_writes_during_the_wait_is_not_clean_and_exits_1(written, label):
    db = labelled(label, written_while_waiting=written_in(label, written))
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines[-1] == (
        f'NOT CLEAN: work in flight wrote after the reset; rerun reset on a quiet stack ({dirty(label, written)})'
    )
    assert outcome.lines[-2] == CLEAN_TAIL[0]
    assert not any('verified clean' in line for line in outcome.lines)
    assert outcome.sleeps == [QUIESCE_SECONDS]
    assert db.commits == 1
    assert_no_canary(outcome.text)


def test_the_not_clean_lines_are_the_design_written_out():
    # The helper above composes the names; one case of each label is written out in full, so it cannot drift.
    assert dirty('test', {'dataset_deletion': 1, 'stock_market_activity': 9}) == (
        'stock_market_activity 9, dataset_deletion 1'
    )
    assert dirty('dev', {'dataset_deletion': 1, 'stock_market_activity': 9}) == (
        'stock_market_activity 12 expected 3, dataset_deletion 1 expected 0'
    )


@pytest.mark.parametrize('label', LABELS)
def test_seed_after_a_not_clean_reset_posts_nothing(label):
    store, db = store_on(label), labelled(label, written_while_waiting=written_in(label, {'dataset_read': 1}))
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1].startswith('NOT CLEAN: ')
    assert store.posts() == []


@pytest.mark.parametrize('label', LABELS)
def test_a_clean_recount_exits_0_and_says_so(label):
    db = labelled(label)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0
    assert outcome.lines[-2:] == tail(label)


# ---- the open requests (tj-7zjov1.51) -------------------------------------------------------------------------------
#
# (2) Inside the same transaction, after the lock and right after the RUNNING count, the reset counts ingest_request rows
# in state PENDING or QUEUED. Either count non-zero refuses: exit 1, one rollback, no TRUNCATE, both counts named.
# NOT_DELIVERED and every finished state do not block: they are emptied with the table. FakeDb.request_states answers
# the count for the literals the statement names.

INGEST_REQUEST_MODEL = STORE_MODELS / 'ingest_request.py'
FINISHED_OR_NOT_DELIVERED = {'NOT_DELIVERED': 2, 'SUCCEEDED': 5, 'EMPTY': 1, 'FAILED': 3}


def _with_requests(states: dict[str, int], running: int = 0, label: str = 'dev') -> FakeDb:
    db = labelled(label, running=running, request_states=dict(states))
    db.counts['ingest_request'] = sum(states.values())
    return db


def _state_literals(sql: str) -> list[str]:
    match = re.fullmatch(r'SELECT count\(\*\) FROM ingest_request WHERE state IN \((?P<states>[^)]*)\)', sql)
    assert match, sql
    return re.findall(r"'([A-Z_]+)'", match.group('states'))


def test_the_open_request_count_is_the_designed_statement():
    assert ' '.join(tool.OPEN_REQUESTS_SQL.split()) == OPEN_REQUESTS_TEXT
    assert _state_literals(' '.join(tool.OPEN_REQUESTS_SQL.split())) == list(OPEN_REQUEST_STATES)


def test_the_open_request_literals_are_the_values_the_store_writes():
    # The store writes IngestRequestState's VALUE (values_callable); revision f2b7d4a91c36's enum type carries the same
    # literals. Read from the source, so a renamed member or value turns this red.
    source = INGEST_REQUEST_MODEL.read_text(encoding='utf-8')
    tree = ast.parse(source)
    state_enum = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IngestRequestState')
    members = {
        node.targets[0].id: node.value.value
        for node in state_enum.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    for state in _state_literals(' '.join(tool.OPEN_REQUESTS_SQL.split())):
        assert members[state] == state
    assert re.search(r"Enum\(IngestRequestState, name='ingest_request_state', values_callable=_values\)", source)
    assert 'return [member.value for member in enum_cls]' in source
    [migration] = MIGRATIONS.glob('f2b7d4a91c36_*.py')
    migration_states = _module_constants_any(migration)['STATE_MEMBERS']
    assert set(OPEN_REQUEST_STATES) <= set(migration_states)
    assert set(migration_states) == set(members.values())
    # NOT_DELIVERED is an open state to the store (OPEN_STATES: it is re-requested), but the design rules it does not
    # block a reset: ingest no longer holds it. So the guard names exactly the other two.
    open_states = re.search(r'^OPEN_STATES = frozenset\(\{(?P<members>[^}]*)\}\)$', source, re.MULTILINE)
    assert open_states, 'OPEN_STATES moved; re-read the design for which states block a reset'
    assert sorted(re.findall(r'IngestRequestState\.([A-Z_]+)', open_states.group('members'))) == [
        'NOT_DELIVERED',
        'PENDING',
        'QUEUED',
    ]
    assert 'NOT_DELIVERED' not in tool.OPEN_REQUESTS_SQL


def _module_constants_any(path) -> dict[str, object]:
    """Module-level NAME = <literal>, tuples included, without importing the module."""
    constants = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return constants


@pytest.mark.parametrize(
    ('running', 'states', 'open_requests'),
    [
        (0, {'PENDING': 2}, 2),
        (0, {'QUEUED': 1}, 1),
        (0, {'PENDING': 1, 'QUEUED': 3}, 4),
        # The non-blocking states beside an open one are not counted.
        (0, {'QUEUED': 1, **FINISHED_OR_NOT_DELIVERED}, 1),
        # Both guards at once: both counts named.
        (2, {'PENDING': 1}, 1),
    ],
    ids=['pending-only', 'queued-only', 'pending-and-queued', 'queued-beside-finished', 'running-and-pending'],
)
def test_an_open_request_refuses_the_reset_naming_both_counts_and_changes_nothing(running, states, open_requests):
    db = _with_requests(states, running=running)
    counts_before = dict(db.counts)
    outcome = reset(db, '--yes-reset')
    assert outcome.status == 1
    assert outcome.lines == [RAIL_DEV, refused_line(running, open_requests)]
    assert db.sql() == [STACK_TEXT, LOCK_TIMEOUT, TABLE_LOCK, RUNNING_COUNT_TEXT, OPEN_REQUESTS_TEXT]
    assert TRUNCATE not in db.sql()
    assert db.writes() == [TABLE_LOCK]
    assert [event[0] for event in db.events] == ['connect', *['execute'] * 5, 'rollback', 'close']
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == counts_before
    assert db.request_states == states
    assert outcome.sleeps == []
    assert_no_canary(outcome.text)


def test_seed_with_an_open_request_posts_nothing():
    store, db = store_for(), _with_requests({'QUEUED': 2})
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1] == refused_line(0, 2)
    assert store.posts() == []
    assert [c.route for c in store.calls] == ['/ui/v1/config']


@pytest.mark.parametrize('label', LABELS)
@pytest.mark.parametrize('state', sorted(FINISHED_OR_NOT_DELIVERED))
def test_a_not_delivered_or_finished_request_does_not_block_and_is_replaced(state, label):
    # Emptied by the TRUNCATE on both labels; on dev the seed's own requests (the last 90 days, O1) are reloaded after.
    db = _with_requests({state: 4}, label=label)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert TRUNCATE in db.sql()
    assert db.commits == 1
    assert db.counts['ingest_request'] == left(label)['ingest_request']
    assert db.request_states == {}
    assert outcome.lines[-2:] == tail(label)


@pytest.mark.parametrize('label', LABELS)
def test_requests_in_every_non_blocking_state_together_do_not_block(label):
    db = _with_requests(FINISHED_OR_NOT_DELIVERED, label=label)
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    assert outcome.lines[1:20] == counts_block('reset: rows before', {**TABLE_COUNTS, 'ingest_request': 11})
    after = outcome.lines.index('reset: rows after:')
    assert outcome.lines[after:] == [*counts_block('reset: rows after', left(label)), *tail(label)]
    if label == 'test':
        assert after == 20


@pytest.mark.parametrize('label', LABELS)
def test_a_reset_after_the_open_requests_drained_replaces_the_requests_and_empties_the_instance_markers(label):
    # Refused while a request is QUEUED; once data_ingest has restarted and marked it NOT_DELIVERED, the same
    # database resets. ingest_instance is empty after on both labels (O1: the live ingest re-registers); ingest_request
    # is empty on test and holds the seed's requests on dev, at commit and after the wait.
    db = _with_requests({'QUEUED': 3}, label=label)
    first = reset_on(label, db, '--yes-reset')
    assert first.status == 1
    assert not any(first.seed_root.iterdir())
    assert (db.counts['ingest_request'], db.counts['ingest_instance']) == (3, TABLE_COUNTS['ingest_instance'])
    db.request_states = {'NOT_DELIVERED': 3}
    outcome = reset_on(label, db, '--yes-reset')
    assert outcome.status == 0, outcome.text
    after = outcome.lines.index('reset: rows after:')
    assert f'  ingest_request: {left(label)["ingest_request"]}' in outcome.lines[after:]
    assert '  ingest_instance: 0' in outcome.lines[after:]
    assert db.counts['ingest_request'] == left(label)['ingest_request']
    assert db.counts['ingest_instance'] == 0
    _, _, recount = db.per_connection()
    assert recount == [STACK_TEXT, LOCK_TIMEOUT, *COUNTS]
    assert outcome.lines[-1] == tail(label)[-1]
