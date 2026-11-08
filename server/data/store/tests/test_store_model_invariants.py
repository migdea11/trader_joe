"""The store's models and repositories at head: the design facts that outlived the per-revision tests (ADR tj-kg521d).

WHY THIS FILE EXISTS. The alpha baseline replaced the sixteen earlier store revisions with one initial revision, and
their per-revision tests were deleted with them: those tests pinned each revision's upgrade and downgrade, which no
longer exist. Some of their assertions were never about a migration step. They pinned the MODELS and the REPOSITORIES
as they stand at head: a table's design columns and nullability, which columns are identity, which CHECK bites through
the ORM, that an append-only table has no update or delete path. Those facts outlive the chain, so they live here, one
test per fact, each naming the file it came from.

WHAT IS NOT HERE. Whether the baseline revision builds exactly what the models declare is
test_alpha_baseline_revision.py's (offline) and the Postgres catalogue test's (tests/system, the schema of `alembic
upgrade head` on an empty database against metadata.create_all). Nothing here loads a revision.

PR-GATE TIER: no database. The CHECK tests build the model's own DDL on SQLite, which is what the store unit tier meets.
"""

import ast
import inspect
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.enums.data_stock import DataSource, ExpiryType, UpdateType
from common.tests.roots import SERVER_ROOT
from data.store.app.database.crud import asset_list as asset_list_crud
from data.store.app.database.crud import corporate_action as corporate_action_crud
from data.store.app.database.crud import dataset_read as dataset_read_crud
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.dataset_read import DatasetRead
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun, IngestRunTrigger
from data.store.app.database.models.panel import (
    Panel,
    PanelMemberTransition,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.tests.test_ui_datasets_route import entry
from schemas.data_ingest.submit import AssetListStatus


pytestmark = pytest.mark.data_store

STORE_DIR = SERVER_ROOT / 'data' / 'store'
MODELS_DIR = STORE_DIR / 'app' / 'database' / 'models'
ENV_PY = STORE_DIR / 'migrations' / 'env.py'
DIALECT = postgresql.dialect()

ENTRY = StoreDatasetEntry.__table__
BAR = StockMarketActivity.__table__
REQUEST = IngestRequest.__table__
INSTANCE = IngestInstance.__table__
RUN = IngestRun.__table__
ACTION = CorporateAction.__table__


def _compiled(element: Any) -> str:
    return ' '.join(str(element.compile(dialect=DIALECT)).split())


def _normalised(sql: str) -> str:
    return ' '.join(sql.split())


def _ddl(table: sa.Table) -> str:
    """CREATE TABLE plus every CREATE INDEX for the table, as Postgres would receive them, whitespace-collapsed."""
    statements = [str(CreateTable(table).compile(dialect=DIALECT))]
    statements += [str(CreateIndex(index).compile(dialect=DIALECT)) for index in table.indexes]
    return ' '.join(' '.join(statements).split())


def _quoted(columns: tuple[str, ...] | list[str]) -> str:
    return ', '.join(DIALECT.identifier_preparer.quote(column) for column in columns)


def _model_check(table: sa.Table, name: str) -> sa.CheckConstraint:
    (check,) = [c for c in table.constraints if isinstance(c, sa.CheckConstraint) and c.name == name]
    return check


def _production_sources() -> list[Path]:
    """Every production .py of the server: tests, revisions, the seed tooling and a venv left out."""
    return [
        path
        for path in SERVER_ROOT.rglob('*.py')
        if not {'tests', 'migrations', 'seeds', '.venv'} & set(path.relative_to(SERVER_ROOT).parts)
    ]


def _importers(model_module: str) -> set[str]:
    """The production modules that import data.store.app.database.models.<model_module>."""
    pattern = re.compile(rf'store\.app\.database\.models(\.{model_module}\b| import .*\b{model_module}\b)')
    return {
        path.relative_to(SERVER_ROOT).as_posix()
        for path in _production_sources()
        if pattern.search(path.read_text(encoding='utf-8'))
    }


# --------------------------------------------------------------------------------------------- the migration env


def test_the_migration_env_loads_every_model_module_so_autogenerate_sees_every_table():
    """env.py imports only ALLOWED_MODELS; a model module missing there is a table `alembic check` would want to drop.

    Re-homed from test_ingest_run_revision.py, test_corporate_action_revision.py, test_asset_list_revision.py,
    test_request_record_revision.py and test_panel_revision.py (ADR tj-kg521d), which each pinned their own module's
    membership; here every module of the models package is pinned at once, so a new one cannot be forgotten.
    """
    tree = ast.parse(ENV_PY.read_text(encoding='utf-8'))
    (allowed,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['ALLOWED_MODELS']
    ]
    modules = {path.stem for path in MODELS_DIR.glob('*.py') if path.stem != '__init__'}
    assert modules, 'non-vacuity: the models package holds no module'
    assert modules <= set(allowed), sorted(modules - set(allowed))


# --------------------------------------------------------------------------------------------- stock_market_activity


def test_the_bar_counts_are_bigint_and_not_null():
    """Volume and trade count are BIGINT (bug tj-1ov1p2): a bar above int32 must be storable.

    Re-homed from test_bar_volume_bigint_revision.py (ADR tj-kg521d).
    """
    for column in ('volume', 'trade_count'):
        assert isinstance(BAR.c[column].type, sa.BigInteger), f'{column}: {BAR.c[column].type!r}'
        assert BAR.c[column].nullable is False, column


def test_neither_bar_count_is_in_a_key_or_an_index_of_the_bar():
    """Neither count is in the natural key, the ON CONFLICT arbiter or any index.

    Re-homed from test_bar_volume_bigint_revision.py (ADR tj-kg521d).
    """
    keyed = {column.name for index in BAR.indexes for column in index.columns}
    keyed |= {column.name for constraint in BAR.constraints for column in getattr(constraint, 'columns', [])}
    assert keyed, 'the bar declares no key or index at all: the read is blind'
    assert not keyed & {'volume', 'trade_count'}, sorted(keyed & {'volume', 'trade_count'})


def test_the_bar_ddl_has_the_natural_key_constraint_and_the_symbol_index():
    """CreateTable against the postgresql dialect: what a fresh database built from the MODEL gets.

    Re-homed from test_head_revision_shape.py (ADR tj-kg521d), model half: the named natural-key UNIQUE in the key's
    order and the (symbol, granularity, timestamp) read index.
    """
    ddl = _ddl(BAR)
    (unique,) = [c for c in BAR.constraints if c.name == StockMarketActivity.NATURAL_KEY_CONSTRAINT]
    key = tuple(column.name for column in unique.columns)
    assert f'CONSTRAINT {StockMarketActivity.NATURAL_KEY_CONSTRAINT} UNIQUE ({_quoted(key)})' in ddl, ddl
    index = StockMarketActivity.SYMBOL_GRANULARITY_TIMESTAMP_INDEX
    assert f'CREATE INDEX {index} ON {BAR.name} ({_quoted(["asset_symbol", "granularity", "timestamp"])})' in ddl, ddl


@pytest.mark.parametrize(
    'dead',
    [
        'split_factor',
        'dividends_factor',
        'expiry',
        'ix_dataset_id_timestamp',
        'ix_stock_market_activity_dataset_id',
        'ix_stock_market_activity_expiry',
    ],
)
def test_the_bar_ddl_does_not_contain_a_removed_column_or_index(dead: str):
    """The design's list (tj-vhboky.12 item 5, Amendment 1): none of these may reach a database built from the model.

    Re-homed from test_head_revision_shape.py (ADR tj-kg521d).
    """
    ddl = _ddl(BAR)
    assert not re.search(rf'\b{dead}\b', ddl), f'the bar DDL contains {dead}: {ddl}'


# --------------------------------------------------------------------------------------------- store_dataset_entry


def test_the_entry_ddl_has_the_identity_constraint_and_an_aware_expiry():
    """The named identity UNIQUE over the model's natural key, and expiry TIMESTAMP WITH TIME ZONE (tj-vhboky.12 5a).

    Re-homed from test_head_revision_shape.py (ADR tj-kg521d), model halves. A naive expiry makes "when does this data
    die" depend on the session timezone.
    """
    ddl = _ddl(ENTRY)
    expected = (
        f'CONSTRAINT {StoreDatasetEntry.NATURAL_KEY_CONSTRAINT} UNIQUE ({_quoted(StoreDatasetEntry.NATURAL_KEY)})'
    )
    assert expected in ddl, ddl
    assert re.search(r'\bexpiry TIMESTAMP WITH TIME ZONE\b', ddl), f'the model compiles expiry as naive: {ddl}'


@pytest.mark.parametrize('column', ['collect', 'stopped_cause'])
def test_collect_and_stopped_cause_are_not_identity_and_in_no_unique_key_or_index(column: str):
    """Collect is the collecting intent and stopped_cause why the store stopped it: neither is identity.

    Re-homed from test_dataset_entry_collect_revision.py and test_push_path_columns_revision.py (ADR tj-kg521d).
    """
    assert column not in StoreDatasetEntry.NATURAL_KEY
    for constraint in ENTRY.constraints:
        if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint):
            assert column not in [c.name for c in constraint.columns], constraint.name
    for index in ENTRY.indexes:
        assert column not in [c.name for c in index.columns], index.name


