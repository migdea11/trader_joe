from typing import Any, Dict, List, Optional, Self, Tuple, Type, TypeVar

from pydantic import BaseModel

from sqlalchemy import Column
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql.base import ReadOnlyColumnCollection

from common.database.sql_alchemy_types import CustomColumn
from common.logging import get_logger

log = get_logger(__name__)


class AppBase:
    """Stored all independent declarative_base() objects for each app."""
    DATA_STORE_BASE = declarative_base()
    # Add other Apps with their own declarative_base() here


S = TypeVar("S", bound=BaseModel)


class CustomTypeTable:
    """Base class for all tables that use custom types."""
    @classmethod
    def _get_columns(
        cls, exclude: Optional[List[str]] = None, exclude_none: bool = False
    ) -> Tuple[Dict[str, Column], Dict[str, CustomColumn]]:
        """Grabs all columns from the table, and separates them into custom type columns and other columns.

        Args:
            exclude (Optional[List[str]], optional): Columns to exclude from output. Defaults to None.
            exclude_none (bool, optional): Excludes columns if default value is None. Defaults to False.

        Returns:
            Tuple[Dict[str, Column], Dict[str, CustomColumn]]: Dictionary of other columns and custom type columns.
        """
        columns: ReadOnlyColumnCollection = cls.__table__.columns
        custom_type_columns = {}
        other_columns = {}
        for column in columns:
            if isinstance(column, CustomColumn) \
                    and (exclude is None or column.name not in exclude) \
                    and (exclude_none is False or column.default is None):
                custom_type_columns[column.name] = column
            elif isinstance(column, Column) \
                    and column.name not in custom_type_columns \
                    and (exclude is None or column.name not in exclude) \
                    and (exclude_none is False or column.default is None):
                other_columns[column.name] = column

        return other_columns, custom_type_columns

    def __to_fields(
        self, schema_type: Type[S], additional: Optional[Dict[str, Any]] = None, exclude: Optional[List[str]] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """Converting columns to fields for schema.

        Args:
            schema_type (Type[S]): Output schema type.
            additional (Optional[Dict[str, Any]], optional): Additional Fields to add to Model to complete the Schema. Defaults to None.
            exclude (Optional[List[str]], optional): Fields to be removed from Model to complete  Schema. Defaults to None.

        Returns:
            Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]: Unflattened fields, custom type fields, additional fields.
        """
        log.debug(f"Converting {self.__class__.__name__} to schema")
        other_columns, custom_type_columns = self.__class__._get_columns(exclude)
        return {
                col.name: getattr(self, col.name)
                for col in other_columns.values() if col.name in schema_type.model_fields
            }, \
            {
                col.name: col.custom_type.to_schema_type(getattr(self, col.name))
                for col in custom_type_columns.values() if col.name in schema_type.model_fields
            }, \
            (additional or {})

    def to_schema(
        self, schema_type: Type[S], additional: Optional[Dict[str, Any]] = None, exclude: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Converts the table Model to fields from Schema.

        Args:
            schema_type (Type[S]): Output schema type.
            additional (Optional[Dict[str, Any]], optional): Additional Fields to add to Model to complete the Schema. Defaults to None.
            exclude (Optional[List[str]], optional): Fields to be removed from Model to complete Schema. Defaults to None.

        Returns:
            Dict[str, Any]: output as Schema fields.
        """
        other_fields, custom_fields, additional = self.__to_fields(schema_type, additional, exclude)
        return {**other_fields, **custom_fields, **additional}

    def to_validated_schema(
        self, schema_type: Type[S], additional: Optional[Dict[str, Any]] = None, exclude: Optional[List[str]] = None
    ) -> S:
        """Converts the table Model to a validated Schema.

        Args:
            schema_type (Type[S]): Output schema type.
            additional (Optional[Dict[str, Any]], optional): Additional Fields to add to Model to complete the Schema. Defaults to None.
            exclude (Optional[List[str]], optional): Fields to be removed from Model to complete Schema. Defaults to None.

        Returns:
            S: output as validated Schema.
        """
        other_fields, custom_fields, additional = self.__to_fields(schema_type, additional, exclude)
        log.debug(f"Validating {schema_type.__name__} with fields: {other_fields}, {custom_fields}, {additional}")
        return schema_type.model_validate({**other_fields, **custom_fields, **additional})

    @classmethod
    def get_fields(cls, schema: S, exclude: Optional[List[str]] = None, exclude_none: bool = False) -> Dict[str, Any]:
        """Gets columns from table that matches content of Schema

        Args:
            schema (S): Schema to match columns with.
            exclude (Optional[List[str]], optional): Fields to ignore from Schema. Defaults to None.
            exclude_none (bool, optional): Excludes columns if default value is None. Defaults to False.

        Returns:
            Dict[str, Any]: Columns that match Schema.
        """
        log.debug(f"Converting {cls.__name__} from schema")
        other_columns, custom_type_columns = cls._get_columns(exclude, exclude_none)
        columns = {}
        for name, value in schema.__dict__.items():
            if name in other_columns:
                columns[name] = value
            elif name in custom_type_columns:
                custom_column = custom_type_columns[name]
                columns[name] = custom_column.custom_type.to_model_type(value)

        return columns

    @classmethod
    def get_model(cls, schema: S, exclude: Optional[List[str]] = None, exclude_none: bool = False) -> Self:
        """Converts Schema to Model.

        Args:
            schema (S): Input Schema.
            exclude (Optional[List[str]], optional): Fields to ignore from Schema. Defaults to None.
            exclude_none (bool, optional): Excludes columns if default value is None. Defaults to False.

        Returns:
            Self: Output Model.
        """
        return cls(**cls.get_fields(schema, exclude, exclude_none))
