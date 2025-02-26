from enum import Enum
from typing import Optional, Type, TypeVar

from sqlalchemy import Integer
from common.database.sql_alchemy_types import BaseCustomSqlType
from common.logging import get_logger

log = get_logger(__name__)
S = TypeVar("S")

class OrderedEnum(BaseCustomSqlType[S, int]):
    """Enum that maintains an order such that the values can be compared by having the SQL representation be an
    integer."""
    def __init__(self, schema_type: Optional[Type[S]], **kwargs):
        BaseCustomSqlType.__init__(self, schema_type, int)

    def get_type(self) -> Type[int]:
        return Integer

    def validate_column_params(self, **kwargs) -> bool:
        return True

    def to_model_type(self, value: S) -> int:
        log.debug(f"  Converting {value} to model type")
        if isinstance(value, Enum):
            return value.value
        raise ValueError(f"Invalid value for IntEnum: {value}")

    def to_schema_type(self, value: int) -> S:
        log.debug(f"  Converting {value} to schema type")
        if value is not None:
            return self._model_type(value)
        raise ValueError(f"Invalid value for IntEnum: {value}")