@pytest.mark.parametrize('update_type', [0, 4, 99])
@pytest.mark.parametrize('collect', [None, True])
def test_the_collect_check_refuses_an_unknown_update_type(update_type: int, collect: bool | None):
    """Through the model's own DDL on SQLite, by a raw UPDATE: the ORM's enum type could not even bind the value.

    Re-homed from test_dataset_entry_collect_revision.py (ADR tj-kg521d).
    """
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    with Session(engine) as db:
        db.add(entry('ZZUNK'))
        db.commit()
    with engine.begin() as conn, pytest.raises(IntegrityError, match=StoreDatasetEntry.COLLECT_BY_TYPE_CONSTRAINT):
        conn.exec_driver_sql(
            f'UPDATE {ENTRY.name} SET update_type = ?, collect = ? WHERE asset_symbol = ?',  # nosec B608
            (update_type, collect, 'ZZUNK'),
        )
    engine.dispose()


@pytest.mark.parametrize(
    ('update_type', 'collect', 'allowed'),
    [
        (UpdateType.STATIC, None, True),
        (UpdateType.STATIC, True, False),
        (UpdateType.DAILY, True, True),
        (UpdateType.DAILY, None, False),
        (UpdateType.STREAM, False, True),
        (UpdateType.STREAM, None, False),
    ],
    ids=lambda value: str(value),
)
def test_the_collect_check_bites_through_the_orm(update_type: UpdateType, collect: bool | None, allowed: bool):
    """NULL exactly for STATIC, a value for a subscription. The SQLite tier builds its tables from the model.

    Re-homed from test_dataset_entry_collect_revision.py (ADR tj-kg521d).
    """
    assert {member.value for member in UpdateType} == {1, 2, 3}, 'a new update type: revisit the <> STATIC rule'
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    row = entry('ZZCHK', update_type=update_type)
    row.collect = collect
    with Session(engine) as db:
        db.add(row)
        if allowed:
            db.commit()
        else:
            with pytest.raises(IntegrityError, match=StoreDatasetEntry.COLLECT_BY_TYPE_CONSTRAINT):
                db.commit()
    engine.dispose()


