"""SensitiveString: layer M1's bind type (tj-w6bpjm; design tj-vhboky.41 Addendum 1, D3).

What the design asks of it, and what each group below pins:
- BIND: the value handed to the driver is a RedactedStr carrying the plain value; None stays None.
  Pinned through the postgresql+asyncpg dialect's own bind processor -- the path an execute takes
  -- and not only by calling process_bind_param, so a type that overrode the method but was
  bypassed by the dialect would still go red.
- READ: a plain str, type exactly str, so the wrapper never reaches an ORM attribute.
- RENDERING: a DBAPIError/StatementError built from the processed parameters renders the marker
  and NOT the value, while a neighbouring non-sensitive parameter IS still rendered -- the user
  ruled for keeping detail, so blanket hiding would be a regression, not extra safety.
- DDL: identical to plain String's, so adopting the type needs no migration.

No Postgres here. Whether asyncpg STORES a RedactedStr as its plain value is the system suite's
question (tests/system/test_asyncpg_bind_spike.py, run on the host). The in-memory sqlite engine
below is not a stand-in for that: it runs SQLAlchemy's real execute path (bind processing, echo,
exception wrapping) end to end, which is the rendering half, and that half is dialect-independent.
"""

import logging
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateTable

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_types import BaseCustomSqlType
from common.sensitive import REDACTED, RedactedStr


pytestmark = pytest.mark.common

SECRET = 'owner-5c1e-must-not-render'
NEIGHBOUR = 'feed-iex-must-render'


@pytest.fixture
def dialect() -> Dialect:
    return asyncpg_dialect.dialect()


def _table(metadata: sa.MetaData, owner_type: sa.types.TypeEngine) -> sa.Table:
    return sa.Table(
        'sensitive_probe',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('owner', owner_type, nullable=False),
        sa.Column('feed', sa.String(32), nullable=False),
    )


def _processed(statement: sa.sql.ClauseElement, dialect: Dialect) -> tuple[str, tuple]:
    """Compile for the dialect and run each bound value through its type's dialect bind processor.

    This is the tuple the execution context hands the DBAPI cursor, and so the one SQLAlchemy
    renders as "[parameters: ...]" when the cursor raises.
    """
    compiled = statement.compile(dialect=dialect)
    values = compiled.construct_params()
    processed = []
    for name in compiled.positiontup:
        processor = compiled.binds[name].type.dialect_impl(dialect).bind_processor(dialect)
        processed.append(processor(values[name]) if processor else values[name])
    return str(compiled), tuple(processed)


# --- BIND -----------------------------------------------------------------------------------


def test_the_asyncpg_bind_processor_hands_the_driver_a_redacted_str(dialect: Dialect) -> None:
    processor = SensitiveString().dialect_impl(dialect).bind_processor(dialect)
    assert processor is not None, 'the dialect must run a bind processor for SensitiveString'

    bound = processor(SECRET)

    assert type(bound) is RedactedStr
    assert bound == SECRET
    assert str.__str__(bound) == SECRET
    assert repr(bound) == REDACTED


def test_the_asyncpg_bind_processor_passes_none_through(dialect: Dialect) -> None:
    processor = SensitiveString().dialect_impl(dialect).bind_processor(dialect)
    assert processor is not None
    assert processor(None) is None


def test_process_bind_param_directly() -> None:
    bound = SensitiveString().process_bind_param(SECRET, asyncpg_dialect.dialect())
    assert type(bound) is RedactedStr
    assert bound == SECRET
    assert SensitiveString().process_bind_param(None, asyncpg_dialect.dialect()) is None


@pytest.mark.parametrize(
    'build',
    [
        pytest.param(lambda t: sa.insert(t).values(id=1, owner=SECRET, feed=NEIGHBOUR), id='insert'),
        pytest.param(
            lambda t: sa.select(t.c.id).where(t.c.owner == SECRET, t.c.feed == NEIGHBOUR), id='equality-filter'
        ),
        pytest.param(lambda t: sa.update(t).where(t.c.feed == NEIGHBOUR).values(owner=SECRET), id='update'),
        pytest.param(
            lambda t: (
                postgresql.insert(t)
                .values(id=1, owner=SECRET, feed=NEIGHBOUR)
                .on_conflict_do_update(index_elements=[t.c.id], set_={'owner': SECRET})
            ),
            id='on-conflict',
        ),
    ],
)
def test_every_statement_shape_binds_the_owner_as_a_redacted_str(build, dialect: Dialect) -> None:
    """Column comparisons and upserts bind through the column's type too, not only VALUES."""
    table = _table(sa.MetaData(), SensitiveString(64))

    _, parameters = _processed(build(table), dialect)

    owners = [p for p in parameters if isinstance(p, str) and str.__eq__(p, SECRET)]
    assert owners, 'the statement must bind the owner'
    assert all(type(p) is RedactedStr for p in owners)
    assert [type(p) for p in parameters if isinstance(p, str) and str.__eq__(p, NEIGHBOUR)] == [str]


