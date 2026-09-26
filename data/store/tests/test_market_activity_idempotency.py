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

# TWO REVISIONS, TWO NAMES, and the distinction is the whole reason this file went red at
# eec8f88a7443 (tj-1njw7c). NATURAL_KEY_REVISION is the revision most of this file INSPECTS --
# the archive/row-move machinery below exists only there. HEAD_REVISION is the current head, and
# the only revision whose constants may be compared against the model. Swapping the one constant
# would have re-pointed all seven of the archive tests at a revision that has no archive.
NATURAL_KEY_REVISION = '8f41c2d7a3b9'
HEAD_REVISION = 'eec8f88a7443'

# The natural key as 8f41c2d7a3b9 itself declares it, written out as a HISTORIC LITERAL rather
# than read off BaseMarketActivity. A past revision's job is to describe the schema as it was;
# comparing it against today's model asserts that history does not change, which is the opposite
# of what is wanted. The model is compared against HEAD_REVISION instead, below.
HISTORIC_BAR_NATURAL_KEY = ('asset_symbol', 'source', 'granularity', 'timestamp')

# Columns the upsert is allowed to leave alone: the surrogate key, the natural key itself,
# and the two timestamps the write path manages directly.
NOT_REFRESHED = {'id', 'created_at', 'updated_at', *BaseMarketActivity.NATURAL_KEY}


@pytest.fixture
def bar_values() -> list[dict[str, Any]]:
    """One row's worth of insert values, in the post-eec8f88a7443 column shape.

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


def _load_revision(revision: str, filename: str) -> Any:
    path = MIGRATIONS_DIR / 'versions' / filename
    spec = importlib.util.spec_from_file_location(revision, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def natural_key_migration():
    return _load_revision(NATURAL_KEY_REVISION, f'{NATURAL_KEY_REVISION}_bar_natural_key_and_upsert.py')


@pytest.fixture
def head_migration():
    return _load_revision(HEAD_REVISION, f'{HEAD_REVISION}_per_dataset_identity_and_feed.py')


def _run_migration(migration: Any, direction: str, scalar_results: list[Any]) -> MagicMock:
    """Run a migration function against a recording stand-in for the alembic.op proxy.

    scalar_results feeds, in order, every probe the migration makes through
    op.get_bind().execute(...).scalar_one(). What those probes ARE differs per revision:
      * 8f41c2d7a3b9 counts rows -- upgrade() asks for rows-before, rows-archived, rows-after;
        downgrade() asks for the rows in the archive before the restore, then the rows still
        left in it afterwards, which are exactly the ones that could not go back.
      * eec8f88a7443 looks up ONE thing, the Postgres-generated name of the unnamed unique
        constraint it replaces on store_dataset_entry, so it takes a single string.
    """
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.side_effect = list(scalar_results)
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


@pytest.fixture
def upgraded_head(head_migration) -> MagicMock:
    """eec8f88a7443's upgrade(), whose single scalar probe is a constraint-name lookup.

    The name handed back is deliberately NOT the one the revision would guess: the lookup exists
    because Postgres generated and truncated that name at CREATE TABLE time, so a stand-in that
    returned the obvious name would let a revision that ignored the lookup still pass.
    """
    return _run_migration(head_migration, 'upgrade', ['store_dataset_entry_asset_symbol_granularity_start_end_so_key'])


def test_natural_key_is_unique_and_leads_with_dataset_id():
    """The decision this whole task turns on, pinned so a later change is a deliberate one.

    INVERTED AT eec8f88a7443, name and all (tj-vhboky.1; the repair is tj-1njw7c). This test
    used to assert dataset_id was EXCLUDED, on the reasoning that two fetches of the same minute
    through different dataset entries are the same bar. Coverage is per-dataset now, so they are
    deliberately two rows, and the old assertion is not a stale detail of this test -- it is the
    previous design stated in a name. Renamed rather than edited in place for that reason: a test
    still called ..._excludes_dataset_id while asserting the opposite is worse than either.

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


