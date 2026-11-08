"""Revision b8e4c1f7a923, the push-path columns, against its design and its models, offline (tj-7zjov1.123, P34c).

THE DESIGN is the bead's WORK items 1 to 5 and its acceptance, with the architect's ruling on P50 (ADR tj-26jt9w
addendum 13). The literals below are the design's:
  * ingest_run.served_start, served_end: timestamptz, nullable;
  * ingest_request.trigger: the EXISTING ingest_run_trigger type (never created or dropped here), nullable;
    expiry (timestamptz) and collect (boolean), nullable; spend_unknown_attempts integer NOT NULL server default 0;
  * store_dataset_entry.stopped_cause: string, nullable, NO server default; the CHECK
    ck_store_dataset_entry_stopped_cause_needs_stop, stopped_cause IS NULL OR collect IS FALSE;
  * purely additive and unmarked, directly on f2b7d4a91c36, the single head; downgrade drops the CHECK and every
    column, and nothing else.

THREE TIERS, NONE A DATABASE.
  1. The revision body against a recording stand-in for alembic.op: what it calls, and that downgrade() is its inverse.
  2. The CHECK EVALUATED: the revision's condition run by SQLite over every (collect, stopped_cause) combination, and
     the model's CHECK shown to bite through the ORM; so "refuses a cause unless collect is false" is a measured truth
     table, not a string match. The SQL text of the two is compared as well.
  3. Alembic's own offline rendering (upgrade f2b7d4a91c36:b8e4c1f7a923 --sql, and the downgrade back) compared
     statement by statement with the MODELS' DDL compiled for Postgres.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb): revision against model, downgrade against upgrade, rendered SQL against
the compiled model; a literal appears only where the design names the value.

NOT PROVED HERE, and tests/system does on Postgres: that the revision applies from empty and on the f2b7d4a91c36 seed
with every row kept and the new columns NULL or 0 (test_migration_with_data.py), that the downgrade restores the
parent's schema with the rows (test_migration_downgrade.py), that Postgres's ADD CONSTRAINT accepts every existing row,
and that alembic check is clean at the head.
"""

import importlib.util
import re
import sqlite3
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import AddConstraint, CreateColumn

from common.enums.data_stock import UpdateType
from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.tests.test_ui_datasets_route import entry


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'b8e4c1f7a923'
PARENT = 'f2b7d4a91c36'
CHILD = '39c28a39b290'  # its one child since tj-7zjov1.153 (P35r): ingest_request.expiry_type
HEAD = 'b6d1f93a7c42'  # the head since tj-7zjov1.33 (P20): the panel tables, on a3f8d2c6e914, three hops above CHILD
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_push_path_columns.py'
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
CHECK_NAME = 'ck_store_dataset_entry_stopped_cause_needs_stop'  # the design's name (addendum 13)
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

