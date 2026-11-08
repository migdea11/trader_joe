"""Revision a3f8d2c6e914, the asset-list snapshots and ingest_request.asset_list_status, against its design, offline.

THE DESIGN is tj-7zjov1.30 (P17): the architect's ruling of 23:31 UTC (ADR tj-26jt9w addendum 17), item (1) COLUMN
ingest_request.asset_list_status, nullable, a NEW Postgres enum type asset_list_status whose labels are the wire domain
AssetListStatus's own values, shared by the snapshot table's list column, created once in upgrade and dropped last in
downgrade; item (2) the CHECK ck_ingest_request_asset_list_status, (kind = 'ASSET_LIST') = (asset_list_status IS NOT
NULL), no backfill; down_revision c7e19a4d5b28; and the migration review checklist (store CLAUDE.md, m7) items the
ruling names: 1, 2 (unmarked), 3, 4, 5 (a new type created, nothing retyped), 6, 7 (nullable, no server_default), 9,
10, 11, 12, 13, 14 (the enum labels compared by hand with AssetListStatus), 17 (downgrade drops the snapshot tables,
then the constraint and column, then the types).

THREE TIERS, NONE A DATABASE, as test_corporate_action_revision.py: the body against a recording op; the created
tables and indexes against the models compiled for Postgres; alembic's own offline SQL. And the append-only property of
the repository: no UPDATE or DELETE path on the three tables.

NOT PROVED HERE, and where it is: applies from empty and on the c7e19a4d5b28 seed with every row kept, and the downgrade
on a database holding data (tests/system/test_migration_with_data.py, tests/system/test_migration_downgrade.py); the
CHECK refusing both ways and the enum labels in the catalogue (tests/system/test_asset_list_pg.py); alembic check clean
at head (tests/system); the canonical seed (test_seed_guard.py).
"""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT
from data.store.app.database.crud import asset_list as crud
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.ingest_request import IngestRequest
from schemas.data_ingest.submit import AssetListStatus


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'a3f8d2c6e914'
PARENT = 'c7e19a4d5b28'  # the 00:45 UTC note: the one head the revision was written on
CHILD = 'b6d1f93a7c42'  # its one child and the head since tj-7zjov1.33 (P20): the panel tables
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_asset_list_snapshots.py'
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

REQUEST = IngestRequest.__table__
MODELS = (AssetListSnapshot, AssetListRow, AssetListEvent)
# The design's names (addendum 17).
STATUS_TYPE = 'asset_list_status'
CHECK_NAME = 'ck_ingest_request_asset_list_status'
CHECK_SQL = "(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(f'revision_{REVISION}', REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str) -> tuple[MagicMock, list[Any], list[str]]:
    """Run upgrade() or downgrade() against a recording op; returns (op, enum types created, names of types dropped)."""
    module = _load()
    op = MagicMock(name='op')
    with (
        patch.object(module, 'op', op),
        patch.object(postgresql.ENUM, 'create', autospec=True) as create,
        patch.object(postgresql.ENUM, 'drop', autospec=True) as drop,
    ):
        getattr(module, direction)()
    return op, [call.args[0] for call in create.call_args_list], [call.args[0].name for call in drop.call_args_list]


def _calls(op: MagicMock) -> list[str]:
    return [name for name, _, _ in op.method_calls if name != 'get_bind']


def _compiled(element: Any) -> str:
    return ' '.join(str(element.compile(dialect=DIALECT)).split())


def _created_tables(op: MagicMock) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        args[0]: sa.Table(args[0], metadata, *args[1:]) for name, args, _ in op.method_calls if name == 'create_table'
    }


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3.

    No longer the head since tj-7zjov1.33 (P20): its one child is b6d1f93a7c42, which is the single head.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [CHILD]
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == REVISION] == [CHILD]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == PARENT] == [REVISION]
    assert (module.branch_labels, module.depends_on) == (None, None)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_revision_runs_in_the_one_migration_transaction_with_no_raw_sql():
    """Checklist items 11 and 12."""
    text = REVISION_FILE.read_text(encoding='utf-8')
    assert 'autocommit_block' not in text and 'CONCURRENTLY' not in text.upper()
    up, _, _ = _run('upgrade')
    down, _, _ = _run('downgrade')
    assert 'execute' not in _calls(up) + _calls(down), 'the revision runs raw SQL'


