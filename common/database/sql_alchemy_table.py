from typing import Any, Dict, List, Optional, Self, Tuple, Type, TypeVar

from pydantic import BaseModel

from sqlalchemy import Column
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql.base import ReadOnlyColumnCollection

from common.database.sql_alchemy_types import CustomColumn
from common.logging import get_logger

log = get_logger(__name__)


class AppBase:
    DATA_STORE_BASE = declarative_base()
    # Add other Apps with their own declarative_base() here


S = TypeVar("S", bound=BaseModel)


class CustomTypeTable:
    @classmethod
    def _get_columns(
        cls, exclude: Optional[List[str]] = None, exclude_none: bool = False
    ) -> Tuple[Dict[str, Column], Dict[str, CustomColumn]]:
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

        # log.debug(f"Other columns: {other_columns}")
        # log.debug(f"Custom type columns: {custom_type_columns}")
        return other_columns, custom_type_columns

    def __to_fields(
        self, schema_type: Type[S], additional: Optional[Dict[str, Any]] = None, exclude: Optional[List[str]] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
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
    ) -> S:
        other_fields, custom_fields, additional = self.__to_fields(schema_type, additional, exclude)
        return {**other_fields, **custom_fields, **additional}

    def to_validated_schema(
        self, schema_type: Type[S], additional: Optional[Dict[str, Any]] = None, exclude: Optional[List[str]] = None
    ) -> S:
        other_fields, custom_fields, additional = self.__to_fields(schema_type, additional, exclude)
        log.debug(f"Validating {schema_type.__name__} with fields: {other_fields}, {custom_fields}, {additional}")
        return schema_type.model_validate({**other_fields, **custom_fields, **additional})

    @classmethod
    def get_fields(cls, schema: S, exclude: Optional[List[str]] = None, exclude_none: bool = False) -> Dict[str, Any]:
        log.debug(f"Converting {cls.__name__} from schema")
        other_columns, custom_type_columns = cls._get_columns(exclude, exclude_none)
        columns = {}
        for name, value in schema.__dict__.items():
            # log.debug(f"  {name}: {value}(type: {type(value)})")
            if name in other_columns:
                columns[name] = value
            elif name in custom_type_columns:
                custom_column = custom_type_columns[name]
                columns[name] = custom_column.custom_type.to_model_type(value)

        return columns

    @classmethod
    def get_model(cls, schema: S, exclude_none: bool = False, exclude: Optional[List[str]] = None) -> Self:
        return cls(**cls.get_fields(schema, exclude_none, exclude))
