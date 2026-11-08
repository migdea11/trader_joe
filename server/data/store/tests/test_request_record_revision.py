"""Revision f2b7d4a91c36, the request record and the instance marker, against its design and its models, offline.

THE DESIGN is bead tj-7zjov1.25 (PR 5 P33) and what it names: tj-26jt9w addenda 4 and 5, PR5-PLAN REV1 N1 (the P33
row) and N4. Its acceptance criteria, which the literals below come from:
  * ingest_request: one row per request; kind (bars, asset list, corporate actions); source; dataset NULLABLE (the
    listing column was dropped by the P49 amend, tj-7zjov1.143: symbol identity is not modelled); the request parameters; ready_by NULLABLE; client label; state (PENDING, QUEUED, NOT_DELIVERED,
    SUCCEEDED, EMPTY, FAILED); instance id; attempts; summed spend; failure cause (EXPIRED among the causes);
    created and finished times;
  * ingest_instance: instance id, first and last seen, superseded at;
  * ingest_run.request_id gains its meaning, still with NO foreign key;
  * ingest_run.vendor_asset_id, NULLABLE, added with the other attempt columns (P49 amend, tj-7zjov1.143);
  * a UNIQUE index on ingest_run (request_id, attempt_no) WHERE both are set;
  * passes the P02 lint UNMARKED: purely additive, a new revision on the head it was written on (a6d3f8e1c205).

TWO TIERS, NEITHER A DATABASE.
  1. The revision body is run against a recording stand-in for alembic.op, as test_ingest_run_revision.py does:
     what it calls, in what order, and that downgrade() is its inverse.
  2. The revision is rendered by ALEMBIC ITSELF in offline mode (alembic upgrade a6d3f8e1c205:f2b7d4a91c36 --sql, and
     the downgrade back), in a subprocess from data/store as CI runs alembic, with a DATABASE_URI no server answers.
     That SQL text is what Postgres would receive, and it is compared statement by statement with the MODELS'
     DDL compiled for Postgres: both new tables whole, the fourteen ingest_run columns one by one, the four
     indexes with their predicates, the two new enum types with their labels. Nothing else may be in it.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb): every comparison is revision against model, downgrade against upgrade,
or the rendered SQL against the compiled model. The literals are the design's (the parent, the type and index
purposes, the acceptance criteria's members).

WHAT THIS DOES NOT PROVE, and tests/system does on Postgres: that the revision applies from empty and on the
a6d3f8e1c205 seed with every row kept, that a repeat upgrade writes nothing (test_migration_with_data.py), that
alembic check is clean at the head, and that the partial unique index admits and refuses what it should
(test_request_record_schema.py).
"""

import ast
import importlib.util
import re
import subprocess
import sys
from collections import Counter
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.named_types import CreateEnumType
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'f2b7d4a91c36'
PARENT = 'a6d3f8e1c205'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_request_record_and_instance_marker.py'
# The one revision after this one (tj-7zjov1.123), which adds columns to ingest_request and ingest_run. The models
# describe the tables as the HEAD leaves them, so the comparisons below take that revision's own additions off the
# model (or put them on the revision's table), read from its upgrade() rather than restated here.
CHILD = 'b8e4c1f7a923'
CHILD_FILE = MIGRATIONS_DIR / 'versions' / f'{CHILD}_push_path_columns.py'
# The head since tj-7zjov1.153 (P35r): directly on the child, it adds ingest_request.expiry_type. Its additions are
# taken off the model exactly as the child's are, read from its own upgrade().
HEAD = '39c28a39b290'
HEAD_FILE = MIGRATIONS_DIR / 'versions' / f'{HEAD}_request_expiry_type.py'
# Every revision after this one, in chain order: the model is what the last of them leaves.
LATER_FILES = [CHILD_FILE, HEAD_FILE]
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)

REQUEST = IngestRequest.__table__
INSTANCE = IngestInstance.__table__
RUN = IngestRun.__table__
NEW_TYPES = ['ingest_request_kind', 'ingest_request_state']

# Nothing listens on port 1 of loopback, and offline mode never connects: the URL only picks the dialect.
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'


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


def _schema_calls(op: MagicMock) -> list[str]:
    return [name for name, _, _ in op.method_calls if name != 'get_bind']


