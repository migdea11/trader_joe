"""The schema and its constraints, against the real, migrated Postgres (tj-vhboky.49, Sys-2).

WHAT THIS REPLACES on the host checklist tj-vhboky.14 (numbering from its preserved original and
amendments):
  * item 1, the tail: alembic_version holds exactly the head of the revision graph read from
    data/store/migrations -- not a revision id typed here;
  * item 3: a bar inserted twice on the same (dataset_id, asset_symbol, source, feed,
    granularity, timestamp) is refused with SQLSTATE 23505 on uq_stock_market_activity_natural_key,
    the name read from StockMarketActivity, not typed twice;
  * item 4a (from the tj-ap3he4 gate): an explicit NULL owner is refused with 23502 naming owner,
    and only an OMITTED owner takes the server default 'unassigned';
  * the entry identity constraint, feed enum and bar column items that tj-vhboky.14 routes here.
Item 4b (the NULLS DISTINCT premise) was dropped on tj-vhboky.14 and is not here.

IT CLOSES the "property of Postgres" concession in
data/store/tests/test_market_activity_idempotency.py: that module proves the model's constraint,
the ON CONFLICT clause and the migration agree, and says that whether a repeat write actually
leaves the row count unchanged needs a real server. test_bar_upsert_repeat_keeps_one_row_and_
refreshes_ohlcv runs the repository's own build_market_activity_upsert twice against one here.

tj-6bvoic's "write a row, read it back" is satisfied by these writes:
test_entry_and_bar_round_trip reads back an entry and a bar and compares every value written,
and every other test here commits real rows through the same path.

THE ENTRY IDENTITY ITEM, AND ONE DISCREPANCY WITH THE BEAD. tj-vhboky.49 asks for "two rows
identical except one identity field each (expiry_type, feed, owner)". store_dataset_entry has NO
feed column: tj-rh4b7f deferred feed on the entry to the gRPC transport work, and
StoreDatasetEntry's comment says so. So the parametrized test below varies EVERY column of
StoreDatasetEntry.NATURAL_KEY -- expiry_type and owner among them -- and feed's absence from the
entry is pinned by test_entry_live_columns_equal_the_model instead. Reported to the architect on
the bead rather than resolved here.

EVERYTHING EXPECTED IS READ FROM THE CODE: constraint names and column lists from the models,
the feed labels from common's Feed, the head from alembic's ScriptDirectory, the owner default
from the model's server_default. The SQLSTATEs are the two literals the bead specifies.

The suite's rules (fail never skip, per-run owner and symbol, cleanup by cascade, no truncation,
no migration driven from here) live in conftest.py.
"""

from collections.abc import Callable
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

from common.enums.data_stock import ExpiryType, Feed, UpdateType
from data.store.app.database.crud.stock.asset_market_activity import build_market_activity_upsert
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


pytestmark = pytest.mark.data_store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / 'data' / 'store' / 'migrations'

ENTRY_TABLE = StoreDatasetEntry.__table__
BAR_TABLE = StockMarketActivity.__table__

SQLSTATE_UNIQUE_VIOLATION = '23505'
SQLSTATE_NOT_NULL_VIOLATION = '23502'

# Columns revision eec8f88a7443 dropped from the bar. Named here because the point of the check
# is that they are ABSENT, so there is no model attribute left to read them from.
DROPPED_BAR_COLUMNS = ('expiry', 'split_factor', 'dividends_factor')


# ---------------------------------------------------------------------------------------------
# Catalogue helpers.


def _live_columns(conn: Connection, table: str) -> set[str]:
    rows = conn.execute(
        sa.text(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = :table'
        ),
        {'table': table},
    ).scalars()
    return set(rows)


def _unique_constraint_columns(conn: Connection, table: str, constraint: str) -> list[str]:
    """The live constraint's columns, in key order, from pg_constraint and pg_attribute."""
    rows = conn.execute(
        sa.text(
            """
            SELECT a.attname
              FROM pg_constraint c
             CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
             WHERE c.conname = :constraint
               AND c.conrelid = CAST(:table AS regclass)
               AND c.contype = 'u'
             ORDER BY k.ord
            """
        ),
        {'table': table, 'constraint': constraint},
    ).scalars()
    return list(rows)


