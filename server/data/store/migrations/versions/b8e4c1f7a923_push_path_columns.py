"""Push-path columns: served range on the attempt row, request trigger, lifecycle fields, unknown spend, stopped cause

Revision ID: b8e4c1f7a923
Revises: f2b7d4a91c36
Create Date: 2026-10-26 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.123, P34c). upgrade() adds seven columns and one CHECK constraint. It reads, rewrites and
deletes nothing that exists and it backfills nothing. Every new column is nullable except one, which is NOT NULL with
a real server default (spend_unknown_attempts, 0), so every existing row takes the default and keeps its meaning.
No existing column is altered, retyped or dropped, no index is touched, and no enum type is created or changed.

WHAT IS ADDED.
  ingest_run.served_start, served_end      timestamptz, nullable. The window a SUCCEEDED attempt answered for
                                           (trailer.served_range); NULL on every other row.
  ingest_request.trigger                   the existing ingest_run_trigger enum type, nullable. NULL reads as REQUEST.
                                           The attempt row of the request's first attempt takes it; later ones are RETRY.
  ingest_request.expiry, collect           timestamptz and boolean, both nullable: the POST body's lifecycle fields,
                                           kept so the dataset entry can be written inside the delivery transaction.
  ingest_request.spend_unknown_attempts    integer NOT NULL, server default 0. Counted FAILED reports that carried no
                                           vendor stats, so the request's spend sums are never read as complete when
                                           they are not.
  store_dataset_entry.stopped_cause        string, nullable, no default. NULL means the store did not stop the dataset,
                                           whatever collect says. This revision writes no value; the one writer is
                                           the stop rule (tj-7zjov1.144), which needs no revision of its own.
  ck_store_dataset_entry_stopped_cause_needs_stop
                                           stopped_cause IS NULL OR collect IS FALSE. A cause is only ever carried by a
                                           stopped subscription: a collecting row (collect true) or a STATIC row
                                           (collect NULL) can never hold one.

THE CHECK HOLDS ON UPGRADE. Every existing row gets stopped_cause NULL, which satisfies the first arm whatever collect
holds, and nothing the previous release writes sets the column, so its writes satisfy the constraint too.

DOWNGRADE drops exactly what upgrade() added, the CHECK first and the columns in reverse order. It loses only the values
this revision introduces. It touches no existing column and drops no type (ingest_run_trigger is the earlier
revision's). It is no recovery path; the store is forward-only and recovery is a restore.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b8e4c1f7a923'
down_revision: Union[str, None] = 'f2b7d4a91c36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUN_TABLE = 'ingest_run'
REQUEST_TABLE = 'ingest_request'
ENTRY_TABLE = 'store_dataset_entry'

STOPPED_CAUSE_CHECK = 'ck_store_dataset_entry_stopped_cause_needs_stop'
TRIGGER_MEMBERS = ('REQUEST', 'SCHEDULE', 'RETRY')


def upgrade() -> None:
    op.add_column(RUN_TABLE, sa.Column('served_start', sa.DateTime(timezone=True), nullable=True))
    op.add_column(RUN_TABLE, sa.Column('served_end', sa.DateTime(timezone=True), nullable=True))

    # The type exists since 6f5a798746a1; referenced here, never created or dropped.
    trigger_type = postgresql.ENUM(*TRIGGER_MEMBERS, name='ingest_run_trigger', create_type=False)
    op.add_column(REQUEST_TABLE, sa.Column('trigger', trigger_type, nullable=True))
    op.add_column(REQUEST_TABLE, sa.Column('expiry', sa.DateTime(timezone=True), nullable=True))
    op.add_column(REQUEST_TABLE, sa.Column('collect', sa.Boolean(), nullable=True))
    op.add_column(
        REQUEST_TABLE, sa.Column('spend_unknown_attempts', sa.Integer(), server_default='0', nullable=False)
    )

    op.add_column(ENTRY_TABLE, sa.Column('stopped_cause', sa.String(), nullable=True))
    op.create_check_constraint(STOPPED_CAUSE_CHECK, ENTRY_TABLE, 'stopped_cause IS NULL OR collect IS FALSE')


def downgrade() -> None:
    op.drop_constraint(STOPPED_CAUSE_CHECK, ENTRY_TABLE, type_='check')
    op.drop_column(ENTRY_TABLE, 'stopped_cause')

    op.drop_column(REQUEST_TABLE, 'spend_unknown_attempts')
    op.drop_column(REQUEST_TABLE, 'collect')
    op.drop_column(REQUEST_TABLE, 'expiry')
    op.drop_column(REQUEST_TABLE, 'trigger')

    op.drop_column(RUN_TABLE, 'served_end')
    op.drop_column(RUN_TABLE, 'served_start')
