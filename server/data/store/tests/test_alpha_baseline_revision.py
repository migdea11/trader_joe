"""The alpha baseline revision, offline: one file, no parent, no marker, no downgrade, and it builds the models.

Design checked against: ADR tj-kg521d (owner rulings 2026-11-06 in tj-46jkey's notes): ONE initial revision with
down_revision None replaces every earlier one; its downgrade() raises naming the record; its enum labels are LITERALS in
the file; no additive-exception marker; it creates the final schema of the models plus tj-46jkey's rulings (no panel
size, both reason columns nullable with their CHECKs kept) and tj-1ov1p2's (the bar counts BIGINT); the archive table
and the redundant UNIQUE on store_dataset_entry.id are not carried. The record's gate items this file answers offline:
B1 (one revision, one head, down_revision None, a fresh id, no marker) and B2 (downgrade raises). B3 (the migration lint
passes it unmarked) is tools/migration_lint/tests/test_migration_lint.py's, where the lint is a declared import.

OFFLINE AGREEMENT WITH THE MODELS (the alembic-check part that needs no database). upgrade() is run against a recording
op (alpha_baseline.py) and the schema it describes is rebuilt as a MetaData, then compared with the models' metadata,
table by table: every column's compiled type, nullability, server default and primary key; every unique constraint,
CHECK (by name and text) and foreign key (columns, target, ON DELETE); every index as Postgres DDL (columns or
expressions, uniqueness, predicate); and every enum type's labels IN ORDER. alembic check compares less than this
(it is blind to enum labels, CHECKs and server defaults). It still compares SQLAlchemy objects, not what Postgres
builds, so the authoritative check is the catalogue test on Postgres, tests/system/test_alpha_baseline_schema_pg.py
(B5), and `alembic check` on a migrated stack (B6), both outside the PR gate.
"""

import ast
import re
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from data.store.app.database.models.panel import PanelSettingsVersion, PanelSourceSettingsVersion
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.tests.alpha_baseline import (
    BASELINE_FILE,
    BASELINE_REVISION,
    DECISION,
    MIGRATIONS_DIR,
    VERSIONS_DIR,
    load_baseline,
    model_metadata,
    record_upgrade,
)


DIALECT = postgresql.dialect()
MARKER = re.compile(r'^\s*# additive-exception:', re.MULTILINE)
# Every revision id the old chain used (ADR tj-kg521d section 1). The baseline's must be none of them, so a database
# stamped with an old id fails loudly at make migrate instead of being taken for the baseline.
OLD_CHAIN_IDS = frozenset(
    {
        '2b88043cd13c', '8f41c2d7a3b9', 'eec8f88a7443', 'c4a1f7b2e905', '6f5a798746a1', 'b3d9e1a47c62',
        'd7e2a9c35b18', 'e5c8b1d4f703', 'a6d3f8e1c205', 'f2b7d4a91c36', 'b8e4c1f7a923', '39c28a39b290',
        'c7e19a4d5b28', 'a3f8d2c6e914', 'b6d1f93a7c42', 'd4b7e1a93c58',
    }
)  # fmt: skip


@pytest.fixture(scope='module')
def baseline():
    return load_baseline()


@pytest.fixture(scope='module')
def recorded(baseline):
    return record_upgrade(baseline)


@pytest.fixture(scope='module')
def models() -> sa.MetaData:
    return model_metadata()


# --------------------------------------------------------------------------------------------- B1: the one revision


def test_the_versions_directory_holds_exactly_the_one_baseline_file():
    files = sorted(path.name for path in VERSIONS_DIR.glob('*.py') if path.name != '__init__.py')
    assert files == [BASELINE_FILE.name], files


def test_the_baseline_has_no_parent_no_branch_and_no_dependency(baseline):
    assert baseline.revision == BASELINE_REVISION
    assert baseline.down_revision is None
    assert baseline.branch_labels is None and baseline.depends_on is None


def test_the_graph_has_one_head_and_one_base_and_both_are_the_baseline():
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert list(script.get_heads()) == [BASELINE_REVISION]
    assert list(script.get_bases()) == [BASELINE_REVISION]
    assert [revision.revision for revision in script.walk_revisions()] == [BASELINE_REVISION]


def test_the_baseline_id_is_fresh_never_an_old_chain_id(baseline):
    assert baseline.revision not in OLD_CHAIN_IDS
    assert re.fullmatch(r'[0-9a-f]{12}', baseline.revision), baseline.revision


def test_the_baseline_carries_no_additive_exception_marker():
    assert not MARKER.search(BASELINE_FILE.read_text(encoding='utf-8'))


# --------------------------------------------------------------------------------------------- B2: no downgrade


def test_downgrade_raises_naming_the_decision_and_touches_nothing(baseline):
    """No downgrade anywhere (owner ruling): it raises before any op call, and says how to recover."""
    recorder = MagicMock(name='op')
    with patch.object(baseline, 'op', recorder), pytest.raises(RuntimeError) as raised:
        baseline.downgrade()
    assert DECISION in str(raised.value)
    assert 'make migrate' in str(raised.value)
    assert recorder.method_calls == [], 'downgrade() did something before it raised'


# --------------------------------------------------------------------------------------------- self-contained


def _module_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or '')
    return names


def test_the_baseline_imports_nothing_from_the_code_it_migrates():
    """Self-contained (section 4): alembic, sqlalchemy and typing only, so the schema cannot move with the image."""
    imports = _module_imports(ast.parse(BASELINE_FILE.read_text(encoding='utf-8')))
    assert imports <= {'typing', 'alembic', 'sqlalchemy', 'sqlalchemy.dialects'}, sorted(imports)


