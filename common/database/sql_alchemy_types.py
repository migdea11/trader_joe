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
    """Custom SQL Column type for SQLAlchemy.

    Args:
        Generic (S, M): Schema Field and Model Column types used to be converted to/from each other.
        ABC: _description_
    """
    def __init__(self, schema_type: Type[S], model_type: Type[M]):
        self._schema_type: Type[S] = schema_type
        self._model_type: Type[M] = model_type

    @abstractmethod
    def get_type(self) -> Type[M]:
        """Get the SQLAlchemy type for the column.

        Returns:
            Type[M]: SQLAlchemy column type.
        """
        ...

    @abstractmethod
    def validate_column_params(self, *args, **kwargs) -> bool:
        """Validate the column parameters.

        Returns:
            bool: True if valid, False otherwise.
        """
        ...

    @abstractmethod
    def to_model_type(self, value: S) -> M:
        """Convert the schema type to the model type.

        Args:
            value (S): Schema type value.

        Returns:
            M: Model type value.
        """
        ...

    @abstractmethod
    def to_schema_type(self, value: M) -> S:
        """Convert the model type to the schema type.

        Args:
            value (M): Model type value.

        Returns:
            S: Schema type value.
        """
        ...


class CustomColumn(Column):
    """Custom Column for SQLAlchemy. Allowing to intercept the conversion between schema and model types."""
    def __init__(self, custom_type: Optional[_CustomSqlType[S, M] | Type[_CustomSqlType[S, M]]] = None, **kwargs):
        if custom_type is not None:
            self.custom_type = custom_type() if isinstance(custom_type, type) else custom_type
            self.custom_type.validate_column_params(**kwargs)
            Column.__init__(self, self.custom_type.get_type(), **kwargs)
        else:
            Column.__init__(self, **kwargs)


class OrderedEnum(_CustomSqlType[S, int]):
    """Enum that maintains an order such that the values can be compared.

    TODO move to own module."""
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
    """DateTime column that allows for None values (None in Schema == EPOCH in Model).

    TODO move to own module."""
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
