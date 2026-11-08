"""What can be proved about the idempotent bar write without a database.

Whether a second identical write actually leaves the row count unchanged is a property of
Postgres, not of this code, and proving it needs a real server with the migrations applied -
that tier is tj-6bvoic. What these tests do prove is that the three artifacts that have to
agree for idempotency to hold actually agree: the model's unique constraint, the ON CONFLICT
clause in the repository, and the constraint the migration creates. Drift between those three
is silent - the write keeps working and simply stops being idempotent - so it is worth pinning.

Two tiers here, and the difference matters:
  * The model and the repository are inspected directly - the constraint object, and the SQL
    the upsert compiles to.
  * The migration is RUN. Since ADR tj-kg521d it is ONE revision, the alpha baseline, which creates
    the bar table and its natural key from empty; alembic.op is patched with a recording stand-in
    (data/store/tests/alpha_baseline.py), upgrade() executes with no database and no docker, and the
    table it creates is asserted on. Asserting on the revision's constants alone would pass a body
    that created something else.

The old chain's archive and row-move tests (a de-duplicating revision, its downgrade restoring the
archive, the policy-column backfill of the identity revision) went with that chain: the baseline
creates the key on an empty table, so there is nothing to de-duplicate, archive or backfill, and no
downgrade (owner ruling 2026-11-06).
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from data.store.app.database.crud.stock.asset_market_activity import build_market_activity_upsert
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.tests.alpha_baseline import record_upgrade


# Columns the upsert is allowed to leave alone: the surrogate key, the natural key itself,
# and the two timestamps the write path manages directly.
NOT_REFRESHED = {'id', 'created_at', 'updated_at', *BaseMarketActivity.NATURAL_KEY}


@pytest.fixture
def bar_values() -> list[dict[str, Any]]:
    """One row's worth of insert values, in the bar's column shape.

    Only the column NAMES matter here -- the values are bound parameters and never reach a
    server -- so the natural-key columns are left None. feed is present and split_factor,
    dividends_factor and expiry are absent because SQLAlchemy raises CompileError on a key the
    table does not have, which is what this fixture did until tj-1njw7c.
    """
    return [
        {
            'dataset_id': None,
            'source': None,
            'asset_symbol': 'AAPL',
            'feed': None,
            'granularity': None,
            'timestamp': None,
            'open': 1.0,
            'high': 2.0,
            'low': 0.5,
            'close': 1.5,
            'volume': 100,
            'trade_count': 10,
        }
    ]


@pytest.fixture
def compiled_upsert(bar_values: list[dict[str, Any]]) -> str:
    return str(build_market_activity_upsert(bar_values).compile(dialect=postgresql.dialect()))


@pytest.fixture(scope='module')
def baseline_bar() -> sa.Table:
    """The bar table exactly as the alpha baseline's upgrade() creates it."""
    return record_upgrade().metadata.tables[StockMarketActivity.TABLE_NAME]


def test_natural_key_is_unique_and_leads_with_dataset_id():
    """The decision this whole task turns on, pinned so a later change is a deliberate one.

    INVERTED in tj-vhboky.1, name and all (the repair is tj-1njw7c). This test used to assert
    dataset_id was EXCLUDED, on the reasoning that two fetches of the same minute through
    different dataset entries are the same bar. Coverage is per-dataset now, so they are
    deliberately two rows, and the old assertion is not a stale detail of this test -- it is the
    previous design stated in a name.

    LEADS, not merely contains. Column ORDER inside a unique constraint decides what the backing
    index can serve: dataset_id first gives a dataset-scoped range read its equality prefix and an
    ordered timestamp, which is why base_market_activity.py drops the standalone dataset_id index.
    A key holding the same six columns in another order satisfies `sorted(...)` below and quietly
    costs that read.
    """
    unique = [
        c for c in StockMarketActivity.__table__.constraints if c.name == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    ]
    assert len(unique) == 1, 'the natural-key unique constraint is missing from the model'
    columns = tuple(column.name for column in unique[0].columns)
    assert sorted(columns) == sorted(BaseMarketActivity.NATURAL_KEY)
    assert columns[0] == 'dataset_id', f'dataset_id no longer leads the natural key: {columns}'
    assert 'feed' in columns, 'feed has left the natural key'


def test_upsert_targets_the_named_constraint(compiled_upsert: str):
    """ON CONFLICT by constraint name, so model and migration cannot drift apart unnoticed."""
    assert f'ON CONFLICT ON CONSTRAINT {StockMarketActivity.NATURAL_KEY_CONSTRAINT} DO UPDATE' in compiled_upsert


def test_upsert_refreshes_every_correctable_column(compiled_upsert: str):
    """DO UPDATE, not DO NOTHING: a vendor correction has to reach every column that can carry one.

    A column added to the model but not to MUTABLE_COLUMNS would leave the stored bar stale
    after a correction, and nothing else would complain.
    """
    correctable = {column.name for column in StockMarketActivity.__table__.columns} - NOT_REFRESHED
    assert set(StockMarketActivity.MUTABLE_COLUMNS) == correctable
    for column in correctable:
        assert f'{column} = excluded.{column}' in compiled_upsert


def test_upsert_stamps_updated_at_but_not_created_at(compiled_upsert: str):
    """ON CONFLICT DO UPDATE does not exercise a column's Python-side onupdate.

    updated_at has to be set in the SET clause explicitly or a refreshed bar keeps the
    timestamp of its first write. created_at must not be, it records first storage.
    """
    conflict_clause = compiled_upsert.split('DO UPDATE SET')[1]
    assert 'updated_at = now()' in conflict_clause
    assert 'created_at' not in conflict_clause


def test_the_baseline_creates_the_natural_key_the_upsert_names_in_the_models_order(baseline_bar: sa.Table):
    """The migration third of the agreement: the constraint the ON CONFLICT names exists, with the model's columns.

    By the MODEL'S name and in the MODEL'S column order (dataset_id leading, for the reason in the first test), on the
    revision's own table object, so a baseline that named the key differently, dropped a column from it or reordered
    it turns this red even though the upsert itself still compiles.
    """
    keys = [
        constraint
        for constraint in baseline_bar.constraints
        if isinstance(constraint, sa.UniqueConstraint) and constraint.name == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    ]
    assert len(keys) == 1, f'the baseline does not create {StockMarketActivity.NATURAL_KEY_CONSTRAINT} once: {keys}'
    model_key = next(
        c for c in StockMarketActivity.__table__.constraints if c.name == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    )
    assert [column.name for column in keys[0].columns] == [column.name for column in model_key.columns]


def test_the_baseline_bar_has_no_other_unique_key_an_upsert_could_trip_on(baseline_bar: sa.Table):
    """Exactly one unique key beside the primary key: a second one would make a correction raise, not update."""
    uniques = [c.name for c in baseline_bar.constraints if isinstance(c, sa.UniqueConstraint)]
    unique_indexes = [index.name for index in baseline_bar.indexes if index.unique]
    assert uniques == [StockMarketActivity.NATURAL_KEY_CONSTRAINT] and unique_indexes == [], (uniques, unique_indexes)