def _assert_refused(error: sa.exc.DBAPIError | None, sqlstate: str) -> Any:
    """The statement was refused by Postgres with this SQLSTATE; return psycopg2's diagnostics."""
    assert error is not None, f'expected SQLSTATE {sqlstate}, but the statement COMMITTED'
    assert isinstance(error, sa.exc.IntegrityError), f'expected an integrity error, got {error!r}'
    assert error.orig.pgcode == sqlstate, f'SQLSTATE {error.orig.pgcode}, expected {sqlstate}: {error.orig}'
    return error.orig.diag


def _other_member(member: Enum) -> Enum:
    """Any member of the same enum other than this one."""
    return next(candidate for candidate in type(member) if candidate is not member)


def _other_int_member(enum_cls: type[Enum], value: int) -> int:
    return next(candidate.value for candidate in enum_cls if candidate.value != value)


def _count_bars(conn: Connection, dataset_id: Any) -> int:
    return conn.execute(
        sa.select(sa.func.count()).select_from(BAR_TABLE).where(BAR_TABLE.c.dataset_id == dataset_id)
    ).scalar_one()


# ---------------------------------------------------------------------------------------------
# Item 1 (tail): the head revision is applied.


def test_alembic_version_holds_exactly_the_head_of_the_revision_graph(pg_conn: Connection) -> None:
    heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    assert len(heads) == 1, f'the revision graph in {MIGRATIONS_DIR} has {len(heads)} heads: {heads}'

    applied = pg_conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalars().all()

    assert applied == heads, f'alembic_version holds {applied}; the graph head is {heads[0]}'


# ---------------------------------------------------------------------------------------------
# tj-6bvoic: write a row, read it back.


def test_entry_and_bar_round_trip(
    pg_conn: Connection,
    pg_engine: Engine,
    insert_entry: Callable[..., sa.Row],
    entry_values: Callable[..., dict[str, Any]],
    bar_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    written_entry = entry_values(asset_symbol=own_symbol)
    entry = insert_entry(asset_symbol=own_symbol)
    written_bar = bar_values(entry)
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE).values(written_bar))

    read_entry = pg_conn.execute(sa.select(ENTRY_TABLE).where(ENTRY_TABLE.c.id == entry.id)).one()._mapping
    read_bar = pg_conn.execute(sa.select(BAR_TABLE).where(BAR_TABLE.c.dataset_id == entry.id)).one()._mapping

    assert {column: read_entry[column] for column in written_entry} == written_entry
    assert {column: read_bar[column] for column in written_bar} == written_bar


# ---------------------------------------------------------------------------------------------
# Item 3 and its controls: the bar's natural key.


def test_bar_exact_duplicate_is_refused_on_the_natural_key_constraint(
    pg_conn: Connection,
    pg_engine: Engine,
    attempt: Callable[[sa.Executable], sa.exc.DBAPIError | None],
    insert_entry: Callable[..., sa.Row],
    bar_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    entry = insert_entry(asset_symbol=own_symbol)
    bar = bar_values(entry)
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE).values(bar))

    diag = _assert_refused(attempt(sa.insert(BAR_TABLE).values(bar)), SQLSTATE_UNIQUE_VIOLATION)

    assert diag.constraint_name == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    assert _count_bars(pg_conn, entry.id) == 1


# How to make a bar differ from the base bar in exactly one natural-key column. Every column of
# BaseMarketActivity.NATURAL_KEY must have an entry; test_every_bar_key_column_has_a_variant
# fails if a column is added to the key without one here.
BAR_KEY_VARIANTS: dict[str, Callable[[dict[str, Any], sa.Row, Callable[..., sa.Row]], Any]] = {
    # A second entry of this run, differing from the first only in owner, so the bar's other
    # key columns still agree with the entry it belongs to.
    'dataset_id': lambda base, entry, insert_entry: (
        insert_entry(asset_symbol=entry.asset_symbol, owner=f'{entry.owner}-second-entry').id
    ),
    'asset_symbol': lambda base, entry, insert_entry: f'{base["asset_symbol"]}X',
    'source': lambda base, entry, insert_entry: _other_member(base['source']),
    'feed': lambda base, entry, insert_entry: _other_member(base['feed']),
    'granularity': lambda base, entry, insert_entry: _other_member(base['granularity']),
    'timestamp': lambda base, entry, insert_entry: base['timestamp'] + timedelta(minutes=1),
}


