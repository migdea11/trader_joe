"""ingest_request and ingest_instance: the durable request record and the instance marker; ingest_run gains the attempt columns

Revision ID: f2b7d4a91c36
Revises: a6d3f8e1c205
Create Date: 2026-10-24 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.25, P33). upgrade() creates two enum types, two tables and four indexes, adds fourteen
NULLABLE columns to ingest_run and one partial unique index on it. It reads, rewrites and deletes nothing that
exists, and it backfills nothing: every existing ingest_run row keeps NULL in each new column, and the unique
index covers only rows where request_id and attempt_no are both set, which no existing row is (attempt_no is new).
So a database at a6d3f8e1c205 with data upgrades with every row byte for byte as it was.

WHY (tj-26jt9w addenda 4 and 5; PR5-PLAN REV1 N1 P33 and N4 items 1, 2 and 4).
  ingest_request  The durable request record: one row per request, written BEFORE the request is sent. Its id is the
                  request id on the wire. It carries the state (PENDING, QUEUED, NOT_DELIVERED, SUCCEEDED, EMPTY,
                  FAILED), the ingest instance holding it, the durable attempt count, the summed spend and the
                  failure cause (EXPIRED among them).
  ingest_instance The instance marker: one row per ingest process the store has seen, so a new id is noticed and
                  the older instances are superseded.
  ingest_run      Becomes the attempt row too: one per reported attempt, with its number, instance, quota key,
                  the verdict given, the vendor call stats and the vendor asset id the bars header carried (opaque,
                  NULL when the header named none). The unique index on (request_id, attempt_no) is what
                  makes a redelivered report idempotent.

NO FOREIGN KEY, ON PURPOSE. ingest_run.request_id gains its meaning but stays unconstrained (runs outlive requests),
and ingest_request.dataset_id and instance_id name their targets by value (a request outlives its dataset).

THE ENUM TYPES. ingest_request_kind and ingest_request_state are created here (checkfirst, so a repeat is a no-op) and
dropped by downgrade(). The label columns reuse the Postgres types the entry already created (datasource, assettype,
granularity, feed) with create_type=False, so this revision neither creates nor drops them.

DOWNGRADE drops exactly what upgrade() created, in reverse order. It loses the request and instance rows and the new
ingest_run columns -- the data this revision introduces -- and touches nothing else: no existing column of ingest_run
or any other table is read or changed, so the seeded rows come back as they were. It is no recovery path (the store
is forward-only; recovery is a restore).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed, Granularity


# revision identifiers, used by Alembic.
revision: str = 'f2b7d4a91c36'
down_revision: Union[str, None] = 'a6d3f8e1c205'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REQUEST_TABLE = 'ingest_request'
INSTANCE_TABLE = 'ingest_instance'
RUN_TABLE = 'ingest_run'

OPEN_INDEX = 'ix_ingest_request_open_state_created_at'
QUEUED_INSTANCE_INDEX = 'ix_ingest_request_queued_instance_id'
DATASET_INDEX = 'ix_ingest_request_dataset_id'
ATTEMPT_UNIQUE_INDEX = 'uq_ingest_run_request_id_attempt_no'

KIND_MEMBERS = ('BARS', 'ASSET_LIST', 'CORPORATE_ACTIONS')
STATE_MEMBERS = ('PENDING', 'QUEUED', 'NOT_DELIVERED', 'SUCCEEDED', 'EMPTY', 'FAILED')

# The columns added to ingest_run, in order. All nullable, no default: an existing row has no attempt.
RUN_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ('attempt_no', sa.Integer()),
    ('instance_id', sa.String()),
    ('quota_key', sa.String()),
    ('verdict', sa.String()),
    ('vendor_calls', sa.BigInteger()),
    ('vendor_pages', sa.BigInteger()),
    ('vendor_throttled_429', sa.BigInteger()),
    ('vendor_retries', sa.BigInteger()),
    ('vendor_first_call_at', sa.DateTime(timezone=True)),
    ('vendor_last_call_at', sa.DateTime(timezone=True)),
    ('vendor_reported_limit', sa.BigInteger()),
    ('vendor_reported_remaining', sa.BigInteger()),
    ('vendor_reported_reset_at', sa.DateTime(timezone=True)),
    ('vendor_asset_id', sa.String()),
)


def _existing_enum(enum_cls: type, name: str) -> postgresql.ENUM:
    """An enum type another revision already created, referenced and never created or dropped here."""
    return postgresql.ENUM(enum_cls, name=name, create_type=False)


def upgrade() -> None:
    kind_type = postgresql.ENUM(*KIND_MEMBERS, name='ingest_request_kind', create_type=False)
    state_type = postgresql.ENUM(*STATE_MEMBERS, name='ingest_request_state', create_type=False)
    # Created explicitly, so create_table below can reference them with create_type=False and the
    # downgrade drops exactly these two.
    kind_type.create(op.get_bind(), checkfirst=True)
    state_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        REQUEST_TABLE,
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('kind', kind_type, nullable=False),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=True),
        sa.Column('asset_symbol', sa.String(), nullable=True),
        sa.Column('asset_type', _existing_enum(AssetType, 'assettype'), nullable=True),
        sa.Column('data_types', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('granularity', _existing_enum(Granularity, 'granularity'), nullable=True),
        sa.Column('update_type', sa.Integer(), nullable=True),
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
        sa.Column('ready_by', sa.DateTime(timezone=True), nullable=True),
        sa.Column('best_effort', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('client_label', sa.String(), nullable=False),
        sa.Column('state', state_type, server_default='PENDING', nullable=False),
        sa.Column('instance_id', sa.String(), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('spent_calls', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('spent_pages', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('spent_throttled_429', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('spent_retries', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('failure_cause', sa.String(), nullable=True),
        sa.Column('failure_detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        OPEN_INDEX,
        REQUEST_TABLE,
        ['state', 'created_at'],
        unique=False,
        postgresql_where=sa.text("state IN ('PENDING', 'QUEUED', 'NOT_DELIVERED')"),
    )
    op.create_index(
        QUEUED_INSTANCE_INDEX,
        REQUEST_TABLE,
        ['instance_id'],
        unique=False,
        postgresql_where=sa.text("state = 'QUEUED'"),
    )
    op.create_index(
        DATASET_INDEX, REQUEST_TABLE, ['dataset_id'], unique=False, postgresql_where=sa.text('dataset_id IS NOT NULL')
    )

    op.create_table(
        INSTANCE_TABLE,
        sa.Column('instance_id', sa.String(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('instance_id'),
    )

    for name, column_type in RUN_COLUMNS:
        op.add_column(RUN_TABLE, sa.Column(name, column_type, nullable=True))
    op.create_index(
        ATTEMPT_UNIQUE_INDEX,
        RUN_TABLE,
        ['request_id', 'attempt_no'],
        unique=True,
        postgresql_where=sa.text('request_id IS NOT NULL AND attempt_no IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        ATTEMPT_UNIQUE_INDEX,
        table_name=RUN_TABLE,
        postgresql_where=sa.text('request_id IS NOT NULL AND attempt_no IS NOT NULL'),
    )
    for name, _ in reversed(RUN_COLUMNS):
        op.drop_column(RUN_TABLE, name)

    op.drop_table(INSTANCE_TABLE)

    op.drop_index(DATASET_INDEX, table_name=REQUEST_TABLE, postgresql_where=sa.text('dataset_id IS NOT NULL'))
    op.drop_index(QUEUED_INSTANCE_INDEX, table_name=REQUEST_TABLE, postgresql_where=sa.text("state = 'QUEUED'"))
    op.drop_index(
        OPEN_INDEX,
        table_name=REQUEST_TABLE,
        postgresql_where=sa.text("state IN ('PENDING', 'QUEUED', 'NOT_DELIVERED')"),
    )
    op.drop_table(REQUEST_TABLE)
    postgresql.ENUM(name='ingest_request_state').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='ingest_request_kind').drop(op.get_bind(), checkfirst=True)
