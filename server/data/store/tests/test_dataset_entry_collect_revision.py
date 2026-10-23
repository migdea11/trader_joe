"""The collect revision, e5c8b1d4f703, and the model column it builds, without Postgres (validator, tj-grna9p.176).

Design: decision tj-hhy3dk section (1) and the bead's MIGRATION-SAFETY CHECKLIST. One ADDITIVE revision on
d7e2a9c35b18: ADD COLUMN store_dataset_entry.collect BOOLEAN NULL; backfill collect = (expiry IS NULL) on every
DAILY and STREAM row (STATIC stays NULL); then CHECK ck_store_dataset_entry_collect_by_type, NULL exactly for
STATIC. Expiries are never written. Downgrade drops the constraint, then the column.

WHAT THIS TIER PROVES, AND HOW. The revision body runs against a stand-in for alembic.op that EXECUTES its data
statement on a real SQLite table holding every row kind, so the backfill is judged by its result, not by its
text: collect per kind, and every expiry byte-for-byte unchanged. The CHECK expression the revision creates is
then EVALUATED by SQLite against every (update_type, collect) combination, so "refuses STATIC with a value and a
subscription with NULL" is a measured truth table, not a string match. The model's own CHECK is shown to bite
through the ORM on SQLite, and the model and the revision are compared to each other (relationships, not
constants: ADR tj-8fxxfb), except where the design names the value.

NOT PROVED HERE -- tests/system/test_migration_with_data.py's, on a real Postgres (unrun until test mode):
  * that the revision applies from EMPTY and on the parent seed WITH DATA (canonical and collect-mix);
  * that Postgres's ADD CONSTRAINT accepts every backfilled row, and refuses the bad rows afterwards;
  * that upgrade -> downgrade -> upgrade round-trips on data, and that a repeat upgrade is a no-op;
  * that alembic check reports no drift at the new head.
"""

import importlib.util
import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.enums.data_stock import UpdateType
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.tests.test_ui_datasets_route import entry


pytestmark = pytest.mark.data_store

STORE_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'e5c8b1d4f703'
PARENT = 'd7e2a9c35b18'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_dataset_entry_collect.py'
TABLE = StoreDatasetEntry.__tablename__
CHECK_NAME = 'ck_store_dataset_entry_collect_by_type'  # the design's name (tj-hhy3dk section 1)
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^# additive-exception: \S+', re.MULTILINE)

