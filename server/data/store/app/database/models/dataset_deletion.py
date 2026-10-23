from sqlalchemy import UUID, BigInteger, Column, DateTime, Index, String, func

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.sensitive import REDACTED


class DatasetDeletion(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """The deletion record: one row per dataset the store deleted, whoever did it.

    APPEND-ONLY, like dataset_override_log: written once in the transaction of the delete, never updated or
    deleted. NO FOREIGN KEY, so it outlives the dataset. The operator's delete writes it with cause OPERATOR;
    the expiry reaper (tj-grna9p.23) reuses it with its own cause. cause is a plain string for that reason: a
    new cause is not a schema change.

    The descriptive columns are the dataset's own at deletion, as member names, so the row reads without the
    entry. owner is a SensitiveString: never logged, never in a repr.
    """

    TABLE_NAME = 'dataset_deletion'
    __tablename__ = TABLE_NAME

    DATASET_INDEX = 'ix_dataset_deletion_dataset_id'
    DELETED_AT_INDEX = 'ix_dataset_deletion_deleted_at'

    CAUSE_OPERATOR = 'OPERATOR'
    # The expiry reaper's deletions, actor 'reaper' (tj-grna9p.23).
    CAUSE_REAPER = 'REAPER'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    dataset_id = Column(UUID(as_uuid=True), nullable=False)
    owner = Column(SensitiveString, nullable=False)
    asset_symbol = Column(String, nullable=False)
    source = Column(String, nullable=False)
    granularity = Column(String, nullable=False)
    update_type = Column(String, nullable=False)
    # The window the entry declared; end is NULL for an open-ended dataset.
    range_start = Column(DateTime(timezone=True), nullable=False)
    range_end = Column(DateTime(timezone=True), nullable=True)
    # Bars the delete removed with the entry.
    bars_deleted = Column(BigInteger, nullable=False, server_default='0')
    cause = Column(String, nullable=False)
    # Who deleted it: the operator principal for an operator delete.
    actor = Column(String, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index(DATASET_INDEX, 'dataset_id'), Index(DELETED_AT_INDEX, 'deleted_at'))

    def __repr__(self):
        return (
            f"<DatasetDeletion(id='{self.id}', dataset_id='{self.dataset_id}', owner='{REDACTED}', "
            f"symbol='{self.asset_symbol}', cause='{self.cause}', deleted_at='{self.deleted_at}')>"
        )
