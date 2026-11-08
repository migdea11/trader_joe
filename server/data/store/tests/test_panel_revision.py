"""Revision b6d1f93a7c42, the panel tables, dataset_override_log.new_value and the request-rate index, offline.

THE DESIGN is tj-7zjov1.33 (P20): ADR tj-26jt9w addendum 20 (the five panel rulings: the ledger now, append-only; the
name panel; the markers saved_at, actor and reason; the architect defaults (a) to (e)); the owner rulings of
2026-10-27 on the bead (no policy default in the schema for cadence or anything else: the default is applied where a
panel is created); the architect's RATE DESIGN note of 14:16 UTC 2026-10-27 (ramp_datasets_per_hour and
request_cap_per_hour, CHECK ck_panel_source_settings_version_numbers_non_negative, panel_member_transition.recorded_at
NOT NULL server default now() with the index (transition, recorded_at), panel_pass.kind of the new enum panel_pass_kind
(MEMBERSHIP, RELEASE) NOT NULL with no default, ingest_request's index ix_ingest_request_source_created_at, deferral
causes ramp_rate and request_rate); and the orchestrator's assumption note of 2026-10-28 (the unit is PER HOUR). The
literals below are the design's; everything else is a relationship (revision against model, upgrade against
downgrade).

THREE TIERS, NONE A DATABASE, as test_asset_list_revision.py: the body against a recording op; the created tables and
indexes against the models compiled for Postgres; alembic's own offline SQL. And the append-only property of the
repository: no production module touches the tables yet (P20b and P20c add the crud and extend that pin).

NOT PROVED HERE, and where it is: the revision applies from empty and on the a3f8d2c6e914 seed with every row kept,
new_value NULL on every earlier override row, the downgrade on a database holding data
(tests/system/test_migration_with_data.py, tests/system/test_migration_downgrade.py); the uniqueness, CHECKs and
foreign keys refusing on real Postgres, a transition outliving its dataset, no panel row seeded (the panel section of
tests/system/test_migration_with_data.py); alembic check clean at head (tests/system); the canonical seed
(test_seed_guard.py, test_seed_head_content.py).
"""

import ast
import importlib.util
import re
import subprocess
import sys
from collections import Counter
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
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelTransition,
)


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION = 'b6d1f93a7c42'
PARENT = 'a3f8d2c6e914'  # the 01:41 UTC architect note on tj-7zjov1.33: base the revision on head a3f8d2c6e914
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_panels.py'
DIALECT = postgresql.dialect()
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

REQUEST = IngestRequest.__table__
OVERRIDE = DatasetOverrideLog.__table__
# In creation order: the foreign keys run panel <- versions <- pass <- transition.
MODELS = (Panel, PanelSettingsVersion, PanelSourceSettingsVersion, PanelPass, PanelMemberTransition)
TABLE_NAMES = [model.TABLE_NAME for model in MODELS]

# The design's names and members (addendum 20, the cadence ruling, the 14:16 rate design).
TYPES = {
    'panel_cadence': ['DAILY', 'WEEKLY', 'MONTHLY'],
    'panel_transition': ['ENTERED', 'CREATED', 'DEFERRED', 'REFUSED', 'EXITED', 'DRAINING', 'DRAINED'],
    'panel_pass_kind': ['MEMBERSHIP', 'RELEASE'],
}
MODEL_ENUMS = {'panel_cadence': PanelCadence, 'panel_transition': PanelTransition, 'panel_pass_kind': PanelPassKind}
REQUEST_RATE_INDEX = 'ix_ingest_request_source_created_at'

