"""The dataset entry's owner is redacted where the store renders it (tj-vhboky.46).

DESIGN: tj-vhboky.41 Addendum 1, D3 -- M1 (the SensitiveString bind type) applied to
StoreDatasetEntry.owner, M3 (the ORM repr), and the search_entries filter log line that renders the
value directly. The field list is owner only (tj-vhboky.45). Keep detail, redact the sensitive
value: every pin below also asserts that a NON-sensitive neighbour still renders, because the user
ruled against blanket hiding and a test that only checked absence would pass for hide_parameters.

WHAT EACH GROUP PINS (the bead's validator stage, items 1-5, plus two additions):
1. Every statement that binds owner -- the upsert's own-overlap SELECT and INSERT ... ON CONFLICT,
   update_entry's exact-collision SELECT, the search owner filter -- binds it as a RedactedStr
   through the postgresql+asyncpg dialect's own bind processors, and a DBAPIError built on the
   processed parameters renders the marker and still renders asset_symbol.
2. StoreDatasetEntry's repr carries the marker and not the owner; and (architect, 04:48 UTC) an
   entry built with a plain owner holds exactly a str, so the wrapper never reaches an ORM
   attribute -- a move onto the house CustomColumn mechanism would red here behaviourally.
3. write_transaction's single ERROR record, formatted by a real logging.Formatter so the traceback
   text is what is searched, carries the marker and a non-sensitive parameter, never the owner.
4. search_entries' filter debug log: the marker for owner, the value for a non-sensitive filter.
5. upsert_entry's debug log of the create request never contains the owner (tj-w6bpjm M2 at
   this call site).
6. The model's owner DDL is the head revision's, so SensitiveString needs no migration.

WHAT TIER THIS IS. No database. The crud functions run against a recording fake session (the style
of test_dataset_entry_identity.py) and the statements they hand it are compiled and bind-processed
exactly as the asyncpg execution context would. Whether asyncpg STORES a RedactedStr as the plain
value is tests/system/test_asyncpg_bind_spike.py's question (host run, tj-vhboky.14), not this file's.
"""

import importlib.util
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect
from sqlalchemy.schema import CreateColumn

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from common.sensitive import REDACTED, RedactedStr
from data.store.app.database.crud.stock import store_dataset_entry as crud
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStoreCreate,
    AssetDatasetStoreUpdate,
    StoreAssetDatasetPath,
    StoreAssetDatasetQuery,
)


pytestmark = pytest.mark.data_store

OWNER = 'owner-7f3a-must-not-render'
SYMBOL = 'ZXQW'
JANUARY = datetime(2026, 1, 1, tzinfo=UTC)
FEBRUARY = datetime(2026, 2, 1, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, tzinfo=UTC)
APRIL = datetime(2026, 4, 1, tzinfo=UTC)

_CRUD_LOGGER = crud.__name__
_TRANSACTION_LOGGER = 'data.store.app.database.transaction'


# ---------------------------------------------------------------------------------------------
# Fakes and builders
# ---------------------------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.rowcount = len(rows)

    def all(self) -> list[tuple]:
        return self._rows

    def first(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0][0]

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class _Session:
    """Records every statement; answers from `results` in order.

    With `fail_at`, the statement at that index raises the DBAPIError SQLAlchemy's engine would
    build for it -- from the statement's asyncpg-processed parameters, which is what
    _handle_dbapi_exception hands the error -- so the error text is the real rendering, not a string
    this test wrote.

    `classifiable` CHOOSES WHICH BRANCH OF write_transaction THE CASE EXERCISES (validator, gating
    tj-3mk3u5.37.8), and the choice used to be implicit. The default builds a plain DBAPIError with
    no SQLSTATE, which `_reason_for` cannot classify, so the helper re-raises it unchanged -- the
    BUG branch, which is what every case in this file reached before TE-6 and still reaches. True
    builds an OperationalError over the same real rendering, which the helper converts into a typed
    ExogenousError -- a branch that emits a DIFFERENT ERROR line and, for the first time, puts a
    message on the WIRE. This file's subject is that the owner reaches neither, so it needs both.
    """

    def __init__(self, *results: _Result, fail_at: int | None = None, classifiable: bool = False):
        self._results = list(results)
        self._fail_at = fail_at
        self._classifiable = classifiable
        self.statements: list = []
        self.error: sa.exc.DBAPIError | None = None

    async def execute(self, statement):
        index = len(self.statements)
        self.statements.append(statement)
        if index == self._fail_at:
            sql, parameters = _processed(statement)
            if self._classifiable:
                self.error = sa.exc.OperationalError(sql, parameters, Exception('server closed the connection'))
            else:
                self.error = sa.exc.DBAPIError.instance(sql, parameters, Exception('driver failure'), Exception)
            raise self.error
        assert self._results, 'the crud path issued more statements than the fixture planned for'
        return self._results.pop(0)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _processed(statement) -> tuple[str, tuple]:
    """Compile for postgresql+asyncpg and run each bound value through its type's dialect bind processor.

    The resulting tuple is what the execution context hands the DBAPI cursor, and so exactly what
    SQLAlchemy renders as "[parameters: ...]" when the cursor raises.
    """
    dialect = asyncpg_dialect.dialect()
    compiled = statement.compile(dialect=dialect)
    values = compiled.construct_params()
    processed = []
    for name in compiled.positiontup:
        processor = compiled.binds[name].type.dialect_impl(dialect).bind_processor(dialect)
        processed.append(processor(values[name]) if processor else values[name])
    return str(compiled), tuple(processed)


