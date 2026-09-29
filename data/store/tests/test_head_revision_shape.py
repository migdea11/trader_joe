"""The single revision of the dataset-model epic, eec8f88a7443: what is provable without a database.

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

from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
VERSIONS_DIR = MIGRATIONS_DIR / 'versions'

HEAD_REVISION = 'eec8f88a7443'
HEAD_FILE = f'{HEAD_REVISION}_per_dataset_identity_and_feed.py'
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
def head() -> Any:
    return _load(HEAD_REVISION, HEAD_FILE)


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


def test_the_head_revision_sits_directly_on_the_named_parent():
    """The epic promised ONE revision on top of 8f41c2d7a3b9 (the value is the design's, named in the bead).

    A second revision slipped in between -- a "small follow-up" -- is how the one-migration promise
    breaks quietly: the head test would re-point, the single-head count would stay 1, and the
    wipe-before-migrate note in eec8f88a7443's docstring would silently cover two revisions.
    """
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    (head_id,) = script.get_heads()
    head_revision = script.get_revision(head_id)
    assert head_revision.module.__file__.endswith(HEAD_FILE)
    # With the chain linear (the test below), this is "exactly one revision follows the parent".
    assert head_revision.down_revision == PARENT_REVISION, (
        f'the head does not sit directly on {PARENT_REVISION}: {head_revision.down_revision}'
    )


def test_the_revision_chain_is_linear_from_the_initial_revision():
    """Every revision file is on ONE chain from the initial revision to the head, with no merge and no branch.

    get_heads() == 1 (test_market_activity_idempotency.py) is not this: a merge revision and a
    branch that was later merged both leave a single head. Walked from the head by down_revision,
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


def test_the_head_revision_carries_the_epics_additive_exception_marker():
    """data/store/CLAUDE.md says migrations are additive; this one is not, and says so by marker.

    THIS ASSERTION IS THE GATE, FOR NOW. The CI check that is meant to require the marker on any
    non-additive revision (tj-aw0tuk) does not exist yet, so nothing else fails if the marker is
    deleted, reworded, or pointed at another bead. The epic id is the design's value.
    """
    source = (VERSIONS_DIR / HEAD_FILE).read_text()
    assert re.search(r'^# additive-exception: tj-vhboky$', source, re.MULTILINE), (
        f'{HEAD_FILE} no longer carries "# additive-exception: tj-vhboky" on a line of its own'
    )


# ---------------------------------------------------------------------------------------------
# Item 3: upgrade and downgrade are structural inverses
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def upgrade_changes(head) -> tuple[dict[str, Counter], MagicMock]:
    recorder, feed_type = _run(head, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    return _schema_changes(recorder, unnamed_constraint=LOOKED_UP_ENTRY_CONSTRAINT), feed_type


@pytest.fixture
def downgrade_changes(head) -> tuple[dict[str, Counter], MagicMock]:
    recorder, feed_type = _run(head, 'downgrade')
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


def test_downgrade_restores_the_indexes_and_entry_constraint_the_parent_schema_had(head):
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
        args for name, args, _ in initial.method_calls if name == 'create_table' and args[0] == head.ENTRY_TABLE
    )
    original_entry = sa.Table(head.ENTRY_TABLE, sa.MetaData(), *entry_create[1:])
    original_multi_column_unique = [
        tuple(column.name for column in constraint.columns)
        for constraint in original_entry.constraints
        if isinstance(constraint, sa.UniqueConstraint) and len(constraint.columns) > 1
    ]

    downgrade, _ = _run(head, 'downgrade')
    restored_indexes = {args[0]: list(args[2]) for name, args, _ in downgrade.method_calls if name == 'create_index'}
    restored_entry_constraints = [
        tuple(args[2])
        for name, args, _ in downgrade.method_calls
        if name == 'create_unique_constraint' and args[1] == head.ENTRY_TABLE
    ]

    assert restored_indexes == original_indexes
    assert restored_entry_constraints == original_multi_column_unique
    assert tuple(head.OLD_ENTRY_NATURAL_KEY) == original_multi_column_unique[0]


# ---------------------------------------------------------------------------------------------
# Item 4: the model and the revision agree -- the ENTRY half (the bar half is in
# test_market_activity_idempotency.py::test_head_migration_declares_the_names_the_model_declares)
# ---------------------------------------------------------------------------------------------


def test_the_revision_declares_the_entry_identity_the_model_declares(head):
    """Two hand-maintained copies of one fact; nothing else keeps them in step.

    upsert_entry's ON CONFLICT ON CONSTRAINT names StoreDatasetEntry.NATURAL_KEY_CONSTRAINT. If the
    revision creates the constraint under another name, every create fails at runtime with
    "constraint does not exist" while this whole suite stays green. Compared against the constraint
    the model actually declares on its table, not only against the NATURAL_KEY tuple it is built
    from, and in ORDER, because column order decides what the backing index can serve.
    """
    model_columns = _named_constraint_columns(StoreDatasetEntry.__table__, StoreDatasetEntry.NATURAL_KEY_CONSTRAINT)
    assert head.ENTRY_TABLE == StoreDatasetEntry.TABLE_NAME
    assert head.ENTRY_CONSTRAINT_NAME == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT
    assert tuple(head.NEW_ENTRY_NATURAL_KEY) == model_columns == StoreDatasetEntry.NATURAL_KEY


def test_the_revision_body_creates_the_entry_constraint_the_model_targets(head):
    """The body, not the constants: a revision that declared them and never used them would pass the test above."""
    recorder, _ = _run(head, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    recorder.create_unique_constraint.assert_any_call(
        StoreDatasetEntry.NATURAL_KEY_CONSTRAINT,
        StoreDatasetEntry.TABLE_NAME,
        list(_named_constraint_columns(StoreDatasetEntry.__table__, StoreDatasetEntry.NATURAL_KEY_CONSTRAINT)),
    )
    recorder.drop_constraint.assert_any_call(LOOKED_UP_ENTRY_CONSTRAINT, StoreDatasetEntry.TABLE_NAME, type_='unique')


# ---------------------------------------------------------------------------------------------
# Item 5 / 5a: the compiled DDL of both models
# ---------------------------------------------------------------------------------------------


def test_the_bar_ddl_has_the_constraint_and_index_the_revision_creates(head):
    """CreateTable against the postgresql dialect: what a fresh database built from the MODEL would get.

    Compared against the revision's constants and body, so the model and the migrated database are
    checked to be the same shape rather than each checked against a copy of itself.
    """
    ddl = _ddl(StockMarketActivity.__table__)
    assert f'CONSTRAINT {head.BAR_CONSTRAINT_NAME} UNIQUE ({_quoted(head.NEW_BAR_NATURAL_KEY)})' in ddl, ddl

    recorder, _ = _run(head, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    (index_call,) = [call for call in recorder.create_index.call_args_list if call.args[1] == head.BAR_TABLE]
    name, _, columns = index_call.args
    assert f'CREATE INDEX {name} ON {head.BAR_TABLE} ({_quoted(columns)})' in ddl, ddl


@pytest.mark.parametrize('dead', BAR_MUST_NOT_CONTAIN)
def test_the_bar_ddl_does_not_contain_a_removed_column_or_index(dead: str):
    """The design's list (tj-vhboky.12 item 5, Amendment 1): none of these may reach a database built from the model."""
    ddl = _ddl(StockMarketActivity.__table__)
    assert not re.search(rf'\b{dead}\b', ddl), f'the bar DDL contains {dead}: {ddl}'


def test_the_bar_ddl_contains_nothing_the_revision_drops(head):
    """The same property derived from the revision body instead of the design's list.

    Every column and index upgrade() drops from the bar must be absent from the model's DDL, or the
    model describes a table the migrated database does not have. Derived, so a column the revision
    starts dropping later is covered here without an edit.
    """
    recorder, _ = _run(head, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    changes = _schema_changes(recorder, unnamed_constraint=LOOKED_UP_ENTRY_CONSTRAINT)
    dropped = sorted(
        name for table, name in (*changes['drop_column'], *changes['drop_index']) if table == head.BAR_TABLE
    )
    assert dropped, 'the revision drops nothing from the bar, so this comparison is vacuous'
    ddl = _ddl(StockMarketActivity.__table__)
    present = [name for name in dropped if re.search(rf'\b{name}\b', ddl)]
    assert present == [], f'the model still declares what the revision drops: {present}'


def test_the_entry_ddl_has_the_identity_constraint_the_revision_creates(head):
    ddl = _ddl(StoreDatasetEntry.__table__)
    assert f'CONSTRAINT {head.ENTRY_CONSTRAINT_NAME} UNIQUE ({_quoted(head.NEW_ENTRY_NATURAL_KEY)})' in ddl, ddl


def test_the_entry_expiry_is_timestamp_with_time_zone_in_the_model_and_the_revision(head):
    """tj-vhboky.12 item 5a. A plain TIMESTAMP is the defect being fixed, and it is invisible unless asserted.

    Both places: the DDL the model compiles to, and the type the revision's add_column gives the
    column on a migrated database. Either one naive makes "when does this data die" depend on the
    session timezone.
    """
    ddl = _ddl(StoreDatasetEntry.__table__)
    assert re.search(r'\bexpiry TIMESTAMP WITH TIME ZONE\b', ddl), f'the model compiles expiry as naive: {ddl}'

    recorder, _ = _run(head, 'upgrade', [LOOKED_UP_ENTRY_CONSTRAINT])
    (added,) = [
        call.args[1]
        for call in recorder.add_column.call_args_list
        if call.args[0] == head.ENTRY_TABLE and call.args[1].name == 'expiry'
    ]
    assert added.type.compile(dialect=DIALECT) == 'TIMESTAMP WITH TIME ZONE', 'the revision adds expiry as naive'
