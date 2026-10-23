from sqlalchemy import UUID, BigInteger, Column, Date, DateTime, Enum, Index, UniqueConstraint, func

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.sensitive import REDACTED
from data.store.app.database.models.dataset_read import ReadClientKind


def _values(enum_cls: type[ReadClientKind]) -> list[str]:
    return [member.value for member in enum_cls]


class DatasetReadDaily(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """Per-read rows past the rollup window, folded to one row per (dataset, client id, UTC day).

    Kept indefinitely. NO FOREIGN KEY, so a summary outlives its dataset. The client kind is carried so the
    UI/SDK filter still works past the window.
    """

    TABLE_NAME = 'dataset_read_daily'
    __tablename__ = TABLE_NAME

    UNIQUE_CONSTRAINT = 'uq_dataset_read_daily_dataset_id_client_id_day'
    DAY_INDEX = 'ix_dataset_read_daily_day'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    dataset_id = Column(UUID(as_uuid=True), nullable=False)
    client_id = Column(SensitiveString, nullable=False)
    client_kind = Column(Enum(ReadClientKind, name='read_client_kind', values_callable=_values), nullable=False)
    # The UTC calendar day of read_at.
    day = Column(Date, nullable=False)
    reads = Column(BigInteger, nullable=False, server_default='0')
    # Stream sessions folded into the day; they are not counted in reads.
    sessions = Column(BigInteger, nullable=False, server_default='0')
    bars_returned = Column(BigInteger, nullable=False, server_default='0')
    # The latest read in the folded rows, so last_read_at stays exact after the rows are gone.
    last_read_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('dataset_id', 'client_id', 'day', name=UNIQUE_CONSTRAINT),
        Index(DAY_INDEX, 'day'),
    )

    def __repr__(self):
        return (
            f"<DatasetReadDaily(dataset_id='{self.dataset_id}', client_id='{REDACTED}', day='{self.day}', "
            f"reads='{self.reads}')>"
        )