def test_every_bar_key_column_has_a_variant() -> None:
    assert set(BAR_KEY_VARIANTS) == set(BaseMarketActivity.NATURAL_KEY)


@pytest.mark.parametrize('column', BaseMarketActivity.NATURAL_KEY)
def test_bar_differing_in_one_natural_key_column_is_accepted(
    column: str,
    pg_conn: Connection,
    pg_engine: Engine,
    attempt: Callable[[sa.Executable], sa.exc.DBAPIError | None],
    insert_entry: Callable[..., sa.Row],
    bar_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    """The control for item 3: the refusal is the key, not a blanket failure to insert a second bar."""
    entry = insert_entry(asset_symbol=own_symbol)
    base = bar_values(entry)
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE).values(base))

    variant_value = BAR_KEY_VARIANTS[column](base, entry, insert_entry)
    assert variant_value != base[column]
    variant = {**base, column: variant_value}

    assert attempt(sa.insert(BAR_TABLE).values(variant)) is None, f'a bar differing only in {column} was refused'
    both_entries = {base['dataset_id'], variant['dataset_id']}
    stored = pg_conn.execute(
        sa.select(sa.func.count()).select_from(BAR_TABLE).where(BAR_TABLE.c.dataset_id.in_(both_entries))
    ).scalar_one()
    assert stored == 2


def test_bar_upsert_repeat_keeps_one_row_and_refreshes_ohlcv(
    pg_conn: Connection,
    pg_engine: Engine,
    insert_entry: Callable[..., sa.Row],
    bar_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    """Closes test_market_activity_idempotency.py's concession: the repeat really is idempotent."""
    entry = insert_entry(asset_symbol=own_symbol)
    first = bar_values(entry)
    correction = {**first, 'close': first['close'] + 1.5, 'volume': first['volume'] + 7}

    with pg_engine.begin() as conn:
        conn.execute(build_market_activity_upsert([first]))
    first_id = pg_conn.execute(sa.select(BAR_TABLE.c.id).where(BAR_TABLE.c.dataset_id == entry.id)).scalar_one()
    with pg_engine.begin() as conn:
        conn.execute(build_market_activity_upsert([correction]))

    rows = pg_conn.execute(sa.select(BAR_TABLE).where(BAR_TABLE.c.dataset_id == entry.id)).all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert (rows[0].close, rows[0].volume) == (correction['close'], correction['volume'])


# ---------------------------------------------------------------------------------------------
# Item 4a: owner is NOT NULL on the real database, and its default applies only when omitted.


def test_entry_explicit_null_owner_is_refused_naming_owner(
    pg_engine: Engine,
    attempt: Callable[[sa.Executable], sa.exc.DBAPIError | None],
    entry_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    statement = sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol=own_symbol, owner=None))
    # The NULL must actually be sent: a statement that left owner out would take the default and
    # prove nothing about an explicit NULL.
    compiled = statement.compile(dialect=pg_engine.dialect)
    assert 'owner' in compiled.params and compiled.params['owner'] is None

    error = attempt(statement)
    if error is None:
        # Remove the row this test just proved should not exist, so the cleanup check is not
        # what reports the defect.
        with pg_engine.begin() as conn:
            conn.execute(
                sa.delete(ENTRY_TABLE).where(ENTRY_TABLE.c.asset_symbol == own_symbol, ENTRY_TABLE.c.owner.is_(None))
            )
        pytest.fail('an explicit NULL owner COMMITTED: owner is nullable on this database -- a migration defect')

    diag = _assert_refused(error, SQLSTATE_NOT_NULL_VIOLATION)
    assert diag.column_name == 'owner'


def test_entry_omitted_owner_takes_the_server_default(insert_entry: Callable[..., sa.Row], own_symbol: str) -> None:
    server_default = ENTRY_TABLE.c.owner.server_default.arg
    assert isinstance(server_default, str) and server_default

    entry = insert_entry(asset_symbol=own_symbol, omit=('owner',))

    assert entry.owner == server_default


# ---------------------------------------------------------------------------------------------
# The entry identity constraint.


