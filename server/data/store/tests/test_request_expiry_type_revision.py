"""Revision 39c28a39b290, ingest_request.expiry_type, against its design and its model, offline (tj-7zjov1.153, P35r).

THE DESIGN is the bead's WORK items 1 to 3 and its acceptance, ruled in ADR tj-26jt9w addendum 14. The literals below
are the design's:
  * ingest_request.expiry_type: integer (ExpiryType's value, the representation the entry's column stores), nullable,
    NO server default (a default would write a guessed entry identity on every existing row);
  * purely additive and unmarked, directly on b8e4c1f7a923, the single head, in the one migration transaction;
  * downgrade drops exactly that column, and nothing else.

THREE TIERS, NONE A DATABASE.
  1. The revision body against a recording stand-in for alembic.op: what it calls, and that downgrade() is its inverse.
  2. Revision against model: the added column compiles, as Postgres DDL, to exactly the model's column, which is an
     Integer, nullable, with no default of either kind.
  3. Alembic's own offline rendering (upgrade b8e4c1f7a923:39c28a39b290 --sql, and the downgrade back) compared
     statement by statement with the MODEL's column compiled for Postgres.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb): revision against model, downgrade against upgrade, rendered SQL against
the compiled model; a literal appears only where the design names the value.

NOT PROVED HERE: the repository round trip of every ExpiryType and None (test_ingest_request_crud.py over SQLite,
tests/system/test_request_record_schema.py on asyncpg); that the revision applies from empty and on the b8e4c1f7a923
seed with every row kept and the column NULL, and the downgrade gives the rows back (tests/system/
test_migration_with_data.py); that it builds exactly its one column (tests/system/test_migration_downgrade.py); that
alembic check is clean at the head (test_alembic_check_is_clean_at_head, tests/system); the head seed's column
(test_seed_head_content.py).
"""

import importlib.util
import re
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn

from common.enums.data_stock import ExpiryType
from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = '39c28a39b290'
PARENT = 'b8e4c1f7a923'
CHILD = 'c7e19a4d5b28'  # the head since tj-7zjov1.31 (P18): corporate_action, on this revision
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_request_expiry_type.py'
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

REQUEST = IngestRequest.__table__
COLUMN = 'expiry_type'  # the design's name (WORK item 1)


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


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3: one new revision, down_revision the previous head, one head, no exception marker.

    No longer the head since tj-7zjov1.31 (P18): its one child is c7e19a4d5b28, which is the single head.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [CHILD]
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


def test_upgrade_only_adds_the_one_designed_column():
    """PURELY ADDITIVE (checklist 4 to 8): exactly one add_column, ingest_request.expiry_type; no type, no backfill."""
    op, created, dropped = _run('upgrade')

    assert _calls(op) == ['add_column']
    assert [(table, column.name) for table, column in _added(op)] == [(REQUEST.name, COLUMN)]
    assert (created, dropped) == ([], [])


def test_the_new_column_is_a_nullable_integer_with_no_default_of_either_kind():
    """Checklist item 7, and the design's reason: a default would write a guessed entry identity silently."""
    op, _, _ = _run('upgrade')
    ((_table, column),) = _added(op)
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True
    assert column.server_default is None, 'the design forbids a server default'
    assert column.default is None


def test_downgrade_drops_exactly_the_column_upgrade_added():
    up, _, _ = _run('upgrade')
    down, created, dropped = _run('downgrade')

    assert _calls(down) == ['drop_column']
    removed = [(args[0], args[1]) for n, args, _ in down.method_calls if n == 'drop_column']
    assert removed == [(table, column.name) for table, column in reversed(_added(up))]
    assert (created, dropped) == ([], [])


# --------------------------------------------------------------------------------------------- revision against model


def test_the_new_column_compiles_to_exactly_the_models_column():
    """Type, nullability and server default, as Postgres DDL, revision column against the model column."""
    op, _, _ = _run('upgrade')
    ((table, column),) = _added(op)
    assert table == REQUEST.name
    assert _compiled(CreateColumn(column)) == _compiled(CreateColumn(REQUEST.c[column.name]))
    assert _compiled(CreateColumn(REQUEST.c[COLUMN])) == 'expiry_type INTEGER', 'the design: integer, NULL, no DEFAULT'


def test_the_models_column_is_a_nullable_integer_with_no_default():
    """The model half of the same facts: alembic check compares neither server defaults nor Python defaults."""
    column = REQUEST.c[COLUMN]
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True
    assert (column.server_default, column.default) == (None, None)


def test_the_model_stores_the_representation_the_entry_column_stores():
    """The entry's expiry_type holds ExpiryType's integer value (SqlIntEnum over INTEGER), as the request column does.

    The entry column is NOT NULL with server default BULK; the request column must not copy either (the design).
    """
    entry_column = StoreDatasetEntry.__table__.c[COLUMN]
    assert _compiled(CreateColumn(entry_column)).split()[1] == _compiled(CreateColumn(REQUEST.c[COLUMN])).split()[1]
    assert _compiled(CreateColumn(REQUEST.c[COLUMN])).split()[1] == 'INTEGER'
    assert {member.value for member in ExpiryType} == {1, 2, 3, 4, 5}, 'the five values the round trips store'


def test_no_other_revision_names_the_column_on_ingest_request():
    """expiry_type exists on the entry since the initial revisions; on ingest_request only this revision adds it."""
    for path in (MIGRATIONS_DIR / 'versions').glob('*.py'):
        if path != REVISION_FILE:
            text = path.read_text(encoding='utf-8')
            assert not ('ingest_request' in text and COLUMN in text), f'{path.name} names ingest_request and {COLUMN}'


def test_the_column_is_not_in_the_request_tables_indexes():
    """The design adds no index: the column is identity of the entry, never a lookup key on the request."""
    for index in REQUEST.indexes:
        assert COLUMN not in [column.name for column in index.columns], index.name


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


def test_the_offline_upgrade_is_one_transaction(upgrade_sql):
    assert upgrade_sql[0] == 'BEGIN' and upgrade_sql[-1] == 'COMMIT'
    assert upgrade_sql.count('BEGIN') == upgrade_sql.count('COMMIT') == 1


def test_the_offline_upgrade_sql_is_exactly_the_models_column(upgrade_sql):
    """One ADD COLUMN, the model's column compiled for Postgres: INTEGER, no NOT NULL, no DEFAULT."""
    assert _bookkeeping(upgrade_sql) == [
        f'ALTER TABLE {REQUEST.name} ADD COLUMN {_compiled(CreateColumn(REQUEST.c[COLUMN]))}'
    ]
    assert _bookkeeping(upgrade_sql) == ['ALTER TABLE ingest_request ADD COLUMN expiry_type INTEGER'], 'the design'


def test_the_offline_upgrade_moves_alembic_version_from_the_parent_to_the_revision(upgrade_sql):
    (version,) = [s for s in upgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{REVISION}' WHERE alembic_version.version_num = '{PARENT}'"
    )


def test_the_offline_downgrade_sql_drops_exactly_the_column_and_moves_the_version_back(downgrade_sql):
    assert _bookkeeping(downgrade_sql) == [f'ALTER TABLE {REQUEST.name} DROP COLUMN {COLUMN}']
    (version,) = [s for s in downgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{PARENT}' WHERE alembic_version.version_num = '{REVISION}'"
    )
