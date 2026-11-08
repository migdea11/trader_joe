from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    UUID,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource


# Safe defaults, named here and applied where a row is CREATED (never by the schema). Only the ruled one is named: the
# corporate-action window is 90 days on both sides (ADR tj-26jt9w, 2026-10-23 C1). The two hourly rates, size and
# cadence have no ruled default, so they have no constant and no server default.
DEFAULT_CORPORATE_ACTION_WINDOW_DAYS = 90


class PanelCadence(StrEnum):
    """How often a panel's pass runs, judged on the panel's source calendar (ADR tj-26jt9w addendum 20 (d)).

    DAILY is the first close of the day, WEEKLY the ISO week, MONTHLY the month. The schema carries NO default cadence:
    the default is applied where a panel is created.
    """

    DAILY = 'DAILY'
    WEEKLY = 'WEEKLY'
    MONTHLY = 'MONTHLY'


class PanelTransition(StrEnum):
    """What a pass did to one member: the grain of the membership ledger (ADR tj-26jt9w addendum 20 (2))."""

    # Selected by the filter for the first time (or again).
    ENTERED = 'ENTERED'
    # The dataset was created for it.
    CREATED = 'CREATED'
    # Selected but held back by a rate window (cause ramp_rate or request_rate); a repeat with the same cause is not a new row.
    DEFERRED = 'DEFERRED'
    # Selected but refused (a refusal cause).
    REFUSED = 'REFUSED'
    # No longer selected by the filter.
    EXITED = 'EXITED'
    # Delisted and still collecting until a fetch covers its last session.
    DRAINING = 'DRAINING'
    # Drain finished.
    DRAINED = 'DRAINED'


