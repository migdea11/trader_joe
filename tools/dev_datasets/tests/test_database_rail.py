"""The database rail: each connection proves cluster_name came from the server command line first (validator).

THE DESIGN (bead tj-grna9p.194, the owner's rule that seed data never reaches prod; the architect's option 3). The HTTP
rail proves only the store that answered; the database is named separately. So every connection the tool opens runs
SELECT setting, source FROM pg_settings WHERE name = 'cluster_name' before any other statement, and refuses unless
EXACTLY ONE row came back whose source is 'command line' (the -c option the dev and test compose files pass) and whose
value is 'dev' or 'test' AND equals the label check_rail returned. Any other source (a client's PGOPTIONS or SET, ALTER
ROLE or ALTER DATABASE, a configuration file, prod's default), no row, two rows, a NULL, empty or other value, and a
value that disagrees with the store all refuse. A refusal names the value and the source only, changes nothing and
exits 1; the connection is rolled back and closed.

Every path that opens the database is driven here through run(): the confirmed reset (and seed's), the counts printed
without --yes-reset, the dry runs' counts, degrade, and the post-reset recount on its own connection. The double
(doubles.FakeDb) records every statement and transaction boundary per connection, so 'nothing ran before or after the
refusal' is read off the record, not inferred from the output.
"""

import sys
import types

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    COMMAND_LINE,
    DB_PASSWORD,
    GOOD_ENV,
    STACK_NAME,
    STACK_TEXT,
    TABLE_COUNTS,
    FakeDb,
    assert_no_canary,
    fake_broker_store,
    run_tool,
    store_for,
)


pytestmark = pytest.mark.build_infra

# Every argv that opens the database, by the path it takes in run().
DATABASE_PATHS = {
    'reset-confirmed': ['reset', '--yes-reset'],
    'seed-confirmed': ['seed', '--yes-reset'],
    'reset-counts': ['reset'],
    'seed-counts': ['seed'],
    'reset-dry-run': ['reset', '--dry-run'],
    'seed-dry-run': ['seed', '--dry-run'],
    'degrade': ['degrade'],
}

# (the store's label, the pg_settings rows the server answers): every one refused.
REFUSED = {
    # The value is wrong, from the command line.
    'value-null': ('dev', [(None, COMMAND_LINE)]),
    'value-empty': ('dev', [('', COMMAND_LINE)]),
    'value-prod': ('dev', [('prod', COMMAND_LINE)]),
    'value-staging': ('dev', [('staging', COMMAND_LINE)]),
    'value-upper-DEV': ('dev', [('DEV', COMMAND_LINE)]),
    'value-padded-dev': ('dev', [(' dev', COMMAND_LINE)]),
    'dev-store-test-db': ('dev', [('test', COMMAND_LINE)]),
    'test-store-dev-db': ('test', [('dev', COMMAND_LINE)]),
    'test-store-null': ('test', [(None, COMMAND_LINE)]),
    'test-store-prod': ('test', [('prod', COMMAND_LINE)]),
    # The value is right, the source is not the server command line.
    'source-client': ('dev', [('dev', 'client')]),
    'source-user': ('dev', [('dev', 'user')]),
    'source-database': ('dev', [('dev', 'database')]),
    'source-session': ('dev', [('dev', 'session')]),
    'source-default': ('dev', [('dev', 'default')]),
    'source-override': ('dev', [('dev', 'override')]),
    'source-configuration-file': ('dev', [('dev', 'configuration file')]),
    'source-padded': ('dev', [('dev', 'command line ')]),
    'source-null': ('dev', [('dev', None)]),
    'test-source-client': ('test', [('test', 'client')]),
    # What a prod server, started without the option, really answers.
    'prod-default': ('dev', [('', 'default')]),
    # The shape is wrong.
    'no-row': ('dev', []),
    'two-good-rows': ('dev', [('dev', COMMAND_LINE), ('dev', COMMAND_LINE)]),
    'good-then-bad-row': ('dev', [('dev', COMMAND_LINE), ('prod', 'client')]),
}

