"""Revision c7e19a4d5b28, corporate_action and the relaxed ingest_run labels, against its design and model, offline.

THE DESIGN is tj-7zjov1.31 (P18): the owner ruling of 2026-10-26 in the 04:47 UTC note (corporate actions stored raw
as published, append-only, with process_date and effective_date, every identifier, and the attempt and requesting
symbol that delivered them; nothing tied to a listing), the 15:31 UTC note (dedupe by vendor_action_id AND the payload
bytes: a vendor revision is a new observation, never an overwrite), the 20:17 UTC note (THIS bead owns the relaxing
revision: alter_column nullable=True only on ingest_run asset_symbol, asset_type, data_type, granularity and
update_type, no server_default keyword, unmarked) and the 16:10 UTC note (down_revision 39c28a39b290). The migration
review checklist (store CLAUDE.md, m7) items 1 to 6, 12 and 13 are checked here as far as a file can show them.

THREE TIERS, NONE A DATABASE.
  1. The revision body against a recording stand-in for alembic.op: what it calls, and that downgrade() is its inverse.
  2. Revision against model: the created table and its indexes compile, as Postgres DDL, to exactly the model's.
  3. Alembic's own offline rendering (upgrade 39c28a39b290:c7e19a4d5b28 --sql and the downgrade back), statement by
     statement against the model compiled for Postgres.

And the append-only property of the repository: no UPDATE or DELETE path on corporate_action exists in the code.

NOT PROVED HERE, and where it is: that the revision applies from empty and on the 39c28a39b290 seed with every row kept,
and that the downgrade refuses, changing nothing, on a database holding a NULL label (tests/system/
test_migration_with_data.py and tests/system/test_migration_downgrade.py, Postgres); alembic check clean at the head
(test_alembic_check_is_clean_at_head, tests/system); the canonical seed (test_seed_guard.py).
"""

import ast
import importlib.util
import inspect
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from common.enums.data_stock import DataSource
from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT
from data.store.app.database.crud import corporate_action as crud
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_run import IngestRun


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'c7e19a4d5b28'
PARENT = '39c28a39b290'  # the 16:10 UTC note
CHILD = 'a3f8d2c6e914'  # its one child since tj-7zjov1.30 (P17): the asset-list snapshots, on this revision
HEAD = 'b6d1f93a7c42'  # the head since tj-7zjov1.33 (P20): the panel tables, on CHILD
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_corporate_action_observations.py'
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

ACTION = CorporateAction.__table__
RUN = IngestRun.__table__
# The design's names (the 20:17 UTC note, and the 15:31 UTC note for the dedupe key).
RELAXED = ('asset_symbol', 'asset_type', 'data_type', 'granularity', 'update_type')
DEDUPE_KEY = ('source', 'vendor_action_id', 'payload_hash')


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


def _compiled(element: Any) -> str:
    return ' '.join(str(element.compile(dialect=DIALECT)).split())


def _created_table(op: MagicMock) -> sa.Table:
    ((_, args, _),) = [call for call in op.method_calls if call[0] == 'create_table']
    return sa.Table(args[0], sa.MetaData(), *args[1:])


def _created_indexes(op: MagicMock) -> set[str]:
    objects = SchemaObjects()
    return {
        _compiled(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }


def _alterations(op: MagicMock) -> list[tuple[str, str, dict]]:
    return [(args[0], args[1], kwargs) for name, args, kwargs in op.method_calls if name == 'alter_column']


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3: one new revision, down_revision the previous head, one head, no exception marker.

    No longer the head since tj-7zjov1.30 (P17): its one child is a3f8d2c6e914; the single head is b6d1f93a7c42 since
    tj-7zjov1.33 (P20).
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [HEAD]
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == REVISION] == [CHILD]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == PARENT] == [REVISION]
    assert (module.branch_labels, module.depends_on) == (None, None)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_revision_runs_in_the_one_migration_transaction_with_no_raw_sql():
    """Checklist items 11 and 12: no autocommit block, no CONCURRENTLY, no op.execute to read by eye."""
    text = REVISION_FILE.read_text(encoding='utf-8')
    assert 'autocommit_block' not in text and 'CONCURRENTLY' not in text.upper()
    up, _, _ = _run('upgrade')
    down, _, _ = _run('downgrade')
    assert 'execute' not in _calls(up) + _calls(down), 'the revision runs raw SQL'