# The columns per table, as the bead and addendum 20 name them, with the rate design's renames and additions; the
# filters are spelled allow_symbols, deny_symbols, asset_class, exchange, active, tradable (the builder's names for
# "allow and deny lists, asset class, exchange, active and tradable"), the window corporate_action_window_pre_days and
# _post_days ("corporate-action window defaults").
DESIGN_COLUMNS = {
    'panel': {'id', 'name', 'source', 'created_at'},
    'panel_settings_version': {
        'id', 'panel_id', 'version_no', 'enabled', 'cadence', 'size', 'allow_symbols', 'deny_symbols',
        'asset_class', 'exchange', 'active', 'tradable', 'saved_at', 'actor', 'reason',
    },
    'panel_source_settings_version': {
        'id', 'source', 'version_no', 'ramp_datasets_per_hour', 'request_cap_per_hour',
        'corporate_action_window_pre_days', 'corporate_action_window_post_days', 'saved_at', 'actor', 'reason',
    },
    'panel_pass': {
        'id', 'panel_id', 'kind', 'settings_version_id', 'source_settings_version_id', 'active_snapshot_id',
        'inactive_snapshot_id', 'started_at', 'finished_at', 'dry_run', 'counts',
    },
    'panel_member_transition': {
        'id', 'pass_id', 'panel_id', 'vendor_asset_id', 'symbol', 'transition', 'dataset_id', 'request_id', 'cause',
        'as_of', 'recorded_at', 'seq',
    },
}  # fmt: skip
# Nullable exactly where the design says so: the filters that may not filter, inactive_snapshot_id, finished_at and
# counts until the finish-pass write, and the ledger's dataset_id, request_id and cause.
DESIGN_NULLABLE = {
    'panel': set(),
    'panel_settings_version': {'asset_class', 'exchange', 'active', 'tradable'},
    'panel_source_settings_version': set(),
    'panel_pass': {'inactive_snapshot_id', 'finished_at', 'counts'},
    'panel_member_transition': {'dataset_id', 'request_id', 'cause'},
}
# NO POLICY DEFAULT (owner ruling 2026-10-27, the 14:16 note for kind): none of these may carry a default of any kind.
POLICY_COLUMNS = {
    'panel_settings_version': {'enabled', 'cadence', 'size'},
    'panel_source_settings_version': {
        'ramp_datasets_per_hour',
        'request_cap_per_hour',
        'corporate_action_window_pre_days',
        'corporate_action_window_post_days',
    },
    'panel_pass': {'kind', 'dry_run'},
}
# The ONLY server defaults: ids, the database-time instants, and the empty symbol lists (no restriction).
SERVER_DEFAULTED = {
    'panel': {'id', 'created_at'},
    'panel_settings_version': {'id', 'saved_at', 'allow_symbols', 'deny_symbols'},
    'panel_source_settings_version': {'id', 'saved_at'},
    'panel_pass': {'id', 'started_at'},
    'panel_member_transition': {'id', 'recorded_at'},
}
# Foreign keys only inside the panel tables (addendum 20; the bead's FILE SCOPE BUILD paragraph).
DESIGN_FOREIGN_KEYS = {
    ('panel_settings_version', 'panel_id', 'panel.id'),
    ('panel_pass', 'panel_id', 'panel.id'),
    ('panel_pass', 'settings_version_id', 'panel_settings_version.id'),
    ('panel_pass', 'source_settings_version_id', 'panel_source_settings_version.id'),
    ('panel_member_transition', 'pass_id', 'panel_pass.id'),
    ('panel_member_transition', 'panel_id', 'panel.id'),
}
# The reads P20c names, and the rate windows (14:16 note): (table, columns, unique).
DESIGN_INDEXES = {
    ('panel', ('name',), True),
    ('panel_pass', ('panel_id', 'started_at'), False),
    ('panel_member_transition', ('panel_id', 'vendor_asset_id', 'as_of'), False),
    ('panel_member_transition', ('transition', 'recorded_at'), False),
    ('ingest_request', ('source', 'created_at'), False),
}
DESIGN_UNIQUES = {
    ('panel_settings_version', 'uq_panel_settings_version_panel_id_version_no', ('panel_id', 'version_no')),
    ('panel_source_settings_version', 'uq_panel_source_settings_version_source_version_no', ('source', 'version_no')),
    # The append ordinal is unique per pass (ruling 2026-10-30 on tj-7zjov1.177): the tie-break of the newest order.
    ('panel_member_transition', 'uq_panel_member_transition_pass_id_seq', ('pass_id', 'seq')),
}
DESIGN_CHECKS = {
    ('panel_settings_version', 'ck_panel_settings_version_version_no_positive', 'version_no >= 1'),
    ('panel_settings_version', 'ck_panel_settings_version_size_non_negative', 'size >= 0'),
    ('panel_settings_version', 'ck_panel_settings_version_actor_not_blank', "btrim(actor) <> ''"),
    ('panel_settings_version', 'ck_panel_settings_version_reason_not_blank', "btrim(reason) <> ''"),
    ('panel_source_settings_version', 'ck_panel_source_settings_version_version_no_positive', 'version_no >= 1'),
    (
        'panel_source_settings_version',
        'ck_panel_source_settings_version_numbers_non_negative',
        'ramp_datasets_per_hour >= 0 AND request_cap_per_hour >= 0 AND corporate_action_window_pre_days >= 0 '
        'AND corporate_action_window_post_days >= 0',
    ),
    ('panel_source_settings_version', 'ck_panel_source_settings_version_actor_not_blank', "btrim(actor) <> ''"),
    ('panel_source_settings_version', 'ck_panel_source_settings_version_reason_not_blank', "btrim(reason) <> ''"),
}
# Tables the ledger must never reference: a ledger row outlives a deleted dataset, request or snapshot.
NEVER_REFERENCED = {'store_dataset_entry', 'ingest_request', 'asset_list_snapshot'}


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


