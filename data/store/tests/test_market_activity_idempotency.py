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
  * The migration is RUN. alembic.op is a proxy onto a live migration context, so patching it
    lets upgrade() and downgrade() execute with no database and no docker, and the calls they
    make are asserted on. Asserting on the revision's module-level constants alone would pass
    a revision whose body did nothing at all, and the body is the part that will reach a real
    database with no runtime reviewing it.
"""

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from data.store.app.database.crud.stock.asset_market_activity import build_market_activity_upsert
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.stock_market_activity import StockMarketActivity


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
NATURAL_KEY_REVISION = '8f41c2d7a3b9'

# Columns the upsert is allowed to leave alone: the surrogate key, the natural key itself,
# and the two timestamps the write path manages directly.
NOT_REFRESHED = {'id', 'created_at', 'updated_at', *BaseMarketActivity.NATURAL_KEY}


@pytest.fixture
def bar_values() -> list[dict[str, Any]]:
    return [
        {
            'dataset_id': None,
            'source': None,
            'asset_symbol': 'AAPL',
            'granularity': None,
            'timestamp': None,
            'expiry': None,
            'open': 1.0,
            'high': 2.0,
            'low': 0.5,
            'close': 1.5,
            'volume': 100,
            'trade_count': 10,
            'split_factor': 1.0,
            'dividends_factor': 1.0,
        }
    ]


@pytest.fixture
def compiled_upsert(bar_values: list[dict[str, Any]]) -> str:
    return str(build_market_activity_upsert(bar_values).compile(dialect=postgresql.dialect()))


@pytest.fixture
def natural_key_migration():
    path = MIGRATIONS_DIR / 'versions' / f'{NATURAL_KEY_REVISION}_bar_natural_key_and_upsert.py'
    spec = importlib.util.spec_from_file_location(NATURAL_KEY_REVISION, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(migration: Any, direction: str, row_counts: list[int]) -> MagicMock:
    """Run a migration function against a recording stand-in for the alembic.op proxy.

    row_counts feeds the SELECT count(*) probes the migration makes through op.get_bind(), in
    the order it makes them: upgrade() asks for rows-before, rows-archived, rows-after;
    downgrade() asks for the rows in the archive before the restore, then the rows still left
    in it afterwards - which are exactly the ones that could not go back.
    """
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.side_effect = list(row_counts)
    recorder = MagicMock(name='op')
    recorder.get_bind.return_value = bind
    with patch.object(migration, 'op', recorder):
        getattr(migration, direction)()
    return recorder


def _statements(recorder: MagicMock) -> list[str]:
    """Every SQL string handed to op.execute, whitespace collapsed so it can be matched on."""
    return [' '.join(str(call.args[0]).split()) for call in recorder.execute.call_args_list]


def _call_sequence(recorder: MagicMock) -> list[tuple[str, str]]:
    """(op method, its first argument) in the order the migration called them."""
    return [(name, ' '.join(str(args[0]).split()) if args else '') for name, args, _ in recorder.method_calls]


def _index_of(sequence: list[tuple[str, str]], method: str, contains: str = '') -> int:
    return next(index for index, (name, argument) in enumerate(sequence) if name == method and contains in argument)


def _the_move(migration: Any, recorder: MagicMock) -> str:
    """The single statement that moves superseded rows into the archive."""
    archive = migration.ARCHIVE_TABLE
    moves = [statement for statement in _statements(recorder) if f'INSERT INTO {archive}' in statement]
    assert len(moves) == 1, f'expected exactly one statement moving rows into {archive}, got {len(moves)}'
    return moves[0]


def _the_restore(migration: Any, recorder: MagicMock) -> str:
    """The single statement that puts archived rows back into the live table."""
    table = migration.TABLE
    restores = [statement for statement in _statements(recorder) if f'INSERT INTO {table} SELECT' in statement]
    assert len(restores) == 1, f'downgrade does not put the archived rows back: {len(restores)} restore statements'
    return restores[0]


def _window_spec(statement: str) -> tuple[list[str], str]:
    """(partition columns, ordering) parsed out of the OVER (...) clause of the move statement.

    The window is the whole of the de-duplication decision: PARTITION BY says which rows count
    as the same bar, ORDER BY says which one of them survives. Both are asserted on exactly.
    """
    window = statement.split('PARTITION BY')[1].split(')')[0]
    partition, _, ordering = window.partition('ORDER BY')
    return [column.strip() for column in partition.split(',')], ' '.join(ordering.split())


@pytest.fixture
def upgraded(natural_key_migration) -> MagicMock:
    """upgrade() run over a dirty table: 4 rows in, 1 superseded duplicate, 3 left behind."""
    return _run_migration(natural_key_migration, 'upgrade', [4, 1, 3])


@pytest.fixture
def downgraded(natural_key_migration) -> MagicMock:
    """downgrade() where every archived row goes back: 2 in the archive, 0 left in it after."""
    return _run_migration(natural_key_migration, 'downgrade', [2, 0])


def test_natural_key_is_unique_and_excludes_dataset_id():
    """The decision this whole task turns on, pinned so a later change is a deliberate one.

    Two fetches of the same minute through different dataset entries are the same bar, so
    dataset_id is not part of the key - including it would defeat the point.
    """
    unique = [
        c for c in StockMarketActivity.__table__.constraints if c.name == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    ]
    assert len(unique) == 1, 'the natural-key unique constraint is missing from the model'
    columns = tuple(column.name for column in unique[0].columns)
    assert sorted(columns) == sorted(BaseMarketActivity.NATURAL_KEY)
    assert 'dataset_id' not in columns


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


def test_migration_declares_the_names_the_model_declares(natural_key_migration):
    assert natural_key_migration.CONSTRAINT_NAME == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    assert tuple(natural_key_migration.NATURAL_KEY) == BaseMarketActivity.NATURAL_KEY
    assert natural_key_migration.TABLE == StockMarketActivity.TABLE_NAME


def test_migration_upgrade_actually_adds_the_constraint(upgraded: MagicMock):
    """Asserted against the MODEL's names, not the revision's constants.

    The constants agreeing proves only that two strings match; a revision that declared them
    and then never called create_unique_constraint would leave the write path's ON CONFLICT
    ON CONSTRAINT targeting a constraint that does not exist on the server.
    """
    upgraded.create_unique_constraint.assert_called_once_with(
        StockMarketActivity.NATURAL_KEY_CONSTRAINT, StockMarketActivity.TABLE_NAME, list(BaseMarketActivity.NATURAL_KEY)
    )


def test_migration_upgrade_moves_superseded_rows_instead_of_deleting_them(natural_key_migration, upgraded: MagicMock):
    """The additive exception itself, pinned - if one assertion in this file fails loudly, this one.

    '# additive-exception: tj-3mk3u5.3' is granted on a single promise: the superseded rows are
    MOVED into an archive table, never deleted. A revision that deleted them outright, or that
    quietly stopped moving anything, would still create the right constraint under the right
    name, so nothing else in this suite would notice.
    """
    table = natural_key_migration.TABLE
    archive = natural_key_migration.ARCHIVE_TABLE
    statements = _statements(upgraded)

    assert any(f'CREATE TABLE IF NOT EXISTS {archive}' in statement for statement in statements), (
        f'{archive} is never created, so there is nowhere for a superseded row to go'
    )

    move = _the_move(natural_key_migration, upgraded)
    assert f'DELETE FROM {table}' in move and 'RETURNING' in move, (
        'the archive insert is not fed by the delete, so the two can come apart'
    )
    assert 'row_number() OVER' in move and 'rn > 1' in move, 'the move no longer selects the superseded rows'

    # EXACTLY the natural key, not merely a superset of it. Adding dataset_id here would
    # de-duplicate per dataset entry - the one thing this task exists to prevent, since two
    # fetches of the same minute through different entries are the same bar. It would also
    # leave real duplicates behind, so create_unique_constraint would then fail on a live
    # database, having already moved rows into the archive.
    partition, _ordering = _window_spec(move)
    assert sorted(partition) == sorted(BaseMarketActivity.NATURAL_KEY), (
        f'duplicates are not grouped by exactly the natural key: partitioned by {partition}'
    )

    # No row may leave the table by any other route.
    for statement in statements:
        if f'DELETE FROM {table}' in statement or 'TRUNCATE' in statement:
            assert f'INSERT INTO {archive}' in statement, f'rows destroyed without being archived: {statement}'


def test_migration_upgrade_keeps_the_most_recently_written_row(natural_key_migration, upgraded: MagicMock):
    """The direction of the window's ORDER BY decides which duplicate survives and which is archived.

    Flipped to ASC the migration keeps the OLDEST bar per key and archives the newest, which is
    silently wrong: it still succeeds, still adds the constraint, and throws away exactly the
    vendor correction that the ON CONFLICT DO UPDATE write path exists to preserve.
    """
    _partition, ordering = _window_spec(_the_move(natural_key_migration, upgraded))
    assert ordering == 'updated_at DESC, id DESC', (
        f'the surviving row is no longer the most recently written one: ORDER BY {ordering}'
    )


def test_migration_upgrade_creates_the_archive_before_it_moves_rows_into_it(natural_key_migration, upgraded: MagicMock):
    """Ordering, not presence: the move is an INSERT INTO the archive, so the archive must exist first.

    Both statements are op.execute calls, so a revision that emits them the wrong way round still
    emits both - and fails at runtime on a table that does not exist yet.
    """
    archive = natural_key_migration.ARCHIVE_TABLE
    sequence = _call_sequence(upgraded)
    create = _index_of(sequence, 'execute', f'CREATE TABLE IF NOT EXISTS {archive}')
    move = _index_of(sequence, 'execute', f'INSERT INTO {archive}')
    assert create < move, 'rows are moved into the archive before the archive is created'


def test_migration_upgrade_reuses_a_surviving_archive(natural_key_migration, upgraded: MagicMock):
    """Re-upgrading after a downgrade that kept the archive must append to it, not fail and not clear it.

    Roll back, fix, roll forward is the scenario the rollback window is for (tj-1945as). A bare
    CREATE TABLE fails outright on the second run; a DROP or TRUNCATE would make it work by
    destroying the rows the additive exception is granted to preserve.
    """
    archive = natural_key_migration.ARCHIVE_TABLE
    statements = _statements(upgraded)

    creates = [statement for statement in statements if f'CREATE TABLE IF NOT EXISTS {archive}' in statement]
    assert len(creates) == 1, f'the archive is not created with IF NOT EXISTS, so a re-upgrade fails: {statements}'

    for statement in statements:
        assert f'DROP TABLE {archive}' not in statement, f'upgrade destroys a surviving archive: {statement}'
        assert 'TRUNCATE' not in statement, f'upgrade empties a surviving archive: {statement}'


def test_migration_upgrade_archives_before_it_adds_the_constraint(natural_key_migration, upgraded: MagicMock):
    """Adding the constraint first fails outright on a database that already holds duplicates.

    That is the whole reason this revision exists rather than a bare create_unique_constraint.
    """
    sequence = _call_sequence(upgraded)
    move = _index_of(sequence, 'execute', f'INSERT INTO {natural_key_migration.ARCHIVE_TABLE}')
    add = _index_of(sequence, 'create_unique_constraint')
    assert move < add, 'the constraint is added before the duplicates are out of the way'


def test_migration_downgrade_restores_the_archive_and_drops_the_constraint(
    natural_key_migration, downgraded: MagicMock
):
    """And in that order: the restored rows are duplicates by definition, so the constraint goes first."""
    table = natural_key_migration.TABLE
    downgraded.drop_constraint.assert_called_once_with(
        StockMarketActivity.NATURAL_KEY_CONSTRAINT, StockMarketActivity.TABLE_NAME, type_='unique'
    )

    restore = _the_restore(natural_key_migration, downgraded)
    assert natural_key_migration.ARCHIVE_TABLE in restore, 'the rows put back do not come from the archive'

    sequence = _call_sequence(downgraded)
    assert _index_of(sequence, 'drop_constraint') < _index_of(sequence, 'execute', f'INSERT INTO {table} SELECT'), (
        'the duplicates are re-inserted while the unique constraint is still in place'
    )


def test_migration_downgrade_moves_restored_rows_out_of_the_archive(natural_key_migration, downgraded: MagicMock):
    """A restored row is MOVED back, not copied back, or a later upgrade archives it a second time.

    Leaving it in the archive is the defect behind tj-1945as: the re-upgrade would re-archive a
    row that is already archived and double-count it. The DELETE and the INSERT have to be one
    statement for the same reason upgrade's move is one - they must not come apart.
    """
    table = natural_key_migration.TABLE
    archive = natural_key_migration.ARCHIVE_TABLE
    restore = _the_restore(natural_key_migration, downgraded)

    assert f'DELETE FROM {archive}' in restore and 'RETURNING' in restore, (
        f'restored rows are copied out of {archive} rather than moved, so a re-upgrade re-archives them'
    )

    # And nothing may leave the archive except by going back into the live table.
    for statement in _statements(downgraded):
        if f'DELETE FROM {archive}' in statement or 'TRUNCATE' in statement:
            assert f'INSERT INTO {table} SELECT' in statement, f'archived rows destroyed, not restored: {statement}'


def test_migration_downgrade_keeps_the_archive_when_a_row_cannot_be_restored(natural_key_migration):
    """The other half of the additive exception: downgrade must not destroy what it cannot restore.

    A row whose dataset entry is gone cannot go back without violating the live foreign key.
    Dropping the archive anyway would destroy it - doomed by ON DELETE CASCADE either way, but
    the exception marker is audited on the claim that this revision destroys nothing.
    """
    kept = _run_migration(natural_key_migration, 'downgrade', [3, 2])
    dropped = [statement for statement in _statements(kept) if statement.startswith('DROP TABLE')]
    assert dropped == [], f'downgrade dropped the archive with 2 un-restorable rows still in it: {dropped}'


def test_migration_downgrade_drops_the_archive_once_every_row_is_back(natural_key_migration, downgraded: MagicMock):
    """Kept only while it still holds something; otherwise the rollback leaves dead weight behind."""
    dropped = [statement for statement in _statements(downgraded) if statement.startswith('DROP TABLE')]
    assert dropped == [f'DROP TABLE {natural_key_migration.ARCHIVE_TABLE}']


def test_migration_history_has_a_single_head():
    """Two heads make `alembic upgrade head` fail outright, and nothing here applies migrations.

    Nothing in this stack runs a migration automatically (tj-rhcllr), so a branched history
    would not surface until someone ran it by hand against a real database.
    """
    heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    assert list(heads) == [NATURAL_KEY_REVISION]
