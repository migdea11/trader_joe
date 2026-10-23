"""degrade: one transaction, bound to the looked-up dev-degraded entry, New York midnights, no other table (validator).

The database is a scripted connection (doubles.FakeDb) that records every statement and its parameters. The SQL text
itself is asserted, because 'touches no other table' is a property of the text the tool sends.
"""

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import STACK_TEXT, FakeDb, dataset_id, run_tool, store_for


pytestmark = pytest.mark.build_infra

OTHER_TABLES = ('dataset_deletion', 'dataset_override_log', 'dataset_read', 'dataset_read_daily', 'ingest_run')
R12_KEY = ('dev-degraded', 'DIA', 'THIRTY_MINUTES')
R13_KEY = ('dev-degraded', 'QQQ', 'FIVE_MINUTES')


def degrade(db: FakeDb, *extra: str):
    return run_tool(['degrade', *extra], http=store_for(), connect=db.connect)


def test_one_connection_one_commit_the_identity_query_then_the_four_statements_in_order():
    # tj-grna9p.194: the database rail's identity query is the connection's first statement.
    db = FakeDb.with_plan_entries()
    outcome = degrade(db)
    assert outcome.status == 0, outcome.text
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 1, 0, 1)
    assert [sql for sql, _ in db.statements] == [
        STACK_TEXT,
        tool.LOOKUP_SQL,
        tool.DELETE_SQL,
        tool.LOOKUP_SQL,
        tool.DELETE_SQL,
    ]
    assert [params for _, params in db.statements][1::2] == [R12_KEY, R13_KEY]


def test_each_delete_is_bound_to_the_looked_up_id_and_the_dev_degraded_owner():
    db = FakeDb.with_plan_entries()
    assert degrade(db).status == 0
    deletes = [params for sql, params in db.statements if sql == tool.DELETE_SQL]
    for params, row_id in zip(deletes, ['r12', 'r13'], strict=True):
        assert params[0] == dataset_id(row_id)
        assert params[3] == dataset_id(row_id)
        assert params[4] == 'dev-degraded'


def test_the_delete_text_repeats_the_owner_test_against_the_entry():
    sql = ' '.join(tool.DELETE_SQL.split())
    assert sql.startswith(
        'DELETE FROM stock_market_activity WHERE dataset_id = %s AND timestamp >= %s AND timestamp < %s'
    )
    assert 'EXISTS (SELECT 1 FROM store_dataset_entry e WHERE e.id = %s AND e.owner = %s)' in sql
    assert sql.count('%s') == 5


def test_the_lookup_text_keys_on_owner_symbol_and_granularity_and_only_reads():
    sql = ' '.join(tool.LOOKUP_SQL.split())
    assert sql == 'SELECT id FROM store_dataset_entry WHERE owner = %s AND asset_symbol = %s AND granularity = %s'


def test_no_statement_names_another_table_or_writes_anything_but_the_bar_delete():
    db = FakeDb.with_plan_entries()
    assert degrade(db).status == 0
    # tj-grna9p.194: the one read outside the dataset tables is the database rail's identity query, exactly.
    assert [sql for sql, _ in db.statements if 'pg_settings' in sql] == [STACK_TEXT]
    for sql, _ in db.statements:
        if sql == STACK_TEXT:
            continue
        lowered = sql.lower()
        for table in OTHER_TABLES:
            assert not re.search(rf'\b{table}\b', lowered), (table, sql)
        assert not re.search(r'\b(insert|update|truncate|drop|alter|create|grant)\b', lowered), sql
        tables = set(re.findall(r'\b(?:from|join|into|update)\s+([a-z_]+)', lowered))
        assert tables <= {'store_dataset_entry', 'stock_market_activity'}, tables
        if 'delete' in lowered:
            assert lowered.startswith('delete from stock_market_activity ')


NEW_YORK = ZoneInfo('America/New_York')


def ny_midnight(day: date) -> datetime:
    """Computed here, independently of the tool: the instant a New York calendar day begins."""
    return datetime(day.year, day.month, day.day, tzinfo=NEW_YORK)


def plan_window(row_id: str, today: date) -> tuple[datetime, datetime]:
    """The plan's drop days for a row, as New York midnights."""
    row = next(row for row in tool.load_plan() if row.id == row_id)
    days = (row.degrade.first_day, row.degrade.last_day)
    first, last = (date.fromordinal(today.toordinal() + int(day[1:])) for day in days)
    return ny_midnight(first), ny_midnight(last)


@pytest.mark.parametrize('day', ['2026-10-20', '2026-11-13', '2026-03-20', '2026-04-08', '2024-03-01'])
def test_the_window_is_bound_as_the_plans_new_york_midnights(day):
    today = date.fromisoformat(day)
    db = FakeDb.with_plan_entries()
    later = datetime(2027, 1, 1, tzinfo=UTC)
    outcome = run_tool(['degrade', '--date', day], http=store_for(), connect=db.connect, now=later)
    assert outcome.status == 0, outcome.text
    deletes = [params for sql, params in db.statements if sql == tool.DELETE_SQL]
    for params, row_id in zip(deletes, ['r12', 'r13'], strict=True):
        first, last = plan_window(row_id, today)
        assert (params[1], params[2]) == (first, last)
        assert params[1].utcoffset() == first.utcoffset() and params[2].utcoffset() == last.utcoffset()