def test_the_migration_env_loads_the_model_so_autogenerate_sees_the_table():
    """Checklist items 13 and 14: the table is a model's, so env.py's ALLOWED_MODELS must name it."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    assert CorporateAction.TABLE_NAME in allowed, allowed


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_creates_one_table_two_indexes_and_relaxes_the_five_labels_and_nothing_else():
    """PURELY ADDITIVE (checklist 4 to 8): no drop, no rename, no retype, no backfill, no enum type created."""
    op, created, dropped = _run('upgrade')

    assert _calls(op) == ['create_table', 'create_index', 'create_index', *['alter_column'] * len(RELAXED)]
    assert (created, dropped) == ([], [])
    (source,) = [column for column in _created_table(op).columns if column.name == 'source']
    assert isinstance(source.type, postgresql.ENUM) and source.type.name == 'datasource'
    assert source.type.create_type is False, 'datasource exists since the first revision; it is referenced only'


def test_the_alterations_are_nullable_true_only_on_exactly_the_designs_five_ingest_run_columns():
    """The 20:17 UTC note: alter_column nullable=True only, no server_default keyword, no type change."""
    op, _, _ = _run('upgrade')

    alterations = _alterations(op)
    assert [(table, column) for table, column, _ in alterations] == [(RUN.name, column) for column in RELAXED]
    assert [kwargs for _, _, kwargs in alterations] == [{'nullable': True}] * len(RELAXED)


def test_downgrade_is_the_inverse_in_reverse_order():
    """NOT NULL restored on the five in reverse, then the two indexes in reverse, then the table. No type dropped."""
    up, _, _ = _run('upgrade')
    down, created, dropped = _run('downgrade')

    restored = _alterations(down)
    assert [(table, column) for table, column, _ in restored] == [(RUN.name, column) for column in reversed(RELAXED)]
    assert [kwargs for _, _, kwargs in restored] == [{'nullable': False}] * len(RELAXED)
    created_indexes = [args[0] for name, args, _ in up.method_calls if name == 'create_index']
    dropped_indexes = [args[0] for name, args, _ in down.method_calls if name == 'drop_index']
    assert dropped_indexes == list(reversed(created_indexes))
    assert [args[0] for name, args, _ in down.method_calls if name == 'drop_table'] == [ACTION.name]
    assert _calls(down) == [*['alter_column'] * len(RELAXED), 'drop_index', 'drop_index', 'drop_table']
    assert (created, dropped) == ([], []), 'datasource belongs to the dataset entry'


# --------------------------------------------------------------------------------------------- revision against model


def test_the_revision_builds_exactly_the_table_the_model_declares():
    """Every column, type, nullability, server default and the primary key, as one Postgres CREATE TABLE."""
    op, _, _ = _run('upgrade')

    assert _compiled(CreateTable(_created_table(op))) == _compiled(CreateTable(ACTION))


def test_the_revision_builds_exactly_the_indexes_the_model_declares():
    op, _, _ = _run('upgrade')

    assert _created_indexes(op) == {_compiled(CreateIndex(index)) for index in ACTION.indexes}
    assert len(ACTION.indexes) == 2


def test_the_model_carries_the_designs_columns_and_dedupe_key_and_no_foreign_key():
    """The 04:47 and 15:31 UTC notes: the raw record and what delivered it, the dedupe key, no foreign key.

    Both new dates and the identifiers are columns; the unique index is (source, vendor id, payload hash); an
    observation is tied to nothing.
    """
    names = set(ACTION.c.keys())
    assert {'process_date', 'effective_date', 'ex_date', 'record_date', 'payable_date'} <= names
    assert {'vendor_action_id', 'action_type', 'identifiers', 'raw_payload', 'payload_hash', 'received_at'} <= names
    assert {'request_id', 'attempt_no', 'requesting_symbol', 'requested_start', 'requested_end', 'as_of'} <= names
    assert not {'dataset_id', 'listing_id'} & names, 'nothing ties an observation to a dataset or a listing'
    (unique,) = [index for index in ACTION.indexes if index.unique]
    assert tuple(column.name for column in unique.columns) == DEDUPE_KEY
    assert not ACTION.foreign_keys
    assert isinstance(ACTION.c.raw_payload.type, sa.LargeBinary) and not ACTION.c.raw_payload.nullable


# --------------------------------------------------------------------------------------------- append-only


class _Captured:
    """A stand-in session that records each statement and reports one row written."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return MagicMock(rowcount=1)