def _created_tables(op: MagicMock) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        args[0]: sa.Table(args[0], metadata, *args[1:]) for name, args, _ in op.method_calls if name == 'create_table'
    }


def _compiled(element: Any) -> str:
    return ' '.join(str(element.compile(dialect=DIALECT)).split())


def _child_added_columns() -> dict[str, list[sa.Column]]:
    """The columns the later revisions add, per table, in chain order, from their own upgrade() against a recorded op.

    The child (tj-7zjov1.123) and, since tj-7zjov1.153, the head on it; each one's add_column calls, nothing restated.
    """
    added: dict[str, list[sa.Column]] = {}
    for path in LATER_FILES:
        spec = importlib.util.spec_from_file_location(f'revision_{path.stem}', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        op = MagicMock(name='op')
        with patch.object(module, 'op', op), patch.object(postgresql.ENUM, 'create', autospec=True):
            module.upgrade()
        for name, args, _ in op.method_calls:
            if name == 'add_column':
                added.setdefault(args[0], []).append(args[1])
    return added


def _model_columns_in(table: sa.Table, names: list[str]) -> sa.Table:
    """A copy of the model table holding exactly the named columns, in the order given (column order is not schema)."""
    by_name = {column.name: column for column in table.columns}
    return sa.Table(table.name, sa.MetaData(), *[by_name[name]._copy() for name in names])


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3: one new revision, down_revision the previous head, one head, no exception marker.

    No longer the head since tj-7zjov1.123: its one child is b8e4c1f7a923. The single head is 39c28a39b290 since
    tj-7zjov1.153 (P35r), directly on that child.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [HEAD]
    assert script.get_revision(HEAD).down_revision == CHILD
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == REVISION] == [CHILD]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_migration_env_loads_both_new_models_so_autogenerate_sees_them():
    """Without them in ALLOWED_MODELS, alembic check would want to drop both tables on every migrated database."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    assert {IngestRequest.TABLE_NAME, IngestInstance.TABLE_NAME, IngestRun.TABLE_NAME} <= allowed, allowed


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_only_creates_two_types_two_tables_columns_and_indexes():
    """PURELY ADDITIVE: no alter, no drop, no data statement; the shared enum types are referenced, never created."""
    op, created, dropped = _run('upgrade')

    added = [args[0] for name, args, _ in op.method_calls if name == 'add_column']
    assert _schema_calls(op) == [
        'create_table',
        'create_index',
        'create_index',
        'create_index',
        'create_table',
        *['add_column'] * len(added),
        'create_index',
    ]
    assert set(added) == {RUN.name}
    assert created == NEW_TYPES
    assert dropped == []
    for table in _created_tables(op).values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, f'{column.name} would CREATE its type inside create_table'


def test_every_column_added_to_ingest_run_is_nullable_with_no_default():
    """Checklist item 7: an existing row has no attempt, so each new column is NULL on it and nothing is backfilled."""
    op, _, _ = _run('upgrade')
    added = [args[1] for name, args, _ in op.method_calls if name == 'add_column']

    assert added, 'the revision adds no ingest_run column, so this check is vacuous'
    for column in added:
        assert column.nullable, f'{column.name} is NOT NULL, which an existing ingest_run row cannot satisfy'
        assert column.server_default is None and column.default is None, f'{column.name} carries a default'


def test_downgrade_drops_exactly_what_upgrade_created_in_reverse():
    """The inverse, as multisets and in order: indexes, columns, tables, then the two types the revision owns."""
    up, created, _ = _run('upgrade')
    down, _, dropped = _run('downgrade')

    created_indexes = [(args[1], args[0]) for name, args, _ in up.method_calls if name == 'create_index']
    dropped_indexes = [
        (kwargs['table_name'], args[0]) for name, args, kwargs in down.method_calls if name == 'drop_index'
    ]
    assert Counter(dropped_indexes) == Counter(created_indexes)
    added = [(args[0], args[1].name) for name, args, _ in up.method_calls if name == 'add_column']
    removed = [(args[0], args[1]) for name, args, _ in down.method_calls if name == 'drop_column']
    assert removed == list(reversed(added))
    created_tables = [args[0] for name, args, _ in up.method_calls if name == 'create_table']
    dropped_tables = [args[0] for name, args, _ in down.method_calls if name == 'drop_table']
    assert dropped_tables == list(reversed(created_tables))
    assert dropped == list(reversed(created)), 'datasource, assettype, granularity and feed belong to other tables'
    assert set(_schema_calls(down)) == {'drop_index', 'drop_column', 'drop_table'}


def test_downgrade_drops_each_index_with_the_predicate_it_was_created_with():
    up, _, _ = _run('upgrade')
    down, _, _ = _run('downgrade')

    def predicates(op: MagicMock, kind: str) -> dict[str, str | None]:
        return {
            args[0]: (str(kwargs['postgresql_where']) if 'postgresql_where' in kwargs else None)
            for name, args, kwargs in op.method_calls
            if name == kind
        }

    assert predicates(down, 'drop_index') == predicates(up, 'create_index')


# --------------------------------------------------------------------------------------------- revision against model


@pytest.mark.parametrize('model', [IngestRequest, IngestInstance], ids=lambda m: m.TABLE_NAME)
def test_the_revision_builds_exactly_the_table_the_model_declares(model):
    """Every column, type, nullability, server default and the primary key, as Postgres DDL, model against revision.

    THE TABLE AT HEAD (tj-7zjov1.123): b8e4c1f7a923 adds four ingest_request columns and 39c28a39b290 (tj-7zjov1.153)
    a fifth, so the revision's table plus the later additions is compared with the model, whole, in the order a
    migrated database holds them.
    """
    op, _, _ = _run('upgrade')
    created = _created_tables(op)[model.TABLE_NAME]
    later = [column._copy() for column in _child_added_columns().get(model.TABLE_NAME, [])]
    head = sa.Table(created.name, sa.MetaData(), *[column._copy() for column in created.columns], *later)
    names = [column.name for column in head.columns]

    assert len(names) == len(set(names)), 'the child re-adds a column this revision created'
    assert sorted(names) == sorted(column.name for column in model.__table__.columns)
    assert _compiled(CreateTable(head)) == _compiled(CreateTable(_model_columns_in(model.__table__, names)))


def test_the_revision_adds_to_ingest_run_exactly_the_columns_the_model_has_that_6f5a798746a1_did_not():
    """The fourteen attempt columns (vendor_asset_id since P49), one by one as Postgres DDL, against the model's own columns of those names."""
    op, _, _ = _run('upgrade')
    added = {
        args[1].name: _compiled(CreateColumn(args[1])) for name, args, _ in op.method_calls if name == 'add_column'
    }

    declared = {column.name: _compiled(CreateColumn(column)) for column in RUN.columns if column.name in added}
    assert added == declared
    assert len(added) == 14
    assert 'vendor_asset_id' in added, 'the P49 amend adds it with the other attempt columns'