_CONSTRAINT_PREFIXES = ('CONSTRAINT ', 'PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE ', 'CHECK ')


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body at its top-level commas (not inside parentheses or quotes)."""
    parts, depth, quoted, current = [], 0, False, []
    for char in body:
        if char == "'":
            quoted = not quoted
        elif not quoted and char == '(':
            depth += 1
        elif not quoted and char == ')':
            depth -= 1
        if char == ',' and depth == 0 and not quoted:
            parts.append(''.join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append(''.join(current).strip())
    return parts


def _table_ddl(statement: str) -> tuple[str, list[str], list[str]]:
    """A whitespace-collapsed CREATE TABLE as (head, columns in order, constraints sorted).

    Column order is schema (a migrated database holds them in it); the order in which the table-level constraints are
    listed is not (SQLAlchemy lists a model's column-level foreign keys before __table_args__, a revision's in the order
    given), so the constraints compare as a sorted list.
    """
    open_at = statement.index('(')
    head, body = statement[:open_at].strip(), statement[open_at + 1 : statement.rindex(')')]
    parts = _split_top_level(body)
    columns = [part for part in parts if not part.startswith(_CONSTRAINT_PREFIXES)]
    constraints = sorted(part for part in parts if part.startswith(_CONSTRAINT_PREFIXES))
    return head, columns, constraints


def _created_tables(op: MagicMock) -> dict[str, sa.Table]:
    """The created tables in ONE metadata, so the foreign keys between them resolve when compiled."""
    metadata = sa.MetaData()
    return {
        args[0]: sa.Table(args[0], metadata, *args[1:]) for name, args, _ in op.method_calls if name == 'create_table'
    }


def _both(table_name: str) -> list[sa.Table]:
    """The revision's table and the model's, so each design property is asserted of both."""
    op, _, _ = _run('upgrade')
    model = next(model for model in MODELS if table_name == model.TABLE_NAME)
    return [_created_tables(op)[table_name], model.__table__]


def _fk_triples(table: sa.Table) -> set[tuple[str, str, str]]:
    return {(table.name, fk.parent.name, fk.target_fullname) for fk in table.foreign_keys}


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_single_head_directly_on_the_head_it_was_written_on_and_is_unmarked():
    """Checklist items 1 to 3: one new revision, down_revision the previous head, one head, no exception marker."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [REVISION]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == PARENT] == [REVISION]
    assert (module.branch_labels, module.depends_on) == (None, None)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_revision_runs_in_the_one_migration_transaction_with_no_raw_sql_and_seeds_nothing():
    """Checklist items 9, 11 and 12; and the bead: NO panel and NO settings row is seeded by the revision."""
    text = REVISION_FILE.read_text(encoding='utf-8')
    assert 'autocommit_block' not in text and 'CONCURRENTLY' not in text.upper()
    up, _, _ = _run('upgrade')
    down, _, _ = _run('downgrade')
    assert not {'execute', 'bulk_insert'} & set(_calls(up) + _calls(down)), 'the revision runs raw SQL or seeds rows'


