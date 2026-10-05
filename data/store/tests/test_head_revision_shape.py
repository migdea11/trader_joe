"""The migration chain's two non-additive revisions: what is provable about them without a database.

TWO REVISIONS NOW, NOT ONE, and which one each test is about is named rather than implied. The file
was written against eec8f88a7443 while it was the head (the dataset-model epic's single revision);
tj-3mk3u5.31 landed c4a1f7b2e905 on top of it, adding feed to store_dataset_entry and rebuilding
the eleven-column identity constraint around it. So the constants and fixtures are
DATASET_MODEL_REVISION / dataset_model and FEED_REVISION / feed_revision, and the three
model-against-revision agreement tests moved to the HEAD, because the head is what decides the
shape a migrated database ends up with. Everything about eec8f88a7443's own body, marker and
inverse is unchanged and still asserted of it.

WHY THIS FILE EXISTS (validator, tj-vhboky.12). test_market_activity_idempotency.py pins the BAR
half of this revision against the model and the single-head property of the history. It does not
pin the revision's place in the chain, its additive-exception marker, that downgrade() undoes
upgrade(), the ENTRY half of the model/revision agreement, or the DDL the models compile to.

WHAT TIER THIS IS. The revision BODY is run against a recording stand-in for alembic.op, the same
technique test_market_activity_idempotency.py uses, and the models are compiled with CreateTable
against the postgresql dialect. Nothing here reaches Postgres. NOT PROVED HERE, and never to be
read off this file being green (all tj-vhboky.14's):
  * that the revision applies at all, in either direction, against a real Postgres;
  * that the unique constraints REJECT the duplicates they declare;
  * that the feed enum type exists before a column needs it and is dropped only after both;
  * that ALTER COLUMN ... SET NOT NULL succeeds on real data (on a wiped database it trivially does);
  * that ON DELETE CASCADE removes exactly the deleted entry's bars;
  * that the old unnamed entry constraint is found under whatever name Postgres gave it;
  * that a naive datetime bound to the new timestamptz column is read in the session timezone.

RELATIONSHIPS, NOT CONSTANTS. Migration tests here have gone green asserting a literal the code also
declared (ADR tj-8fxxfb). Every assertion below compares two places -- the revision against the
model, the upgrade against the downgrade, the downgrade against the revisions before it -- except
where the design itself names the value (the parent revision id, the marker, the dead columns) and
the test says so.
"""

import importlib.util
import re
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from common.enums.data_stock import Feed
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
VERSIONS_DIR = MIGRATIONS_DIR / 'versions'

# THE DATASET-MODEL EPIC'S ONE REVISION. It was the HEAD when this file was written and is not any
# more: tj-3mk3u5.31 landed c4a1f7b2e905 on top of it, so the two are now named separately and the
# tests below say which revision each one is about. Nothing in the dataset-model epic's rulings
# moved -- the revision is unchanged -- so every assertion about its body, its marker and its
# inverse still belongs to this constant.
DATASET_MODEL_REVISION = 'eec8f88a7443'
DATASET_MODEL_FILE = f'{DATASET_MODEL_REVISION}_per_dataset_identity_and_feed.py'
# The CURRENT head: feed on the dataset entry, and the identity constraint rebuilt around it
# (tj-3mk3u5.31, closing tj-f2qz44). It owns the ELEVEN-column entry identity, so the three
# model/revision agreement tests at the bottom of this file read it rather than the revision above.
FEED_REVISION = 'c4a1f7b2e905'
FEED_FILE = f'{FEED_REVISION}_feed_on_the_dataset_entry.py'
# The parent the epic's design names for its one revision (tj-vhboky.12 item 1).
PARENT_REVISION = '8f41c2d7a3b9'
PARENT_FILE = f'{PARENT_REVISION}_bar_natural_key_and_upsert.py'
INITIAL_REVISION = '2b88043cd13c'
INITIAL_FILE = f'{INITIAL_REVISION}_initial_migration.py'

# What the lookup of the old, Postgres-named entry constraint hands back. Deliberately not a name
# the revision could guess, so a revision that ignored the lookup cannot pass.
LOOKED_UP_ENTRY_CONSTRAINT = 'store_dataset_entry_asset_symbol_granularity_start_end_so_key'

# The design's must-not-contain list for the bar (tj-vhboky.12 item 5 and Amendment 1). Written out
# because the design names these; test_the_bar_ddl_contains_nothing_the_revision_drops also derives
# the same kind of list from the revision body, so neither alone decides it.
BAR_MUST_NOT_CONTAIN = (
    'split_factor',
    'dividends_factor',
    'expiry',
    'ix_dataset_id_timestamp',
    'ix_stock_market_activity_dataset_id',
    'ix_stock_market_activity_expiry',
)