def test_the_migration_env_loads_the_model_module_so_autogenerate_sees_the_three_tables():
    """Checklist items 13 and 14: the tables are a model module's, so env.py's ALLOWED_MODELS must name it."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    assert Path(sys.modules[AssetListSnapshot.__module__].__file__).stem in allowed, allowed


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_creates_two_types_then_the_column_and_check_then_three_tables_and_their_indexes_only():
    """PURELY ADDITIVE (checklist 4 to 9): no drop, rename, retype, alter or backfill."""
    op, created, dropped = _run('upgrade')

    assert _calls(op) == [
        'add_column',
        'create_check_constraint',
        'create_table',
        'create_index',
        'create_index',
        'create_table',
        'create_index',
        'create_index',
        'create_table',
        'create_index',
    ]
    assert [enum.name for enum in created] == [STATUS_TYPE, 'asset_list_event_kind']
    assert dropped == []
    for table in _created_tables(op).values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, f'{table.name}.{column.name} would CREATE its type again'


def test_the_status_types_labels_are_the_wire_domains_values_compared_by_hand():
    """Checklist item 14 and ruling (1): the labels ARE AssetListStatus's values, so no mapping exists.

    The model's three columns of the type carry the same labels under the same name.
    """
    _, created, _ = _run('upgrade')
    (status_type,) = [enum for enum in created if enum.name == STATUS_TYPE]

    assert list(status_type.enums) == [member.value for member in AssetListStatus] == ['active', 'inactive']
    for column in (
        REQUEST.c.asset_list_status,
        AssetListSnapshot.__table__.c.asset_list,
        AssetListEvent.__table__.c.asset_list,
    ):
        assert (column.type.name, list(column.type.enums)) == (STATUS_TYPE, ['active', 'inactive']), column


def test_the_event_kind_types_labels_are_the_models():
    _, created, _ = _run('upgrade')
    (kind_type,) = [enum for enum in created if enum.name == 'asset_list_event_kind']
    model = AssetListEvent.__table__.c.kind.type

    assert list(kind_type.enums) == [member.value for member in AssetListEventKind] == list(model.enums)
    assert list(kind_type.enums) == ['appeared', 'vanished', 'status_flip']
    assert model.name == kind_type.name


def test_the_request_column_is_nullable_with_no_default_of_the_shared_type_and_the_models_ddl():
    """Checklist item 7 (no server_default) and item 9 (no backfill): every existing row keeps NULL."""
    op, _, _ = _run('upgrade')
    ((table, column),) = [(args[0], args[1]) for name, args, _ in op.method_calls if name == 'add_column']

    assert table == REQUEST.name
    assert column.nullable and column.server_default is None and column.default is None
    assert _compiled(CreateColumn(column)) == _compiled(CreateColumn(REQUEST.c.asset_list_status))


def test_the_check_is_the_designs_both_ways_under_the_designs_name_and_the_models():
    op, _, _ = _run('upgrade')
    ((name, table, condition),) = [args for call, args, _ in op.method_calls if call == 'create_check_constraint']
    (model_check,) = [c for c in REQUEST.constraints if isinstance(c, sa.CheckConstraint) and c.name == CHECK_NAME]

    assert (name, table) == (CHECK_NAME, REQUEST.name)
    assert ' '.join(str(condition).split()) == CHECK_SQL == ' '.join(str(model_check.sqltext).split())


def test_downgrade_drops_the_tables_then_the_constraint_and_column_then_the_types():
    """Ruling item 17: tables (with their indexes) first, then the CHECK, then the column, then both types, last."""
    up, created, _ = _run('upgrade')
    down, _, dropped = _run('downgrade')

    calls = _calls(down)
    last_table = max(index for index, name in enumerate(calls) if name == 'drop_table')
    assert calls.index('drop_constraint') > last_table
    assert calls.index('drop_column') > calls.index('drop_constraint')
    assert calls[-1] == 'drop_column'
    assert [args for name, args, _ in down.method_calls if name == 'drop_constraint'] == [(CHECK_NAME, REQUEST.name)]
    assert [kwargs for name, _, kwargs in down.method_calls if name == 'drop_constraint'] == [{'type_': 'check'}]
    assert [args for name, args, _ in down.method_calls if name == 'drop_column'] == [
        (REQUEST.name, 'asset_list_status')
    ]
    created_tables = [args[0] for name, args, _ in up.method_calls if name == 'create_table']
    assert [args[0] for name, args, _ in down.method_calls if name == 'drop_table'] == list(reversed(created_tables))
    created_indexes = sorted((args[1], args[0]) for name, args, _ in up.method_calls if name == 'create_index')
    dropped_indexes = sorted(
        (kwargs['table_name'], args[0]) for name, args, kwargs in down.method_calls if name == 'drop_index'
    )
    assert dropped_indexes == created_indexes
    assert dropped == list(reversed([enum.name for enum in created])), 'the types go after everything that uses them'
    assert set(calls) == {'drop_index', 'drop_table', 'drop_constraint', 'drop_column'}


def test_the_types_are_dropped_after_every_op_of_the_downgrade():
    """The bind drops are not op calls: the order is read off the module's own downgrade text."""
    source = ast.get_source_segment(REVISION_FILE.read_text(encoding='utf-8'), _function_node('downgrade'))
    assert source.rindex('op.drop_column') < source.index('.drop(op.get_bind()')


