"""Corporate-action observations: the corporate_action table, and nullable label columns on ingest_run

Revision ID: c7e19a4d5b28
Revises: 39c28a39b290
Create Date: 2026-10-26 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.31, P18). upgrade() creates one table with its indexes and relaxes five NOT NULL columns to
nullable. It reads, rewrites and deletes nothing that exists and backfills nothing. Nothing is dropped, renamed or
retyped, and no enum type is created or changed.

WHAT IS ADDED.
  corporate_action   the vendor's corporate-action records exactly as delivered, append-only. Columns: id, source,
                     vendor_action_id, action_type, the five vendor dates (ex, record, payable, process, effective),
                     identifiers (JSONB), raw_payload (bytea), payload_hash, request_id, attempt_no,
                     requesting_symbol, requested_start, requested_end, as_of, received_at. It is tied to no dataset and
                     no listing (ADR tj-26jt9w addendum 12). The source column reuses the datasource enum type
                     created long ago; it is referenced here and never created or dropped.
  uq_corporate_action_source_vendor_action_id_payload_hash   the arbiter of the dedupe: the same vendor id with the
                     same bytes is one observation, the same id with other bytes is a new one.
  ix_corporate_action_request_id   a request's observations.

WHAT IS RELAXED (alter_column nullable=True only, no server default, no type change).
  ingest_run.asset_symbol, asset_type, data_type, granularity, update_type. The attempt row of a request that is not
  about one bar series (corporate actions now, the asset list later) has no granularity, data type or update type, and
  the asset list has no symbol; a made-up value there would read as a real one, so the columns say NULL. Every reader
  of these columns handles NULL in the same commit.

DOWNGRADE drops the table (its indexes go with it) and restores NOT NULL on the five columns. It is no recovery path:
it refuses, with Postgres's own error and nothing changed, on a database that holds an attempt row with a NULL label,
and it loses the observations. The store is forward-only and recovery is a restore.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_stock import DataSource


# revision identifiers, used by Alembic.
revision: str = 'c7e19a4d5b28'
down_revision: Union[str, None] = '39c28a39b290'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'corporate_action'
RUN_TABLE = 'ingest_run'
RELAXED_RUN_COLUMNS = ('asset_symbol', 'asset_type', 'data_type', 'granularity', 'update_type')


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        # The type exists since the entry's first revision; referenced here, never created or dropped.
        sa.Column('source', postgresql.ENUM(DataSource, name='datasource', create_type=False), nullable=False),
        sa.Column('vendor_action_id', sa.String(), nullable=False),
        sa.Column('action_type', sa.String(), nullable=False),
        sa.Column('ex_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('record_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payable_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('process_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('effective_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('identifiers', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('raw_payload', sa.LargeBinary(), nullable=False),
        sa.Column('payload_hash', sa.String(), nullable=False),
        sa.Column('request_id', sa.UUID(), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('requesting_symbol', sa.String(), nullable=True),
        sa.Column('requested_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_corporate_action_source_vendor_action_id_payload_hash',
        TABLE,
        ['source', 'vendor_action_id', 'payload_hash'],
        unique=True,
    )
    op.create_index('ix_corporate_action_request_id', TABLE, ['request_id'])

    for column in RELAXED_RUN_COLUMNS:
        op.alter_column(RUN_TABLE, column, nullable=True)


def downgrade() -> None:
    for column in reversed(RELAXED_RUN_COLUMNS):
        op.alter_column(RUN_TABLE, column, nullable=False)

    op.drop_index('ix_corporate_action_request_id', table_name=TABLE)
    op.drop_index('uq_corporate_action_source_vendor_action_id_payload_hash', table_name=TABLE)
    op.drop_table(TABLE)