RUN = IngestRun.__table__
REQUEST = IngestRequest.__table__
ENTRY = StoreDatasetEntry.__table__
# The design's columns, per table (WORK items 1 to 5).
DESIGN_COLUMNS = {
    RUN.name: {'served_start', 'served_end'},
    REQUEST.name: {'trigger', 'expiry', 'collect', 'spend_unknown_attempts'},
    ENTRY.name: {'stopped_cause'},
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(f'revision_{REVISION}', REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str) -> tuple[MagicMock, list[str], list[str]]:
    """Run upgrade() or downgrade() against a recording op; returns (op, enum types created, enum types dropped)."""
    module = _load()
    op = MagicMock(name='op')
    with (
        patch.object(module, 'op', op),
        patch.object(postgresql.ENUM, 'create', autospec=True) as create,
        patch.object(postgresql.ENUM, 'drop', autospec=True) as drop,
    ):
        getattr(module, direction)()
    return (
        op,
        [call.args[0].name for call in create.call_args_list],
        [call.args[0].name for call in drop.call_args_list],
    )


def _calls(op: MagicMock) -> list[str]:
    return [name for name, _, _ in op.method_calls]


def _added(op: MagicMock) -> list[tuple[str, sa.Column]]:
    return [(args[0], args[1]) for name, args, _ in op.method_calls if name == 'add_column']


def _compiled(element: Any) -> str:
    return ' '.join(str(element.compile(dialect=DIALECT)).split())


def _check_condition() -> str:
    op, _, _ = _run('upgrade')
    ((name, table, condition),) = [args for name_, args, _ in op.method_calls if name_ == 'create_check_constraint']
    assert (name, table) == (CHECK_NAME, ENTRY.name)
    return str(condition)


def _model_check() -> sa.CheckConstraint:
    (check,) = [c for c in ENTRY.constraints if isinstance(c, sa.CheckConstraint) and c.name == CHECK_NAME]
    return check


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3: one new revision, down_revision the previous head, one head, no exception marker.

    No longer the head since tj-7zjov1.153 (P35r): its one child is 39c28a39b290; the single head is b6d1f93a7c42
    since tj-7zjov1.33 (P20).
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [HEAD]
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == REVISION] == [CHILD]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == PARENT] == [REVISION]
    assert (module.branch_labels, module.depends_on) == (None, None)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_revision_runs_in_the_one_migration_transaction():
    """Checklist item 12: no autocommit block, no commit of its own, no data statement."""
    text = REVISION_FILE.read_text(encoding='utf-8')
    assert 'autocommit_block' not in text and 'CONCURRENTLY' not in text.upper()
    up, _, _ = _run('upgrade')
    down, _, _ = _run('downgrade')
    assert 'execute' not in _calls(up) + _calls(down), 'the revision runs raw SQL'


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_only_adds_the_designs_columns_then_the_check():
    """PURELY ADDITIVE (checklist 4 to 8): add_column and one create_check_constraint, no type created or dropped."""
    op, created, dropped = _run('upgrade')

    added = _added(op)
    assert _calls(op) == [*['add_column'] * len(added), 'create_check_constraint']
    got: dict[str, set[str]] = {}
    for table, column in added:
        got.setdefault(table, set()).add(column.name)
    assert got == DESIGN_COLUMNS
    assert len(added) == sum(len(names) for names in DESIGN_COLUMNS.values()), 'a column is added twice'
    assert (created, dropped) == ([], []), 'ingest_run_trigger belongs to 6f5a798746a1'


def test_every_new_column_is_nullable_or_not_null_with_a_real_server_default():
    """Checklist item 7: only spend_unknown_attempts is NOT NULL, with server default 0; the rest have no default."""
    op, _, _ = _run('upgrade')
    for _table, column in _added(op):
        assert column.default is None, column.name
        if column.name == 'spend_unknown_attempts':
            assert column.nullable is False
            assert column.server_default is not None and str(column.server_default.arg) == '0'
        else:
            assert column.nullable is True, column.name
            assert column.server_default is None, f'{column.name} carries a server default'


def test_the_trigger_column_references_the_existing_type_with_the_models_members():
    """create_type False, or add_column would CREATE TYPE ingest_run_trigger, which exists since 6f5a798746a1."""
    op, _, _ = _run('upgrade')
    (trigger,) = [column for table, column in _added(op) if (table, column.name) == (REQUEST.name, 'trigger')]
    assert isinstance(trigger.type, postgresql.ENUM)
    assert trigger.type.create_type is False
    assert trigger.type.name == REQUEST.c.trigger.type.name == RUN.c.trigger.type.name == 'ingest_run_trigger'
    assert list(trigger.type.enums) == [member.value for member in IngestRunTrigger] == list(RUN.c.trigger.type.enums)
    assert list(REQUEST.c.trigger.type.enums) == list(RUN.c.trigger.type.enums)


def test_downgrade_drops_the_check_first_then_exactly_the_columns_upgrade_added_in_reverse():
    up, _, _ = _run('upgrade')
    down, created, dropped = _run('downgrade')

    calls = _calls(down)
    assert calls[0] == 'drop_constraint', 'the CHECK reads stopped_cause and collect, so it goes before the column'
    assert calls.count('drop_constraint') == 1
    ((name, table), kwargs) = next((args, kw) for n, args, kw in down.method_calls if n == 'drop_constraint')
    assert (name, table, kwargs.get('type_')) == (CHECK_NAME, ENTRY.name, 'check')
    removed = [(args[0], args[1]) for n, args, _ in down.method_calls if n == 'drop_column']
    assert removed == [(table, column.name) for table, column in reversed(_added(up))]
    assert set(calls) == {'drop_constraint', 'drop_column'}
    assert (created, dropped) == ([], []), 'the downgrade drops no type: ingest_run_trigger is the earlier revision'