def test_the_stopped_cause_check_is_the_designs_text():
    """By name, the model's constant and the design's (addendum 13), and the condition's SQL text.

    Re-homed from test_push_path_columns_revision.py (ADR tj-kg521d), model half.
    """
    check = _model_check(ENTRY, StoreDatasetEntry.STOPPED_CAUSE_CONSTRAINT)
    assert check.name == 'ck_store_dataset_entry_stopped_cause_needs_stop'
    assert _normalised(str(check.sqltext)) == 'stopped_cause IS NULL OR collect IS FALSE'


@pytest.mark.parametrize(
    ('collect', 'cause', 'allowed'),
    [
        (False, 'VENDOR_ASSET_CHANGED', True),
        (True, 'VENDOR_ASSET_CHANGED', False),
        (None, 'VENDOR_ASSET_CHANGED', False),
        (False, '', True),
        (True, '', False),
        (None, '', False),
        (False, None, True),
        (True, None, True),
        (None, None, True),
    ],
)
def test_the_stopped_cause_check_bites_through_the_orm(collect, cause, allowed):
    """A cause only on a stopped subscription (collect false); no cause is always allowed.

    Re-homed from test_push_path_columns_revision.py (ADR tj-kg521d).
    """
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    row = entry('ZZSTOP', update_type=UpdateType.STATIC if collect is None else UpdateType.DAILY)
    row.collect = collect
    row.stopped_cause = cause
    with Session(engine) as db:
        db.add(row)
        if allowed:
            db.commit()
        else:
            with pytest.raises(IntegrityError, match=StoreDatasetEntry.STOPPED_CAUSE_CONSTRAINT):
                db.commit()
    engine.dispose()


def test_a_stopped_row_can_be_resumed_only_by_clearing_its_cause():
    """Through the model's DDL by raw UPDATE: flipping collect back to true while a cause is held is refused.

    Re-homed from test_push_path_columns_revision.py (ADR tj-kg521d).
    """
    engine = sa.create_engine('sqlite://')
    ENTRY.create(engine)
    row = entry('ZZRES', update_type=UpdateType.DAILY)
    row.collect = False
    row.stopped_cause = 'VENDOR_ASSET_CHANGED'
    with Session(engine) as db:
        db.add(row)
        db.commit()
    with engine.begin() as conn, pytest.raises(IntegrityError, match=StoreDatasetEntry.STOPPED_CAUSE_CONSTRAINT):
        conn.exec_driver_sql(f"UPDATE {ENTRY.name} SET collect = 1 WHERE asset_symbol = 'ZZRES'")  # nosec B608
    with engine.begin() as conn:
        conn.exec_driver_sql(  # nosec B608 -- module constant
            f"UPDATE {ENTRY.name} SET collect = 1, stopped_cause = NULL WHERE asset_symbol = 'ZZRES'"
        )
    engine.dispose()


# --------------------------------------------------------------------------------------------- ingest_request


def test_the_request_expiry_type_is_a_nullable_integer_with_no_default_and_no_index():
    """NULL where a request carries none; the entry's representation (ExpiryType's integer); never a lookup key.

    Re-homed from test_request_expiry_type_revision.py (ADR tj-kg521d): the model column, the representation the entry
    column stores, and no index. The entry column is NOT NULL with server default BULK; the request column copies
    neither.
    """
    column = REQUEST.c.expiry_type
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True
    assert (column.server_default, column.default) == (None, None)
    assert _compiled(CreateColumn(column)) == 'expiry_type INTEGER'
    assert _compiled(CreateColumn(ENTRY.c.expiry_type)).split()[1] == 'INTEGER'
    assert {member.value for member in ExpiryType} == {1, 2, 3, 4, 5}, 'the five values the round trips store'
    for index in REQUEST.indexes:
        assert 'expiry_type' not in [c.name for c in index.columns], index.name