def test_entry_exact_duplicate_is_refused_on_the_identity_constraint(
    attempt: Callable[[sa.Executable], sa.exc.DBAPIError | None],
    insert_entry: Callable[..., sa.Row],
    entry_values: Callable[..., dict[str, Any]],
    own_symbol: str,
) -> None:
    insert_entry(asset_symbol=own_symbol)

    diag = _assert_refused(
        attempt(sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol=own_symbol))), SQLSTATE_UNIQUE_VIOLATION
    )

    assert diag.constraint_name == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT


# How to make an entry differ from the base entry in exactly one identity column.
ENTRY_KEY_VARIANTS: dict[str, Callable[[dict[str, Any]], Any]] = {
    'asset_symbol': lambda base: f'{base["asset_symbol"]}X',
    'source': lambda base: _other_member(base['source']),
    'granularity': lambda base: _other_member(base['granularity']),
    'asset_type': lambda base: _other_member(base['asset_type']),
    'data_type': lambda base: _other_member(base['data_type']),
    'owner': lambda base: f'{base["owner"]}-variant',
    'expiry_type': lambda base: _other_int_member(ExpiryType, base['expiry_type']),
    'update_type': lambda base: _other_int_member(UpdateType, base['update_type']),
    'start': lambda base: base['start'] - timedelta(days=1),
    'end': lambda base: base['end'] + timedelta(days=1),
}


def test_every_entry_identity_column_has_a_variant() -> None:
    assert set(ENTRY_KEY_VARIANTS) == set(StoreDatasetEntry.NATURAL_KEY)


@pytest.mark.parametrize('column', StoreDatasetEntry.NATURAL_KEY)
def test_entry_differing_in_one_identity_column_is_accepted(
    column: str, insert_entry: Callable[..., sa.Row], entry_values: Callable[..., dict[str, Any]], own_symbol: str
) -> None:
    base_values = entry_values(asset_symbol=own_symbol)
    base = insert_entry(asset_symbol=own_symbol)

    variant_value = ENTRY_KEY_VARIANTS[column](base_values)
    assert variant_value != base_values[column]
    variant = insert_entry(**{**base_values, column: variant_value})

    assert variant.id != base.id


# ---------------------------------------------------------------------------------------------
# Shapes: the feed type, the bar's columns, both constraints' column lists.


def test_feed_type_carries_exactly_the_members_of_common_feed(pg_conn: Connection) -> None:
    feed_type = BAR_TABLE.c.feed.type
    labels = (
        pg_conn.execute(
            sa.text(
                'SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid '
                'WHERE t.typname = :name ORDER BY e.enumsortorder'
            ),
            {'name': feed_type.name},
        )
        .scalars()
        .all()
    )
    column_type = pg_conn.execute(
        sa.text(
            'SELECT udt_name FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column'
        ),
        {'table': BAR_TABLE.name, 'column': 'feed'},
    ).scalar_one()

    assert column_type == feed_type.name
    assert sorted(labels) == sorted(member.value for member in Feed)


def test_bar_live_columns_equal_the_model_without_the_dropped_ones(pg_conn: Connection) -> None:
    live = _live_columns(pg_conn, BAR_TABLE.name)

    assert not live & set(DROPPED_BAR_COLUMNS)
    assert live == set(BAR_TABLE.c.keys())


def test_entry_live_columns_equal_the_model(pg_conn: Connection) -> None:
    """Includes feed's ABSENCE from the entry (tj-rh4b7f): the model declares no feed column."""
    assert _live_columns(pg_conn, ENTRY_TABLE.name) == set(ENTRY_TABLE.c.keys())


def test_bar_natural_key_constraint_is_the_model_key_with_dataset_id_leading(pg_conn: Connection) -> None:
    columns = _unique_constraint_columns(pg_conn, BAR_TABLE.name, StockMarketActivity.NATURAL_KEY_CONSTRAINT)

    assert columns == list(BaseMarketActivity.NATURAL_KEY)
    assert columns[0] == 'dataset_id'


def test_entry_identity_constraint_is_the_model_key(pg_conn: Connection) -> None:
    columns = _unique_constraint_columns(pg_conn, ENTRY_TABLE.name, StoreDatasetEntry.NATURAL_KEY_CONSTRAINT)

    assert columns == list(StoreDatasetEntry.NATURAL_KEY)
