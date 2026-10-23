"""override_action gains SET_COLLECTING: the operator's collect write is audited like the expiry write

Revision ID: a6d3f8e1c205
Revises: e5c8b1d4f703
Create Date: 2026-10-21 00:00:00.000000

PURELY ADDITIVE (tj-grna9p.183). upgrade() adds one value to the native enum type override_action, created by
d7e2a9c35b18 with ('SET_EXPIRY', 'DELETE'); it reads, alters and deletes nothing that exists. No row is backfilled.

WHY. PATCH /ui/v1/datasets/{id}/collecting records an override-log row for another owner's dataset, with the action
SET_COLLECTING (OverrideAction.SET_COLLECTING in app/database/models/dataset_override_log.py). The model maps the
Python members to the type's labels by VALUE (values_callable), and the value is spelled upper case like the two
existing ones, so the Python member, its value and the native label are the same string.

REPEAT RUNS. ADD VALUE IF NOT EXISTS makes a repeat a no-op. Postgres allows ADD VALUE inside the migration's
transaction (12 and later); the new value cannot be USED in that same transaction, and none is.

THE ENUM-VALUE LIMITATION AND THE REFUSING DOWNGRADE. Postgres cannot drop a value from an enum type. downgrade()
therefore recreates the type without it: rename override_action, create it again with the two old values, convert
the column with USING action::text::override_action, drop the renamed type, all in the downgrade's one transaction.
dataset_override_log is APPEND-ONLY audit, so a downgrade never deletes a row to get past the conversion: when any
row holds SET_COLLECTING it raises, with a message, before changing anything, and the revision stays where it was.
Without such a row it is the exact inverse of upgrade().
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a6d3f8e1c205'
down_revision: Union[str, None] = 'e5c8b1d4f703'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'dataset_override_log'
TYPE_NAME = 'override_action'
OLD_TYPE_NAME = 'override_action_old'
NEW_MEMBER = 'SET_COLLECTING'
# The members before this revision (d7e2a9c35b18).
PREVIOUS_MEMBERS = ('SET_EXPIRY', 'DELETE')


def upgrade() -> None:
    op.execute(f"ALTER TYPE {TYPE_NAME} ADD VALUE IF NOT EXISTS '{NEW_MEMBER}'")


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(f'SELECT count(*) FROM {TABLE} WHERE action::text = :member'),  # nosec B608
        {'member': NEW_MEMBER},
    ).scalar_one()
    if used:
        raise RuntimeError(
            f'Cannot downgrade: {used} row(s) of {TABLE} hold the action {NEW_MEMBER}. The table is append-only '
            f'audit and no row is deleted to let a downgrade pass; nothing was changed.'
        )
    op.execute(f'ALTER TYPE {TYPE_NAME} RENAME TO {OLD_TYPE_NAME}')
    postgresql.ENUM(*PREVIOUS_MEMBERS, name=TYPE_NAME).create(bind)
    op.execute(f'ALTER TABLE {TABLE} ALTER COLUMN action TYPE {TYPE_NAME} USING action::text::{TYPE_NAME}')
    op.execute(f'DROP TYPE {OLD_TYPE_NAME}')