# --------------------------------------------------------------------------------------------- revision against model


def test_every_new_column_compiles_to_exactly_the_models_column():
    """Type, nullability and server default, as Postgres DDL, revision column against the model column of that name."""
    op, _, _ = _run('upgrade')
    models = {RUN.name: RUN, REQUEST.name: REQUEST, ENTRY.name: ENTRY}
    compared = 0
    for table, column in _added(op):
        assert _compiled(CreateColumn(column)) == _compiled(CreateColumn(models[table].c[column.name])), column.name
        compared += 1
    assert compared == 7


def test_the_models_hold_the_designs_columns_and_no_other_revision_adds_the_new_names():
    """The design's columns are in the models, and the four new names appear in no other revision of the chain."""
    for table, names in DESIGN_COLUMNS.items():
        model = {RUN.name: RUN, REQUEST.name: REQUEST, ENTRY.name: ENTRY}[table]
        assert names <= set(model.c.keys()), table
    for path in (MIGRATIONS_DIR / 'versions').glob('*.py'):
        if path != REVISION_FILE:
            text = path.read_text(encoding='utf-8')
            for name in ('served_start', 'served_end', 'spend_unknown_attempts', 'stopped_cause'):
                assert name not in text, f'{path.name} also names {name}'


def test_stopped_cause_is_not_identity_and_in_no_unique_key_or_index():
    assert 'stopped_cause' not in StoreDatasetEntry.NATURAL_KEY
    for constraint in ENTRY.constraints:
        if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint):
            assert 'stopped_cause' not in [column.name for column in constraint.columns], constraint.name
    for index in ENTRY.indexes:
        assert 'stopped_cause' not in [column.name for column in index.columns], index.name


def _normalised(sql: str) -> str:
    return ' '.join(sql.split())


def test_the_model_declares_the_same_named_check_the_revision_creates_with_the_same_text():
    """By name, the model's constant and the design's, and the condition's SQL text compared whitespace-collapsed."""
    model_check = _model_check()
    assert model_check.name == StoreDatasetEntry.STOPPED_CAUSE_CONSTRAINT == CHECK_NAME
    assert _normalised(str(model_check.sqltext)) == _normalised(_check_condition())
    assert _normalised(_check_condition()) == 'stopped_cause IS NULL OR collect IS FALSE', 'the design text'


# --------------------------------------------------------------------------------------------- the CHECK, evaluated

# (collect, stopped_cause, allowed): a cause only on a stopped subscription (collect false); a collecting row (true)
# and a STATIC row (NULL) never carry one; no cause is always allowed.
TRUTH_TABLE = [
    (False, 'VENDOR_ASSET_CHANGED', True),
    (True, 'VENDOR_ASSET_CHANGED', False),
    (None, 'VENDOR_ASSET_CHANGED', False),
    (False, '', True),
    (True, '', False),
    (None, '', False),
    (False, None, True),
    (True, None, True),
    (None, None, True),
]


@pytest.mark.parametrize(('collect', 'cause', 'allowed'), TRUTH_TABLE)
def test_the_revisions_check_allows_a_cause_only_when_collect_is_false(collect, cause, allowed):
    """Evaluated by SQLite on the revision's own text: NULL arms make a CHECK pass, so each is measured, not read."""
    conn = sqlite3.connect(':memory:')
    row = conn.execute(
        f'SELECT ({_check_condition()}) IS NOT 0 FROM (SELECT ? AS collect, ? AS stopped_cause)',  # nosec B608
        (collect, cause),
    ).fetchone()
    # A CHECK fails only on FALSE; NULL passes. "IS NOT 0" reads it exactly that way.
    assert bool(row[0]) is allowed


@pytest.mark.parametrize(('collect', 'cause', 'allowed'), TRUTH_TABLE)
def test_the_models_check_bites_through_the_orm(collect, cause, allowed):
    """The SQLite tier builds its tables from the model, so the model's CHECK is what every fixture meets."""
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    update_type = UpdateType.STATIC if collect is None else UpdateType.DAILY
    row = entry('ZZSTOP', update_type=update_type)
    row.collect = collect
    row.stopped_cause = cause
    with Session(engine) as db:
        db.add(row)
        if allowed:
            db.commit()
        else:
            with pytest.raises(IntegrityError, match=CHECK_NAME):
                db.commit()
    engine.dispose()


