"""Bar natural key: archive superseded duplicates, then add the unique constraint

Revision ID: 8f41c2d7a3b9
Revises: 2b88043cd13c
Create Date: 2026-09-21 00:00:00.000000

WHY THIS IS NOT PURELY ADDITIVE, AND WHAT WAS DONE ABOUT IT
------------------------------------------------------------------------------
# additive-exception: tj-3mk3u5.3

stock_market_activity today has a surrogate integer primary key and only a NON-UNIQUE
index on (dataset_id, timestamp), so re-fetching an overlapping window stores the same
bar twice. Adding the natural-key unique constraint therefore cannot succeed on a
database that already holds duplicates - the constraint creation itself would fail.

Duplicate rows must leave the table before the constraint can be added. Rather than
DELETE them, this migration MOVES every superseded row into a plain archive table,
stock_market_activity_superseded_8f41c2d7a3b9, created by this revision. Neither
upgrade() nor downgrade() destroys a row, and downgrade() puts the archived rows back.
That keeps the rollback window - which is exactly the additive window - intact.

WHAT IS STILL LOST, STATED PLAINLY:
  * Downgrade restores the archived rows, but not the pre-migration column VALUES of
    the rows that survived. Once the new ON CONFLICT DO UPDATE write path has refreshed
    a surviving bar, downgrade cannot un-refresh it.
  * Downgrade MOVES back only those archived rows whose dataset_id still exists in
    store_dataset_entry. The archive deliberately carries no foreign key, so it outlives
    a cascade delete; re-inserting such a row would violate the live foreign key. Those
    rows are NOT destroyed either: when any row cannot be restored, downgrade LEAVES THE
    ARCHIVE TABLE IN PLACE and logs the count, and the table is dropped only when every
    archived row went back. Such a row was doomed regardless - its dataset entry is gone
    and ON DELETE CASCADE would have removed it had this revision never run - but the
    additive-exception marker above exists to be audited, and a downgrade that quietly
    dropped the table would destroy the rows the exception is granted to preserve.
    Deliberate cleanup of a kept archive is tj-n3terv.
  * The archive table is left in place on upgrade. It is dead weight once the rollback
    window has closed and an operator must drop it deliberately - that is the point, an
    automatic drop would be the destructive step this revision exists to avoid.

RE-RUNNING UPGRADE AFTER A DOWNGRADE THAT KEPT THE ARCHIVE (tj-1945as)
------------------------------------------------------------------------------
Roll back, fix, roll forward is the scenario the rollback window exists for, so the pair
has to survive being run twice. Two properties make it work, and neither destroys a row:

  * downgrade() restores with a SINGLE data-modifying statement that DELETEs the
    restorable rows out of the archive and INSERTs them into the live table. A restored
    row is MOVED, not copied, so it cannot be archived a second time and double-counted.
    An archive that survives a downgrade therefore holds EXACTLY the un-restorable rows.
  * upgrade() creates the archive with CREATE TABLE IF NOT EXISTS. On a re-upgrade the
    surviving archive is REUSED: every row already in it is left exactly as it is -
    untouched, not deleted, not re-archived - and the newly superseded rows are appended
    alongside them. Nothing clears the archive on the upgrade path; only a downgrade that
    empties it drops it, and only an operator drops one that is kept (tj-n3terv).

Dropping the archive at the start of upgrade() would make the re-run trivially work and is
exactly what this revision may not do: those rows are the rollback window.

WHICH ROW SURVIVES: the most recently written row per natural key, ordered by
updated_at DESC then id DESC. id is a serial, so it breaks ties by insertion order.
"""

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f41c2d7a3b9'
down_revision: Union[str, None] = '2b88043cd13c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger('alembic.runtime.migration')

TABLE = 'stock_market_activity'
ARCHIVE_TABLE = 'stock_market_activity_superseded_8f41c2d7a3b9'
CONSTRAINT_NAME = 'uq_stock_market_activity_natural_key'
# Must stay identical to BaseMarketActivity.NATURAL_KEY and to the constraint targeted by
# the ON CONFLICT clause in data/store/app/database/crud/stock/asset_market_activity.py.
NATURAL_KEY = ['asset_symbol', 'source', 'granularity', 'timestamp']


def _count(bind, table: str) -> int:
    return bind.execute(sa.text(f'SELECT count(*) FROM {table}')).scalar_one()  # nosec B608


def upgrade() -> None:
    bind = op.get_bind()
    before = _count(bind, TABLE)

    # LIKE copies the column definitions and their order but no constraints, indexes or
    # foreign keys. Column order matters: the move below is an INSERT ... SELECT *.
    # IF NOT EXISTS so a re-upgrade after a downgrade that kept the archive reuses it and
    # appends, rather than failing outright (tj-1945as). Rows already in it are untouched.
    op.execute(f'CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (LIKE {TABLE} INCLUDING DEFAULTS)')

    # One statement so the delete and the archive insert cannot come apart.
    op.execute(f"""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY {', '.join(NATURAL_KEY)}
                       ORDER BY updated_at DESC, id DESC
                   ) AS rn
              FROM {TABLE}
        ),
        superseded AS (
            DELETE FROM {TABLE}
             WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
         RETURNING *
        )
        INSERT INTO {ARCHIVE_TABLE} SELECT * FROM superseded
    """)  # nosec B608

    archived = _count(bind, ARCHIVE_TABLE)
    after = _count(bind, TABLE)
    log.info(f'{TABLE}: {before} rows before, {after} after, {archived} archived to {ARCHIVE_TABLE}')

    op.create_unique_constraint(CONSTRAINT_NAME, TABLE, NATURAL_KEY)


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_constraint(CONSTRAINT_NAME, TABLE, type_='unique')

    archived = _count(bind, ARCHIVE_TABLE)

    # A MOVE, mirroring upgrade(): only rows whose dataset entry still exists go back - the
    # rest would violate the live foreign key - and the ones that go back LEAVE the archive
    # in the same statement. Copying instead would let a re-upgrade archive them twice
    # (tj-1945as). One statement so the delete and the insert cannot come apart.
    op.execute(f"""
        WITH restorable AS (
            DELETE FROM {ARCHIVE_TABLE} a
             WHERE EXISTS (SELECT 1 FROM store_dataset_entry d WHERE d.id = a.dataset_id)
         RETURNING *
        )
        INSERT INTO {TABLE} SELECT * FROM restorable
    """)  # nosec B608

    # What is left is exactly what could not be restored.
    unrestorable = _count(bind, ARCHIVE_TABLE)
    log.info(f'{TABLE}: {archived - unrestorable} rows restored from {ARCHIVE_TABLE}')

    if unrestorable:
        # Dropping the archive here would DESTROY these rows, and this revision's additive
        # exception is granted on the claim that it destroys none. See the module docstring.
        log.warning(
            f'{ARCHIVE_TABLE}: kept, {unrestorable} rows not restored, dataset entry gone. '
            f'Drop it deliberately once reviewed (tj-n3terv).'
        )
    else:
        op.execute(f'DROP TABLE {ARCHIVE_TABLE}')
