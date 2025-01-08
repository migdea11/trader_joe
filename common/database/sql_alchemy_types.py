from abc import ABC, abstractmethod
from copy import copy
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Optional, Type, TypeVar

from sqlalchemy import Column, DateTime, Integer

from common.logging import get_logger

log = get_logger(__name__)
S = TypeVar("S")
M = TypeVar("M")


class _CustomSqlType(Generic[S, M], ABC):
    def __init__(self, schema_type: Type[S], model_type: Type[M]):
        self._schema_type: Type[S] = schema_type
        self._model_type: Type[M] = model_type

    @abstractmethod
    def get_type(self) -> Type[M]:
        ...

    @abstractmethod
    def validate_column_params(self, *args, **kwargs) -> bool:
        ...

    @abstractmethod
    def to_model_type(self, value: S) -> M:
        ...

    @abstractmethod
    def to_schema_type(self, value: M) -> S:
        ...


class CustomColumn(Column):
    def __init__(self, custom_type: Optional[_CustomSqlType[S, M] | Type[_CustomSqlType[S, M]]] = None, **kwargs):
        if custom_type is not None:
            self.custom_type = custom_type() if isinstance(custom_type, type) else custom_type
            self.custom_type.validate_column_params(**kwargs)
            Column.__init__(self, self.custom_type.get_type(), **kwargs)
        else:
            Column.__init__(self, **kwargs)


class OrderedEnum(_CustomSqlType[S, int]):
    def __init__(self, schema_type: Optional[Type[S]], **kwargs):
        _CustomSqlType.__init__(self, schema_type, int)

    def get_type(self) -> int:
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


class NullableDateTime(_CustomSqlType[datetime, datetime], Column):
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def __init__(self, *args, **kwargs):
        _CustomSqlType.__init__(self, datetime, DateTime)

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
        if value == copy(self.EPOCH):
            return None
        return value


__all__ = ["CustomColumn", "OrderedEnum", "NullableDateTime"]
