"""dataset_read and dataset_read_daily: the consumption record of a dataset's bars

Revision ID: b3d9e1a47c62
Revises: 6f5a798746a1
Create Date: 2026-10-14 00:00:00.000000

PURELY ADDITIVE (tj-grna9p.46). upgrade() creates two enum types, two tables and their indexes; it reads,
alters and deletes nothing that exists. downgrade() drops exactly what upgrade() created, in the reverse
order, and so loses the usage record it holds -- the only data it touches, and the data the revision
introduces.

NO FOREIGN KEY, ON PURPOSE. dataset_id references nothing: usage summaries outlive a deleted dataset.

THE READER'S LABEL IS client_id, NOT A PRINCIPAL (tj-grna9p.131 OPTION R): the existing dataset owner string,
carried in the X-Client-Id header. The column is a plain String in DDL, like store_dataset_entry.owner; the
model types it SensitiveString, which changes no DDL. There is no reader column.

THE ENUM TYPES. read_client_kind (UI, SDK) is used by both tables; dataset_read_kind (READ, STREAM_SESSION)
by dataset_read only. Both are created here and dropped by downgrade().
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b3d9e1a47c62'
down_revision: Union[str, None] = '6f5a798746a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

READ_TABLE = 'dataset_read'
DAILY_TABLE = 'dataset_read_daily'
DATASET_READ_AT_INDEX = 'ix_dataset_read_dataset_id_read_at'
READ_AT_INDEX = 'ix_dataset_read_read_at'
DAILY_UNIQUE = 'uq_dataset_read_daily_dataset_id_client_id_day'
DAILY_DAY_INDEX = 'ix_dataset_read_daily_day'

CLIENT_KIND_MEMBERS = ('UI', 'SDK')
READ_KIND_MEMBERS = ('READ', 'STREAM_SESSION')


def upgrade() -> None:
    client_kind = postgresql.ENUM(*CLIENT_KIND_MEMBERS, name='read_client_kind', create_type=False)
    read_kind = postgresql.ENUM(*READ_KIND_MEMBERS, name='dataset_read_kind', create_type=False)
    # Created explicitly, so create_table below can reference them with create_type=False and the
    # downgrade drops exactly these two.
    client_kind.create(op.get_bind(), checkfirst=True)
    read_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        READ_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=False),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('client_kind', client_kind, nullable=False),
        sa.Column('kind', read_kind, server_default='READ', nullable=False),
        sa.Column('route', sa.String(), nullable=False),
        sa.Column('bars_returned', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('session_opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('session_closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(DATASET_READ_AT_INDEX, READ_TABLE, ['dataset_id', 'read_at'], unique=False)
    op.create_index(READ_AT_INDEX, READ_TABLE, ['read_at'], unique=False)

    op.create_table(
        DAILY_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=False),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('client_kind', client_kind, nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('reads', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('sessions', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('bars_returned', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'client_id', 'day', name=DAILY_UNIQUE),
    )
    op.create_index(DAILY_DAY_INDEX, DAILY_TABLE, ['day'], unique=False)


def downgrade() -> None:
    op.drop_index(DAILY_DAY_INDEX, table_name=DAILY_TABLE)
    op.drop_table(DAILY_TABLE)
    op.drop_index(READ_AT_INDEX, table_name=READ_TABLE)
    op.drop_index(DATASET_READ_AT_INDEX, table_name=READ_TABLE)
    op.drop_table(READ_TABLE)
    postgresql.ENUM(name='dataset_read_kind').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='read_client_kind').drop(op.get_bind(), checkfirst=True)