def _create() -> AssetDatasetStoreCreate:
    return AssetDatasetStoreCreate(
        owner=OWNER,
        asset_symbol=SYMBOL,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        # The RESOLVED tape (tj-3mk3u5.31). Required on this model, so there is nothing to omit;
        # which member it is does not matter to anything in this file, since feed is not sensitive
        # and binds as an ordinary enum.
        feed=Feed.IEX,
        start=JANUARY,
        end=None,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )


def _update(entry_id: UUID) -> AssetDatasetStoreUpdate:
    return AssetDatasetStoreUpdate(**_create().model_dump(exclude={'end'}), id=entry_id, end=APRIL)


def _stored(entry_id: UUID, owner: str = OWNER) -> StoreDatasetEntry:
    return StoreDatasetEntry(
        id=entry_id,
        owner=owner,
        asset_symbol=SYMBOL,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        feed=Feed.IEX,
        start=JANUARY,
        end=MARCH,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
        created_at=JANUARY,
        updated_at=JANUARY,
    )


def _path() -> StoreAssetDatasetPath:
    return StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol=SYMBOL)


# Every statement that binds owner: (write, statement) -> (the call, the answers to every
# statement BEFORE it). The statement's index is the length of that list. _get_entry_or_raise binds
# only the id, so it is not here (bead pin 1).
_OWNER_BINDING_STATEMENTS = {
    ('upsert_entry', 'own-overlap select'): (lambda db, entry_id: crud.upsert_entry(db, _create()), lambda e: []),
    ('upsert_entry', 'insert on conflict'): (
        lambda db, entry_id: crud.upsert_entry(db, _create()),
        lambda e: [_Result([])],
    ),
    ('update_entry', 'exact-collision select'): (
        lambda db, entry_id: crud.update_entry(db, _update(entry_id)),
        lambda e: [_Result([(_stored(e),)])],
    ),
    ('search_entries', 'owner filter'): (
        lambda db, entry_id: crud.search_entries(db, _path(), StoreAssetDatasetQuery(owner=OWNER)),
        lambda e: [],
    ),
}

# The ones inside write_transaction (pin 3); search_entries is a read and has no helper around it.
_WRITE_CASES = [case for case in _OWNER_BINDING_STATEMENTS if case[0] != 'search_entries']


async def _statement_binding_owner(case: tuple[str, str]):
    """Drive the crud call and return the statement at the case's index, captured on the way to the driver."""
    call, before = _OWNER_BINDING_STATEMENTS[case]
    entry_id = uuid4()
    answers = before(entry_id)
    db = _Session(*answers, fail_at=len(answers))
    with pytest.raises(sa.exc.DBAPIError):
        await call(db, entry_id)
    return db.statements[len(answers)]


# ---------------------------------------------------------------------------------------------
# Pin 1: every statement binding owner binds a RedactedStr, and its error renders the marker
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('case', list(_OWNER_BINDING_STATEMENTS), ids=lambda c: f'{c[0]}-{c[1]}')
async def test_every_statement_binding_owner_hands_the_driver_a_redacted_str(case: tuple[str, str]):
    _, parameters = _processed(await _statement_binding_owner(case))

    owners = [p for p in parameters if isinstance(p, str) and str.__eq__(p, OWNER)]
    assert owners, f'{case} does not bind the owner, so this case is in the wrong table'
    assert all(type(p) is RedactedStr for p in owners), f'{case} binds the owner as {[type(p) for p in owners]}'
    # The driver still gets the real characters.
    assert all(str.__str__(p) == OWNER for p in owners)
    # And a non-sensitive neighbour is bound as itself.
    assert [type(p) for p in parameters if isinstance(p, str) and str.__eq__(p, SYMBOL)] == [str]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', list(_OWNER_BINDING_STATEMENTS), ids=lambda c: f'{c[0]}-{c[1]}')