def test_the_request_and_attempt_trigger_share_one_type_with_the_models_members():
    """ingest_request.trigger references ingest_run's existing type: one type, one label list.

    Re-homed from test_push_path_columns_revision.py (ADR tj-kg521d), model half.
    """
    assert REQUEST.c.trigger.type.name == RUN.c.trigger.type.name == 'ingest_run_trigger'
    assert list(REQUEST.c.trigger.type.enums) == [member.value for member in IngestRunTrigger]
    assert list(REQUEST.c.trigger.type.enums) == list(RUN.c.trigger.type.enums)


def test_no_foreign_key_on_the_request_record_and_the_design_nullabilities_hold():
    """Runs outlive requests and requests outlive datasets: nothing references anything.

    Re-homed from test_request_record_revision.py and test_ingest_run_revision.py (ADR tj-kg521d), model halves.
    """
    for table in (REQUEST, INSTANCE, RUN):
        assert not table.foreign_keys, table.name
    for nullable in ('dataset_id', 'ready_by', 'instance_id', 'finished_at', 'failure_cause'):
        assert REQUEST.c[nullable].nullable, nullable
    for required in ('kind', 'source', 'client_label', 'state', 'attempts', 'created_at'):
        assert not REQUEST.c[required].nullable, required
    assert 'listing_id' not in REQUEST.c, 'P49 (tj-7zjov1.143): symbol identity is not modelled'
    assert INSTANCE.c.superseded_at.nullable
    assert RUN.c.dataset_id.nullable and RUN.c.request_id.nullable and RUN.c.vendor_asset_id.nullable
    assert not RUN.c.bars_written.nullable and RUN.c.bars_written.server_default.arg == '0'


def test_the_attempt_index_is_unique_and_partial_on_both_columns_being_set():
    """The design's 'where both are set', read off the model: the arbiter an idempotent redelivery names.

    Re-homed from test_request_record_revision.py (ADR tj-kg521d), model half.
    """
    (index,) = [index for index in RUN.indexes if index.name == IngestRun.ATTEMPT_UNIQUE_INDEX]
    assert index.unique
    assert [column.name for column in index.columns] == ['request_id', 'attempt_no']
    predicate = _normalised(str(index.dialect_options['postgresql']['where']))
    assert predicate == 'request_id IS NOT NULL AND attempt_no IS NOT NULL'


# --------------------------------------------------------------------------------------------- corporate_action


def test_the_corporate_action_model_carries_the_designs_columns_and_dedupe_key_and_no_foreign_key():
    """The raw record and what delivered it, the dedupe key (source, vendor id, payload hash), no foreign key.

    Re-homed from test_corporate_action_revision.py (ADR tj-kg521d).
    """
    names = set(ACTION.c.keys())
    assert {'process_date', 'effective_date', 'ex_date', 'record_date', 'payable_date'} <= names
    assert {'vendor_action_id', 'action_type', 'identifiers', 'raw_payload', 'payload_hash', 'received_at'} <= names
    assert {'request_id', 'attempt_no', 'requesting_symbol', 'requested_start', 'requested_end', 'as_of'} <= names
    assert not {'dataset_id', 'listing_id'} & names, 'nothing ties an observation to a dataset or a listing'
    (unique,) = [index for index in ACTION.indexes if index.unique]
    assert tuple(column.name for column in unique.columns) == ('source', 'vendor_action_id', 'payload_hash')
    assert not ACTION.foreign_keys
    assert isinstance(ACTION.c.raw_payload.type, sa.LargeBinary) and not ACTION.c.raw_payload.nullable


class _Captured:
    """A stand-in session that records each statement and reports one row written."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return MagicMock(rowcount=1)


@pytest.mark.asyncio
async def test_the_observation_insert_names_the_dedupe_index_and_does_nothing_on_a_repeat_never_an_update():
    """A repeat is never overwritten: ON CONFLICT on exactly the dedupe key, DO NOTHING.

    Re-homed from test_corporate_action_revision.py (ADR tj-kg521d).
    """
    observation = corporate_action_crud.NewObservation(
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

    assert await corporate_action_crud.insert_observations(session, [observation]) == 1

    (statement,) = session.statements
    sql = _compiled(statement)
    assert sql.startswith(f'INSERT INTO {ACTION.name} ')
    assert sql.endswith('ON CONFLICT (source, vendor_action_id, payload_hash) DO NOTHING')
    assert 'DO UPDATE' not in sql


def test_the_corporate_action_repository_has_no_update_or_delete_path():
    """The model's docstring: never updated or deleted. The surface is the one insert and the clamp's one read.

    Re-homed from test_corporate_action_revision.py (ADR tj-kg521d).
    """
    crud = corporate_action_crud
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
    """Only the repository and the panel UI reads import the model; no raw SQL updates or deletes the table.

    Re-homed from test_corporate_action_revision.py (ADR tj-kg521d). panel_reads only select()s corporate_action and
    never raw_payload (pinned by test_panel_ui_reads.py).
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?corporate_action\b', re.IGNORECASE)
    for path in _production_sources():
        assert not raw.search(path.read_text(encoding='utf-8')), f'{path} updates or deletes corporate_action'
    assert _importers('corporate_action') == {
        'data/store/app/database/crud/corporate_action.py',
        'data/store/app/database/crud/panel_reads.py',
    }


