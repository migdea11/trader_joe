"""store_dataset_entry.collect: the operator's collecting intent, apart from expiry

Revision ID: e5c8b1d4f703
Revises: d7e2a9c35b18
Create Date: 2026-10-21 00:00:00.000000

PURELY ADDITIVE (tj-grna9p.176, design decision tj-hhy3dk section 1). upgrade() adds one nullable column and one
CHECK constraint, and writes the new column only; it reads, alters and deletes nothing else. No expiry is touched.

WHY. Today a DAILY or STREAM dataset is "retired" when it has an expiry, so setting a deletion time also stops
collection. The design splits the two: expiry means deletion only, and collect says whether the dataset is being
collected. A STATIC dataset is never collected on a schedule, so its collect is NULL.

WHAT IS KEPT. Every row keeps the state an observer sees today: the backfill sets collect = (expiry IS NULL) on
every DAILY and STREAM row, so a dataset with an expiry (retired today) is not collecting and one without is.

STORED INTEGERS of update_type (UpdateType in common/enums/data_stock.py): STATIC = 1, DAILY = 2, STREAM = 3.
The backfill and the CHECK name them positively (IN (2, 3)). A row holding any other update_type is not
backfilled, so the CHECK refuses it, the upgrade fails cleanly and, being one transaction, rolls back whole.

REPEAT RUNS. No IF NOT EXISTS guard: like every earlier revision here this one relies on alembic's version table
to run once, and the whole upgrade is one transaction on Postgres, so a failure leaves nothing behind.

downgrade() drops the constraint, then the column, and so loses the collecting intent -- the only data it touches.
Production policy is forward-only; recovery is by restore.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c8b1d4f703'
down_revision: Union[str, None] = 'd7e2a9c35b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'store_dataset_entry'
COLUMN = 'collect'
CHECK_NAME = 'ck_store_dataset_entry_collect_by_type'
# Stored integers of UpdateType: STATIC = 1, DAILY = 2, STREAM = 3.
STATIC = 1
COLLECTED = '2, 3'  # DAILY, STREAM


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean(), nullable=True))
    # Every DAILY and STREAM row: collecting exactly when it has no expiry. STATIC rows stay NULL, and so does a row
    # of any unknown update_type, which the CHECK below then refuses (the upgrade rolls back). Expiries are untouched.
    op.execute(f'UPDATE {TABLE} SET {COLUMN} = (expiry IS NULL) WHERE update_type IN ({COLLECTED})')  # nosec B608
    op.create_check_constraint(
        CHECK_NAME,
        TABLE,
        f'(update_type = {STATIC} AND {COLUMN} IS NULL) OR (update_type IN ({COLLECTED}) AND {COLUMN} IS NOT NULL)',
    )


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, TABLE, type_='check')
    op.drop_column(TABLE, COLUMN)
