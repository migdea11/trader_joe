"""The override-log revision, d7e2a9c35b18, and the two audit tables' design shape, without a database.

The design is tj-grna9p.86 and its architect notes of 2026-10-13/14: ONE additive revision on b3d9e1a47c62 holding
dataset_override_log (actor, dataset_id, owner, action, reason, at) and the deletion record dataset_deletion that
the reaper (tj-grna9p.23) reuses; both APPEND-ONLY with no UPDATE or DELETE path; NO foreign key (a row outlives
the dataset a delete removed); owner a SensitiveString in the model (String in DDL), never in a repr; downgrade
drops both tables and the enum type.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb), as in test_dataset_read_revision.py: the revision's DDL is compared
with the MODEL's DDL compiled for Postgres, and the downgrade with the upgrade. That it applies, downgrades and
re-applies against a real Postgres is tests/system/test_http_ui_dataset_writes.py's and the migrate verbs'.
"""

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from common.database.sql_alchemy_sensitive_string import SensitiveString
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction


pytestmark = pytest.mark.data_store

STORE_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = STORE_DIR / 'migrations'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / 'd7e2a9c35b18_dataset_override_log_and_deletion.py'
DIALECT = postgresql.dialect()
NEW_TYPES = ['override_action']
TABLES = {
    DatasetOverrideLog.TABLE_NAME: DatasetOverrideLog.__table__,
    DatasetDeletion.TABLE_NAME: DatasetDeletion.__table__,
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location('revision_d7e2a9c35b18', REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str) -> tuple[MagicMock, list[str], list[str]]:
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


def _revision_tables(op: MagicMock) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        args[0]: sa.Table(args[0], metadata, *args[1:]) for name, args, _ in op.method_calls if name == 'create_table'
    }


def test_the_revision_sits_on_dataset_read_and_carries_no_additive_exception_marker():
    module = _load()
    assert (module.revision, module.down_revision) == ('d7e2a9c35b18', 'b3d9e1a47c62')
    assert 'additive-exception' not in REVISION_FILE.read_text(encoding='utf-8').lower()


def test_upgrade_only_creates_one_type_two_tables_and_three_indexes():
    """Purely additive: nothing is altered, dropped, executed as raw SQL or backfilled."""
    op, created, dropped = _run('upgrade')

    assert _schema_calls(op) == ['create_table', 'create_index', 'create_table', 'create_index', 'create_index']
    assert created == NEW_TYPES
    assert dropped == []
    for table in _revision_tables(op).values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, f'{table.name}.{column.name} would CREATE its type again'


def test_downgrade_drops_exactly_what_upgrade_created_in_reverse():
    up, created, _ = _run('upgrade')
    down, _, dropped = _run('downgrade')

    created_indexes = [args[0] for name, args, _ in up.method_calls if name == 'create_index']
    dropped_indexes = [args[0] for name, args, _ in down.method_calls if name == 'drop_index']
    assert sorted(dropped_indexes) == sorted(created_indexes)
    created_tables = [args[0] for name, args, _ in up.method_calls if name == 'create_table']
    assert [args[0] for name, args, _ in down.method_calls if name == 'drop_table'] == list(reversed(created_tables))
    assert dropped == list(reversed(created))
    assert {name for name, _, _ in down.method_calls} <= {'drop_index', 'drop_table', 'get_bind'}


def test_the_revision_builds_exactly_the_tables_the_models_declare():
    """Every column, type, nullability, server default and constraint, as Postgres DDL, model against revision."""
    op, _, _ = _run('upgrade')
    tables = _revision_tables(op)
    assert set(tables) == set(TABLES)
    for name, model_table in TABLES.items():
        assert _ddl_of(tables[name]) == _ddl_of(model_table), name


def test_the_revision_builds_exactly_the_indexes_the_models_declare():
    op, _, _ = _run('upgrade')
    objects = SchemaObjects()
    built = {
        str(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)).compile(dialect=DIALECT))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }
    declared = {
        str(CreateIndex(index).compile(dialect=DIALECT)) for table in TABLES.values() for index in table.indexes
    }
    assert built == declared
    assert len(declared) == 3


def test_the_action_type_carries_the_models_members_it_was_created_with_in_order():
    """d7e2a9c35b18 created the type with the two actions of its day; history does not change (tj-grna9p.183)."""
    assert list(_load().ACTION_MEMBERS) == ['SET_EXPIRY', 'DELETE']
    assert set(_load().ACTION_MEMBERS) < {member.value for member in OverrideAction}