def test_a_stopped_row_can_be_resumed_only_by_clearing_its_cause():
    """Through the model's DDL by raw UPDATE: flipping collect back to true while a cause is held is refused."""
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    row = entry('ZZRES', update_type=UpdateType.DAILY)
    row.collect = False
    row.stopped_cause = 'VENDOR_ASSET_CHANGED'
    with Session(engine) as db:
        db.add(row)
        db.commit()
    with engine.begin() as conn, pytest.raises(IntegrityError, match=CHECK_NAME):
        conn.exec_driver_sql(f"UPDATE {ENTRY.name} SET collect = 1 WHERE asset_symbol = 'ZZRES'")  # nosec B608
    with engine.begin() as conn:
        conn.exec_driver_sql(  # nosec B608 -- module constant
            f"UPDATE {ENTRY.name} SET collect = 1, stopped_cause = NULL WHERE asset_symbol = 'ZZRES'"
        )
    engine.dispose()


# --------------------------------------------------------------------------------------------- alembic's own offline SQL


def _offline(*argv: str) -> list[str]:
    """Run `alembic <argv> --sql` from data/store as CI does, and return its statements whitespace-collapsed."""
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', *argv, '--sql'],
        cwd=STORE_DIR,
        env={
            'PYTHONPATH': image_pythonpath(SERVER_ROOT),
            'DATABASE_URI': UNREACHABLE_DATABASE_URI,
            'POSTGRES_ASYNC': 'true',
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = '\n'.join(line for line in result.stdout.splitlines() if not line.lstrip().startswith('--'))
    statements = [' '.join(part.split()) for part in text.split(';\n')]
    return [statement for statement in statements if statement]


def _bookkeeping(statements: list[str]) -> list[str]:
    return [s for s in statements if s not in ('BEGIN', 'COMMIT') and not s.startswith('UPDATE alembic_version')]


@pytest.fixture(scope='module')
def upgrade_sql() -> list[str]:
    return _offline('upgrade', f'{PARENT}:{REVISION}')


@pytest.fixture(scope='module')
def downgrade_sql() -> list[str]:
    return _offline('downgrade', f'{REVISION}:{PARENT}')


def test_the_offline_upgrade_sql_is_exactly_the_models_ddl(upgrade_sql):
    """Each ADD COLUMN is the model's column compiled for Postgres, and the CHECK is the model's, added by name."""
    op, _, _ = _run('upgrade')
    models = {RUN.name: RUN, REQUEST.name: REQUEST, ENTRY.name: ENTRY}
    expected = [
        f'ALTER TABLE {table} ADD COLUMN {_compiled(CreateColumn(models[table].c[column.name]))}'
        for table, column in _added(op)
    ]
    expected.append(_compiled(AddConstraint(_model_check())))

    assert _bookkeeping(upgrade_sql) == expected


def test_the_offline_upgrade_moves_alembic_version_from_the_parent_to_the_revision(upgrade_sql):
    (version,) = [s for s in upgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{REVISION}' WHERE alembic_version.version_num = '{PARENT}'"
    )


def test_the_offline_downgrade_sql_drops_exactly_what_the_upgrade_created(upgrade_sql, downgrade_sql):
    created = []
    for statement in _bookkeeping(upgrade_sql):
        if match := re.match(r'ALTER TABLE (\w+) ADD COLUMN (\w+)', statement):
            created.append(('column', match[1], match[2]))
        elif match := re.match(r'ALTER TABLE (\w+) ADD CONSTRAINT (\w+) CHECK', statement):
            created.append(('constraint', match[1], match[2]))
        else:
            pytest.fail(f'upgrade statement of no known kind: {statement}')
    dropped = []
    for statement in _bookkeeping(downgrade_sql):
        if match := re.fullmatch(r'ALTER TABLE (\w+) DROP COLUMN (\w+)', statement):
            dropped.append(('column', match[1], match[2]))
        elif match := re.fullmatch(r'ALTER TABLE (\w+) DROP CONSTRAINT (\w+)', statement):
            dropped.append(('constraint', match[1], match[2]))
        else:
            pytest.fail(f'downgrade statement of no known kind: {statement}')

    assert dropped == list(reversed(created))
    assert len(created) == 8, 'seven columns and one CHECK'
