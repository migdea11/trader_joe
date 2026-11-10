"""Alpha baseline: the whole store schema in one initial revision (ADR tj-kg521d)

The first alpha release is the first version whose data persists. The owner ruled (2026-11-06, tj-46jkey notes)
that nothing before it is supported: the sixteen earlier revisions are dropped, this one creates the final schema from
empty, and the owner drops the database and starts fresh. A database stamped with any earlier revision id fails
loudly at `make migrate` ("Can't locate revision"), because this id is new and is not an old one.

WHAT IT CREATES. Every table, column, type, enum, index, unique constraint, CHECK and foreign key of the models as
they stand (app/database/models), plus the rulings of tj-46jkey and tj-1ov1p2: stock_market_activity.volume and
trade_count are BIGINT, panel_settings_version has no size column and no size CHECK, and panel_settings_version.reason
and panel_source_settings_version.reason are NULLABLE with their not-blank CHECKs kept (a NULL passes a CHECK).

SELF-CONTAINED. The enum label lists are LITERALS (ENUM_LABELS below), not read from imported code: a revision that
read Feed off the image would build a different schema when the enum grows. The models stay the truth, and the
upgrade-versus-create_all catalogue test catches drift between them and this file. Label order is the models' member
order, which is what metadata.create_all builds. NOT carried: stock_market_activity_superseded_8f41c2d7a3b9, a one-off
data-repair artifact of a revision that no longer exists. The first revision declared a single-column UNIQUE on
store_dataset_entry.id beside the primary key and no model declares it, so this baseline does not create it (it is
redundant with the primary key).

EDITED IN PLACE (2026-11-07, ADR tj-2ayia2): the panel source settings rates became optional, so
panel_source_settings_version.ramp_datasets_per_hour and request_cap_per_hour are NULLABLE (NULL is no limit) and the
numbers_non_negative CHECK is replaced by ramp_positive (>= 1), request_cap_min (>= 2) and window_non_negative. The id
is unchanged on purpose: nothing is persisted, and the owner drops every database after the merge, so no new revision
is needed. A database already at this id is NOT upgraded by `make migrate` (a no-op) and keeps the old schema: drop it
and migrate again.

EDITED IN PLACE (2026-11-10, ADR tj-ed4t8m D7): panel_settings_version gained include_otc, BOOLEAN NOT NULL with NO schema
default, the last column of the table (the panel setting 'include OTC', off by default in the repository: owner ruling
2026-11-10). The id is unchanged on purpose: nothing is persisted, and every database is dropped after the merge, so no
new revision is needed. A database already at this id is NOT upgraded by `make migrate` (a no-op) and lacks the column:
drop it and migrate again.

EDITED IN PLACE (2026-11-10, ADR tj-rwoasb D4): stock_market_activity.id is BIGINT (its implicit sequence is bigint too): a
1 Minute panel passes 2^31 bars. The id is unchanged on purpose, for the reason above: drop the database and migrate again.

EDITED IN PLACE (2026-11-10, ADR tj-rwoasb D1, D2): panel gained granularity, the native granularity enum (not re-created),
NOT NULL with NO default, the last column of the table: the bar interval a panel's datasets are minted at, fixed at
creation like source. The id is unchanged on purpose, for the reason above: drop the database and migrate again.

NO DOWNGRADE. downgrade() raises: recovery is a restore of make migrate's pre-migrate dump (ADR tj-x3ig38), and this
revision has no parent to return to.

Revision ID: 5d9a3c7e1b84
Revises:
Create Date: 2026-11-06 23:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5d9a3c7e1b84'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres enum type name -> its labels, in creation order. Literals, on purpose (see the docstring).
ENUM_LABELS: dict[str, tuple[str, ...]] = {
    'datasource': ('IB_API', 'ALPACA_API', 'MANUAL_ENTRY',),
    'asset_list_status': ('active', 'inactive',),
    'asset_list_event_kind': ('appeared', 'vanished', 'status_flip',),
    'assettype': ('STOCK', 'CRYPTO', 'OPTION',),
    'override_action': ('SET_EXPIRY', 'SET_COLLECTING', 'DELETE',),
    'read_client_kind': ('UI', 'SDK',),
    'dataset_read_kind': ('READ', 'STREAM_SESSION',),
    'ingest_request_kind': ('BARS', 'ASSET_LIST', 'CORPORATE_ACTIONS',),
    'granularity': ('ONE_MINUTE', 'FIVE_MINUTES', 'THIRTY_MINUTES', 'ONE_HOUR', 'ONE_DAY', 'ONE_WEEK', 'ONE_MONTH',),
    'feed': ('IEX', 'SIP', 'NOT_APPLICABLE',),
    'ingest_run_trigger': ('REQUEST', 'SCHEDULE', 'RETRY',),
    'ingest_request_state': ('PENDING', 'QUEUED', 'NOT_DELIVERED', 'SUCCEEDED', 'EMPTY', 'FAILED',),
    'datatype': ('MARKET_ACTIVITY', 'QUOTE', 'TRADE',),
    'ingest_run_state': ('RUNNING', 'SUCCEEDED', 'EMPTY', 'FAILED',),
    'panel_cadence': ('DAILY', 'WEEKLY', 'MONTHLY',),
    'panel_pass_kind': ('MEMBERSHIP', 'RELEASE',),
    'panel_transition': ('ENTERED', 'CREATED', 'DEFERRED', 'REFUSED', 'EXITED', 'DRAINING', 'DRAINED',),
}


def _enum(name: str) -> postgresql.ENUM:
    """A reference to an enum type created once, explicitly, in upgrade() (so a table never emits its own CREATE TYPE)."""
    return postgresql.ENUM(*ENUM_LABELS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name in ENUM_LABELS:
        _enum(name).create(bind, checkfirst=False)

    op.create_table('asset_list_event',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('asset_list', _enum('asset_list_status'), nullable=False),
    sa.Column('kind', _enum('asset_list_event_kind'), nullable=False),
    sa.Column('vendor_asset_id', sa.String(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('previous_symbol', sa.String(), nullable=True),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('previous_snapshot_id', sa.UUID(), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('previous_as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_asset_list_event_snapshot_id_vendor_asset_id_kind', 'asset_list_event', ['snapshot_id', 'vendor_asset_id', 'kind'], unique=True)

    op.create_table('asset_list_row',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('vendor_asset_id', sa.String(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('exchange', sa.String(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('tradable', sa.Boolean(), nullable=True),
    sa.Column('raw_payload', sa.LargeBinary(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_asset_list_row_snapshot_id', 'asset_list_row', ['snapshot_id'], unique=False)
    op.create_index('ix_asset_list_row_vendor_asset_id', 'asset_list_row', ['vendor_asset_id'], unique=False)

    op.create_table('asset_list_snapshot',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('asset_type', _enum('assettype'), nullable=False),
    sa.Column('asset_list', _enum('asset_list_status'), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_asset_list_snapshot_source_asset_list_as_of', 'asset_list_snapshot', ['source', 'asset_list', 'as_of'], unique=False)
    op.create_index('uq_asset_list_snapshot_request_id_attempt_no', 'asset_list_snapshot', ['request_id', 'attempt_no'], unique=True)

    op.create_table('corporate_action',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
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
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_corporate_action_request_id', 'corporate_action', ['request_id'], unique=False)
    op.create_index('uq_corporate_action_source_vendor_action_id_payload_hash', 'corporate_action', ['source', 'vendor_action_id', 'payload_hash'], unique=True)

    op.create_table('dataset_deletion',
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
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_dataset_deletion_dataset_id', 'dataset_deletion', ['dataset_id'], unique=False)
    op.create_index('ix_dataset_deletion_deleted_at', 'dataset_deletion', ['deleted_at'], unique=False)

    op.create_table('dataset_override_log',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('action', _enum('override_action'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('new_value', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_dataset_override_log_dataset_id_at', 'dataset_override_log', ['dataset_id', 'at'], unique=False)

    op.create_table('dataset_read',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('client_kind', _enum('read_client_kind'), nullable=False),
    sa.Column('kind', _enum('dataset_read_kind'), server_default='READ', nullable=False),
    sa.Column('route', sa.String(), nullable=False),
    sa.Column('bars_returned', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('session_opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('session_closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_dataset_read_dataset_id_read_at', 'dataset_read', ['dataset_id', 'read_at'], unique=False)
    op.create_index('ix_dataset_read_read_at', 'dataset_read', ['read_at'], unique=False)

    op.create_table('dataset_read_daily',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('client_kind', _enum('read_client_kind'), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('reads', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('sessions', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('bars_returned', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id', 'client_id', 'day', name='uq_dataset_read_daily_dataset_id_client_id_day')
    )
    op.create_index('ix_dataset_read_daily_day', 'dataset_read_daily', ['day'], unique=False)

    op.create_table('ingest_instance',
    sa.Column('instance_id', sa.String(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('instance_id')
    )

    op.create_table('ingest_request',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('kind', _enum('ingest_request_kind'), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=True),
    sa.Column('asset_symbol', sa.String(), nullable=True),
    sa.Column('asset_type', _enum('assettype'), nullable=True),
    sa.Column('data_types', sa.ARRAY(sa.String()), nullable=True),
    sa.Column('granularity', _enum('granularity'), nullable=True),
    sa.Column('update_type', sa.Integer(), nullable=True),
    sa.Column('feed', _enum('feed'), nullable=True),
    sa.Column('requested_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('requested_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ready_by', sa.DateTime(timezone=True), nullable=True),
    sa.Column('best_effort', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('client_label', sa.String(), nullable=False),
    sa.Column('trigger', _enum('ingest_run_trigger'), nullable=True),
    sa.Column('expiry', sa.DateTime(timezone=True), nullable=True),
    sa.Column('collect', sa.Boolean(), nullable=True),
    sa.Column('expiry_type', sa.Integer(), nullable=True),
    sa.Column('asset_list_status', _enum('asset_list_status'), nullable=True),
    sa.Column('state', _enum('ingest_request_state'), server_default='PENDING', nullable=False),
    sa.Column('instance_id', sa.String(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('spent_calls', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('spent_pages', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('spent_throttled_429', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('spent_retries', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('spend_unknown_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('failure_cause', sa.String(), nullable=True),
    sa.Column('failure_detail', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)", name='ck_ingest_request_asset_list_status'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ingest_request_dataset_id', 'ingest_request', ['dataset_id'], unique=False, postgresql_where=sa.text('dataset_id IS NOT NULL'))
    op.create_index('ix_ingest_request_open_state_created_at', 'ingest_request', ['state', 'created_at'], unique=False, postgresql_where=sa.text("state IN ('PENDING', 'QUEUED', 'NOT_DELIVERED')"))
    op.create_index('ix_ingest_request_queued_instance_id', 'ingest_request', ['instance_id'], unique=False, postgresql_where=sa.text("state = 'QUEUED'"))
    op.create_index('ix_ingest_request_source_created_at', 'ingest_request', ['source', 'created_at'], unique=False)

    op.create_table('ingest_run',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=True),
    sa.Column('request_id', sa.UUID(), nullable=True),
    sa.Column('attempt_no', sa.Integer(), nullable=True),
    sa.Column('instance_id', sa.String(), nullable=True),
    sa.Column('quota_key', sa.String(), nullable=True),
    sa.Column('verdict', sa.String(), nullable=True),
    sa.Column('vendor_calls', sa.BigInteger(), nullable=True),
    sa.Column('vendor_pages', sa.BigInteger(), nullable=True),
    sa.Column('vendor_throttled_429', sa.BigInteger(), nullable=True),
    sa.Column('vendor_retries', sa.BigInteger(), nullable=True),
    sa.Column('vendor_first_call_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('vendor_last_call_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('vendor_reported_limit', sa.BigInteger(), nullable=True),
    sa.Column('vendor_reported_remaining', sa.BigInteger(), nullable=True),
    sa.Column('vendor_reported_reset_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('vendor_asset_id', sa.String(), nullable=True),
    sa.Column('served_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('served_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trigger', _enum('ingest_run_trigger'), nullable=False),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('asset_symbol', sa.String(), nullable=True),
    sa.Column('asset_type', _enum('assettype'), nullable=True),
    sa.Column('data_type', _enum('datatype'), nullable=True),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('granularity', _enum('granularity'), nullable=True),
    sa.Column('update_type', sa.Integer(), nullable=True),
    sa.Column('feed', _enum('feed'), nullable=True),
    sa.Column('requested_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('requested_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', _enum('ingest_run_state'), server_default='RUNNING', nullable=False),
    sa.Column('bars_written', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('error_domain', sa.String(), nullable=True),
    sa.Column('error_reason', sa.String(), nullable=True),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('error_id', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ingest_run_dataset_id_started_at_id', 'ingest_run', ['dataset_id', sa.literal_column('started_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_ingest_run_finished_at', 'ingest_run', ['finished_at'], unique=False)
    op.create_index('ix_ingest_run_running_dataset_id', 'ingest_run', ['dataset_id'], unique=False, postgresql_where=sa.text("state = 'RUNNING'"))
    op.create_index('uq_ingest_run_request_id_attempt_no', 'ingest_run', ['request_id', 'attempt_no'], unique=True, postgresql_where=sa.text('request_id IS NOT NULL AND attempt_no IS NOT NULL'))

    op.create_table('panel',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('granularity', _enum('granularity'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_panel_name', 'panel', ['name'], unique=True)

    op.create_table('panel_source_settings_version',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('ramp_datasets_per_hour', sa.Integer(), nullable=True),
    sa.Column('request_cap_per_hour', sa.Integer(), nullable=True),
    sa.Column('corporate_action_window_pre_days', sa.Integer(), nullable=False),
    sa.Column('corporate_action_window_post_days', sa.Integer(), nullable=False),
    sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.CheckConstraint("btrim(actor) <> ''", name='ck_panel_source_settings_version_actor_not_blank'),
    sa.CheckConstraint("btrim(reason) <> ''", name='ck_panel_source_settings_version_reason_not_blank'),
    sa.CheckConstraint('corporate_action_window_pre_days >= 0 AND corporate_action_window_post_days >= 0', name='ck_panel_source_settings_version_window_non_negative'),
    sa.CheckConstraint('ramp_datasets_per_hour >= 1', name='ck_panel_source_settings_version_ramp_positive'),
    sa.CheckConstraint('request_cap_per_hour >= 2', name='ck_panel_source_settings_version_request_cap_min'),
    sa.CheckConstraint('version_no >= 1', name='ck_panel_source_settings_version_version_no_positive'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'version_no', name='uq_panel_source_settings_version_source_version_no')
    )

    op.create_table('store_dataset_entry',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('owner', sa.String(), server_default='unassigned', nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('feed', _enum('feed'), nullable=False),
    sa.Column('asset_symbol', sa.String(), nullable=False),
    sa.Column('asset_type', _enum('assettype'), nullable=False),
    sa.Column('data_type', _enum('datatype'), nullable=False),
    sa.Column('granularity', _enum('granularity'), nullable=False),
    sa.Column('start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expiry', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expiry_type', sa.Integer(), server_default='1', nullable=False),
    sa.Column('update_type', sa.Integer(), server_default='1', nullable=False),
    sa.Column('collect', sa.Boolean(), nullable=True),
    sa.Column('stopped_cause', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('(update_type = 1 AND collect IS NULL) OR (update_type IN (2, 3) AND collect IS NOT NULL)', name='ck_store_dataset_entry_collect_by_type'),
    sa.CheckConstraint('stopped_cause IS NULL OR collect IS FALSE', name='ck_store_dataset_entry_stopped_cause_needs_stop'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_symbol', 'source', 'granularity', 'asset_type', 'data_type', 'owner', 'expiry_type', 'update_type', 'feed', 'start', 'end', name='uq_store_dataset_entry_identity')
    )

    op.create_table('panel_settings_version',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('panel_id', sa.UUID(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('cadence', _enum('panel_cadence'), nullable=False),
    sa.Column('allow_symbols', sa.ARRAY(sa.String()), server_default=sa.text("'{}'::varchar[]"), nullable=False),
    sa.Column('deny_symbols', sa.ARRAY(sa.String()), server_default=sa.text("'{}'::varchar[]"), nullable=False),
    sa.Column('asset_class', _enum('assettype'), nullable=True),
    sa.Column('exchange', sa.String(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('tradable', sa.Boolean(), nullable=True),
    sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('include_otc', sa.Boolean(), nullable=False),
    sa.CheckConstraint("btrim(actor) <> ''", name='ck_panel_settings_version_actor_not_blank'),
    sa.CheckConstraint("btrim(reason) <> ''", name='ck_panel_settings_version_reason_not_blank'),
    sa.CheckConstraint('version_no >= 1', name='ck_panel_settings_version_version_no_positive'),
    sa.ForeignKeyConstraint(['panel_id'], ['panel.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('panel_id', 'version_no', name='uq_panel_settings_version_panel_id_version_no')
    )

    op.create_table('stock_market_activity',
    sa.Column('open', sa.Float(), nullable=False),
    sa.Column('high', sa.Float(), nullable=False),
    sa.Column('low', sa.Float(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.Column('volume', sa.BigInteger(), nullable=False),
    sa.Column('trade_count', sa.BigInteger(), nullable=False),
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=False),
    sa.Column('source', _enum('datasource'), nullable=False),
    sa.Column('asset_symbol', sa.String(), nullable=False),
    sa.Column('feed', _enum('feed'), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('granularity', _enum('granularity'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['store_dataset_entry.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id', 'asset_symbol', 'source', 'feed', 'granularity', 'timestamp', name='uq_stock_market_activity_natural_key')
    )
    op.create_index('ix_stock_market_activity_symbol_granularity_timestamp', 'stock_market_activity', ['asset_symbol', 'granularity', 'timestamp'], unique=False)

    op.create_table('panel_pass',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('panel_id', sa.UUID(), nullable=False),
    sa.Column('kind', _enum('panel_pass_kind'), nullable=False),
    sa.Column('settings_version_id', sa.UUID(), nullable=False),
    sa.Column('source_settings_version_id', sa.UUID(), nullable=False),
    sa.Column('active_snapshot_id', sa.UUID(), nullable=False),
    sa.Column('inactive_snapshot_id', sa.UUID(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.Column('counts', postgresql.JSONB(), nullable=True),
    sa.ForeignKeyConstraint(['panel_id'], ['panel.id'], ),
    sa.ForeignKeyConstraint(['settings_version_id'], ['panel_settings_version.id'], ),
    sa.ForeignKeyConstraint(['source_settings_version_id'], ['panel_source_settings_version.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_panel_pass_panel_id_started_at', 'panel_pass', ['panel_id', 'started_at'], unique=False)
    op.create_index('ix_panel_pass_settings_version_id', 'panel_pass', ['settings_version_id'], unique=False)
    op.create_index('ix_panel_pass_source_settings_version_id', 'panel_pass', ['source_settings_version_id'], unique=False)

    op.create_table('panel_member_transition',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('pass_id', sa.UUID(), nullable=False),
    sa.Column('panel_id', sa.UUID(), nullable=False),
    sa.Column('vendor_asset_id', sa.String(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('transition', _enum('panel_transition'), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=True),
    sa.Column('request_id', sa.UUID(), nullable=True),
    sa.Column('cause', sa.String(), nullable=True),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['panel_id'], ['panel.id'], ),
    sa.ForeignKeyConstraint(['pass_id'], ['panel_pass.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pass_id', 'seq', name='uq_panel_member_transition_pass_id_seq')
    )
    op.create_index('ix_panel_member_transition_panel_id_vendor_asset_id_as_of', 'panel_member_transition', ['panel_id', 'vendor_asset_id', 'as_of'], unique=False)
    op.create_index('ix_panel_member_transition_pass_id', 'panel_member_transition', ['pass_id'], unique=False)
    op.create_index('ix_panel_member_transition_transition_recorded_at', 'panel_member_transition', ['transition', 'recorded_at'], unique=False)


def downgrade() -> None:
    raise RuntimeError(
        'The alpha baseline has no downgrade (ADR tj-kg521d): there is no earlier revision. '
        "Restore the pre-migrate dump `make migrate` took, or drop the database and run make migrate."
    )