# --------------------------------------------------------------------------------------------- asset lists

ASSET_LIST_MODELS = (AssetListSnapshot, AssetListRow, AssetListEvent)


def test_the_asset_list_models_carry_the_designs_keys_and_no_foreign_key():
    """One snapshot per delivered attempt (the replay key); one event per (snapshot, id, kind). Nothing references.

    Re-homed from test_asset_list_revision.py (ADR tj-kg521d).
    """
    (snapshot_key,) = [index for index in AssetListSnapshot.__table__.indexes if index.unique]
    (event_key,) = [index for index in AssetListEvent.__table__.indexes if index.unique]
    assert [column.name for column in snapshot_key.columns] == ['request_id', 'attempt_no']
    assert [column.name for column in event_key.columns] == ['snapshot_id', 'vendor_asset_id', 'kind']
    assert not any(model.__table__.foreign_keys for model in ASSET_LIST_MODELS)
    rows = AssetListRow.__table__.c
    assert rows.active.nullable and rows.tradable.nullable, 'presence, not a value: NULL is the vendor saying nothing'
    assert isinstance(rows.raw_payload.type, sa.LargeBinary) and not rows.raw_payload.nullable
    assert AssetListEvent.__table__.c.previous_symbol.nullable


def test_the_asset_list_labels_are_the_wire_domains_values_and_the_event_kinds_the_models():
    """The status labels ARE AssetListStatus's values (no mapping), on all three columns of the type.

    Re-homed from test_asset_list_revision.py (ADR tj-kg521d), model halves.
    """
    assert [member.value for member in AssetListStatus] == ['active', 'inactive']
    for column in (
        REQUEST.c.asset_list_status,
        AssetListSnapshot.__table__.c.asset_list,
        AssetListEvent.__table__.c.asset_list,
    ):
        assert (column.type.name, list(column.type.enums)) == ('asset_list_status', ['active', 'inactive']), column
    kind = AssetListEvent.__table__.c.kind.type
    assert kind.name == 'asset_list_event_kind'
    assert (
        list(kind.enums) == [member.value for member in AssetListEventKind] == ['appeared', 'vanished', 'status_flip']
    )


def test_the_request_names_its_list_both_ways_under_the_designs_check():
    """A request row names its list exactly when it is an asset-list request (ADR tj-26jt9w addendum 17).

    Re-homed from test_asset_list_revision.py (ADR tj-kg521d), model half.
    """
    check = _model_check(REQUEST, 'ck_ingest_request_asset_list_status')
    assert _normalised(str(check.sqltext)) == "(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)"
    column = REQUEST.c.asset_list_status
    assert column.nullable and column.server_default is None and column.default is None


