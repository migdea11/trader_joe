"""Asset-list snapshots: asset_list_snapshot, asset_list_row, asset_list_event, and ingest_request.asset_list_status

Revision ID: a3f8d2c6e914
Revises: c7e19a4d5b28
Create Date: 2026-10-26 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.30, P17). upgrade() creates two enum types, three tables with their indexes, adds one NULLABLE
column to ingest_request and one CHECK constraint on it. It reads, rewrites and deletes nothing that exists and
backfills nothing. Nothing is dropped, renamed or retyped.

WHAT IS ADDED.
  asset_list_snapshot  one dated snapshot per delivered asset-list attempt, empty ones included (row_count 0): source,
                       asset_type, the list (active or inactive), the delivering request_id and attempt_no, as_of (the
                       snapshot instant), row_count, received_at. APPEND-ONLY. Unique on (request_id, attempt_no).
  asset_list_row       one vendor asset exactly as served: vendor_asset_id, symbol, name, exchange, the vendor's active
                       and tradable flags (NULL when it said nothing) and the raw payload bytes. APPEND-ONLY.
  asset_list_event     appeared, vanished and status_flip, derived by vendor asset id against the previous NON-EMPTY
                       snapshot of the same list, in the delivery transaction. APPEND-ONLY. Unique on
                       (snapshot_id, vendor_asset_id, kind).
  ingest_request.asset_list_status  which list an ASSET_LIST request asks for (ADR tj-26jt9w addendum 17). Nullable, no
                       default, NO BACKFILL: no release has ever written an ASSET_LIST row, so every existing row keeps
                       NULL.
  ck_ingest_request_asset_list_status  (kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL): an asset-list row always
                       names its list and no other kind names one. Every existing row satisfies it (kind is never
                       ASSET_LIST and the new column is NULL), and every write the previous release makes still does.

THE ENUM TYPES. asset_list_status (labels active, inactive: the values of schemas.data_ingest.submit.AssetListStatus) is
created once here and shared by ingest_request.asset_list_status, asset_list_snapshot.asset_list and
asset_list_event.asset_list. asset_list_event_kind (appeared, vanished, status_flip) is created here too. Both are
created checkfirst and dropped by downgrade(), after the tables and the column that use them. The datasource and
assettype types are referenced with create_type=False and never created or dropped.

NO FOREIGN KEY, ON PURPOSE (the request record's rule): snapshots outlive their request.

DOWNGRADE drops exactly what upgrade() created, in reverse order. It loses the snapshots and the column's values, the data
this revision introduces, and touches nothing else, so a database seeded at c7e19a4d5b28 comes back as it was. It is no
recovery path (the store is forward-only; recovery is a restore).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource


# revision identifiers, used by Alembic.
revision: str = 'a3f8d2c6e914'
down_revision: Union[str, None] = 'c7e19a4d5b28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REQUEST_TABLE = 'ingest_request'
SNAPSHOT_TABLE = 'asset_list_snapshot'
ROW_TABLE = 'asset_list_row'
EVENT_TABLE = 'asset_list_event'

STATUS_TYPE = 'asset_list_status'
EVENT_KIND_TYPE = 'asset_list_event_kind'
STATUS_MEMBERS = ('active', 'inactive')
EVENT_KIND_MEMBERS = ('appeared', 'vanished', 'status_flip')

STATUS_CONSTRAINT = 'ck_ingest_request_asset_list_status'
SNAPSHOT_ATTEMPT_INDEX = 'uq_asset_list_snapshot_request_id_attempt_no'
SNAPSHOT_LIST_INDEX = 'ix_asset_list_snapshot_source_asset_list_as_of'
ROW_SNAPSHOT_INDEX = 'ix_asset_list_row_snapshot_id'
ROW_VENDOR_INDEX = 'ix_asset_list_row_vendor_asset_id'
EVENT_UNIQUE_INDEX = 'uq_asset_list_event_snapshot_id_vendor_asset_id_kind'


def _existing_enum(enum_cls: type, name: str) -> postgresql.ENUM:
    """An enum type another revision already created, referenced and never created or dropped here."""
    return postgresql.ENUM(enum_cls, name=name, create_type=False)


def upgrade() -> None:
    status_type = postgresql.ENUM(*STATUS_MEMBERS, name=STATUS_TYPE, create_type=False)
    kind_type = postgresql.ENUM(*EVENT_KIND_MEMBERS, name=EVENT_KIND_TYPE, create_type=False)
    status_type.create(op.get_bind(), checkfirst=True)
    kind_type.create(op.get_bind(), checkfirst=True)

    op.add_column(REQUEST_TABLE, sa.Column('asset_list_status', status_type, nullable=True))
    op.create_check_constraint(
        STATUS_CONSTRAINT, REQUEST_TABLE, sa.text("(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)")
    )

    op.create_table(
        SNAPSHOT_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('asset_type', _existing_enum(AssetType, 'assettype'), nullable=False),
        sa.Column('asset_list', status_type, nullable=False),
        sa.Column('request_id', sa.UUID(), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(SNAPSHOT_ATTEMPT_INDEX, SNAPSHOT_TABLE, ['request_id', 'attempt_no'], unique=True)
    op.create_index(SNAPSHOT_LIST_INDEX, SNAPSHOT_TABLE, ['source', 'asset_list', 'as_of'])

    op.create_table(
        ROW_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('snapshot_id', sa.UUID(), nullable=False),
        sa.Column('vendor_asset_id', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('exchange', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=True),
        sa.Column('tradable', sa.Boolean(), nullable=True),
        sa.Column('raw_payload', sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(ROW_SNAPSHOT_INDEX, ROW_TABLE, ['snapshot_id'])
    op.create_index(ROW_VENDOR_INDEX, ROW_TABLE, ['vendor_asset_id'])

    op.create_table(
        EVENT_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('asset_list', status_type, nullable=False),
        sa.Column('kind', kind_type, nullable=False),
        sa.Column('vendor_asset_id', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('previous_symbol', sa.String(), nullable=True),
        sa.Column('snapshot_id', sa.UUID(), nullable=False),
        sa.Column('previous_snapshot_id', sa.UUID(), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('previous_as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(EVENT_UNIQUE_INDEX, EVENT_TABLE, ['snapshot_id', 'vendor_asset_id', 'kind'], unique=True)


def downgrade() -> None:
    op.drop_index(EVENT_UNIQUE_INDEX, table_name=EVENT_TABLE)
    op.drop_table(EVENT_TABLE)

    op.drop_index(ROW_VENDOR_INDEX, table_name=ROW_TABLE)
    op.drop_index(ROW_SNAPSHOT_INDEX, table_name=ROW_TABLE)
    op.drop_table(ROW_TABLE)

    op.drop_index(SNAPSHOT_LIST_INDEX, table_name=SNAPSHOT_TABLE)
    op.drop_index(SNAPSHOT_ATTEMPT_INDEX, table_name=SNAPSHOT_TABLE)
    op.drop_table(SNAPSHOT_TABLE)

    op.drop_constraint(STATUS_CONSTRAINT, REQUEST_TABLE, type_='check')
    op.drop_column(REQUEST_TABLE, 'asset_list_status')

    postgresql.ENUM(name=EVENT_KIND_TYPE).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=STATUS_TYPE).drop(op.get_bind(), checkfirst=True)
