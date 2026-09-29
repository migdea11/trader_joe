from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from common.sensitive import RedactedStr


class SensitiveString(TypeDecorator[str]):
    """String column whose bound parameters render as the redaction marker (layer M1).

    Design: tj-vhboky.41 ADDENDUM 1, D3, M1; see common/sensitive.py for what it does and does not
    guard. process_bind_param wraps each value in a RedactedStr, so SQLAlchemy's exception text,
    engine echo and uvicorn's traceback render the marker, while the driver still sends and
    stores the real value. Column comparisons (`column == value`) bind through this type too.
    Reads return a plain str. The DDL is String's, so adopting it needs no migration.

    Use it as Column(SensitiveString, ...) -- a plain TypeDecorator, NOT through the house
    BaseCustomSqlType/CustomColumn mechanism (common/database/sql_alchemy_types.py). That one
    converts values when the model is built, which would put the wrapper into ORM attributes;
    the bind processor confines it to the parameters sent to the driver.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return RedactedStr(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        # str() of a str subclass returns an exact str, so no wrapper ever leaves a read.
        return str(value)