# Where the database is, as distinctive fakes: none of them may reach the output (a DSN is never printed).
DSN_PARTS = {
    'DATABASE_NAME': 'dbhost-DSNCANARY-h1',
    'POSTGRES_USER': 'user-DSNCANARY-u1',
    'POSTGRES_DB_NAME': 'n-DSNCANARY',
}
ENV = {**GOOD_ENV, **DSN_PARTS}


def store_labelled(label: str):
    return store_for() if label == 'dev' else fake_broker_store()


def refusal(rows: list[tuple], label: str) -> str:
    """The refusal line, from the design: the value and the source (none when absent or empty) and the store's label.

    With no row or more than one, there is no one value or source to name.
    """
    value, source = rows[0] if len(rows) == 1 else (None, None)
    return (
        f'refused: the database is not this dev or test stack ({STACK_NAME} {value or "none"!r} from source '
        f'{source or "none"!r}, the store says {label!r}); nothing was changed'
    )


def assert_refused_untouched(outcome, db: FakeDb, store, rows, label) -> None:
    assert outcome.status == 1, outcome.text
    assert outcome.lines[-1] == refusal(rows, label)
    # One connection, the identity query alone, then rollback and close: nothing counted, written or committed.
    assert db.per_connection() == [[STACK_TEXT]]
    assert [event[0] for event in db.events] == ['connect', 'execute', 'rollback', 'close']
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert db.counts == TABLE_COUNTS and db.truncated is False
    assert db.writes() == []
    # The store heard the rail and nothing after it.
    assert [(call.method, call.route) for call in store.calls] == [('GET', '/ui/v1/config')]
    assert outcome.sleeps == []
    assert not any(line.startswith(('a reset would remove', 'reset: rows')) for line in outcome.lines)
    # The rail's refusal is reported as itself, never rewrapped as a connection failure.
    assert 'the database connection failed' not in outcome.text
    assert_no_canary(outcome.text)
    assert 'DSNCANARY' not in outcome.text


# ---- the constants are the design's -------------------------------------------------------------------------------


def test_the_setting_the_query_and_the_source_are_the_designs():
    assert tool.STACK_SETTING == STACK_NAME
    assert ' '.join(tool.STACK_SQL.split()) == STACK_TEXT
    assert tool.STACK_SOURCE == COMMAND_LINE
    assert tool.STACK_SQL.count(f"'{tool.STACK_SETTING}'") == 1


@pytest.mark.parametrize('value', ['prod', 'staging', '', None])
def test_the_check_refuses_a_value_outside_dev_and_test_even_when_it_equals_the_label(value):
    # check_rail only ever returns dev or test, so through run() 'equal to the label' implies 'dev or test'; the check
    # must not lean on that: it holds both conditions itself.
    db = FakeDb(stack=value)
    connection = db.connect()
    with pytest.raises(tool.DevDatasetsError, match='refused: the database is not this dev or test stack'):
        tool.check_database_stack(connection, value)
    assert (db.rollbacks, db.closes) == (1, 1)


@pytest.mark.parametrize('label', ['dev', 'test'])
def test_the_check_passes_only_the_one_command_line_row_equal_to_the_label(label):
    db = FakeDb(stack=label)
    connection = db.connect()
    tool.check_database_stack(connection, label)
    # Passing leaves the connection open and untouched for the work that follows.
    assert (db.rollbacks, db.closes, db.commits) == (0, 0, 0)
    assert db.sql() == [STACK_TEXT]


# ---- every path refuses every wrong database before any other statement ------------------------------------------


@pytest.mark.parametrize(('label', 'rows'), list(REFUSED.values()), ids=list(REFUSED))
@pytest.mark.parametrize('argv', list(DATABASE_PATHS.values()), ids=list(DATABASE_PATHS))
def test_every_database_path_refuses_a_wrong_database_before_anything_else(argv, label, rows):
    db = FakeDb.with_plan_entries(label)
    db.identity_rows = rows
    store = store_labelled(label)
    outcome = run_tool(argv, ENV, http=store, connect=db.connect)
    assert_refused_untouched(outcome, db, store, rows, label)