def test_the_enum_labels_are_literals_in_the_file():
    """ENUM_LABELS is a dict literal of tuples of string literals: nothing is read from imported code."""
    tree = ast.parse(BASELINE_FILE.read_text(encoding='utf-8'))
    (node,) = [n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'ENUM_LABELS']
    assert isinstance(node.value, ast.Dict)
    for key, value in zip(node.value.keys, node.value.values, strict=True):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), ast.unparse(key)
        assert isinstance(value, ast.Tuple) and value.elts, ast.unparse(value)
        assert all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts), ast.unparse(value)


def test_upgrade_creates_every_enum_type_once_before_any_table(baseline, recorded):
    assert recorded.created_enums == list(baseline.ENUM_LABELS)
    assert len(set(recorded.created_enums)) == len(recorded.created_enums)
    # Each table column references its type and never creates it (create_type=False), so no CREATE TYPE repeats.
    for table in recorded.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, f'{table.name}.{column.name}'


def test_upgrade_runs_only_create_calls_and_no_raw_sql(recorded):
    """Create-only: tables and indexes, no execute, no alter, no drop, no data."""
    assert set(recorded.calls) == {'get_bind', 'create_table', 'create_index'}, sorted(set(recorded.calls))


# --------------------------------------------------------------------------------------------- the rulings it carries


def test_the_archive_table_of_the_old_chain_is_not_created(recorded, models):
    assert set(recorded.metadata.tables) == set(models.tables)
    assert not [name for name in recorded.metadata.tables if 'superseded' in name]


def test_the_bar_counts_are_bigint(recorded):
    bar = recorded.metadata.tables[StockMarketActivity.TABLE_NAME]
    for name in ('volume', 'trade_count'):
        assert isinstance(bar.c[name].type, sa.BigInteger) and not bar.c[name].nullable, name


def test_the_panel_version_has_no_size_and_no_size_check(recorded):
    version = recorded.metadata.tables[PanelSettingsVersion.__tablename__]
    assert 'size' not in version.c
    assert not [c for c in version.constraints if 'size' in (c.name or '')]


@pytest.mark.parametrize('model', [PanelSettingsVersion, PanelSourceSettingsVersion], ids=lambda m: m.__tablename__)
def test_both_reason_columns_are_nullable_and_keep_their_not_blank_check(recorded, model):
    table = recorded.metadata.tables[model.__tablename__]
    assert table.c.reason.nullable is True
    (check,) = [c for c in table.constraints if c.name == model.REASON_NOT_BLANK]
    assert str(check.sqltext) == "btrim(reason) <> ''"


def test_the_entry_id_has_no_unique_beside_its_primary_key(recorded):
    """The redundant single-column UNIQUE on store_dataset_entry.id is not carried (no model declares it)."""
    entry = recorded.metadata.tables[StoreDatasetEntry.__tablename__]
    singles = [
        c for c in entry.constraints if isinstance(c, sa.UniqueConstraint) and [col.name for col in c.columns] == ['id']
    ]
    assert singles == [] and not entry.c.id.unique
    assert not [i for i in entry.indexes if [col.name for col in i.columns] == ['id']]


# --------------------------------------------------------------------------------------------- the models, offline


def _space(text: str) -> str:
    return ' '.join(str(text).split())


def _default(column: sa.Column) -> str | None:
    return DIALECT.ddl_compiler(DIALECT, None).get_column_default_string(column)


def _table_facts(table: sa.Table) -> dict[str, object]:
    def key(constraint: sa.Constraint, columns: tuple[str, ...]) -> object:
        return constraint.name or ('unnamed', columns)

    uniques, checks, foreign_keys = {}, {}, set()
    for constraint in table.constraints:
        columns = tuple(column.name for column in constraint.columns) if hasattr(constraint, 'columns') else ()
        if isinstance(constraint, sa.UniqueConstraint):
            uniques[key(constraint, columns)] = columns
        elif isinstance(constraint, sa.CheckConstraint):
            checks[key(constraint, columns)] = _space(constraint.sqltext)
        elif isinstance(constraint, sa.ForeignKeyConstraint):
            foreign_keys.add(
                (
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    constraint.ondelete,
                )
            )
    return {
        'columns': {
            column.name: (
                column.type.compile(dialect=DIALECT),
                column.nullable,
                _default(column),
                column.primary_key,
                bool(column.unique),
            )
            for column in table.columns
        },
        'primary_key': tuple(column.name for column in table.primary_key.columns),
        'uniques': uniques,
        'checks': checks,
        'foreign_keys': foreign_keys,
        'indexes': {index.name: _space(CreateIndex(index).compile(dialect=DIALECT)) for index in table.indexes},
    }


def _enum_labels(metadata: sa.MetaData) -> dict[str, tuple[str, ...]]:
    labels: dict[str, tuple[str, ...]] = {}
    for table in metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.Enum):
                found = tuple(column.type.enums)
                assert labels.setdefault(column.type.name, found) == found, (
                    f'{column.type.name}: two label lists in one metadata ({table.name}.{column.name})'
                )
    return labels


@pytest.mark.parametrize('table_name', sorted(model_metadata().tables))
def test_the_baseline_builds_each_table_exactly_as_the_model_declares(recorded, models, table_name):
    built, declared = _table_facts(recorded.metadata.tables[table_name]), _table_facts(models.tables[table_name])
    for fact, value in declared.items():
        assert built[fact] == value, f'{table_name} {fact}: baseline {built[fact]!r} but model {value!r}'


def test_every_enum_type_carries_the_models_labels_in_the_models_order(baseline, recorded, models):
    """Label ORDER is the models' member order, which is what metadata.create_all builds (and what alembic ignores)."""
    declared = _enum_labels(models)
    assert dict(baseline.ENUM_LABELS) == declared
    assert _enum_labels(recorded.metadata) == declared
