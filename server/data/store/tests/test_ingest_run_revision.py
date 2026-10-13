"""The ingest_run revision, 6f5a798746a1, against its design and its model, without a database (tj-grna9p.18).

The design is tj-grna9p.18's architect re-baseline of 2026-10-11, items 2 and 3, and the FILES list:
a NEW additive Alembic revision on down_revision c4a1f7b2e905, "purely additive (enum types, table and
indexes; downgrade drops them), no additive-exception marker", with dataset_id and request_id NULLABLE
and carrying NO foreign key, because a run outlives its dataset.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb), as in test_head_revision_shape.py: the revision's DDL is
compared against the MODEL's DDL compiled for Postgres, and the downgrade against the upgrade. The only
literals are the ones the design names (the parent, the two new type names, the three indexes' purposes).

WHAT THIS DOES NOT PROVE, and the system suite does (tests/system, through the agent-stack MCP): that the
revision applies, downgrades and re-applies against a real Postgres holding seeds.
"""

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / '6f5a798746a1_ingest_run.py'
DIALECT = postgresql.dialect()
NEW_TYPES = ['ingest_run_trigger', 'ingest_run_state']


def _load() -> Any:
    spec = importlib.util.spec_from_file_location('revision_6f5a798746a1', REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str) -> tuple[MagicMock, list[str], list[str]]:
    """Run upgrade() or downgrade() against a recording op; returns (op, enum types created, dropped)."""
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


def _schema_calls(op: MagicMock) -> list[str]:
    return [name for name, _, _ in op.method_calls if name != 'get_bind']


def _ddl_of(table: sa.Table) -> str:
    return ' '.join(str(CreateTable(table).compile(dialect=DIALECT)).split())


def _revision_table(op: MagicMock) -> sa.Table:
    ((_, args, _),) = [call for call in op.method_calls if call[0] == 'create_table']
    return sa.Table(args[0], sa.MetaData(), *args[1:])


def _revision_indexes(op: MagicMock) -> set[str]:
    objects = SchemaObjects()
    return {
        str(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)).compile(dialect=DIALECT))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }


def test_the_revision_sits_on_the_feed_revision_and_carries_no_additive_exception_marker():
    """The re-baseline names the parent and says 'no additive-exception marker': this one IS additive."""
    module = _load()
    assert (module.revision, module.down_revision) == ('6f5a798746a1', 'c4a1f7b2e905')
    assert 'additive-exception' not in REVISION_FILE.read_text(encoding='utf-8').lower()


def test_upgrade_only_creates_two_types_one_table_and_three_indexes():
    """PURELY ADDITIVE: no alter, no drop, no data statement. The existing enum types are reused, not created."""
    op, created, dropped = _run('upgrade')

    assert _schema_calls(op) == ['create_table', 'create_index', 'create_index', 'create_index']
    assert created == NEW_TYPES
    assert dropped == []
    table = _revision_table(op)
    for column in table.columns:
        if isinstance(column.type, postgresql.ENUM):
            assert column.type.create_type is False, f'{column.name} would CREATE its type inside create_table'


def test_downgrade_drops_exactly_what_upgrade_created_in_reverse():
    """The inverse: three indexes, the table, then the two types the revision owns -- and nothing shared."""
    up, created, _ = _run('upgrade')
    down, _, dropped = _run('downgrade')

    created_indexes = [args[0] for name, args, _ in up.method_calls if name == 'create_index']
    dropped_indexes = [args[0] for name, args, _ in down.method_calls if name == 'drop_index']
    assert dropped_indexes == list(reversed(created_indexes))
    assert [args[0] for name, args, _ in down.method_calls if name == 'drop_table'] == [IngestRun.TABLE_NAME]
    assert _schema_calls(down) == ['drop_index', 'drop_index', 'drop_index', 'drop_table']
    assert dropped == list(reversed(created)), 'assettype, feed and the rest belong to other tables'


def test_the_revision_builds_exactly_the_table_the_model_declares():
    """Every column, type, nullability and server default, compared as Postgres DDL, model against revision."""
    op, _, _ = _run('upgrade')

    assert _ddl_of(_revision_table(op)) == _ddl_of(IngestRun.__table__)


def test_the_revision_builds_exactly_the_indexes_the_model_declares():
    """The keyset index, the partial RUNNING index and the finished_at index, as Postgres DDL."""
    op, _, _ = _run('upgrade')
    declared = {str(CreateIndex(index).compile(dialect=DIALECT)) for index in IngestRun.__table__.indexes}

    assert _revision_indexes(op) == declared
    assert len(declared) == 3


def test_the_two_new_types_carry_the_models_members_in_order():
    """A member in the model and not the type is a run the database refuses; the order is Postgres's sort order."""
    module = _load()
    assert list(module.TRIGGER_MEMBERS) == [member.value for member in IngestRunTrigger]
    assert list(module.STATE_MEMBERS) == [member.value for member in IngestRunState]


def test_no_column_is_a_foreign_key_and_the_dataset_and_request_ids_are_nullable():
    """Item 2: runs outlive their dataset, and a run not yet acknowledged has no dataset id at all."""
    op, _, _ = _run('upgrade')
    table = _revision_table(op)

    assert not table.foreign_keys
    assert not IngestRun.__table__.foreign_keys
    assert table.c.dataset_id.nullable and table.c.request_id.nullable
    assert not table.c.bars_written.nullable and table.c.bars_written.server_default.arg == '0'


def test_the_migration_env_loads_the_model_so_autogenerate_sees_the_table():
    """env.py imports only ALLOWED_MODELS; without ingest_run there, alembic check would want to drop it."""
    text = (MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8')
    allowed = next(line for line in text.splitlines() if line.startswith('ALLOWED_MODELS'))

    assert "'ingest_run'" in allowed, allowed
