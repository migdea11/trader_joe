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

import ast
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn, CreateIndex

from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / '6f5a798746a1_ingest_run.py'
# The later revisions that change ingest_run, in chain order: the model is what the HEAD leaves. f2b7d4a91c36
# (tj-7zjov1.25) adds the attempt columns and index; b8e4c1f7a923 (tj-7zjov1.123) adds served_start and served_end;
# c7e19a4d5b28 (tj-7zjov1.31, P18) relaxes five label columns to nullable with alter_column.
LATER_FILES = [
    MIGRATIONS_DIR / 'versions' / 'f2b7d4a91c36_request_record_and_instance_marker.py',
    MIGRATIONS_DIR / 'versions' / 'b8e4c1f7a923_push_path_columns.py',
    MIGRATIONS_DIR / 'versions' / 'c7e19a4d5b28_corporate_action_observations.py',
]
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


def _later_upgrade_ops() -> list[MagicMock]:
    """Each LATER revision's upgrade() run against a recording op, in chain order."""
    ops: list[MagicMock] = []
    for path in LATER_FILES:
        spec = importlib.util.spec_from_file_location(f'revision_{path.name.split("_", 1)[0]}', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        op = MagicMock(name='op')
        with (
            patch.object(module, 'op', op),
            patch.object(postgresql.ENUM, 'create', autospec=True),
            patch.object(postgresql.ENUM, 'drop', autospec=True),
        ):
            module.upgrade()
        ops.append(op)
    return ops


def _later_ingest_run_alterations() -> list[tuple[str, dict[str, Any]]]:
    """The alter_column calls LATER revisions make on ingest_run, in chain order: (column, keyword arguments).

    c7e19a4d5b28 (tj-7zjov1.31) is the first: it relaxes five label columns. The earlier revisions declared them NOT
    NULL on purpose and are not rewritten, so the head's table is this revision's with these alterations applied.
    """
    return [
        (args[1], kwargs)
        for op in _later_upgrade_ops()
        for name, args, kwargs in op.method_calls
        if name == 'alter_column' and args[0] == IngestRun.TABLE_NAME
    ]


def _later_ingest_run_changes() -> tuple[list[sa.Column], set[str]]:
    """The columns and indexes LATER revisions add to ingest_run: f2b7d4a91c36 (tj-7zjov1.25), b8e4c1f7a923 (.123).

    The model describes ingest_run as the HEAD leaves it, so the model comparisons below compose this revision's
    table with what came after it, read from the later revisions' own upgrade() rather than restated here.
    """
    columns: list[sa.Column] = []
    indexes: set[str] = set()
    objects = SchemaObjects()
    for op in _later_upgrade_ops():
        columns += [
            args[1] for name, args, _ in op.method_calls if name == 'add_column' and args[0] == IngestRun.TABLE_NAME
        ]
        indexes |= {
            str(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)).compile(dialect=DIALECT))
            for name, args, kwargs in op.method_calls
            if name == 'create_index' and args[1] == IngestRun.TABLE_NAME
        }
    return columns, indexes


def _column_ddl(table: sa.Table) -> dict[str, str]:
    return {column.name: str(CreateColumn(column).compile(dialect=DIALECT)) for column in table.columns}


def test_the_revision_and_the_later_ones_build_exactly_the_table_the_model_declares():
    """Every column, type, nullability and server default, compared as Postgres DDL, model against the HEAD's table.

    THE MODEL MOVED ON (tj-7zjov1.25): f2b7d4a91c36 adds fourteen nullable columns to ingest_run, and b8e4c1f7a923
    (tj-7zjov1.123) two more, so the model is compared with this revision's table plus those columns. Per column rather than as one CREATE TABLE, because
    ADD COLUMN appends where the model declares the attempt columns beside request_id; column order is not schema
    that any statement here depends on. This revision's own columns are still compared, as a strict subset.

    THE LABEL COLUMNS WERE RELAXED (tj-7zjov1.31): c7e19a4d5b28 alters five of this revision's columns to nullable.
    This revision still declares them NOT NULL (history is not rewritten), so the alterations are applied to a copy of
    this revision's table before the comparison, and only an alteration of nullability is accepted: a type or default
    change there would be a retype the composition does not model, and fails here.
    """
    op, _, _ = _run('upgrade')
    table = _revision_table(op)
    later_columns, _ = _later_ingest_run_changes()
    later = {column.name: str(CreateColumn(column).compile(dialect=DIALECT)) for column in later_columns}
    alterations = _later_ingest_run_alterations()
    for column_name, kwargs in alterations:
        assert set(kwargs) == {'nullable'}, f'{column_name} is altered by more than its nullability: {kwargs}'
        assert column_name in table.c, f'{column_name} is altered but this revision did not create it'
        table.c[column_name].nullable = kwargs['nullable']
    own = _column_ddl(table)

    assert not own.keys() & later.keys(), 'a later revision re-adds a column this one created'
    assert len(later) == len(later_columns), 'two later revisions add the same column'
    assert {**own, **later} == _column_ddl(IngestRun.__table__)
    # The primary key is not a column property, so it is compared on its own.
    assert [c.name for c in table.primary_key] == [c.name for c in IngestRun.__table__.primary_key]


def test_the_head_relaxes_exactly_the_five_label_columns_this_revision_declared_not_null():
    """tj-7zjov1.31 (P34 escalation rulings, 20:17 UTC note): five label columns, nullable only; source stays NOT NULL.

    The five names are the design's. This revision keeps them NOT NULL as written, and the model declares them
    nullable, so a later revision is what relaxed them and nothing rewrote this one.
    """
    label_columns = {'asset_symbol', 'asset_type', 'data_type', 'granularity', 'update_type'}
    alterations = _later_ingest_run_alterations()
    table = _revision_table(_run('upgrade')[0])

    assert [kwargs for _, kwargs in alterations] == [{'nullable': True}] * len(label_columns)
    assert {column for column, _ in alterations} == label_columns
    for column in label_columns:
        assert not table.c[column].nullable, f'{REVISION_FILE.name} was rewritten: {column} is nullable there'
        assert IngestRun.__table__.c[column].nullable, f'the model still declares {column} NOT NULL'
    assert not IngestRun.__table__.c.source.nullable, 'source is not a label the design relaxes'


def test_the_revision_and_the_later_ones_build_exactly_the_indexes_the_model_declares():
    """The keyset index, the partial RUNNING index, the finished_at index, and since f2b7d4a91c36 the attempt index."""
    op, _, _ = _run('upgrade')
    declared = {str(CreateIndex(index).compile(dialect=DIALECT)) for index in IngestRun.__table__.indexes}
    _, later = _later_ingest_run_changes()

    assert len(_revision_indexes(op)) == 3
    assert len(later) == 1
    assert _revision_indexes(op) | later == declared


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
    # Read with ast, not by line: since tj-grna9p.46 the set spans several lines, and a first-line match
    # pinned the formatting rather than the membership.
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]

    assert 'ingest_run' in allowed, allowed