def test_the_superseded_revision_still_declares_the_key_it_shipped(natural_key_migration):
    """8f41c2d7a3b9's constants against a HISTORIC LITERAL, not against the model.

    The name and the table survived eec8f88a7443 unchanged -- deliberately, so the write path's
    ON CONFLICT ON CONSTRAINT does not change shape across the upgrade -- so those two are still
    worth comparing against the model. The COLUMN LIST did not survive it, and re-pointing this
    assertion at today's NATURAL_KEY would demand that a shipped revision rewrite its own
    history. That is what this test was doing when it went red (tj-1njw7c).
    """
    assert natural_key_migration.CONSTRAINT_NAME == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    assert natural_key_migration.TABLE == StockMarketActivity.TABLE_NAME
    assert tuple(natural_key_migration.NATURAL_KEY) == HISTORIC_BAR_NATURAL_KEY


def test_head_migration_declares_the_names_the_model_declares(head_migration):
    """The model-vs-migration agreement check, re-pointed at the revision that is actually live.

    This is the assertion that stops the three artifacts drifting, and it only means anything
    against the HEAD revision: the model describes the schema a migrated database has, and that
    is the last revision's output, not an earlier one's.
    """
    assert head_migration.BAR_CONSTRAINT_NAME == StockMarketActivity.NATURAL_KEY_CONSTRAINT
    assert head_migration.BAR_TABLE == StockMarketActivity.TABLE_NAME
    assert tuple(head_migration.NEW_BAR_NATURAL_KEY) == BaseMarketActivity.NATURAL_KEY
    # And the revision's own record of what it replaced agrees with this file's historic literal,
    # so the two do not drift into disagreeing about what the old key was.
    assert tuple(head_migration.OLD_BAR_NATURAL_KEY) == HISTORIC_BAR_NATURAL_KEY


def test_head_migration_feed_type_suppresses_implicit_create_by_the_flag(head_migration):
    """create_type=False is honoured BY THE FLAG, not by add_column's incidental output.

    The revision creates and drops the feed enum type explicitly, once, and passes
    create_type=False so that add_column does not also try. create_type is a postgresql.ENUM
    keyword: generic sa.Enum accepts it and silently discards it into **kw, so under sa.Enum the
    suppression held only by accident of what add_column happens to compile to today (tj-uxl817
    item 2). This assertion is what makes it a guarantee -- a tidy-up back to the generic type
    costs a red test instead of quietly restoring the accident.

    WHY THE ATTRIBUTE AND NOT SOMETHING MORE OBVIOUS. The two alternatives are both vacuous.
    isinstance(FEED_TYPE, sa.Enum) can never fail, because postgresql.ENUM is a subclass of it.
    And a compiled-SQL assertion cannot tell the two apart either: both render the identical bare
    'feed' column type on the pinned SQLAlchemy, which is exactly why the defect was invisible.
    The attribute is the only observable difference -- on sa.Enum the keyword is swallowed and
    hasattr is False, so getattr returns None and this goes red.
    """
    assert getattr(head_migration.FEED_TYPE, 'create_type', None) is False, (
        'FEED_TYPE does not carry create_type=False as a real attribute, so the revision is asking '
        'a generic sa.Enum to suppress an implicit CREATE TYPE and the keyword is being discarded'
    )


def test_migration_upgrade_actually_adds_the_constraint(upgraded: MagicMock):
    """The revision BODY, not its constants.

    A revision that declared them and never called create_unique_constraint would leave the
    write path's ON CONFLICT ON CONSTRAINT targeting a constraint that does not exist on the
    server.

    Asserted against the historic literal for the column list and against the model for the name
    and table, for the reason in test_the_superseded_revision_still_declares_the_key_it_shipped.
    """
    upgraded.create_unique_constraint.assert_called_once_with(
        StockMarketActivity.NATURAL_KEY_CONSTRAINT, StockMarketActivity.TABLE_NAME, list(HISTORIC_BAR_NATURAL_KEY)
    )