def test_the_revision_builds_exactly_the_four_indexes_the_models_declare():
    """Three on ingest_request (open states, QUEUED by instance, dataset) and the attempt unique index on ingest_run."""
    op, _, _ = _run('upgrade')
    objects = SchemaObjects()
    built = {
        _compiled(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }
    declared = {_compiled(CreateIndex(index)) for index in [*REQUEST.indexes, *INSTANCE.indexes]}
    declared |= {_compiled(CreateIndex(index)) for index in RUN.indexes if index.name == IngestRun.ATTEMPT_UNIQUE_INDEX}

    assert built == declared
    assert len(declared) == 4


def test_the_two_new_types_carry_the_models_members_in_order_and_the_designs_members():
    """A member in the model and not the type is a write the database refuses; the order is Postgres's sort order."""
    module = _load()
    assert (
        list(module.KIND_MEMBERS) == [member.value for member in IngestRequestKind] == list(REQUEST.c.kind.type.enums)
    )
    assert list(module.STATE_MEMBERS) == [m.value for m in IngestRequestState] == list(REQUEST.c.state.type.enums)
    # The bead's acceptance criteria, in its order.
    assert list(module.STATE_MEMBERS) == ['PENDING', 'QUEUED', 'NOT_DELIVERED', 'SUCCEEDED', 'EMPTY', 'FAILED']
    assert {member.value for member in IngestRequestKind} == {'BARS', 'ASSET_LIST', 'CORPORATE_ACTIONS'}
    assert IngestRequestFailureCause.EXPIRED.value == 'EXPIRED'


def test_every_enum_column_names_the_models_type_with_the_models_labels():
    """CreateTable renders an enum column by type NAME only, so the labels of every enum column are compared here."""
    op, _, _ = _run('upgrade')
    tables = _created_tables(op)
    compared = 0
    for name, table in tables.items():
        model_table = {REQUEST.name: REQUEST, INSTANCE.name: INSTANCE}[name]
        for column in table.columns:
            if isinstance(column.type, sa.Enum):
                model_type = model_table.c[column.name].type
                assert (column.type.name, list(column.type.enums)) == (model_type.name, list(model_type.enums)), (
                    column.name
                )
                compared += 1
    assert compared == 6, 'kind, state, source, asset_type, granularity and feed'


def test_no_foreign_key_anywhere_and_the_design_nullabilities_hold():
    """Runs outlive requests and requests outlive datasets: nothing references anything, in revision or model."""
    op, _, _ = _run('upgrade')
    tables = _created_tables(op)

    for table in [*tables.values(), REQUEST, INSTANCE, RUN]:
        assert not table.foreign_keys, table.name
    request = tables[REQUEST.name]
    for nullable in ('dataset_id', 'ready_by', 'instance_id', 'finished_at', 'failure_cause'):
        assert request.c[nullable].nullable, nullable
    # P49 (tj-7zjov1.143): listing_id was never written and symbol identity is not modelled, so neither the
    # revision nor the model has it.
    assert 'listing_id' not in request.c
    assert 'listing_id' not in REQUEST.c
    assert RUN.c.vendor_asset_id.nullable
    for required in ('kind', 'source', 'client_label', 'state', 'attempts', 'created_at'):
        assert not request.c[required].nullable, required
    assert RUN.c.request_id.nullable
    assert tables[INSTANCE.name].c.superseded_at.nullable


def test_the_attempt_index_is_unique_and_partial_on_both_columns_being_set():
    """The design's 'where both are set', read off the model and the revision call, not off a constant."""
    (index,) = [index for index in RUN.indexes if index.name == IngestRun.ATTEMPT_UNIQUE_INDEX]
    assert index.unique
    assert [column.name for column in index.columns] == ['request_id', 'attempt_no']
    predicate = ' '.join(str(index.dialect_options['postgresql']['where']).split())
    assert predicate == 'request_id IS NOT NULL AND attempt_no IS NOT NULL'

    op, _, _ = _run('upgrade')
    (kwargs,) = [kwargs for name, args, kwargs in op.method_calls if name == 'create_index' and args[1] == RUN.name]
    assert kwargs['unique'] is True
    assert ' '.join(str(kwargs['postgresql_where']).split()) == predicate


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
    # Alembic's '-- Running upgrade ...' comment lines precede a statement in the same chunk, so they go first.
    text = '\n'.join(line for line in result.stdout.splitlines() if not line.lstrip().startswith('--'))
    statements = [' '.join(part.split()) for part in text.split(';\n')]
    return [statement for statement in statements if statement]


@pytest.fixture(scope='module')
def upgrade_sql() -> list[str]:
    return _offline('upgrade', f'{PARENT}:{REVISION}')


@pytest.fixture(scope='module')
def downgrade_sql() -> list[str]:
    return _offline('downgrade', f'{REVISION}:{PARENT}')


def _bookkeeping(statements: list[str]) -> list[str]:
    """Drop alembic's own transaction and version-table statements, keeping only the revision's."""
    return [s for s in statements if s not in ('BEGIN', 'COMMIT') and not s.startswith('UPDATE alembic_version')]


def _model_upgrade_ddl() -> list[str]:
    """What the MODELS say the revision must send, in the revision's order, compiled for Postgres.

    The later revisions' columns (tj-7zjov1.123, tj-7zjov1.153) are taken off the models: this revision's SQL never
    held them.
    """
    child = {table: {column.name for column in columns} for table, columns in _child_added_columns().items()}
    later_run = child.get(RUN.name, set())
    added = [c for c in RUN.columns if c.name not in _ingest_run_columns_before() and c.name not in later_run]
    later_request = child.get(REQUEST.name, set())
    request = _model_columns_in(REQUEST, [c.name for c in REQUEST.columns if c.name not in later_request])
    return [
        _compiled(CreateEnumType(REQUEST.c.kind.type)),
        _compiled(CreateEnumType(REQUEST.c.state.type)),
        _compiled(CreateTable(request)),
        *sorted(_compiled(CreateIndex(index)) for index in REQUEST.indexes),
        _compiled(CreateTable(INSTANCE)),
        *[f'ALTER TABLE {RUN.name} ADD COLUMN {_compiled(CreateColumn(column))}' for column in added],
        *[_compiled(CreateIndex(index)) for index in RUN.indexes if index.name == IngestRun.ATTEMPT_UNIQUE_INDEX],
    ]


def _ingest_run_columns_before() -> set[str]:
    """ingest_run's columns as 6f5a798746a1 created them: the only revision before this one to touch the table."""
    spec = importlib.util.spec_from_file_location(
        'revision_6f5a798746a1', MIGRATIONS_DIR / 'versions' / '6f5a798746a1_ingest_run.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    op = MagicMock(name='op')
    with patch.object(module, 'op', op), patch.object(postgresql.ENUM, 'create', autospec=True):
        module.upgrade()
    ((_, args, _),) = [call for call in op.method_calls if call[0] == 'create_table']
    return {column.name for column in args[1:] if isinstance(column, sa.Column)}


def test_the_offline_upgrade_sql_is_exactly_the_models_ddl(upgrade_sql):
    """Alembic's rendering of upgrade(), statement by statement, against the models compiled for Postgres.

    Ordered except within ingest_request's three indexes, which are compared sorted: their order is not schema. The
    alembic_version bookkeeping is the only other thing in the output, so a stray statement of any kind is red.
    """
    revision = _bookkeeping(upgrade_sql)
    request_indexes = slice(3, 3 + len(REQUEST.indexes))
    revision[request_indexes] = sorted(revision[request_indexes])

    assert revision == _model_upgrade_ddl()


def test_the_offline_upgrade_moves_alembic_version_from_the_parent_to_the_revision(upgrade_sql):
    (version,) = [s for s in upgrade_sql if s.startswith('UPDATE alembic_version')]
    assert (
        version == f"UPDATE alembic_version SET version_num='{REVISION}' WHERE alembic_version.version_num = '{PARENT}'"
    )


def test_the_offline_downgrade_sql_drops_exactly_what_the_upgrade_created(upgrade_sql, downgrade_sql):
    """Every index, column, table and type the upgrade's SQL creates, the downgrade's SQL drops, and nothing else."""
    created: Counter = Counter()
    for statement in _bookkeeping(upgrade_sql):
        if match := re.match(r'CREATE TYPE (\w+)', statement):
            created['type', match[1]] += 1
        elif match := re.match(r'CREATE TABLE (\w+)', statement):
            created['table', match[1]] += 1
        elif match := re.match(r'CREATE (?:UNIQUE )?INDEX (\w+)', statement):
            created['index', match[1]] += 1
        elif match := re.match(r'ALTER TABLE (\w+) ADD COLUMN (\w+)', statement):
            created['column', f'{match[1]}.{match[2]}'] += 1
        else:
            pytest.fail(f'upgrade statement of no known kind: {statement}')
    dropped: Counter = Counter()
    for statement in _bookkeeping(downgrade_sql):
        if match := re.fullmatch(r'DROP TYPE (\w+)', statement):
            dropped['type', match[1]] += 1
        elif match := re.fullmatch(r'DROP TABLE (\w+)', statement):
            dropped['table', match[1]] += 1
        elif match := re.fullmatch(r'DROP INDEX (\w+)', statement):
            dropped['index', match[1]] += 1
        elif match := re.fullmatch(r'ALTER TABLE (\w+) DROP COLUMN (\w+)', statement):
            dropped['column', f'{match[1]}.{match[2]}'] += 1
        else:
            pytest.fail(f'downgrade statement of no known kind: {statement}')

    assert dropped == created
    assert sum(created.values()) == 2 + 2 + 4 + 14
    assert created['column', f'{RUN.name}.vendor_asset_id'] == 1, 'downgrade drops it with the other attempt columns'
