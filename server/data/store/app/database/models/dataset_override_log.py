from enum import StrEnum

from sqlalchemy import UUID, Column, DateTime, Enum, Index, String, Text, func

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.sensitive import REDACTED


class OverrideAction(StrEnum):
    """What the operator did to a dataset another owner holds."""

    SET_EXPIRY = 'SET_EXPIRY'
    SET_COLLECTING = 'SET_COLLECTING'
    DELETE = 'DELETE'


def _values(enum_cls: type[OverrideAction]) -> list[str]:
    return [member.value for member in enum_cls]


class DatasetOverrideLog(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """The operator override log: one row per write the operator made on a dataset another owner holds.

    APPEND-ONLY. Nothing in this service updates or deletes a row of it; the only write is the INSERT in
    app/database/crud/dataset_override.py. NO FOREIGN KEY, so a row outlives the dataset a DELETE removed.

    owner is a SensitiveString like StoreDatasetEntry.owner: never logged, never in a repr.
    """

    TABLE_NAME = 'dataset_override_log'
    __tablename__ = TABLE_NAME

    DATASET_AT_INDEX = 'ix_dataset_override_log_dataset_id_at'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    # Who acted: the operator principal (app/client_label.py OPERATOR_PRINCIPAL).
    actor = Column(String, nullable=False)
    dataset_id = Column(UUID(as_uuid=True), nullable=False)
    # The dataset's owner when the override was made.
    owner = Column(SensitiveString, nullable=False)
    action = Column(Enum(OverrideAction, name='override_action', values_callable=_values), nullable=False)
    # Stored trimmed; never blank.
    reason = Column(Text, nullable=False)
    at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index(DATASET_AT_INDEX, 'dataset_id', 'at'),)

    def __repr__(self):
        return (
            f"<DatasetOverrideLog(id='{self.id}', dataset_id='{self.dataset_id}', owner='{REDACTED}', "
            f"action='{self.action}', at='{self.at}')>"
        )