def _function_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(REVISION_FILE.read_text(encoding='utf-8'))
    (node,) = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return node


# --------------------------------------------------------------------------------------------- revision against model


@pytest.mark.parametrize('model', MODELS, ids=lambda model: model.TABLE_NAME)
def test_the_revision_builds_exactly_the_table_the_model_declares(model):
    op, _, _ = _run('upgrade')

    assert _compiled(CreateTable(_created_tables(op)[model.TABLE_NAME])) == _compiled(CreateTable(model.__table__))


def test_the_revision_builds_exactly_the_indexes_the_models_declare():
    op, _, _ = _run('upgrade')
    objects = SchemaObjects()
    built = {
        _compiled(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }

    assert built == {_compiled(CreateIndex(index)) for model in MODELS for index in model.__table__.indexes}
    assert len(built) == 5


def test_the_models_carry_the_designs_keys_and_no_foreign_key():
    """One snapshot per delivered attempt (the replay key); one event per (snapshot, id, kind).

    Nothing references anything: snapshots outlive their request.
    """
    (snapshot_key,) = [index for index in AssetListSnapshot.__table__.indexes if index.unique]
    (event_key,) = [index for index in AssetListEvent.__table__.indexes if index.unique]
    assert [column.name for column in snapshot_key.columns] == ['request_id', 'attempt_no']
    assert [column.name for column in event_key.columns] == ['snapshot_id', 'vendor_asset_id', 'kind']
    assert not any(model.__table__.foreign_keys for model in MODELS)
    rows = AssetListRow.__table__.c
    assert rows.active.nullable and rows.tradable.nullable, 'presence, not a value: NULL is the vendor saying nothing'
    assert isinstance(rows.raw_payload.type, sa.LargeBinary) and not rows.raw_payload.nullable
    assert AssetListEvent.__table__.c.previous_symbol.nullable


# --------------------------------------------------------------------------------------------- append-only


def test_the_repository_has_no_update_or_delete_path():
    """The models' docstrings: APPEND-ONLY. No statement in the crud is an update, a delete or a DO UPDATE."""
    tree = ast.parse(Path(crud.__file__).read_text(encoding='utf-8'))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not imported & {'update', 'delete'}, imported
    bare = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not bare & {'update', 'delete'}, bare
    attributes = {
        (ast.unparse(node.func.value), node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # No .update or .delete call of any kind: ids_in_snapshot, whose Python set held the one .update, went with the
    # vanished suppression (P17f, ADR tj-26jt9w addendum 18 b), so a statement .update or .delete on any receiver is red.
    assert {pair for pair in attributes if pair[1] in {'update', 'delete'}} == set()
    assert not {attr for _, attr in attributes} & {'on_conflict_do_update', 'exec_driver_sql'}
    raw = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r'\b(UPDATE|DELETE)\b', node.value)
    ]
    assert raw == []


def test_no_production_module_writes_the_asset_list_tables_other_than_by_the_repository():
    """Only the repository and the arm import the models; no raw SQL in the server updates or deletes the tables.

    The seed tooling is excluded: it empties the tables of a disposable stack before writing the cast.
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?asset_list_(snapshot|row|event)\b', re.I)
    importing_the_model = set()
    for path in SERVER_ROOT.rglob('*.py'):
        parts = path.relative_to(SERVER_ROOT).parts
        if 'tests' in parts or 'migrations' in parts or 'seeds' in parts or '.venv' in parts:
            continue
        text = path.read_text(encoding='utf-8')
        assert not raw.search(text), f'{path} updates or deletes an asset-list table in raw SQL'
        if re.search(r'store\.app\.database\.models(\.asset_list\b| import .*\basset_list\b)', text):
            importing_the_model.add(path.relative_to(SERVER_ROOT).as_posix())
    assert importing_the_model == {'data/store/app/database/crud/asset_list.py', 'data/store/app/world_snapshot.py'}


# --------------------------------------------------------------------------------------------- alembic's own offline SQL


def _offline(*argv: str) -> list[str]:
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


def test_the_offline_upgrade_is_one_transaction_and_moves_the_version(upgrade_sql):
    assert upgrade_sql[0] == 'BEGIN' and upgrade_sql[-1] == 'COMMIT'
    assert upgrade_sql.count('BEGIN') == upgrade_sql.count('COMMIT') == 1
    (version,) = [s for s in upgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{REVISION}' WHERE alembic_version.version_num = '{PARENT}'"
    )


def test_the_offline_upgrade_sql_is_the_types_the_column_the_check_and_the_models_tables(upgrade_sql):
    statements = _bookkeeping(upgrade_sql)
    tables = []
    for model in MODELS:
        table = model.__table__
        tables += [_compiled(CreateTable(table)), *sorted(_compiled(CreateIndex(index)) for index in table.indexes)]
    head = statements[:4]
    body = statements[4:]
    # Index order within one table is not schema: compared sorted, as the expectation is built.
    at = 0
    for model in MODELS:
        count = 1 + len(model.__table__.indexes)
        body[at + 1 : at + count] = sorted(body[at + 1 : at + count])
        at += count

    assert head == [
        "CREATE TYPE asset_list_status AS ENUM ('active', 'inactive')",
        "CREATE TYPE asset_list_event_kind AS ENUM ('appeared', 'vanished', 'status_flip')",
        f'ALTER TABLE {REQUEST.name} ADD COLUMN {_compiled(CreateColumn(REQUEST.c.asset_list_status))}',
        f'ALTER TABLE {REQUEST.name} ADD CONSTRAINT {CHECK_NAME} CHECK ({CHECK_SQL})',
    ]
    assert body == tables


def test_the_offline_downgrade_sql_drops_tables_then_constraint_then_column_then_types(downgrade_sql):
    statements = _bookkeeping(downgrade_sql)

    assert statements[-4:] == [
        f'ALTER TABLE {REQUEST.name} DROP CONSTRAINT {CHECK_NAME}',
        f'ALTER TABLE {REQUEST.name} DROP COLUMN asset_list_status',
        'DROP TYPE asset_list_event_kind',
        'DROP TYPE asset_list_status',
    ]
    assert [s for s in statements if s.startswith('DROP TABLE')] == [
        f'DROP TABLE {model.TABLE_NAME}' for model in reversed(MODELS)
    ]
    assert all(s.startswith(('DROP INDEX', 'DROP TABLE')) for s in statements[:-4])