async def test_a_database_error_on_a_statement_binding_owner_renders_the_marker_and_the_symbol(case: tuple[str, str]):
    call, before = _OWNER_BINDING_STATEMENTS[case]
    entry_id = uuid4()
    answers = before(entry_id)
    db = _Session(*answers, fail_at=len(answers))

    with pytest.raises(sa.exc.DBAPIError) as raised:
        await call(db, entry_id)
    rendered = str(raised.value)

    assert '[parameters:' in rendered, 'the error must render its parameters, or the checks below are vacuous'
    assert REDACTED in rendered
    assert OWNER not in rendered
    assert repr(SYMBOL) in rendered, 'non-sensitive parameters must stay in the rendered detail'


# ---------------------------------------------------------------------------------------------
# Pin 2: the ORM repr, and the attribute type
# ---------------------------------------------------------------------------------------------


def test_the_entry_repr_carries_the_marker_and_not_the_owner():
    entry_id = uuid4()
    rendered = repr(_stored(entry_id))

    assert f"owner='{REDACTED}'" in rendered
    assert OWNER not in rendered
    # The other fields still render: redaction is of the owner, not of the row.
    assert f"id='{entry_id}'" in rendered
    assert f"symbol='{SYMBOL}'" in rendered
    assert f"source='{DataSource.ALPACA_API}'" in rendered


def test_an_entry_built_with_a_plain_owner_holds_exactly_a_str():
    """Architect, 04:48 UTC (from the tj-w6bpjm gate).

    SensitiveString is a plain TypeDecorator so the wrapper lives only in the parameters sent to the
    driver. The house CustomColumn mechanism converts values when the model is built; a move onto it
    would put a RedactedStr into the attribute, and every f'{entry.owner}' comparison and response
    body would then carry the subclass. This reds on that move whatever the type hierarchy says.
    """
    entry = _stored(uuid4())

    assert type(entry.owner) is str
    assert entry.owner == OWNER


# ---------------------------------------------------------------------------------------------
# Pin 3: write_transaction's ERROR record, formatted as a handler would write it
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('case', _WRITE_CASES, ids=lambda c: f'{c[0]}-{c[1]}')
async def test_the_write_error_record_formats_the_marker_and_a_neighbour_never_the_owner(
    case: tuple[str, str], caplog: pytest.LogCaptureFixture
):
    call, before = _OWNER_BINDING_STATEMENTS[case]
    entry_id = uuid4()
    answers = before(entry_id)
    db = _Session(*answers, fail_at=len(answers))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(sa.exc.DBAPIError):
        await call(db, entry_id)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f'expected one ERROR record, got {[r.getMessage() for r in errors]}'
    (record,) = errors
    assert record.name == _TRANSACTION_LOGGER
    assert record.exc_info is not None and record.exc_info[1] is db.error, 'the record must carry the raised error'

    text = logging.Formatter('%(levelname)s %(name)s %(message)s').format(record)

    assert '[parameters:' in text, 'the formatted traceback must include the parameters, or this is vacuous'
    assert REDACTED in text
    assert OWNER not in text
    assert repr(SYMBOL) in text


@pytest.mark.asyncio
@pytest.mark.parametrize('case', _WRITE_CASES, ids=lambda c: f'{c[0]}-{c[1]}')
async def test_a_classified_database_failure_redacts_the_owner_in_the_log_and_omits_it_from_the_answer(
    case: tuple[str, str], caplog: pytest.LogCaptureFixture
):
    """Pin 3's other branch, and the first time this file's subject reaches the WIRE.

    WHY IT IS NEW (validator, gating tj-3mk3u5.37.8). Every case in this file builds a DBAPIError
    with no SQLSTATE, which write_transaction cannot classify, so all of them take its BUG branch:
    re-raised unchanged, logged under ``{operation} failed: {class}, SQLSTATE {code}``. TE-6 added a
    second branch that none of them reaches -- a classifiable error is converted, logged under a
    DIFFERENT message that also names the reason and an error_id, and, unlike a bug, is RENDERED TO
    THE CALLER as a problem+json body. This file's whole subject is where the owner may appear, and
    a branch that produces a new log line and a new wire message is exactly where it would next
    appear. Nothing was asserting it.

    BOTH DIRECTIONS, as Pin 3 does. The log must still carry the redaction marker and the
    non-sensitive neighbour -- which is what keeps "OWNER not in text" from passing because nothing
    rendered at all -- and the ANSWER must carry neither the owner nor the symbol, because the
    detail on the converted error is a fixed sentence per reason and quotes no parameter of any
    kind. The symbol is the sharper half of that second check: it is not secret, so its absence can
    only mean the detail is not derived from the statement.

    Args:
        case: (the write, the statement whose execute raises).
        caplog: Captures the single ERROR record.
    """
    call, before = _OWNER_BINDING_STATEMENTS[case]
    entry_id = uuid4()
    answers = before(entry_id)
    db = _Session(*answers, fail_at=len(answers), classifiable=True)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ExogenousError) as raised:
        await call(db, entry_id)

    assert raised.value.reason is Reason.DATABASE_UNAVAILABLE, (
        f'the fixture must reach the CONVERSION branch for this case to mean anything; it reported '
        f'{raised.value.reason}'
    )
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    text = logging.Formatter('%(levelname)s %(name)s %(message)s').format(record)
    assert '[parameters:' in text, 'the formatted traceback must include the parameters, or this is vacuous'
    assert REDACTED in text, 'the owner is no longer redacted on the classified branch'
    assert OWNER not in text, 'the raw owner reached the ERROR record for a classified failure'
    assert repr(SYMBOL) in text, 'the non-sensitive neighbour is absent, so the check above may be vacuous'

    # THE WIRE. The detail is what a problem+json body prints, and it is the surface no case in this
    # file could see before TE-6.
    assert OWNER not in raised.value.detail, 'the owner reached the detail the caller is answered with'
    assert SYMBOL not in raised.value.detail, (
        'the asset symbol reached the detail, so the detail is being derived from the failing '
        'statement rather than being the fixed sentence per reason that D8 requires'
    )