# --- READ -----------------------------------------------------------------------------------


@pytest.mark.parametrize('driver_value', [SECRET, RedactedStr(SECRET)], ids=['plain-from-driver', 'subclass'])
def test_the_asyncpg_result_processor_returns_an_exact_str(driver_value: str, dialect: Dialect) -> None:
    processor = SensitiveString().dialect_impl(dialect).result_processor(dialect, None)
    assert processor is not None, 'the dialect must run a result processor for SensitiveString'

    read = processor(driver_value)

    assert type(read) is str
    assert read == SECRET
    assert repr(read) == repr(SECRET)


def test_the_asyncpg_result_processor_passes_none_through(dialect: Dialect) -> None:
    processor = SensitiveString().dialect_impl(dialect).result_processor(dialect, None)
    assert processor is not None
    assert processor(None) is None


# --- RENDERING ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'error_class', [sa.exc.DBAPIError, sa.exc.StatementError], ids=['DBAPIError', 'StatementError']
)
def test_an_error_built_from_the_processed_parameters_renders_the_marker_and_the_neighbour(
    error_class: type[sa.exc.StatementError], dialect: Dialect
) -> None:
    table = _table(sa.MetaData(), SensitiveString(64))
    sql, parameters = _processed(sa.insert(table).values(id=1, owner=SECRET, feed=NEIGHBOUR), dialect)

    if error_class is sa.exc.DBAPIError:
        error = sa.exc.DBAPIError.instance(sql, parameters, Exception('driver failure'), Exception)
    else:
        error = sa.exc.StatementError('statement failure', sql, parameters, Exception('driver failure'))
    rendered = str(error)

    assert '[parameters:' in rendered, 'the error must render its parameters, or the checks below are vacuous'
    assert REDACTED in rendered
    assert SECRET not in rendered
    assert repr(NEIGHBOUR) in rendered, 'non-sensitive parameters must stay in the rendered detail'


def test_a_plain_string_control_renders_the_value(dialect: Dialect) -> None:
    """Without the type the same error DOES render the value: what makes the pin above non-vacuous."""
    table = _table(sa.MetaData(), sa.String(64))
    sql, parameters = _processed(sa.insert(table).values(id=1, owner=SECRET, feed=NEIGHBOUR), dialect)

    rendered = str(sa.exc.DBAPIError.instance(sql, parameters, Exception('driver failure'), Exception))

    assert SECRET in rendered
    assert REDACTED not in rendered


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine('sqlite://', echo=True)
    yield engine
    engine.dispose()


def test_a_real_execute_renders_the_marker_in_echo_and_in_the_raised_error(
    sqlite_engine: sa.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """SQLAlchemy's own execute path: engine echo and the wrapped IntegrityError both show the marker."""
    metadata = sa.MetaData()
    table = _table(metadata, SensitiveString(64))
    metadata.create_all(sqlite_engine)
    insert = sa.insert(table).values(id=1, owner=SECRET, feed=NEIGHBOUR)

    with sqlite_engine.connect() as conn, caplog.at_level(logging.INFO, logger='sqlalchemy.engine'):
        conn.execute(insert)
        stored = conn.execute(sa.select(table.c.owner).where(table.c.owner == SECRET)).scalar_one()
        with pytest.raises(sa.exc.IntegrityError) as raised:
            conn.execute(insert)

    echoed = caplog.text
    assert REDACTED in echoed
    assert SECRET not in echoed
    assert repr(NEIGHBOUR) in echoed

    rendered = str(raised.value)
    assert '[parameters:' in rendered
    assert REDACTED in rendered
    assert SECRET not in rendered
    assert repr(NEIGHBOUR) in rendered

    # And the value itself went through: stored, matched by equality, read back as an exact str.
    assert stored == SECRET
    assert type(stored) is str


# --- DDL and shape --------------------------------------------------------------------------


@pytest.mark.parametrize('length', [None, 64], ids=['no-length', 'length-64'])
def test_the_ddl_is_plain_strings(length: int | None) -> None:
    dialect = postgresql.dialect()
    sensitive = CreateTable(_table(sa.MetaData(), SensitiveString(length))).compile(dialect=dialect)
    plain = CreateTable(_table(sa.MetaData(), sa.String(length))).compile(dialect=dialect)
    assert str(sensitive) == str(plain)


def test_it_is_a_statement_cacheable_type_decorator_over_string() -> None:
    assert issubclass(SensitiveString, sa.types.TypeDecorator)
    assert SensitiveString.impl is sa.String
    assert SensitiveString.cache_ok is True


def test_it_is_not_a_house_custom_sql_type() -> None:
    """BaseCustomSqlType converts on model build, which would put the wrapper into ORM attributes."""
    assert not issubclass(SensitiveString, BaseCustomSqlType)
