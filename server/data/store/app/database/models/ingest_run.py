from enum import StrEnum

from sqlalchemy import UUID, BigInteger, Column, DateTime, Enum, Index, Integer, String, func

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.sensitive import REDACTED


class IngestRunTrigger(StrEnum):
    """What started a collection run. Store-local: the UI's RunTrigger is a wire type, this is a column's."""

    REQUEST = 'REQUEST'
    SCHEDULE = 'SCHEDULE'
    RETRY = 'RETRY'


class IngestRunState(StrEnum):
    """Where a collection run stands. RUNNING is the only non-terminal member."""

    RUNNING = 'RUNNING'
    SUCCEEDED = 'SUCCEEDED'
    # The vendor answered for the window and held no bars. Not a failure (tj-fe19tu).
    EMPTY = 'EMPTY'
    FAILED = 'FAILED'


# error_reason written by the startup sweep for a run a previous process left RUNNING. It is a
# RUN-RECORD MARKER and NOT a member of common.errors.vocabulary.Reason: nothing raises it, it never
# appears on problem+json, and a client branching on Reason must never meet it there. It exists only
# so the run history can say "this run was cut off" rather than "this run failed for a reason".
INTERRUPTED_REASON = 'INTERRUPTED'


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class IngestRun(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One attempt to collect a dataset: the COLLECTION record (tj-grna9p.8 item 1).

    WRITTEN IN ITS OWN TRANSACTIONS, on its own session, never inside the fetch's all-or-nothing
    transaction (app/ingest/run_recording.py). The RUNNING row has to be committed before the stream
    opens, and a fetch that rolls back must still leave its FAILED row.

    RUNS OUTLIVE THEIR DATASET, so dataset_id is nullable and carries NO foreign key, and the label
    columns below are denormalised copies of what the entry's identity said when the run started.
    """

    TABLE_NAME = 'ingest_run'
    __tablename__ = TABLE_NAME

    DATASET_STARTED_INDEX = 'ix_ingest_run_dataset_id_started_at_id'
    RUNNING_INDEX = 'ix_ingest_run_running_dataset_id'
    FINISHED_INDEX = 'ix_ingest_run_finished_at'

    # No unique=True: primary_key=True already provides it (see StoreDatasetEntry.id).
    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)

    # EMPTY until ingest acknowledges the fetch: a new dataset's entry is written on the ack, so its
    # id does not exist before then. A run that fails before its ack keeps NULL and is identified by
    # its label. No foreign key, because the run must survive the dataset's deletion.
    dataset_id = Column(UUID(as_uuid=True), nullable=True)
    # The request-log entry that caused the run. This is the link the scheduler's request log will use
    # (tj-pd41mt); no request log exists yet, so it stays NULL and carries no foreign key.
    request_id = Column(UUID(as_uuid=True), nullable=True)

    trigger = Column(Enum(IngestRunTrigger, name='ingest_run_trigger', values_callable=_values), nullable=False)
    # Who caused the run: the request body's owner for POST /store. A SensitiveString exactly like
    # StoreDatasetEntry.owner, so no statement binding it renders the value, and it is never logged (D8).
    actor = Column(SensitiveString, nullable=False)

    # The label: what the dataset's identity said when the run started. Same enum types as the entry.
    asset_symbol = Column(String, nullable=False)
    asset_type = Column(Enum(AssetType), nullable=False)
    data_type = Column(Enum(DataType), nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)
    # UpdateType's integer value. A plain column rather than the entry's CustomColumn: this table is
    # read and written through the crud's own functions, never through get_fields.
    update_type = Column(Integer, nullable=False)
    # The RESOLVED tape, known only at the ack. NULL for a run that never got one.
    feed = Column(Enum(Feed, name='feed', values_callable=lambda enum_cls: [m.value for m in enum_cls]), nullable=True)

    # The window asked for, half-open [start, end). NULL end is an open end.
    requested_start = Column(DateTime(timezone=True), nullable=True)
    requested_end = Column(DateTime(timezone=True), nullable=True)

    state = Column(
        Enum(IngestRunState, name='ingest_run_state', values_callable=_values),
        nullable=False,
        server_default=IngestRunState.RUNNING.value,
    )
    bars_written = Column(BigInteger, nullable=False, server_default='0')

    # error_domain and error_reason are the typed vocabulary's (domain, reason); error_message is
    # TraderJoeError.detail and NEVER str() of an exception (D8). error_reason also takes
    # INTERRUPTED_REASON, a run-record marker that is not a Reason member and never reaches
    # problem+json. All four are NULL unless state is FAILED.
    error_domain = Column(String, nullable=True)
    error_reason = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    error_id = Column(String, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # NULL while RUNNING.
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A dataset's runs, newest first: the keyset of the runs list and the window function's
        # partition in the catalog's last_run read.
        Index(DATASET_STARTED_INDEX, 'dataset_id', started_at.desc(), id.desc()),
        # The Running facet and filter read only the few rows that are RUNNING.
        Index(RUNNING_INDEX, 'dataset_id', postgresql_where=(state == IngestRunState.RUNNING)),
        # Retention and the collection stats window by finish time (tj-grna9p.65).
        Index(FINISHED_INDEX, 'finished_at'),
    )

    def __repr__(self):
        # actor is sensitive: the marker, never the value.
        return (
            f"<IngestRun(id='{self.id}', dataset_id='{self.dataset_id}', actor='{REDACTED}', "
            f"symbol='{self.asset_symbol}', state='{self.state}', bars_written='{self.bars_written}')>"
        )