# ---------------------------------------------------------------------------------------------
# Pin 4: search_entries' filter debug log
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_search_filter_log_renders_the_marker_for_owner_and_the_value_otherwise(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG, logger=_CRUD_LOGGER)
    db = _Session(_Result([]))

    await crud.search_entries(
        db, _path(), StoreAssetDatasetQuery(owner=OWNER, source=DataSource.ALPACA_API, granularity=Granularity.ONE_DAY)
    )

    messages = [r.getMessage() for r in caplog.records if r.name == _CRUD_LOGGER]
    assert f'Filtering by owner: {REDACTED}' in messages
    assert f'Filtering by source: {DataSource.ALPACA_API}' in messages
    assert f'Filtering by granularity: {Granularity.ONE_DAY}' in messages
    assert not [m for m in messages if OWNER in m], 'a crud log line rendered the owner'
    # The redaction is of the rendering only: the filter still binds the real owner.
    _, parameters = _processed(db.statements[0])
    assert OWNER in parameters


# ---------------------------------------------------------------------------------------------
# Pin 5: upsert_entry's debug log of the request
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_upsert_request_log_never_contains_the_owner(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    entry_id = uuid4()
    db = _Session(_Result([]), _Result([(entry_id,)]))

    assert await crud.upsert_entry(db, _create()) == entry_id

    (upserting,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Upserting entry:')]
    assert SYMBOL in upserting, 'the request log must still carry its non-sensitive detail'
    assert OWNER not in upserting
    assert not [r.getMessage() for r in caplog.records if OWNER in r.getMessage()]


# ---------------------------------------------------------------------------------------------
# DDL: the model's owner column is the one the head revision created, so no migration
# ---------------------------------------------------------------------------------------------

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations' / 'versions'
_HEAD_FILE = 'eec8f88a7443_per_dataset_identity_and_feed.py'


def _revision_owner_column() -> sa.Column:
    """The sa.Column the head revision's upgrade() passes to op.add_column for the entry's owner."""
    spec = importlib.util.spec_from_file_location('revision_eec8f88a7443_owner', _VERSIONS_DIR / _HEAD_FILE)
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.side_effect = ['looked_up_entry_constraint']
    recorder = MagicMock(name='op')
    recorder.get_bind.return_value = bind
    recorder.f.side_effect = lambda name: name
    with patch.object(module, 'op', recorder), patch.object(module, 'FEED_TYPE', MagicMock(name='FEED_TYPE')):
        module.upgrade()
    (added,) = [
        call.args[1]
        for call in recorder.add_column.call_args_list
        if call.args[0] == module.ENTRY_TABLE and call.args[1].name == 'owner'
    ]
    return added


def test_the_model_owner_ddl_is_the_head_revisions():
    """Relationship, not constant: the model's column spec compiled beside the revision's, on postgresql.

    SensitiveString's impl is String, so the migrated column and the model agree and no revision is
    needed. A length, a different impl or a changed default on the model would split them.
    """
    dialect = postgresql.dialect()
    model = str(CreateColumn(StoreDatasetEntry.__table__.c.owner).compile(dialect=dialect))
    revision_column = _revision_owner_column()
    sa.Table('store_dataset_entry_revision_probe', sa.MetaData(), revision_column)
    revision = str(CreateColumn(revision_column).compile(dialect=dialect))

    assert model == revision
    assert model == "owner VARCHAR DEFAULT 'unassigned' NOT NULL", 'the builder-reported DDL (tj-vhboky.46 notes)'
