from enum import StrEnum

from sqlalchemy import UUID, BigInteger, Column, DateTime, Enum, Index, String, func

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.sensitive import REDACTED


class ReadClientKind(StrEnum):
    """The kind of client a read came from: the UI, or anything else (SDK). Derived at record time."""

    UI = 'UI'
    SDK = 'SDK'


class DatasetReadKind(StrEnum):
    """What one dataset_read row records: a bars read, or a stream session's open and close."""

    READ = 'READ'
    STREAM_SESSION = 'STREAM_SESSION'


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class DatasetRead(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One read of a dataset's bars: the CONSUMPTION record (db -> client), tj-grna9p.46.

    Distinct from ingest_run, which is the COLLECTION record. Rows are buffered in process and inserted in
    batches by the usage recorder (app/usage_recorder.py), so the read path never issues a per-read INSERT.
    Rows older than the rollup window are folded into dataset_read_daily and deleted.

    NO FOREIGN KEY: usage outlives a deleted dataset, which is the point of the summaries.
    """

    TABLE_NAME = 'dataset_read'
    __tablename__ = TABLE_NAME

    DATASET_READ_AT_INDEX = 'ix_dataset_read_dataset_id_read_at'
    READ_AT_INDEX = 'ix_dataset_read_read_at'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    dataset_id = Column(UUID(as_uuid=True), nullable=False)
    # The reader's label: the existing dataset owner string, carried in the X-Client-Id request header
    # (tj-grna9p.131 OPTION R). A SensitiveString exactly like StoreDatasetEntry.owner, so no statement binding
    # it renders the value, and it is never logged. '_unattributed' and '_overflow' are the reserved values.
    client_id = Column(SensitiveString, nullable=False)
    # UI iff client_id was the operator principal when the read was recorded; everything else is SDK.
    client_kind = Column(Enum(ReadClientKind, name='read_client_kind', values_callable=_values), nullable=False)
    kind = Column(
        Enum(DatasetReadKind, name='dataset_read_kind', values_callable=_values),
        nullable=False,
        server_default=DatasetReadKind.READ.value,
    )
    # The route template that served the read, never a path with values in it.
    route = Column(String, nullable=False)
    bars_returned = Column(BigInteger, nullable=False, server_default='0')
    # For a READ the instant the read was served; for a STREAM_SESSION the instant it opened.
    read_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # STREAM_SESSION only; both NULL on a READ, and the close stays NULL while the session is open.
    session_opened_at = Column(DateTime(timezone=True), nullable=True)
    session_closed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # last_read_at and the reads in a window, per dataset.
        Index(DATASET_READ_AT_INDEX, 'dataset_id', 'read_at'),
        # The rollup's range scan.
        Index(READ_AT_INDEX, 'read_at'),
    )

    def __repr__(self):
        # client_id is sensitive: the marker, never the value.
        return (
            f"<DatasetRead(id='{self.id}', dataset_id='{self.dataset_id}', client_id='{REDACTED}', "
            f"kind='{self.kind}', bars_returned='{self.bars_returned}')>"
        )
