"""Per-dataset bar identity, full entry identity, feed on the bar

Revision ID: eec8f88a7443
Revises: 8f41c2d7a3b9
Create Date: 2026-09-25 00:00:00.000000

WHY THIS IS NOT PURELY ADDITIVE, AND WHAT WAS DONE ABOUT IT
------------------------------------------------------------------------------
# additive-exception: tj-vhboky

Both tables change shape in a way an operator cannot roll forward without losing something a
column used to mean. On stock_market_activity: dataset_id rejoins the natural key (it used to be
excluded on purpose -- see the comment this revision rewrites in
data/store/app/database/models/base_market_activity.py), feed joins it too, split_factor and
dividends_factor are dropped outright, and expiry is dropped outright. On store_dataset_entry:
owner and expiry are added, expiry_type and update_type become NOT NULL, and the six-column
unique constraint is replaced by a ten-column one under a new, explicit name.

NO BACKFILL, NO PRE-DROP ASSERTION, NO LOSSY DOWNGRADE TO DESIGN (USER RULING, 2026-09-24, carried
forward from the sibling revision on feat/tj-u12tjo-dataset-entry-bar and restated on tj-vhboky.1):
"What data we have today is trivial to replace. I think it is easier just to wipe it than to
bother with any migration at this stage." Operators wipe stock_market_activity and
store_dataset_entry by hand before this revision runs -- the Postgres data directory is a BIND
MOUNT, not a Docker volume, so `docker compose down -v` removes nothing; the directory under
volumes/ has to be deleted by hand, then the stack comes back up empty and this migration runs
against nothing. So there is nothing here to preserve: no backfill of dataset_id, feed or owner
from anywhere, no pre-drop "every row already agrees with the new key" assertion, and no lossy
per-row downgrade path.

WHAT DOWNGRADE DOES AND DOES NOT RESTORE, STATED PLAINLY (the wording the head revision on this
branch, 8f41c2d7a3b9, already uses -- the revision this task named as the wording to copy,
a380773570b7, lives only on feat/tj-u12tjo-dataset-entry-bar and is not in this tree): downgrade()
below puts the SCHEMA back -- dataset_id drops out of the natural key, feed is dropped from the
bar together with its enum type, split_factor and dividends_factor are re-added NULLABLE (there is
nothing to put in them), the bar's old four-column constraint and its three dropped indexes come
back; on the entry, owner and expiry are dropped, expiry_type and update_type return to NULLABLE
with no default, and the old six-column constraint is recreated -- and NOTHING ELSE. It does not,
and cannot, restore any VALUE: feed, owner and expiry come back NULL (or absent) for every row
written under this revision, and any bar written under the new natural key is gone the moment the
old four-column constraint would collide with it. This is a one-way door in substance even though
the SQL runs in both directions: which tape a bar came from and which principal a dataset belongs
to were never durable anywhere else once this revision has run.

ONE MORE DOWNGRADE HAZARD, NOT COVERED ABOVE (tj-uxl817): if 8f41c2d7a3b9's archive table
(stock_market_activity_superseded_8f41c2d7a3b9) still holds rows, downgrading this revision and
then downgrading past 8f41c2d7a3b9 too can fail loudly instead of restoring correctly -- see the
paragraph in upgrade() step 4 below for the mechanism. Only reachable on a database that went
through 8f41c2d7a3b9 with duplicates present, i.e. the archive is non-empty; an operator not in
that situation can stop reading. Remediate by emptying or dropping that archive table before
downgrading past this revision. tj-lf7tuf is the permanent fix.

DEPLOYMENT ORDER: the migration and the code that depends on it land in the SAME release. The bar
upsert's ON CONFLICT clause targets uq_stock_market_activity_natural_key by name
(StockMarketActivity.NATURAL_KEY_CONSTRAINT) -- unchanged by this revision, but the column list
underneath it does not widen to six columns until this revision runs, and a write against the old
four-column shape colliding on a value not in the old key would surprise nobody worse than a write
against the new six-column shape landing on a database that has not been migrated yet.

FEED HAS NO SERVER DEFAULT, DELIBERATELY, AND THAT IS DIFFERENT FROM OWNER/EXPIRY_TYPE/UPDATE_TYPE
BELOW -- FEED IS THE ONE COLUMN LEFT DEPENDING ON THE WIPE (tj-uxl817 narrowed this claim; it
previously said none of the four depended on the table being empty, which was false for two of
them). owner is a genuinely NEW column: ADD COLUMN ... DEFAULT 'unassigned' NOT NULL gives every
existing row that default as part of the same statement, so it never depended on the table
already being empty. expiry_type and update_type are different -- they are EXISTING nullable
columns, not new ones, and ALTER COLUMN ... SET DEFAULT does not backfill a value into a row that
already exists the way ADD COLUMN ... DEFAULT does. Worse, alembic compiles
alter_column(nullable=False, server_default=...) as two separate statements, SET NOT NULL before
SET DEFAULT, so the constraint would have landed before the default value was even in place to
satisfy it. Both columns were nullable in 2b88043cd13c, so a pre-existing NULL was possible. This
revision therefore backfills both columns explicitly -- an UPDATE ... WHERE <column> IS NULL to
the same value as the server default -- immediately before their ALTER COLUMN statements below, so
by the time SET NOT NULL runs neither column can hold a NULL, regardless of the table's population
or alembic's statement order. feed cannot take the same fix: the enum's "no sentinel for we do not
know" ruling (tj-vhboky.1, 2026-09-25) removed the UNKNOWN member a default -- and therefore a
backfill value -- would have pointed at, on the grounds that a value which should never be written
is better expressed as an error than as a vocabulary member. So ADD COLUMN ... NOT NULL for feed
is the one step in this revision that still relies on the documented wipe above -- on a table that
is not actually empty it fails loudly with a Postgres constraint error rather than silently
writing a wrong tape into every existing row, which is the intended failure mode, not an
oversight.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_stock import Feed


# revision identifiers, used by Alembic.
revision: str = 'eec8f88a7443'
down_revision: Union[str, None] = '8f41c2d7a3b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BAR_TABLE = 'stock_market_activity'
ENTRY_TABLE = 'store_dataset_entry'

# Must stay identical to StockMarketActivity.NATURAL_KEY_CONSTRAINT. The name is unchanged by
# this revision -- only the columns underneath it widen -- so the bar upsert's ON CONFLICT ON
# CONSTRAINT keeps targeting the same name across the upgrade.
BAR_CONSTRAINT_NAME = 'uq_stock_market_activity_natural_key'
OLD_BAR_NATURAL_KEY = ['asset_symbol', 'source', 'granularity', 'timestamp']
# Must stay identical to BaseMarketActivity.NATURAL_KEY.
NEW_BAR_NATURAL_KEY = ['dataset_id', 'asset_symbol', 'source', 'feed', 'granularity', 'timestamp']
BAR_SYMBOL_GRANULARITY_TIMESTAMP_INDEX = 'ix_stock_market_activity_symbol_granularity_timestamp'

# Must stay identical to StoreDatasetEntry.NATURAL_KEY_CONSTRAINT.
ENTRY_CONSTRAINT_NAME = 'uq_store_dataset_entry_identity'
OLD_ENTRY_NATURAL_KEY = ['asset_symbol', 'granularity', 'start', 'end', 'source', 'data_type']
# Must stay identical to StoreDatasetEntry.NATURAL_KEY. feed is deliberately NOT here -- see the
# model's comment and tj-rh4b7f: the entry is upserted before the ingest adapter has resolved a
# feed, so adding it to the entry's identity is deferred to the gRPC transport work.
NEW_ENTRY_NATURAL_KEY = [
    'asset_symbol',
    'source',
    'granularity',
    'asset_type',
    'data_type',
    'owner',
    'expiry_type',
    'update_type',
    'start',
    'end',
]

FEED_ENUM_NAME = 'feed'
# WHERE THE MEMBER LIST COMES FROM, which is the one thing that must not be guessed: THIS CLASS,
# not a literal list of strings. Composition (common/enums/composed_enum.py) runs at import time,
# so by the time this module body executes Feed is already the full superset of every per-market
# subset. A literal list gets no error the day a market is added and fails at runtime on the
# first insert of the new value instead.
FEED_TYPE = postgresql.ENUM(
    Feed,
    name=FEED_ENUM_NAME,
    # sa.Enum persists the member NAME by default. Every current Feed member's name equals its
    # value (IEX, SIP, NOT_APPLICABLE), so this makes no visible difference today -- it is here
    # so the database label matches the string Pydantic and the API use the day that stops being
    # true, rather than silently drifting until an insert with a value/name mismatch fails.
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    # The type is created and dropped explicitly, once, below -- not implicitly by
    # add_column with an inline Enum, which does not reliably emit CREATE TYPE outside of a full
    # create_table on every SQLAlchemy/Postgres combination. create_type=False is a
    # postgresql.ENUM keyword, not a generic sa.Enum one -- the generic type accepts and silently
    # discards it into **kw, so this guarantee held by accident (of add_column's own compiled
    # output) rather than by this flag until postgresql.ENUM replaced sa.Enum here (tj-uxl817).
    create_type=False,
)


def _find_unnamed_unique_constraint(bind, table: str, min_columns: int) -> str:
    """Look up a Postgres-generated unique constraint name from the live catalogue.

    The constraint this revision replaces was declared with no ``name=`` on both the model and
    the initial migration (2b88043cd13c), so Postgres generated one at CREATE TABLE time and
    truncated it to 63 characters. A migration that drops it by a guessed name passes every test
    in this container -- there is no Postgres here -- and fails on the user's host the moment the
    guess is wrong. min_columns filters out the table's other unique constraints (store_dataset_
    entry also carries a single-column UNIQUE on id from the initial migration).
    """
    conname = bind.execute(
        sa.text("""
            SELECT conname FROM pg_constraint
             WHERE conrelid = CAST(:table AS regclass)
               AND contype = 'u'
               AND array_length(conkey, 1) >= :min_columns
        """),
        {'table': table, 'min_columns': min_columns},
    ).scalar_one()
    return conname


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1: create the feed enum type once, explicitly. Only the bar uses it in this revision
    # (feed on the entry is deferred, tj-rh4b7f), but it is created the same way the sibling
    # revision on feat/tj-u12tjo-dataset-entry-bar does it, in case a later revision adds a
    # second column of this type.
    FEED_TYPE.create(bind, checkfirst=True)

    # Step 2: store_dataset_entry.
    old_entry_constraint = _find_unnamed_unique_constraint(bind, ENTRY_TABLE, min_columns=2)
    op.drop_constraint(old_entry_constraint, ENTRY_TABLE, type_='unique')

    op.add_column(ENTRY_TABLE, sa.Column('owner', sa.String(), nullable=False, server_default='unassigned'))
    # Plain nullable DateTime(timezone=True), not the NullableDateTime custom type `end` uses --
    # expiry is a value the request asks for, not part of the identity, so there is no need to
    # map an absent value to a sentinel to keep it out of the unique index below. timezone=True
    # on purpose: the bar's old expiry column was naive DateTime while every other timestamp
    # here is timestamptz, and a naive value bound to timestamptz is interpreted in the session
    # timezone -- a silent, environment-dependent wrong answer about when data dies.
    op.add_column(ENTRY_TABLE, sa.Column('expiry', sa.DateTime(timezone=True), nullable=True))

    # expiry_type and update_type join the unique constraint below. NOT NULL is load-bearing
    # there: Postgres treats NULL as distinct from NULL in a unique index, so a nullable column
    # inside this constraint would make every null-bearing row unique and silently stop
    # ON CONFLICT from ever matching a repeat. The server defaults match the schema defaults
    # (ExpiryType.BULK, UpdateType.STATIC), both value 1.
    #
    # Backfill BEFORE the alter, not after: ALTER COLUMN ... SET DEFAULT never touches an
    # existing row, and alembic compiles the alter below as SET NOT NULL followed by SET
    # DEFAULT -- the constraint would land before the default is even in place to satisfy it.
    # Both columns were nullable in 2b88043cd13c, so a pre-existing NULL is possible. This
    # makes the module docstring's claim true instead of documenting the sharp edge (tj-uxl817).
    op.execute(f'UPDATE {ENTRY_TABLE} SET expiry_type = 1 WHERE expiry_type IS NULL')  # nosec B608
    op.execute(f'UPDATE {ENTRY_TABLE} SET update_type = 1 WHERE update_type IS NULL')  # nosec B608

    op.alter_column(ENTRY_TABLE, 'expiry_type', existing_type=sa.Integer(), nullable=False, server_default='1')
    op.alter_column(ENTRY_TABLE, 'update_type', existing_type=sa.Integer(), nullable=False, server_default='1')

    op.create_unique_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, NEW_ENTRY_NATURAL_KEY)

    # Step 3: stock_market_activity.
    op.drop_constraint(BAR_CONSTRAINT_NAME, BAR_TABLE, type_='unique')

    # Single-column indexes on dataset_id and expiry (Column(..., index=True) in the pre-revision
    # model) and the composite ix_dataset_id_timestamp all reference columns this revision drops
    # or folds into a constraint that already covers the read they served. Dropped explicitly
    # rather than relying on Postgres to cascade them away, matching this project's style of
    # never leaving a schema change implicit (see 2b88043cd13c).
    op.drop_index('ix_dataset_id_timestamp', table_name=BAR_TABLE)
    op.drop_index('ix_stock_market_activity_dataset_id', table_name=BAR_TABLE)
    op.drop_index('ix_stock_market_activity_expiry', table_name=BAR_TABLE)

    # No server_default -- see the module docstring for why none is valid, and why relying on the
    # documented wipe-before-migrate is the deliberate, safe choice here.
    op.add_column(BAR_TABLE, sa.Column('feed', FEED_TYPE, nullable=False))

    op.drop_column(BAR_TABLE, 'split_factor')
    op.drop_column(BAR_TABLE, 'dividends_factor')
    op.drop_column(BAR_TABLE, 'expiry')

    op.create_unique_constraint(BAR_CONSTRAINT_NAME, BAR_TABLE, NEW_BAR_NATURAL_KEY)
    # Reads asset_symbol, granularity and a timestamp range and NEVER source or feed
    # (read_market_activity_data, verified) -- a value-identity-leading index would give that
    # read a one-column equality prefix and scan every bar for the symbol across all vendors,
    # tapes and time. This order gives it two equality columns and an ordered range, and still
    # serves a future cross-dataset correction UPDATE well (filters symbol, source, feed,
    # granularity and a range): two equality columns and the range from the index, source and
    # feed checked on the rows it already has to touch.
    op.create_index(BAR_SYMBOL_GRANULARITY_TIMESTAMP_INDEX, BAR_TABLE, ['asset_symbol', 'granularity', 'timestamp'])

    # Step 4: the archive table from 8f41c2d7a3b9, stock_market_activity_superseded_8f41c2d7a3b9,
    # is left exactly as it is. Not touched here, and not cleaned up here -- tj-n3terv owns
    # deliberate cleanup of a kept archive.
    #
    # ITS DOWNGRADE IS NOT SAFE AGAINST THE FULL CHAIN WHEN THE ARCHIVE STILL HOLDS ROWS
    # (tj-uxl817; this paragraph corrects a previous claim that it was). 8f41c2d7a3b9's own
    # downgrade restores archived rows with `INSERT INTO {TABLE} SELECT * FROM restorable`,
    # which maps columns POSITIONALLY. The archive was created by that revision's own upgrade
    # as `LIKE {TABLE}` at THAT revision's column order, where split_factor and dividends_factor
    # sit at positions 7 and 8. This revision's downgrade puts those two columns (and expiry)
    # back, but Postgres ADD COLUMN always APPENDS -- on the live table they land at the END,
    # not at 7 and 8. So a downgrade of this revision followed by a downgrade of 8f41c2d7a3b9,
    # on a database whose archive still holds rows, restores against that mismatched order --
    # e.g. archive.split_factor (float8) lands positionally on live id (int4). No such cast
    # exists, so it errors and rolls back rather than mis-writing a row: loud, not silent, and
    # only reachable when the archive is non-empty, i.e. a database that went through
    # 8f41c2d7a3b9 with duplicates present. The permanent fix is to name the columns on both
    # sides of that INSERT in 8f41c2d7a3b9 instead of relying on SELECT *, which removes the
    # positional coupling outright; it is deferred out of this revision because it requires a
    # matching change to the substring assertions in
    # data/store/tests/test_market_activity_idempotency.py (_the_restore and the two tests that
    # match on the literal text `INSERT INTO {table} SELECT`), and that file is validator-owned.


def downgrade() -> None:
    bind = op.get_bind()

    # ARCHIVE HAZARD, READ BEFORE DOWNGRADING PAST THIS REVISION (tj-uxl817): if 8f41c2d7a3b9's
    # archive table (stock_market_activity_superseded_8f41c2d7a3b9) still holds rows, downgrading
    # this revision and then that one restores them against a mismatched column order -- see the
    # paragraph in upgrade() step 4 above for the mechanism. Only reachable if that archive is
    # non-empty, i.e. a database that went through 8f41c2d7a3b9 with duplicates present.
    # Remediate by emptying or dropping the archive table before downgrading past this revision.
    # tj-lf7tuf is the permanent fix.

    # Inverse of step 3.
    op.drop_index(BAR_SYMBOL_GRANULARITY_TIMESTAMP_INDEX, table_name=BAR_TABLE)
    op.drop_constraint(BAR_CONSTRAINT_NAME, BAR_TABLE, type_='unique')

    # NULLABLE, no default: there is nothing to put in a restored split_factor/dividends_factor
    # (see module docstring), so they come back empty rather than falsely populated.
    op.add_column(BAR_TABLE, sa.Column('split_factor', sa.Float(), nullable=True))
    op.add_column(BAR_TABLE, sa.Column('dividends_factor', sa.Float(), nullable=True))
    # Naive DateTime, matching the column this revision dropped -- not DateTime(timezone=True).
    op.add_column(BAR_TABLE, sa.Column('expiry', sa.DateTime(), nullable=True))

    op.drop_column(BAR_TABLE, 'feed')

    op.create_index('ix_dataset_id_timestamp', BAR_TABLE, ['dataset_id', 'timestamp'], unique=False)
    op.create_index('ix_stock_market_activity_dataset_id', BAR_TABLE, ['dataset_id'], unique=False)
    op.create_index('ix_stock_market_activity_expiry', BAR_TABLE, ['expiry'], unique=False)

    op.create_unique_constraint(BAR_CONSTRAINT_NAME, BAR_TABLE, OLD_BAR_NATURAL_KEY)

    # Inverse of step 2.
    op.drop_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, type_='unique')

    op.alter_column(ENTRY_TABLE, 'update_type', existing_type=sa.Integer(), nullable=True, server_default=None)
    op.alter_column(ENTRY_TABLE, 'expiry_type', existing_type=sa.Integer(), nullable=True, server_default=None)

    op.drop_column(ENTRY_TABLE, 'expiry')
    op.drop_column(ENTRY_TABLE, 'owner')

    # Unnamed, matching how 2b88043cd13c originally created it (inside CREATE TABLE, with no
    # name= passed), so Postgres assigns it the same conventional, truncated name it always would
    # have.
    op.create_unique_constraint(None, ENTRY_TABLE, OLD_ENTRY_NATURAL_KEY)

    # Inverse of step 1. Dropped last, once both tables' feed columns are gone.
    FEED_TYPE.drop(bind, checkfirst=True)