def test_the_migration_env_loads_the_model_module_so_autogenerate_sees_the_five_tables():
    """Checklist items 13 and 14: the tables are a model module's, so env.py's ALLOWED_MODELS must name it."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    assert Path(sys.modules[Panel.__module__].__file__).stem in allowed, allowed
    assert {model.__module__ for model in MODELS} == {Panel.__module__}, 'one module holds all five models'


# --------------------------------------------------------------------------------------------- the op sequence


def test_upgrade_creates_three_types_the_column_and_request_index_then_five_tables_and_their_indexes_only():
    """PURELY ADDITIVE (checklist 4 to 9): no drop, rename, retype, alter, constraint on an existing table or backfill."""
    op, created, dropped = _run('upgrade')

    assert _calls(op) == [
        'add_column',
        'create_index',
        'create_table',
        'create_index',
        'create_table',
        'create_table',
        'create_table',
        'create_index',
        'create_index',
        'create_index',
        'create_table',
        'create_index',
        'create_index',
        'create_index',
    ]
    assert [args[0] for name, args, _ in op.method_calls if name == 'create_table'] == TABLE_NAMES
    assert [enum.name for enum in created] == list(TYPES)
    assert dropped == []
    for table in _created_tables(op).values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, f'{table.name}.{column.name} would CREATE its type again'


def test_each_new_type_carries_the_designs_members_and_the_models_in_order():
    """Checklist item 14: the labels compared by hand with the Python enums (alembic check does not compare them)."""
    _, created, _ = _run('upgrade')
    module = _load()
    by_name = {enum.name: list(enum.enums) for enum in created}

    assert by_name == TYPES
    assert list(module.CADENCE_MEMBERS) == TYPES['panel_cadence']
    assert list(module.TRANSITION_MEMBERS) == TYPES['panel_transition']
    assert list(module.RUN_KIND_MEMBERS) == TYPES['panel_pass_kind']
    for name, enum_cls in MODEL_ENUMS.items():
        assert [member.value for member in enum_cls] == TYPES[name], name


def test_every_enum_column_names_its_type_with_the_same_labels_in_revision_and_model():
    """CreateTable renders an enum column by type NAME only, so every enum column's labels are compared here."""
    op, _, _ = _run('upgrade')
    tables = _created_tables(op)
    compared = 0
    for model in MODELS:
        for column in tables[model.TABLE_NAME].columns:
            if isinstance(column.type, sa.Enum):
                model_type = model.__table__.c[column.name].type
                assert (column.type.name, list(column.type.enums)) == (model_type.name, list(model_type.enums)), (
                    f'{model.TABLE_NAME}.{column.name}'
                )
                compared += 1
    assert compared == 6, 'panel.source, cadence, asset_class, source settings source, pass kind, transition'


def test_the_existing_types_are_referenced_never_created():
    """Datasource and assettype belong to earlier revisions: create_type False, and neither is created nor dropped."""
    op, created, _ = _run('upgrade')
    _, _, dropped = _run('downgrade')
    tables = _created_tables(op)
    existing = [
        tables['panel'].c.source.type,
        tables['panel_source_settings_version'].c.source.type,
        tables['panel_settings_version'].c.asset_class.type,
    ]
    assert {t.name for t in existing} == {'datasource', 'assettype'}
    assert all(t.create_type is False for t in existing)
    assert not {'datasource', 'assettype'} & ({enum.name for enum in created} | set(dropped))


def test_new_value_is_a_nullable_text_column_with_no_default_and_the_models_ddl():
    """Addendum 20 parked default: nullable new_value (Text); no backfill, so every earlier row keeps NULL."""
    op, _, _ = _run('upgrade')
    ((table, column),) = [(args[0], args[1]) for name, args, _ in op.method_calls if name == 'add_column']

    assert (table, column.name) == (OVERRIDE.name, 'new_value')
    assert column.nullable and column.server_default is None and column.default is None
    assert isinstance(column.type, sa.Text)
    assert _compiled(CreateColumn(column)) == _compiled(CreateColumn(OVERRIDE.c.new_value))


