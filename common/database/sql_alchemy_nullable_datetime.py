from copy import copy
from datetime import datetime, timezone
from typing import Optional, TypeVar

from sqlalchemy import DateTime
from common.database.sql_alchemy_types import BaseCustomSqlType
from common.logging import get_logger

log = get_logger(__name__)
S = TypeVar("S")


class NullableDateTime(BaseCustomSqlType[datetime, datetime]):
    """DateTime column that allows for None values (None in Schema == EPOCH in Model)."""
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def __init__(self, *args, **kwargs):
        BaseCustomSqlType.__init__(self, datetime, DateTime)

    def get_type(self) -> datetime:
        return DateTime(timezone=True)

    def validate_column_params(self, *args, **kwargs) -> bool:
        if "nullable" in kwargs and kwargs["nullable"] is True:
            raise ValueError("NullableDateTime does not support nullable parameter")
        return True

    def to_model_type(self, value: Optional[datetime]) -> datetime:
        log.debug(f"  Converting {value} to model type")
        if value is None:
            return copy(self.EPOCH)
        return value

    def to_schema_type(self, value: datetime) -> Optional[datetime]:
        log.debug(f"  Converting {value} to schema type")
        if value == self.EPOCH:
            return None
        return value