def test_a_window_across_the_autumn_change_is_bound_with_each_midnights_offset():
    row = next(row for row in tool.load_plan() if row.id == 'r13')
    rogue = tool.PlanRow(**{**row.__dict__, 'degrade': tool.Degrade('D-15', 'D-11', 'x')})
    db = FakeDb.with_plan_entries()
    tool.run_degrade(db.connect, [rogue], date(2026, 11, 13), tool.Reporter(lambda _: None))
    _, first, last, _, _ = db.statements[1][1]
    assert first.astimezone(UTC) == datetime(2026, 10, 29, 4, tzinfo=UTC)
    assert last.astimezone(UTC) == datetime(2026, 11, 2, 5, tzinfo=UTC)


def test_each_row_prints_its_deleted_count_and_window():
    db = FakeDb.with_plan_entries()
    db.delete_counts = {dataset_id('r12'): 39, dataset_id('r13'): 0}
    outcome = degrade(db)
    today = date(2026, 10, 20)
    lines = []
    for row_id, count in (('r12', 39), ('r13', 0)):
        first, last = plan_window(row_id, today)
        lines.append(f'{row_id} deleted={count} window={first.isoformat()}..{last.isoformat()}')
    assert outcome.lines[1:] == lines


@pytest.mark.parametrize('found', [[], [('a',), ('b',)], [('a',), ('b',), ('c',)]])
@pytest.mark.parametrize('broken', ['r12', 'r13'])
def test_anything_but_exactly_one_entry_refuses_and_rolls_the_whole_transaction_back(found, broken):
    db = FakeDb.with_plan_entries()
    db.lookups[R12_KEY if broken == 'r12' else R13_KEY] = found
    outcome = degrade(db)
    assert outcome.status == 1
    assert f'{broken}: refused, expected exactly one entry and found {len(found)}' in outcome.text
    assert (db.commits, db.rollbacks, db.closes) == (0, 1, 1)
    # Nothing is deleted for the broken row, and the earlier row's delete is rolled back with it.
    deletes = [params for sql, params in db.statements if sql == tool.DELETE_SQL]
    assert len(deletes) == (0 if broken == 'r12' else 1)


def test_a_database_error_rolls_back_and_names_the_type_only():
    db = FakeDb.with_plan_entries()
    db.execute_error = RuntimeError('relation "stock_market_activity" does not exist DETAILTEXT')
    outcome = degrade(db)
    assert outcome.status == 1
    assert 'the degrade transaction failed and was rolled back (RuntimeError)' in outcome.text
    assert 'DETAILTEXT' not in outcome.text
    assert (db.commits, db.rollbacks, db.closes) == (0, 1, 1)


def test_a_connection_error_names_the_type_only():
    db = FakeDb.with_plan_entries()
    db.connect_error = ConnectionError('could not connect to server at db:5432 DETAILTEXT')
    outcome = degrade(db)
    assert outcome.status == 1
    assert 'the database connection failed (ConnectionError)' in outcome.text
    assert 'DETAILTEXT' not in outcome.text


def test_a_row_whose_owner_is_not_dev_degraded_is_refused_before_connecting():
    # The plan cannot say this (the parser refuses it), so the row is built directly.
    row = next(row for row in tool.load_plan() if row.id == 'r12')
    rogue = tool.PlanRow(**{**row.__dict__, 'owner': 'dev-datasets'})
    db = FakeDb.with_plan_entries()
    with pytest.raises(tool.DevDatasetsError, match='refused, the owner is not dev-degraded'):
        tool.run_degrade(db.connect, [rogue], date(2026, 10, 20), tool.Reporter(lambda _: None))
    assert db.connects == 0


def test_row_limits_degrade_to_that_row():
    db = FakeDb.with_plan_entries()
    assert degrade(db, '--row', 'r13').status == 0
    assert db.statements[0][0] == STACK_TEXT
    assert db.statements[1][1] == R13_KEY
    assert len(db.statements) == 3


def test_rows_without_a_degrade_are_never_touched():
    db = FakeDb.with_plan_entries()
    assert degrade(db, '--row', 'r1', '--row', 'r7').status == 0
    assert db.statements == []


def test_the_real_connect_takes_the_environment_and_reveals_the_password_only_to_the_driver(monkeypatch):
    import sys
    import types

    seen = {}
    fake = types.ModuleType('psycopg2')
    fake.connect = lambda **kwargs: seen.update(kwargs) or 'connection'
    monkeypatch.setitem(sys.modules, 'psycopg2', fake)
    env = tool.load_environment(
        {'POSTGRES_PASS': 'pw-value', 'DATABASE_NAME': 'dbhost', 'POSTGRES_USER': 'u', 'POSTGRES_DB_NAME': 'n'}
    )
    assert tool._connect(env) == 'connection'
    assert seen == {
        'host': 'dbhost',
        'port': 5432,
        'user': 'u',
        'dbname': 'n',
        'password': 'pw-value',
        'connect_timeout': 10,
    }


def test_the_environment_defaults_are_the_dev_stacks():
    env = tool.load_environment({})
    assert (env.store_url, env.db_host, env.db_user, env.db_name) == ('http://data_store', 'db', 'postgres', 'postgres')
    assert tool.load_environment({'DEV_DATASETS_STORE_URL': 'http://x:8080/'}).store_url == 'http://x:8080'