def test_the_request_rate_index_is_the_designs_and_the_models():
    """The 14:16 note: ix_ingest_request_source_created_at on (source, created_at), plain, additive."""
    op, _, _ = _run('upgrade')
    ((args, kwargs),) = [
        (args, kwargs) for name, args, kwargs in op.method_calls if name == 'create_index' and args[1] == REQUEST.name
    ]
    assert (args[0], list(args[2])) == (REQUEST_RATE_INDEX, ['source', 'created_at'])
    assert not kwargs.get('unique') and 'postgresql_where' not in kwargs
    (model_index,) = [index for index in REQUEST.indexes if index.name == REQUEST_RATE_INDEX]
    built = SchemaObjects().index(args[0], args[1], args[2], **kwargs)
    assert _compiled(CreateIndex(built)) == _compiled(CreateIndex(model_index))
    assert IngestRequest.SOURCE_CREATED_INDEX == REQUEST_RATE_INDEX


# --------------------------------------------------------------------------------------------- downgrade


def test_downgrade_drops_exactly_what_upgrade_created_in_reverse_and_nothing_else():
    """Checklist item 17: tables reversed, every index, the request index and new_value, then the types last."""
    up, created, _ = _run('upgrade')
    down, _, dropped = _run('downgrade')

    calls = _calls(down)
    assert set(calls) == {'drop_index', 'drop_table', 'drop_column'}
    created_tables = [args[0] for name, args, _ in up.method_calls if name == 'create_table']
    assert [args[0] for name, args, _ in down.method_calls if name == 'drop_table'] == list(reversed(created_tables))
    created_indexes = Counter((args[1], args[0]) for name, args, _ in up.method_calls if name == 'create_index')
    dropped_indexes = Counter(
        (kwargs['table_name'], args[0]) for name, args, kwargs in down.method_calls if name == 'drop_index'
    )
    assert dropped_indexes == created_indexes
    assert [args for name, args, _ in down.method_calls if name == 'drop_column'] == [(OVERRIDE.name, 'new_value')]
    assert dropped == list(reversed([enum.name for enum in created])), 'the types go after everything that uses them'
    # Each table's indexes go before the table itself.
    for table in created_tables:
        own = [
            i
            for i, (name, args, kwargs) in enumerate(down.method_calls)
            if name == 'drop_index' and kwargs['table_name'] == table
        ]
        (at,) = [i for i, (name, args, _) in enumerate(down.method_calls) if name == 'drop_table' and args[0] == table]
        assert all(i < at for i in own), table


def test_the_types_are_dropped_after_every_op_of_the_downgrade():
    """The bind drops are not op calls: the order is read off the module's own downgrade text."""
    tree = ast.parse(REVISION_FILE.read_text(encoding='utf-8'))
    (node,) = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'downgrade']
    source = ast.get_source_segment(REVISION_FILE.read_text(encoding='utf-8'), node)
    last_op = max(source.rindex('op.drop_column'), source.rindex('op.drop_index'), source.rindex('op.drop_table'))
    assert last_op < source.index('.drop(op.get_bind()')


# --------------------------------------------------------------------------------------------- revision against model


@pytest.mark.parametrize('model', MODELS, ids=lambda model: model.TABLE_NAME)
def test_the_revision_builds_exactly_the_table_the_model_declares(model):
    """Every column, type, nullability, server default, constraint and foreign key, as Postgres DDL."""
    op, _, _ = _run('upgrade')
    built = _table_ddl(_compiled(CreateTable(_created_tables(op)[model.TABLE_NAME])))
    declared = _table_ddl(_compiled(CreateTable(model.__table__)))

    assert built == declared
    assert len(built[1]) == len(DESIGN_COLUMNS[model.TABLE_NAME]), 'the split saw every column'