def test_the_revision_chain_gives_the_native_type_every_model_member_exactly_once():
    """The labels at the head: d7e2a9c35b18's, then each ADD VALUE after it, are exactly the model's values.

    The native order is creation order (ADD VALUE appends): SET_EXPIRY, DELETE, SET_COLLECTING. The model declares
    SET_COLLECTING between the two; nothing sorts or compares the action, so only membership is pinned here, and the
    ordering difference is reported to the architect rather than made a contract.
    """
    path = MIGRATIONS_DIR / 'versions' / 'a6d3f8e1c205_override_action_set_collecting.py'
    spec = importlib.util.spec_from_file_location('revision_a6d3f8e1c205', path)
    later = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(later)
    native = [*_load().ACTION_MEMBERS, later.NEW_MEMBER]
    assert native == ['SET_EXPIRY', 'DELETE', 'SET_COLLECTING']
    assert sorted(native) == sorted(member.value for member in OverrideAction)
    assert len(set(native)) == len(native)


def test_both_tables_are_in_allowed_models_so_autogenerate_sees_them():
    """env.py imports only ALLOWED_MODELS; without these two, alembic check would want to drop both tables."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    assert {DatasetOverrideLog.TABLE_NAME, DatasetDeletion.TABLE_NAME} <= allowed


# ---------------------------------------------------------------------------------------------
# The design shape


def test_the_override_log_has_the_designed_columns_all_not_null_and_no_foreign_key():
    table = DatasetOverrideLog.__table__
    assert set(table.c.keys()) == {'id', 'actor', 'dataset_id', 'owner', 'action', 'reason', 'at'}
    assert all(not column.nullable for column in table.columns), [c.name for c in table.columns if c.nullable]
    assert not table.foreign_keys
    assert isinstance(table.c.owner.type, SensitiveString)


def test_the_deletion_record_has_no_foreign_key_and_a_sensitive_owner():
    table = DatasetDeletion.__table__
    assert not table.foreign_keys
    assert isinstance(table.c.owner.type, SensitiveString)
    # range_end is the one nullable column: NULL for an open-ended dataset (the entry stores the EPOCH sentinel).
    assert [column.name for column in table.columns if column.nullable] == ['range_end']


@pytest.mark.parametrize(
    'row',
    [
        DatasetOverrideLog(owner='distinctive-owner-7731', action=OverrideAction.DELETE, reason='r'),
        DatasetDeletion(owner='distinctive-owner-7731', asset_symbol='AAA', cause=DatasetDeletion.CAUSE_OPERATOR),
    ],
    ids=['override-log', 'deletion'],
)
def test_neither_repr_carries_the_owner(row):
    assert 'distinctive-owner-7731' not in repr(row)


# ---------------------------------------------------------------------------------------------
# APPEND-ONLY: no UPDATE or DELETE path against either table anywhere in the service


_MUTATING = re.compile(r'\b(update|delete)\s*\(\s*(DatasetOverrideLog|DatasetDeletion|_OVERRIDE|_DELETION)\b')
_METHOD = re.compile(
    r'\b(DatasetOverrideLog|DatasetDeletion|_OVERRIDE|_DELETION)(\.__table__)?\s*\.\s*(update|delete)\s*\('
)
_RAW = re.compile(r'(UPDATE|DELETE\s+FROM)\s+(dataset_override_log|dataset_deletion)\b', re.IGNORECASE)


def _sources() -> list[Path]:
    roots = [STORE_DIR / 'app', STORE_DIR.parents[1] / 'routers' / 'data_store']
    return [path for root in roots for path in root.rglob('*.py')]


def test_nothing_in_the_service_updates_or_deletes_an_audit_row():
    offenders = [
        f'{path}: {match.group(0)}'
        for path in _sources()
        for pattern in (_MUTATING, _METHOD, _RAW)
        for match in pattern.finditer(path.read_text(encoding='utf-8'))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    'line',
    [
        'stmt = delete(DatasetOverrideLog).where(x)',
        'await db.execute(_DELETION.update().values(cause="x"))',
        "text('DELETE FROM dataset_override_log')",
        'update(DatasetDeletion)',
    ],
)
def test_the_append_only_scan_would_see_a_mutation(line):
    """The scan above is only evidence if it reds on the shapes a mutation would take."""
    assert any(pattern.search(line) for pattern in (_MUTATING, _METHOD, _RAW))