class PanelPassKind(StrEnum):
    """What a pass is, by what it may do (ADR tj-26jt9w addendum 20).

    MEMBERSHIP decides membership from the snapshots; RELEASE only creates members the last membership pass DEFERRED,
    and runs between membership passes.
    """

    MEMBERSHIP = 'MEMBERSHIP'
    RELEASE = 'RELEASE'


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class Panel(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """A panel: a named, operator-defined selection of one source's assets that the panel manager keeps datasets for.

    APPEND-ONLY: no update or delete path. The source is immutable (another source is another panel). The market is
    NOT a column: one DataSource per vendor and market, so the source implies it. No panel is seeded by any revision;
    names and filters are operator data.
    """

    TABLE_NAME = 'panel'
    __tablename__ = TABLE_NAME

    NAME_INDEX = 'uq_panel_name'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    name = Column(String, nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index(NAME_INDEX, 'name', unique=True),)

    def __repr__(self):
        return f"<Panel(id='{self.id}', name='{self.name}', source='{self.source}')>"


class PanelSettingsVersion(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One version of a panel's own settings: enabled, cadence, size and filters (ADR tj-26jt9w addendum 20 (4)).

    APPEND-ONLY: a change is a new version row, numbered per panel. Enable and disable are versions too. Every row is a
    marker: saved_at (database time), actor (the operator principal) and reason (trimmed, never blank). EFFECTIVE-FROM
    IS DERIVED, no column: the first panel_pass naming the version. NO default for cadence, size or enabled.

    Filters: an empty allow_symbols list restricts nothing; deny_symbols removes; asset_class, exchange, active and
    tradable are NULL when they do not filter.
    """

    TABLE_NAME = 'panel_settings_version'
    __tablename__ = TABLE_NAME

    VERSION_UNIQUE = 'uq_panel_settings_version_panel_id_version_no'
    VERSION_POSITIVE = 'ck_panel_settings_version_version_no_positive'
    SIZE_NON_NEGATIVE = 'ck_panel_settings_version_size_non_negative'
    ACTOR_NOT_BLANK = 'ck_panel_settings_version_actor_not_blank'
    REASON_NOT_BLANK = 'ck_panel_settings_version_reason_not_blank'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    panel_id = Column(UUID(as_uuid=True), ForeignKey('panel.id'), nullable=False)
    version_no = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False)
    cadence = Column(Enum(PanelCadence, name='panel_cadence', values_callable=_values), nullable=False)
    size = Column(Integer, nullable=False)
    allow_symbols = Column(ARRAY(String), nullable=False, server_default=text("'{}'::varchar[]"))
    deny_symbols = Column(ARRAY(String), nullable=False, server_default=text("'{}'::varchar[]"))
    asset_class = Column(Enum(AssetType), nullable=True)
    exchange = Column(String, nullable=True)
    active = Column(Boolean, nullable=True)
    tradable = Column(Boolean, nullable=True)
    saved_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # The operator principal (app/client_label.py OPERATOR_PRINCIPAL).
    actor = Column(String, nullable=False)
    # Stored trimmed; never blank.
    reason = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint('panel_id', 'version_no', name=VERSION_UNIQUE),
        CheckConstraint('version_no >= 1', name=VERSION_POSITIVE),
        CheckConstraint('size >= 0', name=SIZE_NON_NEGATIVE),
        CheckConstraint("btrim(actor) <> ''", name=ACTOR_NOT_BLANK),
        CheckConstraint("btrim(reason) <> ''", name=REASON_NOT_BLANK),
    )

    def __repr__(self):
        return f"<PanelSettingsVersion(id='{self.id}', panel_id='{self.panel_id}', version_no='{self.version_no}')>"


class PanelSourceSettingsVersion(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One version of a SOURCE's settings, shared by every panel on it: two hourly rates, corporate-action window.

    They act on datasets, which panels on one source share (ADR tj-26jt9w addendum 20 (b)). APPEND-ONLY, numbered per
    source, with the same marker columns as a panel version. No policy default for any number.

    Both rates are read as a SLIDING WINDOW of the trailing 60 minutes from the database at each tick; nothing is
    stored, so a restart resets nothing. ramp_datasets_per_hour is the most NEW panel datasets created per source in
    any trailing 60 minutes (CREATED transitions by recorded_at; 0 pauses creation). request_cap_per_hour is the most
    ingest requests the panel manager writes per source in any trailing 60 minutes, of every kind (ingest_request rows
    by created_at; the daily refresh's own requests do not count). The window is the days before a dataset's start and
    after its end that corporate actions are pulled for (see DEFAULT_CORPORATE_ACTION_WINDOW_DAYS).
    """

    TABLE_NAME = 'panel_source_settings_version'
    __tablename__ = TABLE_NAME

    VERSION_UNIQUE = 'uq_panel_source_settings_version_source_version_no'
    VERSION_POSITIVE = 'ck_panel_source_settings_version_version_no_positive'
    NUMBERS_NON_NEGATIVE = 'ck_panel_source_settings_version_numbers_non_negative'
    ACTOR_NOT_BLANK = 'ck_panel_source_settings_version_actor_not_blank'
    REASON_NOT_BLANK = 'ck_panel_source_settings_version_reason_not_blank'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    version_no = Column(Integer, nullable=False)
    ramp_datasets_per_hour = Column(Integer, nullable=False)
    request_cap_per_hour = Column(Integer, nullable=False)
    corporate_action_window_pre_days = Column(Integer, nullable=False)
    corporate_action_window_post_days = Column(Integer, nullable=False)
    saved_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    actor = Column(String, nullable=False)
    reason = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint('source', 'version_no', name=VERSION_UNIQUE),
        CheckConstraint('version_no >= 1', name=VERSION_POSITIVE),
        CheckConstraint(
            'ramp_datasets_per_hour >= 0 AND request_cap_per_hour >= 0 AND corporate_action_window_pre_days >= 0 '
            'AND corporate_action_window_post_days >= 0',
            name=NUMBERS_NON_NEGATIVE,
        ),
        CheckConstraint("btrim(actor) <> ''", name=ACTOR_NOT_BLANK),
        CheckConstraint("btrim(reason) <> ''", name=REASON_NOT_BLANK),
    )

    def __repr__(self):
        return f"<PanelSourceSettingsVersion(id='{self.id}', source='{self.source}', version_no='{self.version_no}')>"


class PanelPass(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One pass of the panel manager over one panel: which settings it ran under and which snapshots it read.

    APPEND-ONLY, with ONE ruled exception: the finish-pass write sets finished_at and counts on the pass's own row once,
    from NULL, guarded by finished_at IS NULL, and never changes them after. The first pass naming a settings version is
    that version's effective-from. A dry-run pass writes flagged rows that every read ignores. active_snapshot_id and
    inactive_snapshot_id name asset_list_snapshot rows with NO foreign key (snapshots are their own append-only record).
    """

    TABLE_NAME = 'panel_pass'
    __tablename__ = TABLE_NAME

    PANEL_STARTED_INDEX = 'ix_panel_pass_panel_id_started_at'
    SETTINGS_VERSION_INDEX = 'ix_panel_pass_settings_version_id'
    SOURCE_SETTINGS_VERSION_INDEX = 'ix_panel_pass_source_settings_version_id'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    panel_id = Column(UUID(as_uuid=True), ForeignKey('panel.id'), nullable=False)
    # MEMBERSHIP or RELEASE. No default.
    kind = Column(Enum(PanelPassKind, name='panel_pass_kind', values_callable=_values), nullable=False)
    settings_version_id = Column(UUID(as_uuid=True), ForeignKey('panel_settings_version.id'), nullable=False)
    source_settings_version_id = Column(
        UUID(as_uuid=True), ForeignKey('panel_source_settings_version.id'), nullable=False
    )
    active_snapshot_id = Column(UUID(as_uuid=True), nullable=False)
    inactive_snapshot_id = Column(UUID(as_uuid=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # NULL until the finish-pass write; set once.
    finished_at = Column(DateTime(timezone=True), nullable=True)
    dry_run = Column(Boolean, nullable=False)
    # Written with finished_at, once.
    counts = Column(JSONB, nullable=True)

    __table_args__ = (
        Index(PANEL_STARTED_INDEX, 'panel_id', 'started_at'),
        Index(SETTINGS_VERSION_INDEX, 'settings_version_id'),
        Index(SOURCE_SETTINGS_VERSION_INDEX, 'source_settings_version_id'),
    )

    def __repr__(self):
        return f"<PanelPass(id='{self.id}', panel_id='{self.panel_id}', started_at='{self.started_at}')>"


class PanelMemberTransition(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """The membership ledger: one row per member per transition, written only by the panel manager in its pass.

    APPEND-ONLY, no update or delete path. dataset_id and request_id name a dataset and a request with NO foreign key,
    so a row outlives a deleted dataset, as the override log does. A repeated deferral with the same cause is not a new
    row. Foreign keys run only inside the panel tables.
    """

    TABLE_NAME = 'panel_member_transition'
    __tablename__ = TABLE_NAME

    MEMBER_INDEX = 'ix_panel_member_transition_panel_id_vendor_asset_id_as_of'
    TRANSITION_RUN_INDEX = 'ix_panel_member_transition_pass_id'
    TRANSITION_RECORDED_INDEX = 'ix_panel_member_transition_transition_recorded_at'
    APPEND_ORDINAL_UNIQUE = 'uq_panel_member_transition_pass_id_seq'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    pass_id = Column(UUID(as_uuid=True), ForeignKey('panel_pass.id'), nullable=False)
    panel_id = Column(UUID(as_uuid=True), ForeignKey('panel.id'), nullable=False)
    vendor_asset_id = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    transition = Column(Enum(PanelTransition, name='panel_transition', values_callable=_values), nullable=False)
    dataset_id = Column(UUID(as_uuid=True), nullable=True)
    request_id = Column(UUID(as_uuid=True), nullable=True)
    cause = Column(String, nullable=True)
    # The snapshot instant the pass decided on.
    as_of = Column(DateTime(timezone=True), nullable=False)
    # Wall time (database time) of the write: the rate window reads it.
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # The row's 1-based append ordinal within its pass, assigned by the ledger repository (no server default). The
    # tie-break of the newest-first order, since id is random and recorded_at is the transaction start.
    seq = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint('pass_id', 'seq', name=APPEND_ORDINAL_UNIQUE),
        Index(MEMBER_INDEX, 'panel_id', 'vendor_asset_id', 'as_of'),
        Index(TRANSITION_RUN_INDEX, 'pass_id'),
        Index(TRANSITION_RECORDED_INDEX, 'transition', 'recorded_at'),
    )

    def __repr__(self):
        return (
            f"<PanelMemberTransition(id='{self.id}', pass_id='{self.pass_id}', "
            f"vendor_asset_id='{self.vendor_asset_id}', transition='{self.transition}')>"
        )