def test_the_revision_builds_exactly_the_indexes_the_models_declare():
    """The five panel tables' indexes (panel 1, pass 3, transition 3) plus the request-rate index: eight in all."""
    op, _, _ = _run('upgrade')
    objects = SchemaObjects()
    built = {
        _compiled(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }
    declared = {_compiled(CreateIndex(index)) for model in MODELS for index in model.__table__.indexes}
    declared |= {_compiled(CreateIndex(index)) for index in REQUEST.indexes if index.name == REQUEST_RATE_INDEX}

    assert built == declared
    assert len(built) == 8


@pytest.mark.parametrize('table_name', TABLE_NAMES)
def test_each_table_has_the_designs_columns_and_nullability(table_name):
    """The ruled names: ramp and per_pass_cap are gone, renamed in place to the hourly rates (14:16 note)."""
    for table in _both(table_name):
        assert set(table.c.keys()) == DESIGN_COLUMNS[table_name], table
        assert {c.name for c in table.columns if c.nullable} == DESIGN_NULLABLE[table_name], table
        assert not {'ramp', 'per_pass_cap'} & set(table.c.keys())


@pytest.mark.parametrize('table_name', TABLE_NAMES)
def test_no_policy_default_and_only_the_designs_server_defaults(table_name):
    """Owner ruling 2026-10-27: the schema carries no default for cadence, enabled, size, either rate or the window.

    Both sides: the revision's columns and the model's, server default and client default alike. The only server
    defaults anywhere are the ids, the database-time instants and the empty symbol lists.
    """
    for table in _both(table_name):
        for name in POLICY_COLUMNS.get(table_name, set()):
            column = table.c[name]
            assert column.server_default is None and column.default is None, f'{table.name}.{name} has a default'
            assert not column.nullable, f'{table.name}.{name} is nullable, so NULL would be its default'
        defaulted = {c.name for c in table.columns if c.server_default is not None}
        assert defaulted == SERVER_DEFAULTED[table_name], table
        assert {c.name for c in table.columns if c.default is not None} == set(), table


def test_the_recorded_at_and_saved_at_instants_are_database_time_and_as_of_is_not():
    """recorded_at is wall (database) time, NOT NULL default now(); as_of is the snapshot instant, written by the pass."""
    for table in _both('panel_member_transition'):
        assert ' '.join(str(table.c.recorded_at.server_default.arg).split()) in {'now()'}
        assert not table.c.recorded_at.nullable
        assert table.c.as_of.server_default is None and not table.c.as_of.nullable
        assert isinstance(table.c.recorded_at.type, sa.DateTime) and table.c.recorded_at.type.timezone
    for name in ('panel_settings_version', 'panel_source_settings_version'):
        for table in _both(name):
            assert str(table.c.saved_at.server_default.arg) == 'now()'


def test_seq_is_an_app_assigned_not_null_integer_unique_per_pass_in_revision_and_model():
    """The ruling of 2026-10-30 on tj-7zjov1.177: seq INTEGER NOT NULL, the 1-based append ordinal within its pass.

    NOT an identity or a sequence, and no default of any kind: append_transitions assigns it (the store unit tier builds
    the schema with create_all on SQLite, which generates no value for a non-PK identity). Unique with pass_id under the
    model's APPEND_ORDINAL_UNIQUE name.
    """
    assert PanelMemberTransition.APPEND_ORDINAL_UNIQUE == 'uq_panel_member_transition_pass_id_seq'
    for table in _both('panel_member_transition'):
        seq = table.c.seq
        assert type(seq.type) is sa.Integer, seq.type
        assert not seq.nullable and seq.server_default is None and seq.default is None
        assert seq.identity is None and seq.computed is None, 'seq is app-assigned, never generated'
        assert not seq.primary_key
        (unique,) = [
            c
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and 'seq' in {col.name for col in c.columns}
        ]
        assert (unique.name, [col.name for col in unique.columns]) == (
            PanelMemberTransition.APPEND_ORDINAL_UNIQUE,
            ['pass_id', 'seq'],
        )


def test_the_foreign_keys_run_only_inside_the_panel_tables_and_never_cascade():
    """No foreign key from the ledger to store_dataset_entry, ingest_request or asset_list_snapshot (addendum 20).

    And none deletes in cascade: the tables are append-only, and a cascade would be a delete path.
    """
    op, _, _ = _run('upgrade')
    tables = _created_tables(op)
    for source in (tables.values(), [model.__table__ for model in MODELS]):
        triples = set().union(*(_fk_triples(table) for table in source))
        assert triples == DESIGN_FOREIGN_KEYS
        for table in source:
            for fk in table.foreign_keys:
                assert fk.ondelete is None and fk.onupdate is None, f'{table.name}.{fk.parent.name}'
                assert fk.column.table.name not in NEVER_REFERENCED


def test_the_design_uniques_indexes_and_checks_hold_in_revision_and_model():
    """Uniqueness: panel name; version_no per panel and per source. The reads' indexes. The CHECKs, by name and text."""
    op, _, _ = _run('upgrade')
    created = _created_tables(op)
    for tables in (created, {model.TABLE_NAME: model.__table__ for model in MODELS}):
        uniques = {
            (name, c.name, tuple(col.name for col in c.columns))
            for name, table in tables.items()
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        assert uniques == DESIGN_UNIQUES
        checks = {
            (name, c.name, ' '.join(str(c.sqltext).split()))
            for name, table in tables.items()
            for c in table.constraints
            if isinstance(c, sa.CheckConstraint)
        }
        assert checks == DESIGN_CHECKS
    built = {
        (args[1], tuple(args[2]), bool(kwargs.get('unique')))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }
    assert built >= DESIGN_INDEXES
    declared = {
        (index.table.name, tuple(c.name for c in index.columns), bool(index.unique))
        for model in MODELS
        for index in model.__table__.indexes
    }
    assert DESIGN_INDEXES - {('ingest_request', ('source', 'created_at'), False)} <= declared


# --------------------------------------------------------------------------------------------- append-only


# The production modules that import the panel models, EXACTLY: an exact set, never a 'contains', so a new writer is a
# decision its validator records here. One line per crud bead; P20cv (tj-7zjov1.179) adds P20c's ledger module.
PANEL_MODEL_IMPORTERS = frozenset(
    {
        # One line per module, each naming the bead that added it and the bead that pins its statements.
        'data/store/app/database/crud/panel_settings.py',  # P20b tj-7zjov1.176, pinned by P20bv tj-7zjov1.178
        'data/store/app/database/crud/panel_ledger.py',  # P20c tj-7zjov1.177, pinned by P20cv tj-7zjov1.179
        # P22 tj-7zjov1.34, judged by P20d-v tj-7zjov1.217 at 4cb30ec: THE production writer of the ledger, as this
        # pin anticipated. It imports the model's enums only (PanelCadence, PanelPassKind, PanelTransition), builds no
        # statement of its own, and writes the panel tables solely through crud/panel_ledger.py (open_pass,
        # append_transitions, finish_pass). P22v tj-7zjov1.180 pins the pass's rows.
        'data/store/app/panel_manager.py',
        # P22 tj-7zjov1.34, judged by P20d-v tj-7zjov1.217 at 4cb30ec: the manager's reads. It only SELECTs from
        # panel_pass and panel_member_transition; its two writes, stop_collecting and (P22r tj-7zjov1.264)
        # resume_panel_entry, are UPDATEs of store_dataset_entry, not a panel table (the guarded-update pin below
        # still finds finish_pass alone). P22v and P22rv (tj-7zjov1.265) pin its statements.
        'data/store/app/database/crud/panel_manager_reads.py',
        # P24 tj-7zjov1.36 (c716268), judged by P24v tj-7zjov1.241: the /ui/v1 reads. Every statement is a select()
        # (no insert, update, delete, text() or commit anywhere in the module); pinned SELECT-only by
        # test_panel_ui_reads.py, which also counts its statements per request.
        'data/store/app/database/crud/panel_reads.py',
        # P24 (c716268): composition only. It imports PanelTransition (an enum) and calls panel_reads and the ledger's
        # own reads (latest_state, boundary_read); it builds no statement and never commits.
        'data/store/app/panel_views.py',
        # P24 (c7388f4): record-to-message mapping. It imports the enums PanelCadence, PanelPassKind and PanelTransition
        # for the proto mirrors and touches no session; the routes write only through crud/panel_settings.py.
        'routers/data_store/ui_mapping.py',
    }
)


def test_no_production_module_writes_the_panel_tables_yet():
    """APPEND-ONLY: only the listed crud modules import the model, and no raw SQL mutates the tables.

    P20 added no crud. P20b (tj-7zjov1.176) added crud/panel_settings.py, whose own statements are pinned SELECT or
    INSERT only by test_panel_settings.py. P20c (tj-7zjov1.177) added crud/panel_ledger.py, whose statements are pinned
    SELECT or INSERT plus THE ONE RULED EXCEPTION by test_panel_ledger.py: finish_pass's UPDATE of panel_pass, which
    sets finished_at and counts only, guarded by finished_at IS NULL (pinned below as well). P22 (tj-7zjov1.34) added
    the manager, which writes only through that ledger module, and its reads module, which only reads the panel tables.
    The seed tooling and the migrations are excluded. The router of P20b does not import the model (it imports
    PANEL_OWNER).
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?panel(_[a-z_]+)?\b', re.I)
    importing_the_model = set()
    for path in SERVER_ROOT.rglob('*.py'):
        parts = path.relative_to(SERVER_ROOT).parts
        if 'tests' in parts or 'migrations' in parts or 'seeds' in parts or '.venv' in parts:
            continue
        text = path.read_text(encoding='utf-8')
        assert not raw.search(text), f'{path} updates or deletes a panel table in raw SQL'
        if re.search(r'store\.app\.database\.models(\.panel\b| import .*\bpanel\b)', text):
            importing_the_model.add(path.relative_to(SERVER_ROOT).as_posix())
    assert importing_the_model == PANEL_MODEL_IMPORTERS


def test_the_one_update_of_a_panel_table_is_the_guarded_finish_pass():
    """The finish-pass exception, read from the source of every importer.

    One .update() in all of them, on panel_pass, in finish_pass, whose WHERE carries finished_at IS NULL and whose
    values are finished_at and counts alone; no .delete() anywhere.
    """
    found = []
    for relative in sorted(PANEL_MODEL_IMPORTERS):
        tree = ast.parse((SERVER_ROOT / relative).read_text(encoding='utf-8'))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and node.attr in {'update', 'delete'}:
                    found.append((relative, function.name, node.attr, ast.unparse(node.value)))
                    finish = function
    assert found == [('data/store/app/database/crud/panel_ledger.py', 'finish_pass', 'update', '_PASS')]
    text = ast.unparse(finish)
    assert '_PASS.c.finished_at.is_(None)' in text
    (values,) = [n for n in ast.walk(finish) if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'values']
    assert sorted(keyword.arg for keyword in values.keywords) == ['counts', 'finished_at']


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


def test_the_offline_upgrade_sql_is_the_types_the_column_the_index_and_the_models_tables(upgrade_sql):
    """Statement by statement against the models compiled for Postgres; index order within a table compared sorted."""
    statements = _bookkeeping(upgrade_sql)
    (request_index,) = [index for index in REQUEST.indexes if index.name == REQUEST_RATE_INDEX]
    head = [
        *[f'CREATE TYPE {name} AS ENUM ({", ".join(repr(m) for m in members)})' for name, members in TYPES.items()],
        f'ALTER TABLE {OVERRIDE.name} ADD COLUMN {_compiled(CreateColumn(OVERRIDE.c.new_value))}',
        _compiled(CreateIndex(request_index)),
    ]
    tables: list[Any] = []
    for model in MODELS:
        table = model.__table__
        tables += [
            _table_ddl(_compiled(CreateTable(table))),
            *sorted(_compiled(CreateIndex(index)) for index in table.indexes),
        ]
    body: list[Any] = statements[len(head) :]
    at = 0
    for model in MODELS:
        count = 1 + len(model.__table__.indexes)
        body[at] = _table_ddl(body[at])
        body[at + 1 : at + count] = sorted(body[at + 1 : at + count])
        at += count

    assert statements[: len(head)] == head
    assert body == tables


def test_the_offline_downgrade_sql_drops_the_tables_the_index_the_column_then_the_types(downgrade_sql):
    statements = _bookkeeping(downgrade_sql)

    assert statements[-5:] == [
        f'DROP INDEX {REQUEST_RATE_INDEX}',
        f'ALTER TABLE {OVERRIDE.name} DROP COLUMN new_value',
        *[f'DROP TYPE {name}' for name in reversed(TYPES)],
    ]
    assert [s for s in statements if s.startswith('DROP TABLE')] == [f'DROP TABLE {t}' for t in reversed(TABLE_NAMES)]
    assert all(s.startswith(('DROP INDEX', 'DROP TABLE')) for s in statements[:-5])
