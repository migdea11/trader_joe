"""dataset_override_log and dataset_deletion: the operator's audit trail

Revision ID: d7e2a9c35b18
Revises: b3d9e1a47c62
Create Date: 2026-10-14 00:00:00.000000

PURELY ADDITIVE (tj-grna9p.86). upgrade() creates one enum type, two tables and their indexes; it reads, alters
and deletes nothing that exists. downgrade() drops exactly what upgrade() created, in the reverse order, and so
loses the two logs -- the only data it touches, and the data the revision introduces.

BOTH TABLES ARE APPEND-ONLY: no code path updates or deletes a row.

NO FOREIGN KEY, ON PURPOSE. dataset_id references nothing: a row outlives the dataset a delete removed, which
is the point of recording the delete.

dataset_deletion.cause is a plain string, so the expiry reaper (tj-grna9p.23) records its deletions in the same
table with its own cause and no further revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7e2a9c35b18'
down_revision: Union[str, None] = 'b3d9e1a47c62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OVERRIDE_TABLE = 'dataset_override_log'
DELETION_TABLE = 'dataset_deletion'
OVERRIDE_DATASET_AT_INDEX = 'ix_dataset_override_log_dataset_id_at'
DELETION_DATASET_INDEX = 'ix_dataset_deletion_dataset_id'
DELETION_AT_INDEX = 'ix_dataset_deletion_deleted_at'

ACTION_MEMBERS = ('SET_EXPIRY', 'DELETE')


def upgrade() -> None:
    action = postgresql.ENUM(*ACTION_MEMBERS, name='override_action', create_type=False)
    # Created explicitly, so create_table below references it with create_type=False and the downgrade drops
    # exactly this one.
    action.create(op.get_bind(), checkfirst=True)

    op.create_table(
        OVERRIDE_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('action', action, nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(OVERRIDE_DATASET_AT_INDEX, OVERRIDE_TABLE, ['dataset_id', 'at'], unique=False)

    op.create_table(
        DELETION_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('asset_symbol', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('granularity', sa.String(), nullable=False),
        sa.Column('update_type', sa.String(), nullable=False),
        sa.Column('range_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('range_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('bars_deleted', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('cause', sa.String(), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(DELETION_DATASET_INDEX, DELETION_TABLE, ['dataset_id'], unique=False)
    op.create_index(DELETION_AT_INDEX, DELETION_TABLE, ['deleted_at'], unique=False)


def downgrade() -> None:
    op.drop_index(DELETION_AT_INDEX, table_name=DELETION_TABLE)
    op.drop_index(DELETION_DATASET_INDEX, table_name=DELETION_TABLE)
    op.drop_table(DELETION_TABLE)
    op.drop_index(OVERRIDE_DATASET_AT_INDEX, table_name=OVERRIDE_TABLE)
    op.drop_table(OVERRIDE_TABLE)
    postgresql.ENUM(name='override_action').drop(op.get_bind(), checkfirst=True)