@pytest.mark.asyncio
async def test_the_insert_names_the_dedupe_index_and_does_nothing_on_a_repeat_never_an_update():
    """A repeat is never overwritten: ON CONFLICT on exactly the dedupe key, DO NOTHING."""
    observation = crud.NewObservation(
        source=DataSource.ALPACA_API,
        vendor_action_id='ca-1',
        action_type='split',
        ex_date=None,
        record_date=None,
        payable_date=None,
        process_date=None,
        effective_date=None,
        identifiers={},
        raw_payload=b'{}',
        payload_hash='0' * 64,
        request_id=uuid.uuid4(),
        attempt_no=1,
        requesting_symbol='VFV',
        requested_start=None,
        requested_end=None,
        as_of=datetime(2026, 3, 2, tzinfo=UTC),
    )
    session = _Captured()

    assert await crud.insert_observations(session, [observation]) == 1

    (statement,) = session.statements
    sql = _compiled(statement)
    assert sql.startswith(f'INSERT INTO {ACTION.name} ')
    assert sql.endswith(f'ON CONFLICT ({", ".join(DEDUPE_KEY)}) DO NOTHING')
    assert 'DO UPDATE' not in sql


def test_the_repository_has_no_update_or_delete_path():
    """The model's docstring: never updated or deleted.

    The repository's public surface is the one insert and, since P51 (tj-7zjov1.146), the one READ of the clamp
    (name_changes_naming and the NameChange it returns); no statement in it is an update, a delete or raw text, and
    the read builds a SELECT and nothing else (test_name_change_clamp.py compiles it in full).
    """
    public = {name for name, value in vars(crud).items() if callable(value) and value.__module__ == crud.__name__}
    assert public == {'NewObservation', 'insert_observations', 'NameChange', 'name_changes_naming'}
    read = inspect.getsource(crud.name_changes_naming)
    assert 'select(' in read and 'insert(' not in read, 'the clamp read writes'
    tree = ast.parse(Path(crud.__file__).read_text(encoding='utf-8'))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {'update', 'delete', 'text', 'on_conflict_do_update', 'exec_driver_sql'}, called


def test_no_production_module_writes_corporate_action_other_than_by_the_repository_insert():
    """Only the repository imports the model, and no raw SQL in the server updates or deletes the table.

    Tests and revisions are left out of the scan.
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?corporate_action\b', re.IGNORECASE)
    importing_the_model = set()
    for path in SERVER_ROOT.rglob('*.py'):
        parts = path.relative_to(SERVER_ROOT).parts
        if 'tests' in parts or 'migrations' in parts:
            continue
        text = path.read_text(encoding='utf-8')
        assert not raw.search(text), f'{path} updates or deletes corporate_action in raw SQL'
        if re.search(r'store\.app\.database\.models(\.corporate_action\b| import .*\bcorporate_action\b)', text):
            importing_the_model.add(path.relative_to(SERVER_ROOT).as_posix())
    assert importing_the_model == {
        'data/store/app/database/crud/corporate_action.py',
        # P24 tj-7zjov1.36 (c716268), judged by P24v tj-7zjov1.241: the per-dataset event read (ADR tj-26jt9w addendum
        # 22). It only select()s corporate_action, never raw_payload; pinned by test_panel_ui_reads.py.
        'data/store/app/database/crud/panel_reads.py',
    }


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


def test_the_offline_upgrade_is_one_transaction_and_moves_the_version(upgrade_sql):
    assert upgrade_sql[0] == 'BEGIN' and upgrade_sql[-1] == 'COMMIT'
    assert upgrade_sql.count('BEGIN') == upgrade_sql.count('COMMIT') == 1
    (version,) = [s for s in upgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{REVISION}' WHERE alembic_version.version_num = '{PARENT}'"
    )


def test_the_offline_upgrade_sql_is_the_models_table_and_indexes_then_drop_not_null_on_the_five(upgrade_sql):
    statements = _bookkeeping(upgrade_sql)
    unique = next(index for index in ACTION.indexes if index.unique)
    plain = next(index for index in ACTION.indexes if not index.unique)

    assert statements == [
        _compiled(CreateTable(ACTION)),
        _compiled(CreateIndex(unique)),
        _compiled(CreateIndex(plain)),
        *[f'ALTER TABLE {RUN.name} ALTER COLUMN {column} DROP NOT NULL' for column in RELAXED],
    ]


def test_the_offline_downgrade_sql_restores_not_null_then_drops_the_indexes_and_the_table(downgrade_sql):
    statements = _bookkeeping(downgrade_sql)

    assert statements == [
        *[f'ALTER TABLE {RUN.name} ALTER COLUMN {column} SET NOT NULL' for column in reversed(RELAXED)],
        f'DROP INDEX {CorporateAction.REQUEST_INDEX}',
        f'DROP INDEX {CorporateAction.OBSERVATION_UNIQUE_INDEX}',
        f'DROP TABLE {ACTION.name}',
    ]
    (version,) = [s for s in downgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{PARENT}' WHERE alembic_version.version_num = '{REVISION}'"
    )