# Every row kind the backfill meets: (label, update_type, expiry). The stored integers are the enum's values.
EXPIRIES = {
    'daily-expiry': '2025-06-01 00:00:00+00',
    'stream-expiry': '2030-01-01 00:00:00+00',
    'static-expiry': '2031-02-03 04:05:06+00',
}
ROWS = [
    ('daily-expiry', UpdateType.DAILY, EXPIRIES['daily-expiry']),
    ('daily-open', UpdateType.DAILY, None),
    ('stream-expiry', UpdateType.STREAM, EXPIRIES['stream-expiry']),
    ('stream-open', UpdateType.STREAM, None),
    ('static-expiry', UpdateType.STATIC, EXPIRIES['static-expiry']),
    ('static-open', UpdateType.STATIC, None),
]
# The design's backfill, per kind: a subscription collects exactly when it has no expiry; STATIC is NULL.
EXPECTED_COLLECT = {
    'daily-expiry': False,
    'daily-open': True,
    'stream-expiry': False,
    'stream-open': True,
    'static-expiry': None,
    'static-open': None,
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(f'revision_{REVISION}', REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded(direction: str) -> MagicMock:
    module = _load()
    op = MagicMock(name='op')
    with patch.object(module, 'op', op):
        getattr(module, direction)()
    return op


def _calls(op: MagicMock) -> list[str]:
    return [name for name, _args, _kwargs in op.method_calls]


class ExecutingOp:
    """alembic.op for this revision on SQLite: add_column and execute run; the CHECK is recorded, not created.

    SQLite cannot ALTER TABLE ... ADD CONSTRAINT, so the condition the revision passes is kept and evaluated by
    the tests below against the very rows the backfill wrote.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.checks: list[tuple[str, str, str]] = []
        self.added: list[sa.Column] = []

    def add_column(self, table: str, column: sa.Column) -> None:
        self.added.append(column)
        type_sql = column.type.compile(dialect=sqlite_dialect.dialect())
        null_sql = '' if column.nullable else ' NOT NULL'
        self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column.name} {type_sql}{null_sql}')

    def execute(self, statement: Any) -> None:
        self.conn.execute(str(statement))

    def create_check_constraint(self, name: str, table: str, condition: Any) -> None:
        self.checks.append((name, table, str(condition)))


@pytest.fixture
def migrated() -> tuple[sqlite3.Connection, ExecutingOp]:
    """A parent-shaped entry table holding every row kind, then the revision's upgrade() run against it."""
    conn = sqlite3.connect(':memory:')
    conn.execute(f'CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, update_type INTEGER NOT NULL, expiry TEXT)')
    conn.executemany(f'INSERT INTO {TABLE} VALUES (?, ?, ?)', [(label, ut.value, exp) for label, ut, exp in ROWS])
    executing = ExecutingOp(conn)
    module = _load()
    with patch.object(module, 'op', executing):
        module.upgrade()
    return conn, executing


def _check_condition(executing: ExecutingOp) -> str:
    (check,) = executing.checks
    return check[2]


def _satisfies(conn: sqlite3.Connection, condition: str, update_type: int, collect: bool | None) -> bool:
    row = conn.execute(
        f'SELECT ({condition}) FROM (SELECT ? AS update_type, ? AS collect)',  # nosec B608 -- the revision's own text
        (update_type, collect),
    ).fetchone()
    return bool(row[0])


# --------------------------------------------------------------------------------------------- place in the chain


def test_the_revision_is_the_head_directly_on_the_override_log_revision_and_is_additive():
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PARENT
    # Additive: no exception marker, so test_migration_with_data's GENERIC RULE holds it lossless both ways.
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_adds_one_column_then_backfills_then_creates_the_check_and_nothing_else():
    op = _recorded('upgrade')
    assert _calls(op) == ['add_column', 'execute', 'create_check_constraint']
    table, column = op.add_column.call_args.args
    assert (table, column.name) == (TABLE, 'collect')
    name, check_table, _condition = op.create_check_constraint.call_args.args
    assert (name, check_table) == (CHECK_NAME, TABLE)


def test_downgrade_drops_the_check_then_the_column_exactly_what_upgrade_created():
    up, down = _recorded('upgrade'), _recorded('downgrade')
    assert _calls(down) == ['drop_constraint', 'drop_column']
    name, table = down.drop_constraint.call_args.args
    assert (name, table) == up.create_check_constraint.call_args.args[:2]
    assert down.drop_constraint.call_args.kwargs.get('type_') == 'check'
    added_table, added = up.add_column.call_args.args
    assert down.drop_column.call_args.args == (added_table, added.name)


# --------------------------------------------------------------------------------------------- the backfill, run


def test_the_backfill_sets_collect_per_row_kind(migrated):
    conn, _executing = migrated
    got = {
        row_id: (None if collect is None else bool(collect))
        for row_id, collect in conn.execute(
            f'SELECT id, collect FROM {TABLE}'  # nosec B608 -- module constant
        )
    }
    assert got == EXPECTED_COLLECT


def test_the_backfill_leaves_every_expiry_and_update_type_as_it_was(migrated):
    conn, _executing = migrated
    got = {row_id: (ut, exp) for row_id, ut, exp in conn.execute(f'SELECT id, update_type, expiry FROM {TABLE}')}  # nosec B608
    assert got == {label: (ut.value, exp) for label, ut, exp in ROWS}


def test_every_backfilled_row_satisfies_the_check_the_revision_then_creates(migrated):
    conn, executing = migrated
    condition = _check_condition(executing)
    violating = conn.execute(f'SELECT id FROM {TABLE} WHERE NOT ({condition})').fetchall()  # nosec B608
    assert violating == []


# --------------------------------------------------------------------------------------------- the CHECK, evaluated


@pytest.mark.parametrize(
    ('update_type', 'collect', 'allowed'),
    [
        (UpdateType.STATIC, None, True),
        (UpdateType.STATIC, True, False),
        (UpdateType.STATIC, False, False),
        (UpdateType.DAILY, True, True),
        (UpdateType.DAILY, False, True),
        (UpdateType.DAILY, None, False),
        (UpdateType.STREAM, True, True),
        (UpdateType.STREAM, False, True),
        (UpdateType.STREAM, None, False),
    ],
    ids=lambda value: str(value),
)
def test_the_revisions_check_allows_exactly_null_for_static_and_a_value_for_a_subscription(
    migrated, update_type: UpdateType, collect: bool | None, allowed: bool
):
    conn, executing = migrated
    assert _satisfies(conn, _check_condition(executing), update_type.value, collect) is allowed


# Stored update_type values no UpdateType member has: what a corrupt or future row would hold.
UNKNOWN_UPDATE_TYPES = [0, 4, 99]


@pytest.mark.parametrize('update_type', UNKNOWN_UPDATE_TYPES)
@pytest.mark.parametrize('collect', [None, True, False])
def test_the_revisions_check_refuses_an_unknown_update_type_whatever_its_collect(
    migrated, update_type: int, collect: bool | None
):
    """The architect's ruling on the first gate: name DAILY and STREAM, so an unknown value is refused, not absorbed."""
    conn, executing = migrated
    assert _satisfies(conn, _check_condition(executing), update_type, collect) is False


@pytest.mark.parametrize('update_type', UNKNOWN_UPDATE_TYPES)
def test_an_unknown_update_type_row_is_not_backfilled_so_the_check_would_refuse_the_upgrade(update_type: int):
    """Upgrade over a table holding a row of an unknown update_type: it stays NULL, and violates the CHECK.

    On Postgres that violation fails ADD CONSTRAINT and rolls the one-transaction upgrade back whole
    (tests/system/test_migration_with_data.py, unrun offline); here the constraint cannot be added, so the row
    is shown to violate the very condition the revision passes.
    """
    conn = sqlite3.connect(':memory:')
    conn.execute(f'CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, update_type INTEGER NOT NULL, expiry TEXT)')
    conn.executemany(
        f'INSERT INTO {TABLE} VALUES (?, ?, ?)',  # nosec B608 -- module constant
        [('unknown', update_type, None), *((label, ut.value, exp) for label, ut, exp in ROWS)],
    )
    executing = ExecutingOp(conn)
    module = _load()
    with patch.object(module, 'op', executing):
        module.upgrade()

    assert conn.execute(f"SELECT collect FROM {TABLE} WHERE id = 'unknown'").fetchone() == (None,)  # nosec B608
    violating = conn.execute(f'SELECT id FROM {TABLE} WHERE NOT ({_check_condition(executing)})').fetchall()  # nosec B608
    assert violating == [('unknown',)]


@pytest.mark.parametrize('update_type', UNKNOWN_UPDATE_TYPES)
@pytest.mark.parametrize('collect', [None, True])
def test_the_models_check_refuses_an_unknown_update_type(update_type: int, collect: bool | None):
    """Through the model's own DDL on SQLite, by a raw INSERT: the ORM's enum type could not even bind the value."""
    engine = sa.create_engine('sqlite://')
    StoreDatasetEntry.__table__.create(engine)
    row = entry('ZZUNK')
    with Session(engine) as db:
        db.add(row)
        db.commit()
    with engine.begin() as conn, pytest.raises(IntegrityError, match=CHECK_NAME):
        conn.exec_driver_sql(
            f'UPDATE {TABLE} SET update_type = ?, collect = ? WHERE asset_symbol = ?',  # nosec B608
            (update_type, collect, 'ZZUNK'),
        )
    engine.dispose()


def test_the_revisions_static_constant_is_the_stored_value_of_static():
    module = _load()
    assert UpdateType.STATIC.value == module.STATIC
    assert {member.value for member in UpdateType} == {1, 2, 3}, 'a new update type: revisit the <> STATIC rule'


# --------------------------------------------------------------------------------------------- model agreement


def test_the_model_column_matches_the_revision_column_in_type_and_nullability():
    model = StoreDatasetEntry.__table__.c.collect
    (added,) = [call.args[1] for call in _recorded('upgrade').add_column.call_args_list]
    assert isinstance(model.type, sa.Boolean) and isinstance(added.type, sa.Boolean)
    assert model.nullable is True and added.nullable is True
    assert model.server_default is None and added.server_default is None


def test_collect_is_not_identity_and_in_no_unique_key():
    assert 'collect' not in StoreDatasetEntry.NATURAL_KEY
    for constraint in StoreDatasetEntry.__table__.constraints:
        if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint):
            assert 'collect' not in [column.name for column in constraint.columns], constraint.name
    for index in StoreDatasetEntry.__table__.indexes:
        assert 'collect' not in [column.name for column in index.columns], index.name


def _normalised(sql: str) -> str:
    return ' '.join(sql.split())


def test_the_model_declares_the_same_named_check_the_revision_creates():
    checks = [c for c in StoreDatasetEntry.__table__.constraints if isinstance(c, sa.CheckConstraint)]
    (model_check,) = checks
    name, _table, condition = _recorded('upgrade').create_check_constraint.call_args.args
    assert model_check.name == name == StoreDatasetEntry.COLLECT_BY_TYPE_CONSTRAINT
    assert _normalised(str(model_check.sqltext)) == _normalised(condition)


@pytest.mark.parametrize(
    ('update_type', 'collect', 'allowed'),
    [
        (UpdateType.STATIC, None, True),
        (UpdateType.STATIC, True, False),
        (UpdateType.DAILY, True, True),
        (UpdateType.DAILY, None, False),
        (UpdateType.STREAM, False, True),
        (UpdateType.STREAM, None, False),
    ],
    ids=lambda value: str(value),
)
def test_the_models_check_bites_through_the_orm(update_type: UpdateType, collect: bool | None, allowed: bool):
    """The SQLite tier builds its tables from the model, so the model's CHECK is what every fixture meets."""
    engine = sa.create_engine('sqlite://')
    StoreDatasetEntry.__table__.create(engine)
    row = entry('ZZCHK', update_type=update_type)
    row.collect = collect
    with Session(engine) as db:
        db.add(row)
        if allowed:
            db.commit()
        else:
            with pytest.raises(IntegrityError, match=CHECK_NAME):
                db.commit()
    engine.dispose()
