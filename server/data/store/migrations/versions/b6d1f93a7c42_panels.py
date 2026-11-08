"""Panels: panel, panel_settings_version, panel_source_settings_version, panel_pass, panel_member_transition, and dataset_override_log.new_value

Revision ID: b6d1f93a7c42
Revises: a3f8d2c6e914
Create Date: 2026-10-27 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.33, P20). upgrade() creates three enum types, five tables with their indexes and constraints, adds
one NULLABLE column to dataset_override_log and one index to ingest_request. It reads, rewrites and deletes nothing that exists and backfills
nothing. Nothing is dropped, renamed or retyped. NO panel and NO settings row is seeded: names and filters are operator
data (this repo is generic).

WHAT IS ADDED (ADR tj-26jt9w addendum 20). ALL FIVE TABLES ARE APPEND-ONLY, with one ruled exception: the finish-pass
write sets panel_pass.finished_at and counts once, from NULL.
  panel                           id, name (unique), source (immutable), created_at. The market is NOT a column: one
                                  DataSource per vendor and market.
  panel_settings_version          per-panel enabled, cadence, size and filters (allow and deny symbols, asset class,
                                  exchange, active, tradable); version_no unique per panel; saved_at, actor, reason (the
                                  markers; actor and reason never blank). Enable and disable are versions.
  panel_source_settings_version   per-source ramp_datasets_per_hour and request_cap_per_hour (rates over a sliding 60
                                  minutes, see below) and the corporate-action window (days before and after);
                                  version_no unique per source; the same markers.
  panel_pass                      one row per pass: the panel, its kind (MEMBERSHIP or RELEASE), the two settings
                                  versions it ran under, the snapshot ids it read, started_at, finished_at (NULL until
                                  finished), dry_run, counts.
  panel_member_transition         the membership ledger: pass, panel, vendor asset id, symbol, transition, dataset_id and
                                  request_id (nullable), cause, as_of (the snapshot instant) and recorded_at (database
                                  time of the write; the rate window reads it) and seq (the row's 1-based append ordinal
                                  within its pass, unique per pass, app-assigned: the tie-break of the newest-first order).
  ingest_request                  gains the index ix_ingest_request_source_created_at (source, created_at), which the
                                  request-rate window reads.
  dataset_override_log.new_value  Text, nullable: what the action set; NULL for DELETE and every earlier row.

RATES. ramp_datasets_per_hour is the most NEW panel datasets created per source in any trailing 60 minutes (0 pauses
creation); request_cap_per_hour the most ingest requests the panel manager writes per source in any trailing 60 minutes,
of every kind (the daily refresh's own requests do not count). The allowance is read from the database at each tick
(CREATED transitions by recorded_at, ingest_request rows by created_at): no stored budget, so a restart resets nothing.

NO POLICY DEFAULT. The schema carries no default for cadence, enabled, size, either rate or the window: the
default is applied where a panel is created. The only server defaults are the empty symbol lists (no restriction) and the
usual id and timestamps.

FOREIGN KEYS only inside the panel tables. NONE from panel_pass or panel_member_transition to store_dataset_entry,
ingest_request or asset_list_snapshot: a ledger row outlives a deleted dataset, as the override log does.

THE ENUM TYPES. panel_cadence (DAILY, WEEKLY, MONTHLY), panel_transition (ENTERED, CREATED, DEFERRED, REFUSED,
EXITED, DRAINING, DRAINED) and panel_pass_kind (MEMBERSHIP, RELEASE) are created here, checkfirst, and dropped by downgrade(). The datasource and assettype types
are referenced with create_type=False and never created or dropped.

DOWNGRADE drops exactly what upgrade() created, in reverse order (the ingest_request index included), and touches
nothing else, so a database seeded at a3f8d2c6e914 comes back as it was. It loses the panel data and the new_value values, the data this revision introduces.
It is no recovery path (the store is forward-only; recovery is a restore).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource


# revision identifiers, used by Alembic.
revision: str = 'b6d1f93a7c42'
down_revision: Union[str, None] = 'a3f8d2c6e914'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PANEL_TABLE = 'panel'
SETTINGS_TABLE = 'panel_settings_version'
SOURCE_SETTINGS_TABLE = 'panel_source_settings_version'
RUN_TABLE = 'panel_pass'
TRANSITION_TABLE = 'panel_member_transition'
OVERRIDE_TABLE = 'dataset_override_log'

CADENCE_TYPE = 'panel_cadence'
TRANSITION_TYPE = 'panel_transition'
RUN_KIND_TYPE = 'panel_pass_kind'
CADENCE_MEMBERS = ('DAILY', 'WEEKLY', 'MONTHLY')
TRANSITION_MEMBERS = ('ENTERED', 'CREATED', 'DEFERRED', 'REFUSED', 'EXITED', 'DRAINING', 'DRAINED')
RUN_KIND_MEMBERS = ('MEMBERSHIP', 'RELEASE')

PANEL_NAME_INDEX = 'uq_panel_name'
RUN_PANEL_STARTED_INDEX = 'ix_panel_pass_panel_id_started_at'
RUN_SETTINGS_INDEX = 'ix_panel_pass_settings_version_id'
RUN_SOURCE_SETTINGS_INDEX = 'ix_panel_pass_source_settings_version_id'
TRANSITION_MEMBER_INDEX = 'ix_panel_member_transition_panel_id_vendor_asset_id_as_of'
TRANSITION_RUN_INDEX = 'ix_panel_member_transition_pass_id'
TRANSITION_RECORDED_INDEX = 'ix_panel_member_transition_transition_recorded_at'
REQUEST_TABLE = 'ingest_request'
REQUEST_SOURCE_CREATED_INDEX = 'ix_ingest_request_source_created_at'

EMPTY_LIST = sa.text("'{}'::varchar[]")


def _existing_enum(enum_cls: type, name: str) -> postgresql.ENUM:
    """An enum type another revision already created, referenced and never created or dropped here."""
    return postgresql.ENUM(enum_cls, name=name, create_type=False)


def _id_column() -> sa.Column:
    return sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False)


def upgrade() -> None:
    cadence_type = postgresql.ENUM(*CADENCE_MEMBERS, name=CADENCE_TYPE, create_type=False)
    transition_type = postgresql.ENUM(*TRANSITION_MEMBERS, name=TRANSITION_TYPE, create_type=False)
    run_kind_type = postgresql.ENUM(*RUN_KIND_MEMBERS, name=RUN_KIND_TYPE, create_type=False)
    cadence_type.create(op.get_bind(), checkfirst=True)
    transition_type.create(op.get_bind(), checkfirst=True)
    run_kind_type.create(op.get_bind(), checkfirst=True)

    op.add_column(OVERRIDE_TABLE, sa.Column('new_value', sa.Text(), nullable=True))
    op.create_index(REQUEST_SOURCE_CREATED_INDEX, REQUEST_TABLE, ['source', 'created_at'])

    op.create_table(
        PANEL_TABLE,
        _id_column(),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(PANEL_NAME_INDEX, PANEL_TABLE, ['name'], unique=True)

    op.create_table(
        SETTINGS_TABLE,
        _id_column(),
        sa.Column('panel_id', sa.UUID(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('cadence', cadence_type, nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('allow_symbols', sa.ARRAY(sa.String()), server_default=EMPTY_LIST, nullable=False),
        sa.Column('deny_symbols', sa.ARRAY(sa.String()), server_default=EMPTY_LIST, nullable=False),
        sa.Column('asset_class', _existing_enum(AssetType, 'assettype'), nullable=True),
        sa.Column('exchange', sa.String(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=True),
        sa.Column('tradable', sa.Boolean(), nullable=True),
        sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['panel_id'], ['panel.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('panel_id', 'version_no', name='uq_panel_settings_version_panel_id_version_no'),
        sa.CheckConstraint('version_no >= 1', name='ck_panel_settings_version_version_no_positive'),
        sa.CheckConstraint('size >= 0', name='ck_panel_settings_version_size_non_negative'),
        sa.CheckConstraint("btrim(actor) <> ''", name='ck_panel_settings_version_actor_not_blank'),
        sa.CheckConstraint("btrim(reason) <> ''", name='ck_panel_settings_version_reason_not_blank'),
    )

    op.create_table(
        SOURCE_SETTINGS_TABLE,
        _id_column(),
        sa.Column('source', _existing_enum(DataSource, 'datasource'), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('ramp_datasets_per_hour', sa.Integer(), nullable=False),
        sa.Column('request_cap_per_hour', sa.Integer(), nullable=False),
        sa.Column('corporate_action_window_pre_days', sa.Integer(), nullable=False),
        sa.Column('corporate_action_window_post_days', sa.Integer(), nullable=False),
        sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'version_no', name='uq_panel_source_settings_version_source_version_no'),
        sa.CheckConstraint('version_no >= 1', name='ck_panel_source_settings_version_version_no_positive'),
        sa.CheckConstraint(
            'ramp_datasets_per_hour >= 0 AND request_cap_per_hour >= 0 AND corporate_action_window_pre_days >= 0 '
            'AND corporate_action_window_post_days >= 0',
            name='ck_panel_source_settings_version_numbers_non_negative',
        ),
        sa.CheckConstraint("btrim(actor) <> ''", name='ck_panel_source_settings_version_actor_not_blank'),
        sa.CheckConstraint("btrim(reason) <> ''", name='ck_panel_source_settings_version_reason_not_blank'),
    )

    op.create_table(
        RUN_TABLE,
        _id_column(),
        sa.Column('panel_id', sa.UUID(), nullable=False),
        sa.Column('kind', run_kind_type, nullable=False),
        sa.Column('settings_version_id', sa.UUID(), nullable=False),
        sa.Column('source_settings_version_id', sa.UUID(), nullable=False),
        sa.Column('active_snapshot_id', sa.UUID(), nullable=False),
        sa.Column('inactive_snapshot_id', sa.UUID(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dry_run', sa.Boolean(), nullable=False),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['panel_id'], ['panel.id']),
        sa.ForeignKeyConstraint(['settings_version_id'], ['panel_settings_version.id']),
        sa.ForeignKeyConstraint(['source_settings_version_id'], ['panel_source_settings_version.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(RUN_PANEL_STARTED_INDEX, RUN_TABLE, ['panel_id', 'started_at'])
    op.create_index(RUN_SETTINGS_INDEX, RUN_TABLE, ['settings_version_id'])
    op.create_index(RUN_SOURCE_SETTINGS_INDEX, RUN_TABLE, ['source_settings_version_id'])

    op.create_table(
        TRANSITION_TABLE,
        _id_column(),
        sa.Column('pass_id', sa.UUID(), nullable=False),
        sa.Column('panel_id', sa.UUID(), nullable=False),
        sa.Column('vendor_asset_id', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('transition', transition_type, nullable=False),
        sa.Column('dataset_id', sa.UUID(), nullable=True),
        sa.Column('request_id', sa.UUID(), nullable=True),
        sa.Column('cause', sa.String(), nullable=True),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['pass_id'], ['panel_pass.id']),
        sa.ForeignKeyConstraint(['panel_id'], ['panel.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pass_id', 'seq', name='uq_panel_member_transition_pass_id_seq'),
    )
    op.create_index(TRANSITION_MEMBER_INDEX, TRANSITION_TABLE, ['panel_id', 'vendor_asset_id', 'as_of'])
    op.create_index(TRANSITION_RUN_INDEX, TRANSITION_TABLE, ['pass_id'])
    op.create_index(TRANSITION_RECORDED_INDEX, TRANSITION_TABLE, ['transition', 'recorded_at'])


def downgrade() -> None:
    op.drop_index(TRANSITION_RECORDED_INDEX, table_name=TRANSITION_TABLE)
    op.drop_index(TRANSITION_RUN_INDEX, table_name=TRANSITION_TABLE)
    op.drop_index(TRANSITION_MEMBER_INDEX, table_name=TRANSITION_TABLE)
    op.drop_table(TRANSITION_TABLE)

    op.drop_index(RUN_SOURCE_SETTINGS_INDEX, table_name=RUN_TABLE)
    op.drop_index(RUN_SETTINGS_INDEX, table_name=RUN_TABLE)
    op.drop_index(RUN_PANEL_STARTED_INDEX, table_name=RUN_TABLE)
    op.drop_table(RUN_TABLE)

    op.drop_table(SOURCE_SETTINGS_TABLE)
    op.drop_table(SETTINGS_TABLE)

    op.drop_index(PANEL_NAME_INDEX, table_name=PANEL_TABLE)
    op.drop_table(PANEL_TABLE)

    op.drop_index(REQUEST_SOURCE_CREATED_INDEX, table_name=REQUEST_TABLE)
    op.drop_column(OVERRIDE_TABLE, 'new_value')

    postgresql.ENUM(name=RUN_KIND_TYPE).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=TRANSITION_TYPE).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=CADENCE_TYPE).drop(op.get_bind(), checkfirst=True)