DIALECT = postgresql.dialect()


def _load(revision: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(f'revision_{revision}', VERSIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dataset_model() -> Any:
    return _load(DATASET_MODEL_REVISION, DATASET_MODEL_FILE)


@pytest.fixture
def feed_revision() -> Any:
    return _load(FEED_REVISION, FEED_FILE)


def _run(module: Any, direction: str, scalar_results: list[Any] | None = None) -> tuple[MagicMock, MagicMock]:
    """Run upgrade() or downgrade() against a recording op; returns (op recorder, feed type recorder).

    op.f is made the identity so a revision that wraps names in op.f() records the name itself.
    FEED_TYPE is replaced where the module has one, because it is created and dropped through the
    bind rather than through op, and the recorder would otherwise not see it.
    """
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.side_effect = list(scalar_results or [])
    recorder = MagicMock(name='op')
    recorder.get_bind.return_value = bind
    recorder.f.side_effect = lambda name: name
    feed_type = MagicMock(name='FEED_TYPE')
    with patch.object(module, 'op', recorder):
        if hasattr(module, 'FEED_TYPE'):
            with patch.object(module, 'FEED_TYPE', feed_type):
                getattr(module, direction)()
        else:
            getattr(module, direction)()
    return recorder, feed_type


# Every op method a schema-changing call can use here, and how to key it. A method NOT in this map
# fails _schema_changes outright, so a new kind of operation (drop_table, rename, a batch op) cannot
# slip past the inverse check by simply not being looked at.
_DATA_ONLY_OPS = {'get_bind', 'execute', 'f'}


def _schema_changes(recorder: MagicMock, unnamed_constraint: str | None = None) -> dict[str, Counter]:
    """Bucket every schema-changing op call by kind, keyed by (table, name).

    unnamed_constraint is the name the constraint lookup returned. The old entry constraint was
    declared with no name, so upgrade drops it under Postgres's name and downgrade recreates it
    with name=None for Postgres to name the same way; both are keyed as (table, None) so that the
    pair can be compared as the inverse it is.
    """
    changes: dict[str, Counter] = {
        kind: Counter()
        for kind in (
            'add_column',
            'drop_column',
            'create_constraint',
            'drop_constraint',
            'create_index',
            'drop_index',
            'alter_column',
        )
    }
    for name, args, kwargs in recorder.method_calls:
        if name in _DATA_ONLY_OPS or name.startswith('get_bind'):
            continue
        if name == 'add_column':
            changes['add_column'][(args[0], args[1].name)] += 1
        elif name == 'drop_column':
            changes['drop_column'][(args[0], args[1])] += 1
        elif name == 'create_unique_constraint':
            changes['create_constraint'][(args[1], args[0])] += 1
        elif name == 'drop_constraint':
            constraint = None if args[0] == unnamed_constraint else args[0]
            changes['drop_constraint'][(args[1], constraint)] += 1
        elif name == 'create_index':
            changes['create_index'][(args[1], args[0])] += 1
        elif name == 'drop_index':
            changes['drop_index'][(kwargs['table_name'], args[0])] += 1
        elif name == 'alter_column':
            changes['alter_column'][(args[0], args[1], kwargs['nullable'])] += 1
        else:
            pytest.fail(f'the revision calls op.{name}, which the inverse check does not know how to pair')
    return changes


def _ddl(table: sa.Table) -> str:
    """CREATE TABLE plus every CREATE INDEX for the table, as Postgres would receive them, whitespace-collapsed."""
    statements = [str(CreateTable(table).compile(dialect=DIALECT))]
    statements += [str(CreateIndex(index).compile(dialect=DIALECT)) for index in table.indexes]
    return ' '.join(' '.join(statements).split())


def _quoted(columns: list[str] | tuple[str, ...]) -> str:
    return ', '.join(DIALECT.identifier_preparer.quote(column) for column in columns)


def _named_constraint_columns(table: sa.Table, name: str) -> tuple[str, ...]:
    matching = [constraint for constraint in table.constraints if constraint.name == name]
    assert len(matching) == 1, f'{table.name} declares {len(matching)} constraints named {name}'
    return tuple(column.name for column in matching[0].columns)


# ---------------------------------------------------------------------------------------------
# Item 1: one revision, on the named parent, in a linear chain
# ---------------------------------------------------------------------------------------------


def test_each_revision_of_the_two_epics_sits_directly_on_the_one_before_it():
    """Both one-revision promises, as a single chain of named parents (the values are the designs').

    TWO EPICS, ONE REVISION EACH, and this test went red the correct way when the second arrived.
    The dataset-model epic promised one revision on 8f41c2d7a3b9; tj-3mk3u5.31 promised one more,
    c4a1f7b2e905, on top of that. Spelled as "the head is the feed revision, its parent is the
    dataset-model revision, and that one's parent is 8f41c2d7a3b9" rather than as a single
    head-to-parent hop, because a hop is what hid the thing this test exists to catch: a third
    revision slipped in between -- a "small follow-up" -- would re-point the head, leave the
    single-head count at 1, and silently extend the wipe-before-migrate notes in BOTH docstrings
    over a revision neither of them was written about.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    (head_id,) = script.get_heads()
    head_revision = script.get_revision(head_id)

    assert head_revision.module.__file__.endswith(FEED_FILE), (
        f'the head is not {FEED_FILE}: {head_revision.module.__file__}'
    )
    # With the chain linear (the test below), each of these is "exactly one revision follows".
    assert head_revision.down_revision == DATASET_MODEL_REVISION, (
        f'the head does not sit directly on {DATASET_MODEL_REVISION}: {head_revision.down_revision}'
    )
    assert script.get_revision(DATASET_MODEL_REVISION).down_revision == PARENT_REVISION, (
        f'{DATASET_MODEL_REVISION} no longer sits directly on {PARENT_REVISION}'
    )


def test_the_revision_chain_is_linear_from_the_initial_revision():
    """Every revision file is on ONE chain from the initial revision to the head, with no merge and no branch.

    get_heads() == 1 (test_market_activity_idempotency.py) is not this: a merge revision and a
    branch that was later merged both leave a single dataset_model. Walked from the head by down_revision,
    compared against every revision alembic can load, and against the files on disk -- a file
    alembic loads but the chain does not reach is a second line of history.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    (head_id,) = script.get_heads()

    chain: list[str] = []
    current = head_id
    while current is not None:
        revision = script.get_revision(current)
        assert isinstance(revision.down_revision, str | None), f'{current} is a merge of {revision.down_revision}'
        assert not revision.branch_labels, f'{current} carries branch labels {revision.branch_labels}'
        assert not revision.dependencies, f'{current} depends on {revision.dependencies}'
        assert len(revision.nextrev) <= 1, f'{current} has more than one child: {sorted(revision.nextrev)}'
        chain.append(current)
        current = revision.down_revision

    assert chain[-1] == INITIAL_REVISION, f'the chain does not bottom out at the initial revision: {chain}'
    loadable = sorted(revision.revision for revision in script.walk_revisions())
    assert sorted(chain) == loadable, f'revisions exist off the chain: {sorted(set(loadable) - set(chain))}'
    files = sorted(path.name.split('_', 1)[0] for path in VERSIONS_DIR.glob('*.py'))
    assert sorted(chain) == files, f'revision files and the chain disagree: files {files}, chain {sorted(chain)}'


# ---------------------------------------------------------------------------------------------
# Item 2: the additive-exception marker
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('filename', 'bead'),
    [(DATASET_MODEL_FILE, 'tj-vhboky'), (FEED_FILE, 'tj-3mk3u5.31')],
    ids=[DATASET_MODEL_REVISION, FEED_REVISION],
)
def test_every_non_additive_revision_carries_its_additive_exception_marker(filename: str, bead: str):
    """data/store/CLAUDE.md says migrations are additive; neither of these is, and each says so by marker.

    THIS ASSERTION IS THE GATE, FOR NOW. The CI check that is meant to require the marker on any
    non-additive revision (tj-aw0tuk) does not exist yet, so nothing else fails if a marker is
    deleted, reworded, or pointed at another bead. The bead ids are the designs' values.

    c4a1f7b2e905 is non-additive TWICE OVER and for different reasons from eec8f88a7443's, which is
    why it needed its own declaration rather than shelter under the epic's: it DELETEs every bar
    and every entry (the ruled alternative to a backfill, tj-3mk3u5.22 Q6), and it drops and
    recreates uq_store_dataset_entry_identity, because a unique constraint's column list cannot be
    widened in place.

    Args:
        filename: The revision file that must carry a marker.
        bead: The bead id its marker must name.
    """
    source = (VERSIONS_DIR / filename).read_text()
    assert re.search(rf'^# additive-exception: {re.escape(bead)}$', source, re.MULTILINE), (
        f'{filename} no longer carries "# additive-exception: {bead}" on a line of its own'
    )


# ---------------------------------------------------------------------------------------------
# Item 3: upgrade and downgrade are structural inverses
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def upgrade_changes(dataset_model) -> tuple[dict[str, Counter], MagicMock]:
    recorder, feed_type = _run(dataset_model, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    return _schema_changes(recorder, unnamed_constraint=LOOKED_UP_ENTRY_CONSTRAINT), feed_type


@pytest.fixture
def downgrade_changes(dataset_model) -> tuple[dict[str, Counter], MagicMock]:
    recorder, feed_type = _run(dataset_model, 'downgrade')
    return _schema_changes(recorder), feed_type


@pytest.mark.parametrize(
    ('made_by_upgrade', 'undone_by_downgrade'),
    [
        ('add_column', 'drop_column'),
        ('drop_column', 'add_column'),
        ('create_constraint', 'drop_constraint'),
        ('drop_constraint', 'create_constraint'),
        ('create_index', 'drop_index'),
        ('drop_index', 'create_index'),
    ],
)
def test_downgrade_undoes_every_schema_change_upgrade_makes(
    upgrade_changes, downgrade_changes, made_by_upgrade: str, undone_by_downgrade: str
):
    """Every column, constraint and index upgrade adds, downgrade drops -- and vice versa (tj-vhboky.12 item 3).

    Compared as multisets keyed by (table, name) over the op calls actually made, not by reading the
    function. A column added in upgrade and forgotten in downgrade leaves a downgraded database
    that the previous release's model does not describe, and nothing complains until someone
    downgrades for real. The old entry constraint is the one unnamed object: see _schema_changes.
    """
    upgrade, _ = upgrade_changes
    downgrade, _ = downgrade_changes
    assert upgrade[made_by_upgrade] == downgrade[undone_by_downgrade], (
        f'upgrade {made_by_upgrade} {sorted(upgrade[made_by_upgrade])} is not undone by downgrade '
        f'{undone_by_downgrade} {sorted(downgrade[undone_by_downgrade])}'
    )


# The pairs the inverse test above checks, read from its own parametrize mark rather than copied, so
# a pair added there is guarded here without a second edit.
_INVERSE_PAIRS = next(
    mark.args[1]
    for mark in test_downgrade_undoes_every_schema_change_upgrade_makes.pytestmark
    if mark.name == 'parametrize'
)


@pytest.mark.parametrize(('made_by_upgrade', 'undone_by_downgrade'), _INVERSE_PAIRS)
def test_every_inverse_pair_is_exercised_on_both_sides(
    upgrade_changes, downgrade_changes, made_by_upgrade: str, undone_by_downgrade: str
):
    """Vacuity guard for the inverse test above (tj-6e2a2q, from the architect gate on tj-vhboky.12).

    Two empty multisets compare equal, so a kind that upgrade stops using -- and downgrade stops
    undoing -- drops out of the inverse check silently while it stays green. Every pair is used on
    both sides by eec8f88a7443 as written (counts recorded on tj-vhboky.12), so each side must
    carry at least one op. Checked per side: one empty side is already red above, but naming
    which side is empty is the useful failure.
    """
    upgrade, _ = upgrade_changes
    downgrade, _ = downgrade_changes
    assert upgrade[made_by_upgrade], f'upgrade makes no {made_by_upgrade} call, so its inverse check is vacuous'
    assert downgrade[undone_by_downgrade], (
        f'downgrade makes no {undone_by_downgrade} call, so the inverse check of upgrade {made_by_upgrade} is vacuous'
    )


def test_downgrade_reverses_every_nullability_change_upgrade_makes(upgrade_changes, downgrade_changes):
    """Each column upgrade makes NOT NULL is made nullable again by downgrade, and nothing else is altered."""
    upgrade, _ = upgrade_changes
    downgrade, _ = downgrade_changes
    flipped = Counter(
        {(table, column, not nullable): count for (table, column, nullable), count in upgrade['alter_column'].items()}
    )
    assert upgrade['alter_column'], 'upgrade alters no column, so this comparison is vacuous'
    assert flipped == downgrade['alter_column']


def test_the_feed_type_created_by_upgrade_is_dropped_by_downgrade(upgrade_changes, downgrade_changes):
    """The enum type is the one schema object made through the bind rather than op, so it is paired separately."""
    _, upgrade_feed = upgrade_changes
    _, downgrade_feed = downgrade_changes
    assert (upgrade_feed.create.call_count, upgrade_feed.drop.call_count) == (1, 0)
    assert (downgrade_feed.create.call_count, downgrade_feed.drop.call_count) == (0, 1)


def test_downgrade_restores_the_indexes_and_entry_constraint_the_parent_schema_had(dataset_model):
    """Inverse at the level of CONTENT, not just names: what comes back matches what was there.

    The parent schema's bar indexes and entry constraint were created by 2b88043cd13c, and
    8f41c2d7a3b9 touches neither (asserted here, so this comparison cannot go stale silently). A
    downgrade that re-created ix_dataset_id_timestamp over the wrong columns, or the old entry
    constraint in another column order, would pass the name-level check above.
    """
    initial, _ = _run(_load(INITIAL_REVISION, INITIAL_FILE), 'upgrade')
    parent, _ = _run(_load(PARENT_REVISION, PARENT_FILE), 'upgrade', [4, 1, 3])
    assert not [name for name, *_ in parent.method_calls if 'index' in name], f'{PARENT_REVISION} now touches indexes'

    original_indexes = {args[0]: list(args[2]) for name, args, _ in initial.method_calls if name == 'create_index'}
    entry_create = next(
        args
        for name, args, _ in initial.method_calls
        if name == 'create_table' and args[0] == dataset_model.ENTRY_TABLE
    )
    original_entry = sa.Table(dataset_model.ENTRY_TABLE, sa.MetaData(), *entry_create[1:])
    original_multi_column_unique = [
        tuple(column.name for column in constraint.columns)
        for constraint in original_entry.constraints
        if isinstance(constraint, sa.UniqueConstraint) and len(constraint.columns) > 1
    ]

    downgrade, _ = _run(dataset_model, 'downgrade')
    restored_indexes = {args[0]: list(args[2]) for name, args, _ in downgrade.method_calls if name == 'create_index'}
    restored_entry_constraints = [
        tuple(args[2])
        for name, args, _ in downgrade.method_calls
        if name == 'create_unique_constraint' and args[1] == dataset_model.ENTRY_TABLE
    ]

    assert restored_indexes == original_indexes
    assert restored_entry_constraints == original_multi_column_unique
    assert tuple(dataset_model.OLD_ENTRY_NATURAL_KEY) == original_multi_column_unique[0]


# ---------------------------------------------------------------------------------------------
# Item 4: the model and the revision agree -- the ENTRY half (the bar half is in
# test_market_activity_idempotency.py::test_head_migration_declares_the_names_the_model_declares)
# ---------------------------------------------------------------------------------------------


def test_the_head_revision_declares_the_entry_identity_the_model_declares(feed_revision):
    """Two hand-maintained copies of one fact; nothing else keeps them in step.

    READ OFF THE HEAD, c4a1f7b2e905, WHICH IS A CHANGE. It used to read eec8f88a7443, correctly,
    while that was the last revision to touch this constraint. tj-3mk3u5.31 rebuilt the constraint
    around ELEVEN columns, so the revision that now decides what a migrated database holds is the
    feed one, and the ten-column list eec8f88a7443 still declares is history -- pinned as history
    by test_the_head_revision_restores_the_identity_the_revision_before_it_created below.

    upsert_entry's ON CONFLICT ON CONSTRAINT names StoreDatasetEntry.NATURAL_KEY_CONSTRAINT. If the
    revision creates the constraint under another name, every create fails at runtime with
    "constraint does not exist" while this whole suite stays green. Compared against the constraint
    the model actually declares on its table, not only against the NATURAL_KEY tuple it is built
    from, and in ORDER, because column order decides what the backing index can serve -- which is
    load-bearing for feed specifically: the own-overlap check keys on the other eight columns, so
    feed anywhere earlier than last-before-the-range truncates the prefix that check can use
    (tj-xn3qa6 D1).
    """
    model_columns = _named_constraint_columns(StoreDatasetEntry.__table__, StoreDatasetEntry.NATURAL_KEY_CONSTRAINT)
    assert feed_revision.ENTRY_TABLE == StoreDatasetEntry.TABLE_NAME
    assert feed_revision.ENTRY_CONSTRAINT_NAME == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT
    assert tuple(feed_revision.NEW_ENTRY_NATURAL_KEY) == model_columns == StoreDatasetEntry.NATURAL_KEY


def test_the_head_revision_body_creates_the_entry_constraint_the_model_targets(feed_revision):
    """The body, not the constants: a revision that declared them and never used them would pass the test above."""
    recorder, _ = _run(feed_revision, 'upgrade')
    recorder.create_unique_constraint.assert_any_call(
        StoreDatasetEntry.NATURAL_KEY_CONSTRAINT,
        StoreDatasetEntry.TABLE_NAME,
        list(_named_constraint_columns(StoreDatasetEntry.__table__, StoreDatasetEntry.NATURAL_KEY_CONSTRAINT)),
    )
    # BY NAME, with no catalogue lookup, and that is a deliberate difference from eec8f88a7443's
    # drop rather than a shortcut: the constraint this revision drops was created BY eec8f88a7443
    # under an explicit name that the model and the upsert's ON CONFLICT both already depend on,
    # whereas the one eec8f88a7443 dropped was unnamed and had to be found in pg_constraint. A
    # lookup here would be looking up a value the module can state.
    recorder.drop_constraint.assert_any_call(
        StoreDatasetEntry.NATURAL_KEY_CONSTRAINT, StoreDatasetEntry.TABLE_NAME, type_='unique'
    )


def test_the_head_revision_restores_the_identity_the_revision_before_it_created(dataset_model, feed_revision):
    """The chain's one hand-copied tuple, compared across the two revisions that own it.

    c4a1f7b2e905's downgrade recreates the TEN-column constraint, and its OLD_ENTRY_NATURAL_KEY is
    a second copy of the list eec8f88a7443 created. Nothing makes the two agree, and a disagreement
    is invisible until someone downgrades for real: the constraint would come back over the wrong
    columns, under the right name, and the previous release's model would not describe it.

    BOTH DIRECTIONS OF THE RELATIONSHIP, since the pair is what the chain promises -- the older
    revision's NEW key is the newer one's OLD key, and the newer one's NEW key is that plus feed in
    the ruled position.
    """
    assert feed_revision.OLD_ENTRY_NATURAL_KEY == dataset_model.NEW_ENTRY_NATURAL_KEY, (
        'the head restores a different identity from the one the revision before it created, so a '
        'downgrade rebuilds the constraint over the wrong columns'
    )
    old = feed_revision.OLD_ENTRY_NATURAL_KEY
    # Computed side first: ruff reads the UPPER_CASE attribute as the comparison's constant (SIM300).
    expected_new = [*old[:-2], 'feed', *old[-2:]]
    assert expected_new == feed_revision.NEW_ENTRY_NATURAL_KEY, (
        f'the head does not add feed as the last equality column before the range: '
        f'{feed_revision.NEW_ENTRY_NATURAL_KEY}'
    )


def test_the_head_revision_empties_both_tables_before_it_adds_a_not_null_column(feed_revision):
    """THE ORDER IS THE WHOLE CORRECTNESS ARGUMENT, and getting it wrong is a runtime failure.

    ADD COLUMN feed ... NOT NULL with no server default is REFUSED by Postgres on a table that
    holds rows. The revision is valid only because it empties both tables first, in the same
    transaction (the ruled alternative to a backfill: user, tj-3mk3u5.22 Q6). Swap the two steps --
    or lose the deletes to a later "this looks dangerous" edit -- and `alembic upgrade head` fails
    on any populated database while every other test in this file stays green, because nothing else
    here looks at ordering at all.

    THE DELETES ARE ALSO ASSERTED TO BE DELETES, not TRUNCATE: TRUNCATE on the entry table needs
    CASCADE to get past the bar's foreign key, and TRUNCATE ... CASCADE reaches every referencing
    table transitively -- aimed by the schema rather than by this revision.

    NOT PROVED HERE (tj-vhboky.14's tier): that the statements run, that the NOT NULL is accepted,
    or that the tables really come out empty. CI's System Testing job runs alembic upgrade head.
    """
    recorder, _ = _run(feed_revision, 'upgrade')

    calls = [(name, args) for name, args, _ in recorder.method_calls]
    executed = [args[0] for name, args in calls if name == 'execute']
    assert executed == [f'DELETE FROM {feed_revision.BAR_TABLE}', f'DELETE FROM {feed_revision.ENTRY_TABLE}'], (
        f'the head no longer empties the bar table and then the entry table with plain DELETEs: {executed}'
    )

    kinds = [name for name, _ in calls]
    last_delete = max(index for index, name in enumerate(kinds) if name == 'execute')
    assert kinds.index('add_column') > last_delete, (
        'the NOT NULL column is added before the tables are emptied, so the upgrade fails on any '
        f'populated database: {kinds}'
    )


def test_the_head_revision_adds_the_tape_as_not_null_with_no_server_default(feed_revision):
    """The "no sentinel" ruling, in the one place a sentinel would have been written (tj-vhboky.1 item 1).

    Feed carries no UNKNOWN member for a server default to point at, deliberately: a value that
    should never be written is better expressed as an error than as a vocabulary member. A server
    default here would reintroduce exactly that, and on an IDENTITY column, where no later
    correction could rewrite it -- two datasets that asked for different tapes would merge under
    the default before anyone noticed.

    Read off the Column object the body hands add_column, not off the revision's constants, because
    a constant cannot carry either property.
    """
    recorder, feed_type = _run(feed_revision, 'upgrade')

    (added,) = [
        call.args[1]
        for call in recorder.add_column.call_args_list
        if call.args[0] == feed_revision.ENTRY_TABLE and call.args[1].name == 'feed'
    ]
    assert added.nullable is False, 'the tape column is nullable, so an entry can record no tape at all'
    assert added.server_default is None, (
        'the tape column has a server default, which is the removed UNKNOWN sentinel under another '
        'name -- and on an identity column (tj-vhboky.1 item 1)'
    )
    # _run patches FEED_TYPE with a recorder, so `added.type` is that stand-in (SQLAlchemy
    # instantiates a type it is handed). Identity against the recorder is what proves the column was
    # declared with the MODULE's type rather than a second, locally built enum; the type's own
    # properties are then read off the real constant below.
    assert added.type in (feed_type, feed_type.return_value), (
        'the tape column is not declared with the revision module FEED_TYPE, so its enum name and '
        'create_type are whatever a second declaration happened to say'
    )


def test_the_head_revisions_tape_type_is_the_bars_existing_type_with_the_members_off_the_class(feed_revision):
    """The enum type the new column reuses, read off the real constant rather than through a patched run.

    THREE PROPERTIES, EACH A DIFFERENT RUNTIME FAILURE IF IT MOVES:
    * the NAME must be the bar's type name, or the entry column gets a second Postgres type and the
      two tables that are meant to share one vocabulary stop sharing it;
    * create_type must be False, or add_column emits a CREATE TYPE for a type eec8f88a7443 already
      created and the upgrade fails outright against it;
    * the MEMBER LIST must come from the Feed class, as member VALUES. A literal list of strings
      gets no error and no warning the day a market is added -- composition
      (common/enums/composed_enum.py) runs at import time, so the class is already the full
      superset here, and a hand-written list simply stops matching and fails on the first insert of
      the new value.

    The member list is compared against the class, which is the one comparison that cannot be
    satisfied by a copy of itself.
    """
    assert feed_revision.FEED_TYPE.name == feed_revision.FEED_ENUM_NAME == 'feed', (
        f'the tape type is not the shared Postgres type named feed: {feed_revision.FEED_TYPE.name!r}'
    )
    assert feed_revision.FEED_TYPE.create_type is False, (
        'the tape type would emit a CREATE TYPE for a type eec8f88a7443 already created for the '
        'bar, so the upgrade fails against it'
    )
    assert list(feed_revision.FEED_TYPE.enums) == [member.value for member in Feed], (
        f'the tape type does not take its members from the Feed class as values: {list(feed_revision.FEED_TYPE.enums)}'
    )


@pytest.mark.parametrize(
    ('made_by_upgrade', 'undone_by_downgrade'),
    [('add_column', 'drop_column'), ('create_constraint', 'drop_constraint')],
)
def test_the_head_revisions_downgrade_undoes_its_schema_changes(
    feed_revision, made_by_upgrade: str, undone_by_downgrade: str
):
    """The inverse property, for the head as well as for the revision before it (tj-vhboky.12 item 3).

    The pairs are a SUBSET of the ones checked above, because this revision makes fewer kinds of
    change: no index moves and no nullability altered on an existing column. Listing only the pairs
    it uses is what keeps the vacuity argument intact -- two empty multisets compare equal, so a
    pair this revision does not exercise would be a check that cannot fail, and each side is
    asserted non-empty here for the same reason.

    WHAT THE INVERSE DOES NOT CLAIM, and the revision's docstring says so at length: that downgrade
    is reversible in SUBSTANCE. It restores no rows, it restores no tape, and it can refuse
    outright when two entries differ only by feed -- which is routine after this revision, since
    that is the state it exists to make possible. Structural inverse, one-way door.

    Args:
        feed_revision: The head revision module.
        made_by_upgrade: The schema-change kind upgrade must make.
        undone_by_downgrade: The kind downgrade must make the same number of times.
    """
    upgrade_recorder, _ = _run(feed_revision, 'upgrade')
    downgrade_recorder, _ = _run(feed_revision, 'downgrade')
    upgrade = _schema_changes(upgrade_recorder)
    downgrade = _schema_changes(downgrade_recorder)

    assert upgrade[made_by_upgrade], f'upgrade makes no {made_by_upgrade} call, so this comparison is vacuous'
    assert downgrade[undone_by_downgrade], f'downgrade makes no {undone_by_downgrade} call, so it is vacuous'
    assert upgrade[made_by_upgrade] == downgrade[undone_by_downgrade], (
        f'upgrade {made_by_upgrade} {sorted(upgrade[made_by_upgrade])} is not undone by downgrade '
        f'{undone_by_downgrade} {sorted(downgrade[undone_by_downgrade])}'
    )


def test_the_head_revision_leaves_the_shared_feed_enum_type_alone(feed_revision):
    """Neither side creates or drops the Postgres type, because the bar still uses it.

    eec8f88a7443 created the type named 'feed' for the bar's column and drops it in its own
    downgrade; stock_market_activity has a column of it on both sides of this revision. So creating
    it here would fail against the existing type and dropping it would break the bar -- and the
    pairing test above cannot see either, because the type goes through the bind rather than
    through op. This is the assertion that does.
    """
    _, upgrade_feed_type = _run(feed_revision, 'upgrade')
    _, downgrade_feed_type = _run(feed_revision, 'downgrade')

    assert (upgrade_feed_type.create.call_count, upgrade_feed_type.drop.call_count) == (0, 0), (
        'the head creates or drops the feed enum type, which eec8f88a7443 owns and the bar still uses'
    )
    assert (downgrade_feed_type.create.call_count, downgrade_feed_type.drop.call_count) == (0, 0), (
        'the head downgrade touches the feed enum type, which would break the bar column that still uses it'
    )


# ---------------------------------------------------------------------------------------------
# Item 5 / 5a: the compiled DDL of both models
# ---------------------------------------------------------------------------------------------


def test_the_bar_ddl_has_the_constraint_and_index_the_revision_creates(dataset_model):
    """CreateTable against the postgresql dialect: what a fresh database built from the MODEL would get.

    Compared against the revision's constants and body, so the model and the migrated database are
    checked to be the same shape rather than each checked against a copy of itself.
    """
    ddl = _ddl(StockMarketActivity.__table__)
    assert (
        f'CONSTRAINT {dataset_model.BAR_CONSTRAINT_NAME} UNIQUE ({_quoted(dataset_model.NEW_BAR_NATURAL_KEY)})' in ddl
    ), ddl

    recorder, _ = _run(dataset_model, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    (index_call,) = [call for call in recorder.create_index.call_args_list if call.args[1] == dataset_model.BAR_TABLE]
    name, _, columns = index_call.args
    assert f'CREATE INDEX {name} ON {dataset_model.BAR_TABLE} ({_quoted(columns)})' in ddl, ddl


@pytest.mark.parametrize('dead', BAR_MUST_NOT_CONTAIN)
def test_the_bar_ddl_does_not_contain_a_removed_column_or_index(dead: str):
    """The design's list (tj-vhboky.12 item 5, Amendment 1): none of these may reach a database built from the model."""
    ddl = _ddl(StockMarketActivity.__table__)
    assert not re.search(rf'\b{dead}\b', ddl), f'the bar DDL contains {dead}: {ddl}'


def test_the_bar_ddl_contains_nothing_the_revision_drops(dataset_model):
    """The same property derived from the revision body instead of the design's list.

    Every column and index upgrade() drops from the bar must be absent from the model's DDL, or the
    model describes a table the migrated database does not have. Derived, so a column the revision
    starts dropping later is covered here without an edit.
    """
    recorder, _ = _run(dataset_model, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    changes = _schema_changes(recorder, unnamed_constraint=LOOKED_UP_ENTRY_CONSTRAINT)
    dropped = sorted(
        name for table, name in (*changes['drop_column'], *changes['drop_index']) if table == dataset_model.BAR_TABLE
    )
    assert dropped, 'the revision drops nothing from the bar, so this comparison is vacuous'
    ddl = _ddl(StockMarketActivity.__table__)
    present = [name for name in dropped if re.search(rf'\b{name}\b', ddl)]
    assert present == [], f'the model still declares what the revision drops: {present}'


def test_the_entry_ddl_has_the_identity_constraint_the_head_revision_creates(feed_revision):
    """Read off the HEAD, which owns the eleven-column constraint (tj-3mk3u5.31)."""
    ddl = _ddl(StoreDatasetEntry.__table__)
    expected = (
        f'CONSTRAINT {feed_revision.ENTRY_CONSTRAINT_NAME} UNIQUE ({_quoted(feed_revision.NEW_ENTRY_NATURAL_KEY)})'
    )
    assert expected in ddl, ddl


def test_the_entry_expiry_is_timestamp_with_time_zone_in_the_model_and_the_revision(dataset_model):
    """tj-vhboky.12 item 5a. A plain TIMESTAMP is the defect being fixed, and it is invisible unless asserted.

    Both places: the DDL the model compiles to, and the type the revision's add_column gives the
    column on a migrated database. Either one naive makes "when does this data die" depend on the
    session timezone.
    """
    ddl = _ddl(StoreDatasetEntry.__table__)
    assert re.search(r'\bexpiry TIMESTAMP WITH TIME ZONE\b', ddl), f'the model compiles expiry as naive: {ddl}'

    recorder, _ = _run(dataset_model, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    (added,) = [
        call.args[1]
        for call in recorder.add_column.call_args_list
        if call.args[0] == dataset_model.ENTRY_TABLE and call.args[1].name == 'expiry'
    ]
    assert added.type.compile(dialect=DIALECT) == 'TIMESTAMP WITH TIME ZONE', 'the revision adds expiry as naive'
