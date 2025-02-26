from abc import ABC, abstractmethod
from typing import Generic, Optional, Type, TypeVar

from sqlalchemy import Column

from common.logging import get_logger

log = get_logger(__name__)
S = TypeVar("S")
M = TypeVar("M")


class BaseCustomSqlType(Generic[S, M], ABC):
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
    def __init__(self, custom_type: Optional[BaseCustomSqlType[S, M] | Type[BaseCustomSqlType[S, M]]] = None, **kwargs):
        """Create a custom column.

        Args:
            custom_type (Optional[BaseCustomSqlType[S, M]  |  Type[BaseCustomSqlType[S, M]]], optional): Type or
                Instance of custom column. Defaults to None.
        """
        if custom_type is not None:
            self.custom_type = custom_type() if isinstance(custom_type, type) else custom_type
            self.custom_type.validate_column_params(**kwargs)
            Column.__init__(self, self.custom_type.get_type(), **kwargs)
        else:
            Column.__init__(self, **kwargs)
