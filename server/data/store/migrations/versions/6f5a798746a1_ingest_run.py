"""ingest_run: one row per attempt to collect a dataset

Revision ID: 6f5a798746a1
Revises: c4a1f7b2e905
Create Date: 2026-10-11 00:00:00.000000

PURELY ADDITIVE (tj-grna9p.18). upgrade() creates two enum types, one table and three indexes; it
reads, alters and deletes nothing that exists. downgrade() drops exactly what upgrade() created, in
the reverse order, and so loses the run history it holds -- that is the only data it touches, and
that data is the thing the revision introduces.

NO FOREIGN KEY, ON PURPOSE. dataset_id is nullable and references nothing: a run outlives its
dataset (tj-grna9p.8 item 1), and a run that has not yet been acknowledged has no dataset id at
all. request_id is likewise unconstrained: it is the link the scheduler's request log will use
(tj-pd41mt), and no request log exists yet.

THE ENUM TYPES. ingest_run_trigger and ingest_run_state are created here and dropped by downgrade().
The label columns reuse the Postgres types the entry already created (datasource, assettype,
datatype, granularity, feed) with create_type=False, so this revision neither creates nor drops
them: other tables still hold columns of each.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity


# revision identifiers, used by Alembic.
revision: str = '6f5a798746a1'
down_revision: Union[str, None] = 'c4a1f7b2e905'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'ingest_run'
DATASET_STARTED_INDEX = 'ix_ingest_run_dataset_id_started_at_id'
RUNNING_INDEX = 'ix_ingest_run_running_dataset_id'
FINISHED_INDEX = 'ix_ingest_run_finished_at'

TRIGGER_MEMBERS = ('REQUEST', 'SCHEDULE', 'RETRY')
STATE_MEMBERS = ('RUNNING', 'SUCCEEDED', 'EMPTY', 'FAILED')


def _existing_enum(enum_cls: type, name: str) -> postgresql.ENUM:
    """An enum type another revision already created, referenced and never created or dropped here."""
    return postgresql.ENUM(enum_cls, name=name, create_type=False)


def upgrade() -> None:
    trigger_type = postgresql.ENUM(*TRIGGER_MEMBERS, name='ingest_run_trigger', create_type=False)
    state_type = postgresql.ENUM(*STATE_MEMBERS, name='ingest_run_state', create_type=False)
    # Created explicitly, so create_table below can reference them with create_type=False and the
    # downgrade drops exactly these two.
    trigger_type.create(op.get_bind(), checkfirst=True)
    state_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=True),
        sa.Column('request_id', sa.UUID(), nullable=True),
        sa.Column('trigger', trigger_type, nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('asset_symbol', sa.String(), nullable=False),
        sa.Column('asset_type', _existing_enum(AssetType, 'assettype'), nullable=False),
        sa.Column('data_type', _existing_enum(DataType, 'datatype'), nullable=False),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('granularity', _existing_enum(Granularity, 'granularity'), nullable=False),
        sa.Column('update_type', sa.Integer(), nullable=False),
        sa.Column(
            'feed',
            postgresql.ENUM(
                Feed,
                name='feed',
                values_callable=lambda enum_cls: [member.value for member in enum_cls],
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column('requested_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('state', state_type, server_default='RUNNING', nullable=False),
        sa.Column('bars_written', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('error_domain', sa.String(), nullable=True),
        sa.Column('error_reason', sa.String(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('error_id', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        DATASET_STARTED_INDEX, TABLE, ['dataset_id', sa.text('started_at DESC'), sa.text('id DESC')], unique=False
    )
    op.create_index(RUNNING_INDEX, TABLE, ['dataset_id'], unique=False, postgresql_where=sa.text("state = 'RUNNING'"))
    op.create_index(FINISHED_INDEX, TABLE, ['finished_at'], unique=False)


def downgrade() -> None:
    op.drop_index(FINISHED_INDEX, table_name=TABLE)
    op.drop_index(RUNNING_INDEX, table_name=TABLE, postgresql_where=sa.text("state = 'RUNNING'"))
    op.drop_index(DATASET_STARTED_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
    postgresql.ENUM(name='ingest_run_state').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='ingest_run_trigger').drop(op.get_bind(), checkfirst=True)