@pytest.mark.parametrize('label', ['dev', 'test'])
@pytest.mark.parametrize('argv', list(DATABASE_PATHS.values()), ids=list(DATABASE_PATHS))
def test_on_the_right_database_every_connection_asks_first_and_only_first(argv, label):
    db = FakeDb.with_plan_entries(label)
    outcome = run_tool(argv, ENV, http=store_labelled(label), connect=db.connect)
    assert outcome.status in (0, 1), outcome.text
    assert 'refused: the database' not in outcome.text
    groups = db.per_connection()
    assert groups, 'the path opened no connection'
    for statements in groups:
        assert statements[0] == STACK_TEXT, statements
        assert STACK_TEXT not in statements[1:], statements


# ---- the post-reset recount opens its own connection, and it is checked too ---------------------------------------


@pytest.mark.parametrize(
    'rows',
    [[('', 'default')], [('dev', 'client')], [('test', COMMAND_LINE)], []],
    ids=['prod-default', 'client', 'test-db', 'no-row'],
)
@pytest.mark.parametrize('mode', ['reset', 'seed'])
def test_the_recount_connection_is_checked_and_a_wrong_one_refuses(mode, rows):
    first, second = FakeDb(), FakeDb(identity_rows=rows)
    handed = iter([first, second])
    store = store_for()
    outcome = run_tool([mode, '--yes-reset'], ENV, http=store, connect=lambda: next(handed).connect())
    assert outcome.status == 1
    assert outcome.lines[-1] == refusal(rows, 'dev')
    # The reset itself ran on the first, checked, connection; the recount's asked and stopped.
    assert first.per_connection()[0][0] == STACK_TEXT and first.commits == 1
    assert second.per_connection() == [[STACK_TEXT]]
    assert [event[0] for event in second.events] == ['connect', 'execute', 'rollback', 'close']
    assert not any('verified clean' in line for line in outcome.lines)
    assert store.posts() == []
    assert_no_canary(outcome.text)


# ---- the identity query failing ------------------------------------------------------------------------------------


@pytest.mark.parametrize('argv', list(DATABASE_PATHS.values()), ids=list(DATABASE_PATHS))
def test_an_identity_query_that_fails_stops_names_the_type_only_and_closes(argv):
    db = FakeDb.with_plan_entries()
    db.identity_error = RuntimeError(f'permission denied password={DB_PASSWORD} DSNCANARY')
    store = store_for()
    outcome = run_tool(argv, ENV, http=store, connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the database identity check failed (RuntimeError)'
    assert db.per_connection() == [[STACK_TEXT]]
    assert (db.commits, db.closes) == (0, 1)
    assert store.posts() == []
    assert_no_canary(outcome.text)
    assert 'DSNCANARY' not in outcome.text


# ---- the real driver path: connect=None is checked as well ---------------------------------------------------------


@pytest.mark.parametrize(
    ('rows', 'status'),
    [([('', 'default')], 1), ([('dev', 'client')], 1), ([('dev', COMMAND_LINE)], 0)],
    ids=['prod-default', 'client', 'dev'],
)
def test_the_default_driver_connection_is_checked(monkeypatch, rows, status):
    db = FakeDb(identity_rows=rows)
    driver = types.ModuleType('psycopg2')
    driver.connect = lambda **_: db.connect()
    monkeypatch.setitem(sys.modules, 'psycopg2', driver)
    outcome = run_tool(['reset', '--yes-reset'], ENV, http=store_for(), connect=None)
    assert outcome.status == status, outcome.text
    assert db.per_connection()[0][0] == STACK_TEXT
    if status:
        assert outcome.lines[-1] == refusal(rows, 'dev')
        assert db.per_connection() == [[STACK_TEXT]]
        assert db.commits == 0
