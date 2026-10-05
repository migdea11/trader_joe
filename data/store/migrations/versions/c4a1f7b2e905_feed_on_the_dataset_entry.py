"""Feed on the dataset entry: an identity column, and the identity constraint rebuilt around it

Revision ID: c4a1f7b2e905
Revises: eec8f88a7443
Create Date: 2026-10-03 00:00:00.000000

THIS REVISION DESTROYS DATA. READ THIS PARAGRAPH BEFORE RUNNING IT.
------------------------------------------------------------------------------
upgrade() DELETEs every row of stock_market_activity and every row of store_dataset_entry. Not a
backfill, not a partial cleanup -- both tables come out empty, and downgrade() does not and cannot
put the rows back.

WHY, AS RULED (user, 02:31 UTC 2026-09-28, closing tj-3mk3u5.22 Q6, carried onto tj-3mk3u5.31 by
the architect's amendment of 01:15 UTC 2026-10-02): there is no dataset data worth preserving at
this stage, so the feed column is added without a backfill. The RECOMMENDED alternative -- derive
each entry's feed from its own bars, give a bar-less entry iex, and assert loudly that no entry
holds bars of two feeds -- was considered and SUPERSEDED by that ruling. There is therefore no
backfill here, no mixed-feed assertion, and no sentinel: the "no sentinel for we do not know"
ruling (tj-vhboky.1, 2026-09-25) removed the UNKNOWN member that a server default would have had
to point at, on the grounds that a value which should never be written is better expressed as an
error than as a vocabulary member. tj-vhboky.1 item 1 restates it for this column specifically:
NOT NULL, no server default.

The deletes are what make that safe rather than merely declared. ADD COLUMN feed ... NOT NULL with
no default is rejected by Postgres on a table that holds rows, so on a populated database this
revision would fail loudly at that statement -- the intended failure mode when a wipe has NOT
happened. eec8f88a7443 relied on the operator having wiped by hand (the Postgres data directory is
a BIND MOUNT, so `docker compose down -v` removes nothing and the directory under volumes/ has to
be deleted by hand). This revision does not rely on that: it does the deleting itself, in the same
transaction as the schema change, so a run against a populated database destroys the data the
ruling said was disposable instead of stopping half way.

WHY THIS IS NOT PURELY ADDITIVE, AND WHAT WAS DONE ABOUT IT
------------------------------------------------------------------------------
# additive-exception: tj-3mk3u5.31

Two departures from the additive rule, declared under the same exception protocol eec8f88a7443
used, for the same kind of change:

1. THE DELETES above. Rows go away; they do not come back.
2. RECREATING uq_store_dataset_entry_identity. feed joins the entry's identity (tj-f2qz44, decision
   tj-xn3qa6 D1), and a unique constraint's column list cannot be widened in place -- the old
   ten-column constraint is dropped and an eleven-column one is created under the SAME name. There
   is a window inside this revision's transaction during which the table carries no identity
   constraint; env.py wraps the whole run in a single context.begin_transaction(), so no other
   session sees it.

The constraint is addressed BY ITS NAME, not by a guess and not by a catalogue lookup, and the
difference from eec8f88a7443 is deliberate: the constraint that revision dropped was UNNAMED
(Postgres generated and truncated a name at CREATE TABLE time), which is why it had to read
pg_constraint to find it. This one was created BY eec8f88a7443 with an explicit name that
StoreDatasetEntry.NATURAL_KEY_CONSTRAINT and the bar upsert's ON CONFLICT both already depend on.
Looking it up would be looking up a value this module can state.

WHAT DOWNGRADE DOES AND DOES NOT RESTORE, STATED PLAINLY
------------------------------------------------------------------------------
downgrade() puts the SCHEMA back: the eleven-column constraint is dropped, feed is dropped from
store_dataset_entry, and the ten-column constraint is recreated under the same name. NOTHING ELSE,
and in particular:

* IT RESTORES NO ROWS. The bars and entries upgrade() deleted are gone. downgrade() issues no
  INSERT and has nothing to insert from -- there is no archive table here, by design, because the
  ruling above is that the data is disposable rather than worth staging.
* IT RESTORES NO TAPE. Which feed served a dataset was durable nowhere else once this revision had
  run, so dropping the column loses it outright for every entry written after the upgrade. This is
  a one-way door in substance even though the SQL runs in both directions.
* IT CAN REFUSE, AND REFUSING IS CORRECT. Recreating the ten-column constraint FAILS if two entries
  differ only by feed -- which is ROUTINE after this revision, since that is the exact state it
  exists to make possible (tj-f2qz44). "The constraint will be checked immediately, so the table
  data must satisfy the constraint before it can be added" (PostgreSQL, "Adding a Constraint",
  https://www.postgresql.org/docs/current/ddl-alter.html). Nothing is deleted to make room: the
  downgrade refuses and the whole invocation rolls back to the schema and rows it started from,
  because env.py wraps the entire run in one transaction rather than one per migration. An operator
  who means to downgrade past this revision must first decide, by hand, which of each pair of
  same-window different-tape entries to keep.

THE FEED ENUM TYPE IS NEITHER CREATED NOR DROPPED HERE. eec8f88a7443 created the Postgres type
named 'feed' for the bar's column and drops it in its own downgrade; stock_market_activity still
uses it on both sides of this revision, so creating it would be a no-op and dropping it would break
the bar. The type object below is declared with create_type=False for exactly that reason.

DEPLOYMENT ORDER: the migration and the code that depends on it land in the SAME release.
AssetDatasetStoreCreate.feed is required and the entry upsert writes it, so the application cannot
run against a database that has not had this revision applied; and this revision's wipe means there
is no older data for a newer binary to misread.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_stock import Feed


# revision identifiers, used by Alembic.
revision: str = 'c4a1f7b2e905'
down_revision: Union[str, None] = 'eec8f88a7443'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BAR_TABLE = 'stock_market_activity'
ENTRY_TABLE = 'store_dataset_entry'

# Must stay identical to StoreDatasetEntry.NATURAL_KEY_CONSTRAINT. The NAME does not change across
# this revision -- only the column list underneath it widens -- so the entry upsert's
# ON CONFLICT ON CONSTRAINT keeps targeting the same name on both sides of the upgrade.
ENTRY_CONSTRAINT_NAME = 'uq_store_dataset_entry_identity'

# Must stay identical to the tuple eec8f88a7443 created, which is what downgrade() restores.
OLD_ENTRY_NATURAL_KEY = [
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
# Must stay identical to StoreDatasetEntry.NATURAL_KEY. feed sits last among the equality columns,
# immediately before the range: the own-overlap check does NOT key on feed (tj-xn3qa6 D1), so any
# earlier position would truncate the index prefix that check can use. See the model's comment.
NEW_ENTRY_NATURAL_KEY = [
    'asset_symbol',
    'source',
    'granularity',
    'asset_type',
    'data_type',
    'owner',
    'expiry_type',
    'update_type',
    'feed',
    'start',
    'end',
]

FEED_ENUM_NAME = 'feed'
# THE MEMBER LIST COMES FROM THE CLASS, never from a literal list of strings -- the same reasoning
# eec8f88a7443 spells out: composition (common/enums/composed_enum.py) runs at import time, so Feed
# is already the full superset by the time this module body executes, and a literal list would get
# no error the day a market is added and fail at runtime on the first insert instead.
FEED_TYPE = postgresql.ENUM(
    Feed,
    name=FEED_ENUM_NAME,
    # The database label is the member VALUE, matching the bar's column exactly. Identical today
    # (IEX, SIP, NOT_APPLICABLE all have name == value); here so the two columns of this one shared
    # type cannot drift the day that stops being true.
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    # The type already exists -- eec8f88a7443 created it for the bar. create_type=False stops
    # add_column emitting a CREATE TYPE that would fail against it. It is a postgresql.ENUM
    # keyword, not a generic sa.Enum one (the generic type silently discards it into **kw).
    create_type=False,
)


def upgrade() -> None:
    # Step 1: EMPTY BOTH TABLES. See the module docstring -- this is the ruled alternative to a
    # backfill, not a cleanup step. Bars first and entries second: the bar's dataset_id foreign key
    # is ON DELETE CASCADE, so deleting the entries alone would take the bars with it, but doing it
    # in this order states the intent rather than relying on the cascade to mean it.
    #
    # DELETE, not TRUNCATE. TRUNCATE on the entry table would need CASCADE to get past the foreign
    # key, and TRUNCATE ... CASCADE reaches every referencing table transitively -- a blunt
    # instrument aimed by the schema rather than by this revision. These two tables are named here.
    op.execute(f'DELETE FROM {BAR_TABLE}')  # nosec B608
    op.execute(f'DELETE FROM {ENTRY_TABLE}')  # nosec B608

    # Step 2: the identity constraint comes off BEFORE the column goes on, so the constraint is
    # only ever created once, already carrying feed.
    op.drop_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, type_='unique')

    # Step 3: the column. NOT NULL with NO server_default -- the "no sentinel" ruling. Valid only
    # because step 1 emptied the table; against rows, Postgres refuses this statement, which is the
    # intended failure rather than an oversight.
    op.add_column(ENTRY_TABLE, sa.Column('feed', FEED_TYPE, nullable=False))

    # Step 4: the identity constraint, rebuilt around eleven columns under the same name.
    op.create_unique_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, NEW_ENTRY_NATURAL_KEY)


def downgrade() -> None:
    # READ THE MODULE DOCSTRING FIRST. This restores the SCHEMA and no data: the rows upgrade()
    # deleted stay deleted, and every entry written since loses the tape it recorded. The last
    # statement here REFUSES outright if two entries differ only by feed -- routine after this
    # revision -- and the whole invocation then rolls back rather than dropping one of them.
    op.drop_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, type_='unique')

    op.drop_column(ENTRY_TABLE, 'feed')

    # The feed enum TYPE is deliberately left in place: stock_market_activity still has a column of
    # it. eec8f88a7443's own downgrade drops it, after the bar's column goes.
    op.create_unique_constraint(ENTRY_CONSTRAINT_NAME, ENTRY_TABLE, OLD_ENTRY_NATURAL_KEY)