def test_the_asset_list_repository_has_no_update_or_delete_path():
    """The models' docstrings: APPEND-ONLY. No statement in the crud is an update, a delete or a DO UPDATE.

    Re-homed from test_asset_list_revision.py (ADR tj-kg521d).
    """
    tree = ast.parse(Path(asset_list_crud.__file__).read_text(encoding='utf-8'))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not imported & {'update', 'delete'}, imported
    bare = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not bare & {'update', 'delete'}, bare
    attributes = {
        (ast.unparse(node.func.value), node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
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
    """Only the repository, the arm and the panel UI reads import the models; no raw SQL updates or deletes them.

    Re-homed from test_asset_list_revision.py (ADR tj-kg521d). The seed tooling is excluded: it empties the tables of a
    disposable stack before writing the cast.
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?asset_list_(snapshot|row|event)\b', re.I)
    for path in _production_sources():
        assert not raw.search(path.read_text(encoding='utf-8')), f'{path} updates or deletes an asset-list table'
    assert _importers('asset_list') == {
        'data/store/app/database/crud/asset_list.py',
        'data/store/app/world_snapshot.py',
        'data/store/app/database/crud/panel_reads.py',
    }


# --------------------------------------------------------------------------------------------- dataset_read


def test_the_design_shape_of_the_two_usage_tables():
    """No foreign key; the label NOT NULL and sensitive; no reader column; the daily key; last_read_at and sessions.

    Re-homed from test_dataset_read_revision.py (ADR tj-kg521d).
    """
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


class _RollupSession:
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


@pytest.mark.asyncio
async def test_the_rollup_is_one_statement_in_one_transaction():
    """Re-homed from test_dataset_read_revision.py (ADR tj-kg521d)."""
    session = _RollupSession()
    assert await dataset_read_crud.fold_reads_before(session, datetime(2026, 1, 1, tzinfo=UTC)) == 5
    assert len(session.statements) == 1 and session.commits == 1 and session.rollbacks == 0


@pytest.mark.asyncio
async def test_the_rollup_sql_deletes_and_upserts_in_one_top_level_cte_and_adds_on_conflict():
    """The fold is one data-modifying CTE; a day folded in two runs sums; last_read_at keeps the latest.

    Re-homed from test_dataset_read_revision.py (ADR tj-kg521d).
    """
    session = _RollupSession()
    await dataset_read_crud.fold_reads_before(session, datetime(2026, 1, 1, tzinfo=UTC))
    sql = _compiled(session.statements[0])

    # Postgres accepts a data-modifying CTE only at the top level of the statement.
    assert sql.startswith('WITH moved AS (DELETE FROM dataset_read WHERE dataset_read.read_at < '), sql
    assert ' RETURNING ' in sql.split(') INSERT INTO', 1)[0]
    assert (
        ') INSERT INTO dataset_read_daily (dataset_id, client_id, client_kind, day, reads, sessions, bars_returned, '
        'last_read_at) SELECT ' in sql
    )
    assert 'FROM moved GROUP BY moved.dataset_id, moved.client_id, moved.client_kind, ' in sql
    assert 'count(*) FILTER (WHERE moved.kind = ' in sql
    assert 'CAST(timezone(' in sql and 'AS DATE)' in sql
    assert f'ON CONFLICT ON CONSTRAINT {DatasetReadDaily.UNIQUE_CONSTRAINT} DO UPDATE SET ' in sql
    for column in ('reads', 'sessions', 'bars_returned'):
        assert f'{column} = (dataset_read_daily.{column} + excluded.{column})' in sql
    assert 'last_read_at = greatest(dataset_read_daily.last_read_at, excluded.last_read_at)' in sql


# --------------------------------------------------------------------------------------------- the audit logs


def test_the_override_log_has_the_designed_columns_all_not_null_but_new_value_and_no_foreign_key():
    """Six columns NOT NULL; new_value Text and NULLABLE with no default (NULL for DELETE). The owner is sensitive.

    Re-homed from test_override_log_revision.py (ADR tj-kg521d). The override log's reason stays required (ADR
    tj-46jkey: only the panel versions' reasons became optional).
    """
    table = DatasetOverrideLog.__table__
    assert set(table.c.keys()) == {'id', 'actor', 'dataset_id', 'owner', 'action', 'reason', 'at', 'new_value'}
    assert [c.name for c in table.columns if c.nullable] == ['new_value']
    new_value = table.c.new_value
    assert isinstance(new_value.type, sa.Text)
    assert new_value.server_default is None and new_value.default is None
    assert not table.foreign_keys
    assert isinstance(table.c.owner.type, SensitiveString)


def test_the_deletion_record_has_no_foreign_key_and_a_sensitive_owner():
    """range_end is the one nullable column: NULL for an open-ended dataset.

    Re-homed from test_override_log_revision.py (ADR tj-kg521d).
    """
    table = DatasetDeletion.__table__
    assert not table.foreign_keys
    assert isinstance(table.c.owner.type, SensitiveString)
    assert [column.name for column in table.columns if column.nullable] == ['range_end']


@pytest.mark.parametrize(
    'row',
    [
        DatasetOverrideLog(owner='distinctive-owner-7731', action=OverrideAction.DELETE, reason='r'),
        DatasetDeletion(owner='distinctive-owner-7731', asset_symbol='AAA', cause=DatasetDeletion.CAUSE_OPERATOR),
    ],
    ids=['override-log', 'deletion'],
)
def test_neither_audit_repr_carries_the_owner(row):
    """Re-homed from test_override_log_revision.py (ADR tj-kg521d)."""
    assert 'distinctive-owner-7731' not in repr(row)


_MUTATING = re.compile(r'\b(update|delete)\s*\(\s*(DatasetOverrideLog|DatasetDeletion|_OVERRIDE|_DELETION)\b')
_METHOD = re.compile(
    r'\b(DatasetOverrideLog|DatasetDeletion|_OVERRIDE|_DELETION)(\.__table__)?\s*\.\s*(update|delete)\s*\('
)
_RAW = re.compile(r'(UPDATE|DELETE\s+FROM)\s+(dataset_override_log|dataset_deletion)\b', re.IGNORECASE)


def test_nothing_in_the_service_updates_or_deletes_an_audit_row():
    """Both audit tables are append-only: never an UPDATE or DELETE, in the store app or its routers.

    Re-homed from test_override_log_revision.py (ADR tj-kg521d).
    """
    roots = [STORE_DIR / 'app', SERVER_ROOT / 'routers' / 'data_store']
    sources = [path for root in roots for path in root.rglob('*.py')]
    assert sources, 'non-vacuity'
    offenders = [
        f'{path}: {match.group(0)}'
        for path in sources
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
    """The scan above is only evidence if it reds on the shapes a mutation would take.

    Re-homed from test_override_log_revision.py (ADR tj-kg521d).
    """
    assert any(pattern.search(line) for pattern in (_MUTATING, _METHOD, _RAW))


# --------------------------------------------------------------------------------------------- the panel tables

PANEL_MODELS = (Panel, PanelSettingsVersion, PanelSourceSettingsVersion, PanelPass, PanelMemberTransition)
PANEL_TABLES = {model.TABLE_NAME: model.__table__ for model in PANEL_MODELS}

# The columns per table as addendum 20 and the rate design name them, WITHOUT size (ADR tj-46jkey: a panel has none).
PANEL_DESIGN_COLUMNS = {
    'panel': {'id', 'name', 'source', 'created_at'},
    'panel_settings_version': {
        'id', 'panel_id', 'version_no', 'enabled', 'cadence', 'allow_symbols', 'deny_symbols',
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
# Nullable exactly where the design says so; the two version reasons are optional (ADR tj-46jkey, NULL means none).
PANEL_DESIGN_NULLABLE = {
    'panel': set(),
    'panel_settings_version': {'asset_class', 'exchange', 'active', 'tradable', 'reason'},
    'panel_source_settings_version': {'reason'},
    'panel_pass': {'inactive_snapshot_id', 'finished_at', 'counts'},
    'panel_member_transition': {'dataset_id', 'request_id', 'cause'},
}
# NO POLICY DEFAULT (owner ruling 2026-10-27): none of these may carry a default of any kind.
PANEL_POLICY_COLUMNS = {
    'panel_settings_version': {'enabled', 'cadence'},
    'panel_source_settings_version': {
        'ramp_datasets_per_hour',
        'request_cap_per_hour',
        'corporate_action_window_pre_days',
        'corporate_action_window_post_days',
    },
    'panel_pass': {'kind', 'dry_run'},
}
# The ONLY server defaults: ids, the database-time instants, and the empty symbol lists (no restriction).
PANEL_SERVER_DEFAULTED = {
    'panel': {'id', 'created_at'},
    'panel_settings_version': {'id', 'saved_at', 'allow_symbols', 'deny_symbols'},
    'panel_source_settings_version': {'id', 'saved_at'},
    'panel_pass': {'id', 'started_at'},
    'panel_member_transition': {'id', 'recorded_at'},
}
PANEL_DESIGN_FOREIGN_KEYS = {
    ('panel_settings_version', 'panel_id', 'panel.id'),
    ('panel_pass', 'panel_id', 'panel.id'),
    ('panel_pass', 'settings_version_id', 'panel_settings_version.id'),
    ('panel_pass', 'source_settings_version_id', 'panel_source_settings_version.id'),
    ('panel_member_transition', 'pass_id', 'panel_pass.id'),
    ('panel_member_transition', 'panel_id', 'panel.id'),
}
PANEL_NEVER_REFERENCED = {'store_dataset_entry', 'ingest_request', 'asset_list_snapshot'}
PANEL_DESIGN_INDEXES = {
    ('panel', ('name',), True),
    ('panel_pass', ('panel_id', 'started_at'), False),
    ('panel_member_transition', ('panel_id', 'vendor_asset_id', 'as_of'), False),
    ('panel_member_transition', ('transition', 'recorded_at'), False),
}
PANEL_DESIGN_UNIQUES = {
    ('panel_settings_version', 'uq_panel_settings_version_panel_id_version_no', ('panel_id', 'version_no')),
    ('panel_source_settings_version', 'uq_panel_source_settings_version_source_version_no', ('source', 'version_no')),
    ('panel_member_transition', 'uq_panel_member_transition_pass_id_seq', ('pass_id', 'seq')),
}
# No size CHECK (size is gone). The reason not-blank CHECKs are KEPT: Postgres passes a CHECK on NULL, so they read
# 'NULL or non-blank' (ADR tj-46jkey section 1).
PANEL_DESIGN_CHECKS = {
    ('panel_settings_version', 'ck_panel_settings_version_version_no_positive', 'version_no >= 1'),
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


@pytest.mark.parametrize('table_name', list(PANEL_DESIGN_COLUMNS))
def test_each_panel_table_has_the_designs_columns_and_nullability(table_name):
    """The ruled names (the hourly rates, no ramp or per_pass_cap), no size, and both version reasons nullable.

    Re-homed from test_panel_revision.py (ADR tj-kg521d), model half, updated for ADR tj-46jkey: size removed, reason
    optional.
    """
    table = PANEL_TABLES[table_name]
    assert set(table.c.keys()) == PANEL_DESIGN_COLUMNS[table_name], table
    assert {c.name for c in table.columns if c.nullable} == PANEL_DESIGN_NULLABLE[table_name], table
    assert not {'ramp', 'per_pass_cap', 'size'} & set(table.c.keys())


@pytest.mark.parametrize('table_name', list(PANEL_DESIGN_COLUMNS))
def test_no_policy_default_and_only_the_designs_server_defaults_on_the_panel_tables(table_name):
    """Owner ruling 2026-10-27: no default for cadence, enabled, either rate or the window, server or client side.

    Re-homed from test_panel_revision.py (ADR tj-kg521d), model half.
    """
    table = PANEL_TABLES[table_name]
    for name in PANEL_POLICY_COLUMNS.get(table_name, set()):
        column = table.c[name]
        assert column.server_default is None and column.default is None, f'{table.name}.{name} has a default'
        assert not column.nullable, f'{table.name}.{name} is nullable, so NULL would be its default'
    assert {c.name for c in table.columns if c.server_default is not None} == PANEL_SERVER_DEFAULTED[table_name]
    assert {c.name for c in table.columns if c.default is not None} == set(), table


def test_the_recorded_at_and_saved_at_instants_are_database_time_and_as_of_is_not():
    """recorded_at is database time, NOT NULL default now(); as_of is the snapshot instant, written by the pass.

    Re-homed from test_panel_revision.py (ADR tj-kg521d), model half.
    """
    table = PANEL_TABLES['panel_member_transition']
    assert _normalised(str(table.c.recorded_at.server_default.arg)) == 'now()'
    assert not table.c.recorded_at.nullable
    assert table.c.as_of.server_default is None and not table.c.as_of.nullable
    assert isinstance(table.c.recorded_at.type, sa.DateTime) and table.c.recorded_at.type.timezone
    for name in ('panel_settings_version', 'panel_source_settings_version'):
        assert str(PANEL_TABLES[name].c.saved_at.server_default.arg) == 'now()'


def test_seq_is_an_app_assigned_not_null_integer_unique_per_pass():
    """The column seq is INTEGER NOT NULL, the 1-based append ordinal within its pass (ruling 2026-10-30 on tj-7zjov1.177).

    Not an identity or a sequence and no default: append_transitions assigns it. Re-homed from test_panel_revision.py
    (ADR tj-kg521d), model half.
    """
    assert PanelMemberTransition.APPEND_ORDINAL_UNIQUE == 'uq_panel_member_transition_pass_id_seq'
    table = PANEL_TABLES['panel_member_transition']
    seq = table.c.seq
    assert type(seq.type) is sa.Integer, seq.type
    assert not seq.nullable and seq.server_default is None and seq.default is None
    assert seq.identity is None and seq.computed is None, 'seq is app-assigned, never generated'
    assert not seq.primary_key
    (unique,) = [
        c for c in table.constraints if isinstance(c, sa.UniqueConstraint) and 'seq' in {col.name for col in c.columns}
    ]
    assert (unique.name, [col.name for col in unique.columns]) == (
        PanelMemberTransition.APPEND_ORDINAL_UNIQUE,
        ['pass_id', 'seq'],
    )


def test_the_panel_foreign_keys_run_only_inside_the_panel_tables_and_never_cascade():
    """No foreign key from the ledger to a dataset, a request or a snapshot (addendum 20), and no cascade.

    Re-homed from test_panel_revision.py (ADR tj-kg521d), model half.
    """
    triples = {
        (table.name, fk.parent.name, fk.target_fullname) for table in PANEL_TABLES.values() for fk in table.foreign_keys
    }
    assert triples == PANEL_DESIGN_FOREIGN_KEYS
    for table in PANEL_TABLES.values():
        for fk in table.foreign_keys:
            assert fk.ondelete is None and fk.onupdate is None, f'{table.name}.{fk.parent.name}'
            assert fk.column.table.name not in PANEL_NEVER_REFERENCED


def test_the_panel_design_uniques_indexes_and_checks_hold():
    """Uniqueness, the reads' indexes and the request-rate index, and the CHECKs by name and text (no size CHECK).

    Re-homed from test_panel_revision.py (ADR tj-kg521d), model half, updated for ADR tj-46jkey.
    """
    uniques = {
        (name, c.name, tuple(col.name for col in c.columns))
        for name, table in PANEL_TABLES.items()
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert uniques == PANEL_DESIGN_UNIQUES
    checks = {
        (name, c.name, _normalised(str(c.sqltext)))
        for name, table in PANEL_TABLES.items()
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    assert checks == PANEL_DESIGN_CHECKS
    declared = {
        (index.table.name, tuple(c.name for c in index.columns), bool(index.unique))
        for table in PANEL_TABLES.values()
        for index in table.indexes
    }
    assert declared >= PANEL_DESIGN_INDEXES
    (rate,) = [index for index in REQUEST.indexes if index.name == 'ix_ingest_request_source_created_at']
    assert [c.name for c in rate.columns] == ['source', 'created_at'] and not rate.unique


def test_the_one_update_of_a_panel_table_is_the_guarded_finish_pass():
    """APPEND-ONLY with one ruled exception, read from the source of every production importer of the panel models.

    One .update() in all of them, on panel_pass, in finish_pass, whose WHERE carries finished_at IS NULL and whose
    values are finished_at and counts alone; no .delete() anywhere; and no raw SQL mutates a panel table. Re-homed from
    test_panel_revision.py (ADR tj-kg521d); the importer set is derived here, not pinned.
    """
    raw = re.compile(r'\b(UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?"?panel(_[a-z_]+)?\b', re.I)
    for path in _production_sources():
        assert not raw.search(path.read_text(encoding='utf-8')), f'{path} updates or deletes a panel table in raw SQL'
    importers = _importers('panel')
    assert 'data/store/app/database/crud/panel_ledger.py' in importers, importers
    found = []
    finish = None
    for relative in sorted(importers):
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