def test_head_migration_upgrade_actually_adds_the_widened_constraint(upgraded_head: MagicMock):
    """The same body-level check on the head revision, against the MODEL's names.

    The bar constraint is dropped and recreated here, so create_unique_constraint is called twice
    in this upgrade -- once for the entry's new identity and once for the bar. Only the bar call
    is this file's business.
    """
    upgraded_head.create_unique_constraint.assert_any_call(
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

    # EXACTLY the key THIS REVISION adds, not merely a superset of it. Grouping by more columns
    # than the constraint covers leaves real duplicates behind, so create_unique_constraint then
    # fails on a live database having already moved rows into the archive; grouping by fewer
    # archives rows that were never duplicates.
    #
    # THE HISTORIC LITERAL, NOT THE MODEL (tj-1njw7c). eec8f88a7443 later widened the key to
    # include dataset_id and feed, and this revision's window must NOT follow it there: this
    # upgrade runs against a database that still has the four-column constraint, and its job is
    # to clear the duplicates that constraint will reject. It cannot be re-pointed at the head
    # revision either -- that one has no archive and no row-move at all.
    partition, _ordering = _window_spec(move)
    assert sorted(partition) == sorted(HISTORIC_BAR_NATURAL_KEY), (
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

    THE COUNT IS THE PROPERTY THIS TEST IS NAMED FOR, and it is asserted first and on its own.
    This test used to assert only `heads == [NATURAL_KEY_REVISION]`, so landing eec8f88a7443 --
    an ordinary, correct, single-head revision -- turned it red for a non-reason and said
    'branched history' while pointing at a history that was not branched (tj-1njw7c).
    """
    heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    assert len(heads) == 1, f'the migration history has branched: {sorted(heads)}'


def test_the_named_head_is_the_current_revision():
    """Separate from the count above, and separate on purpose.

    Worth pinning because every other constant in this file is chosen relative to which revision
    is live -- but a new revision makes this one line go red and nothing else, which is a
    one-line edit rather than a hunt through seven archive tests.
    """
    assert list(ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()) == [HEAD_REVISION]


@pytest.mark.parametrize('column', ['expiry_type', 'update_type'])
def test_head_migration_backfills_a_policy_column_before_making_it_not_null(upgraded_head: MagicMock, column: str):
    """A migration-safety property that IS provable without a database, so it is proved here.

    expiry_type and update_type join eec8f88a7443's ten-column entry identity, and NOT NULL is
    load-bearing there: Postgres treats NULL as distinct from NULL inside a unique index, so a
    nullable column in that constraint makes every null-bearing row unique and silently stops
    ON CONFLICT from ever matching a repeat.

    WHY ORDER IS THE WHOLE PROPERTY. Both columns were already nullable in 2b88043cd13c, so a
    pre-existing NULL is possible. ALTER COLUMN ... SET DEFAULT does not backfill an existing
    row the way ADD COLUMN ... DEFAULT does, and alembic compiles
    alter_column(nullable=False, server_default=...) as SET NOT NULL followed by SET DEFAULT --
    the constraint lands before the value that would satisfy it exists. An explicit UPDATE
    AFTER the alter is therefore no fix at all, and an UPDATE before it is a complete one.

    AND WHY IT IS A TEST RATHER THAN A COMMENT. The builder made this true today instead of
    rewording the docstring that claimed it (tj-uxl817). Nothing else notices if the two
    statements swap: both still run, the migration still succeeds on the empty database this
    project's deploy note promises, and it fails only on an operator's populated one.
    """
    calls = list(upgraded_head.method_calls)

    backfills = [
        (index, ' '.join(str(args[0]).split()))
        for index, (name, args, _) in enumerate(calls)
        if name == 'execute' and args and f'SET {column} =' in ' '.join(str(args[0]).split())
    ]
    assert len(backfills) == 1, f'{column} is not backfilled exactly once before it is made NOT NULL: {backfills}'
    backfill_index, backfill = backfills[0]
    assert f'WHERE {column} IS NULL' in backfill, (
        f'the {column} backfill is not restricted to the rows that need it: {backfill}'
    )

    alters = [
        (index, kwargs)
        for index, (name, args, kwargs) in enumerate(calls)
        if name == 'alter_column' and args[1:2] == (column,) and kwargs.get('nullable') is False
    ]
    assert len(alters) == 1, f'{column} is not made NOT NULL exactly once: {alters}'
    alter_index, alter_kwargs = alters[0]

    assert backfill_index < alter_index, (
        f'{column} is made NOT NULL at call {alter_index} before it is backfilled at call '
        f'{backfill_index}, so an existing NULL row fails the alter'
    )

    # The backfill must write the SAME value the column is about to default to. A backfill to
    # some other value satisfies NOT NULL just as well and leaves the pre-existing rows saying
    # something different from every row written afterwards.
    written = backfill.split(f'SET {column} =')[1].split('WHERE')[0].strip()
    assert written == str(alter_kwargs.get('server_default')).strip("'"), (
        f'{column} is backfilled to {written} but defaults to {alter_kwargs.get("server_default")}'
    )
