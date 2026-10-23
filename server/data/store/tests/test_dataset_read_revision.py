"""The dataset_read revision, b3d9e1a47c62, and the rollup statement, against the design, without a database.

The design is tj-grna9p.46 and its architect notes of 2026-10-13 (OPTION R wins over the body): ONE additive
revision on 6f5a798746a1; dataset_read with client_id (SensitiveString in the model, String in DDL) and
client_kind (enum read_client_kind: UI, SDK) both NOT NULL, kind (enum dataset_read_kind: READ, STREAM_SESSION);
dataset_read_daily unique on (dataset_id, client_id, day), carrying client_kind, sessions and last_read_at; NO
foreign key (usage outlives a dataset); no reader column; downgrade drops both tables and both enum types.

THE ROLLUP IS ONE STATEMENT (the design's "a crash mid-rollup never double-counts"): the rendered SQL is pinned
here -- a top-level data-modifying CTE (Postgres refuses one that is not top-level), the DELETE's RETURNING feeding
an INSERT ... ON CONFLICT that ADDS -- and executed once inside one write_transaction. That it folds the right
sums, is idempotent and survives an abort between fold and delete is tests/system/test_usage_record.py's.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb), as in test_ingest_run_revision.py: the revision's DDL is compared
with the MODEL's DDL compiled for Postgres, and the downgrade with the upgrade.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations.schemaobj import SchemaObjects
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from common.database.sql_alchemy_sensitive_string import SensitiveString
from data.store.app.database.crud import dataset_read as crud
from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind, ReadClientKind
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / 'b3d9e1a47c62_dataset_read.py'
DIALECT = postgresql.dialect()
NEW_TYPES = ['read_client_kind', 'dataset_read_kind']
TABLES = {DatasetRead.TABLE_NAME: DatasetRead.__table__, DatasetReadDaily.TABLE_NAME: DatasetReadDaily.__table__}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location('revision_b3d9e1a47c62', REVISION_FILE)
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


def _revision_indexes(op: MagicMock) -> set[str]:
    objects = SchemaObjects()
    return {
        str(CreateIndex(objects.index(args[0], args[1], args[2], **kwargs)).compile(dialect=DIALECT))
        for name, args, kwargs in op.method_calls
        if name == 'create_index'
    }


def test_the_revision_sits_on_ingest_run_and_carries_no_additive_exception_marker():
    module = _load()
    assert (module.revision, module.down_revision) == ('b3d9e1a47c62', '6f5a798746a1')
    assert 'additive-exception' not in REVISION_FILE.read_text(encoding='utf-8').lower()


def test_upgrade_only_creates_two_types_two_tables_and_three_indexes():
    op, created, dropped = _run('upgrade')

    assert _schema_calls(op) == ['create_table', 'create_index', 'create_index', 'create_table', 'create_index']
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


def test_the_revision_builds_exactly_the_tables_the_models_declare():
    """Every column, type, nullability, server default and constraint, as Postgres DDL, model against revision."""
    op, _, _ = _run('upgrade')
    tables = _revision_tables(op)
    assert set(tables) == set(TABLES)
    for name, model_table in TABLES.items():
        assert _ddl_of(tables[name]) == _ddl_of(model_table), name


def test_the_revision_builds_exactly_the_indexes_the_models_declare():
    op, _, _ = _run('upgrade')
    declared = {
        str(CreateIndex(index).compile(dialect=DIALECT)) for table in TABLES.values() for index in table.indexes
    }
    assert _revision_indexes(op) == declared
    assert len(declared) == 3


def test_the_two_new_types_carry_the_models_members_in_order():
    module = _load()
    assert list(module.CLIENT_KIND_MEMBERS) == [member.value for member in ReadClientKind]
    assert list(module.READ_KIND_MEMBERS) == [member.value for member in DatasetReadKind]


def test_the_design_shape_of_the_two_tables():
    """No foreign key; the label NOT NULL and sensitive; no reader column; the daily key; last_read_at and sessions."""
    read, daily = DatasetRead.__table__, DatasetReadDaily.__table__
    assert not read.foreign_keys and not daily.foreign_keys
    for table in (read, daily):
        assert isinstance(table.c.client_id.type, SensitiveString)
        assert not table.c.client_id.nullable and not table.c.client_kind.nullable
        assert 'reader' not in table.c
    (unique,) = [c for c in daily.constraints if isinstance(c, sa.UniqueConstraint)]
    assert [column.name for column in unique.columns] == ['dataset_id', 'client_id', 'day']
    assert unique.name == DatasetReadDaily.UNIQUE_CONSTRAINT
    assert not daily.c.last_read_at.nullable and not daily.c.sessions.nullable


# ---------------------------------------------------------------------------------------------
# The rollup: one statement, one transaction.


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement: Any, *args: Any) -> Any:
        self.statements.append(statement)
        result = MagicMock()
        result.rowcount = 5
        return result

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _sql(statement: Any) -> str:
    return ' '.join(str(statement.compile(dialect=DIALECT)).split())


@pytest.mark.asyncio
async def test_the_rollup_is_one_statement_in_one_transaction():
    session = _Session()
    assert await crud.fold_reads_before(session, datetime(2026, 1, 1, tzinfo=UTC)) == 5
    assert len(session.statements) == 1 and session.commits == 1 and session.rollbacks == 0


@pytest.mark.asyncio
async def test_the_rollup_sql_deletes_and_upserts_in_one_top_level_cte_and_adds_on_conflict():
    session = _Session()
    await crud.fold_reads_before(session, datetime(2026, 1, 1, tzinfo=UTC))
    sql = _sql(session.statements[0])

    # Postgres accepts a data-modifying CTE only at the top level of the statement.
    assert sql.startswith('WITH moved AS (DELETE FROM dataset_read WHERE dataset_read.read_at < '), sql
    assert ' RETURNING ' in sql.split(') INSERT INTO', 1)[0]
    assert (
        ') INSERT INTO dataset_read_daily (dataset_id, client_id, client_kind, day, reads, sessions, bars_returned, last_read_at) SELECT '
        in sql
    )
    assert 'FROM moved GROUP BY moved.dataset_id, moved.client_id, moved.client_kind, ' in sql
    # Reads and sessions are counted apart; the day is the UTC calendar day.
    assert 'count(*) FILTER (WHERE moved.kind = ' in sql
    assert 'CAST(timezone(' in sql and 'AS DATE)' in sql
    # ADDED to an existing summary, so a day folded in two runs sums; last_read_at keeps the latest.
    assert f'ON CONFLICT ON CONSTRAINT {DatasetReadDaily.UNIQUE_CONSTRAINT} DO UPDATE SET ' in sql
    for column in ('reads', 'sessions', 'bars_returned'):
        assert f'{column} = (dataset_read_daily.{column} + excluded.{column})' in sql
    assert 'last_read_at = greatest(dataset_read_daily.last_read_at, excluded.last_read_at)' in sql
