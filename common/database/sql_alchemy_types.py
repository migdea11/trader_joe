from sqlalchemy.types import Integer
from sqlalchemy import INTEGER, TypeDecorator


class IntEnum(TypeDecorator):
    """
    Custom SQLAlchemy type to store IntEnum as integers in the database
    and convert back to the Enum in Python.
    """
    impl = Integer
    cache_ok = True  # Required for Alembic to cache the type

    def __init__(self, enum_class):
        super().__init__()
        self.enum_class = enum_class

    def process_bind_param(self, value, dialect):
        if isinstance(value, self.enum_class):
            return value.value
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return self.enum_class(value)
        return value

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(INTEGER)

    def __repr__(self):
        return f"IntEnum({self.enum_class.__name__})"
