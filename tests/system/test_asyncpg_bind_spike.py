"""SPIKE, now a regression pin (tj-vhboky.51, tj-w6bpjm): asyncpg binds RedactedStr as its plain value.

tj-w6bpjm step 0, moved to the system suite (tj-vhboky.14 item H3). Design: tj-vhboky.41
Addendum 1, D3, layer M1 -- SensitiveString (common/database/sql_alchemy_sensitive_string.py), a
TypeDecorator whose process_bind_param wraps the value in RedactedStr (common/sensitive.py), a
str subclass that overrides __repr__ ONLY, so every repr()-based rendering (exception text,
engine echo, uvicorn's traceback) shows the marker while the driver sends and stores the real
value. The rendering half is pinned offline in common/tests/database/
test_sql_alchemy_sensitive_string.py; the whole layer rests on the other half, which needs a real
Postgres: that asyncpg encodes such a subclass exactly as it encodes a plain str.

HISTORY. This module first ran (host, 2026-09-29) against local stand-ins written to the design's
wording, before M1 existed; both halves passed, the output is on tj-w6bpjm, and the architect ruled
M1 proceeds as designed. M1 is now built, so the stand-ins are gone and the module drives the real
RedactedStr and SensitiveString (tj-w6bpjm validator stage). A red here now means the shipped type
does not store, match or upsert as a plain str would -- the owner column (tj-vhboky.46) must not
adopt it until that is understood. The host run copies both tests' printed SPIKE blocks onto the
bead that asked for the run. The blocks are printed straight to the terminal, past pytest's
capture, so they appear in a passing run's output too.

WHAT IS COMPARED. One scenario -- insert, select back, filter by equality, ON CONFLICT on the
wrapped column -- runs twice: through a plain String column (the control) and through
SensitiveString. Both runs must produce identical observations, and each run must show which type
actually reached the driver in every statement binding the value; without that, the SensitiveString run
could pass because something upstream had already turned the subclass back into a plain str. ON
CONFLICT is the discriminating step: had the driver sent anything but the value, the second insert
would find no conflict and add a second row.

THE SECOND HALF. A statement carrying the value is forced to fail (division by zero in a sibling
column, a server error whose text echoes no parameter), and the rendered "[parameters: ...]" is
printed. On the SensitiveString run the plain value must be absent and the marker present -- the
stand-in run only observed the marker; the real type is held to it. That parameters were rendered
at all is a precondition, and the plain control must show the value, which is what keeps
SensitiveString's absence from being vacuous.

TEMP TABLES ONLY. Each test opens one connection and one transaction, creates a TEMPORARY table in
it, and rolls the transaction back at the end, which drops the table. Nothing is written to the
stack's schema and nothing survives the test. The value is synthetic and belongs to this run.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.sensitive import REDACTED, RedactedStr


pytestmark = pytest.mark.data_store

TEMP_TABLE = 'zzsys_asyncpg_bind_spike'


# The two ways the value is bound: the control first, so a broken scenario shows up there.
BINDINGS = [
    pytest.param(sa.String(), str, id='plain-str-control'),
    pytest.param(SensitiveString(), RedactedStr, id='sensitive-string'),
]


@pytest.fixture
def spike_value(run_identity) -> str:
    """The value bound: synthetic, this run's, and unmistakable for the marker."""
    value = f'{run_identity.owner}-spike-owner'
    assert REDACTED not in value and value not in REDACTED
    return value


@dataclass
class DriverParameters:
    """For each statement that bound the spike value: its leading keyword and the value's type at the driver."""

    value: str
    seen: list[tuple[str, type]] = field(default_factory=list)


@pytest.fixture
def driver_parameters(pg_async_engine: AsyncEngine, spike_value: str) -> Iterator[DriverParameters]:
    """Record what the asyncpg cursor is handed, after every bind processor has run."""
    recorded = DriverParameters(value=spike_value)

    def _before(conn, cursor, statement, parameters, context, executemany) -> None:
        for parameter in parameters or ():
            if isinstance(parameter, str) and str.__eq__(parameter, spike_value):
                recorded.seen.append((statement.split(None, 1)[0].upper(), type(parameter)))

    sa.event.listen(pg_async_engine.sync_engine, 'before_cursor_execute', _before)
    yield recorded
    sa.event.remove(pg_async_engine.sync_engine, 'before_cursor_execute', _before)


def _temp_table(column_type: sa.types.TypeEngine) -> sa.Table:
    return sa.Table(
        TEMP_TABLE,
        sa.MetaData(),
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('owner', column_type, nullable=False, unique=True),
        sa.Column('note', sa.Text, nullable=False),
        prefixes=['TEMPORARY'],
    )


@dataclass(frozen=True)
class Observed:
    stored: list[tuple[Any, ...]]
    stored_types: list[type]
    matched: list[tuple[Any, ...]]
    after_upsert: list[tuple[Any, ...]]
    rows_holding_marker: int
    typed_read: Any


async def _scenario(conn: AsyncConnection, table: sa.Table, value: str) -> Observed:
    """Insert, select back, equality filter and ON CONFLICT on `owner`; everything read back as the server holds it.

    Read-backs go through untyped SQL, so no result processor touches what the server returns;
    only typed_read goes through the column's own type.
    """
    await conn.execute(sa.insert(table).values(owner=value, note='first'))
    stored = (await conn.execute(sa.text(f'SELECT owner, octet_length(owner) FROM {TEMP_TABLE}'))).all()
    matched = (await conn.execute(sa.select(table.c.note).where(table.c.owner == value))).all()
    upsert = pg_insert(table).values(owner=value, note='second')
    upsert = upsert.on_conflict_do_update(index_elements=[table.c.owner], set_={'note': upsert.excluded.note})
    await conn.execute(upsert)
    after_upsert = (await conn.execute(sa.text(f'SELECT owner, note FROM {TEMP_TABLE} ORDER BY id'))).all()
    rows_holding_marker = (
        await conn.execute(sa.text(f'SELECT count(*) FROM {TEMP_TABLE} WHERE owner = :marker'), {'marker': REDACTED})
    ).scalar_one()
    typed_read = (await conn.execute(sa.select(table.c.owner))).scalar_one()
    return Observed(
        stored=[tuple(row) for row in stored],
        stored_types=[type(row[0]) for row in stored],
        matched=[tuple(row) for row in matched],
        after_upsert=[tuple(row) for row in after_upsert],
        rows_holding_marker=rows_holding_marker,
        typed_read=typed_read,
    )


def _print_block(capsys: pytest.CaptureFixture[str], title: str, lines: list[str]) -> None:
    """Print a SPIKE block past pytest's capture, so it reaches the host run's output on a pass."""
    with capsys.disabled():
        print(f'\n----- SPIKE {title} -----')
        for line in lines:
            print(f'  {line}')
        print(f'----- END SPIKE {title} -----')


@pytest.mark.asyncio
@pytest.mark.parametrize(('column_type', 'expected_driver_type'), BINDINGS)
async def test_a_repr_override_subclass_binds_exactly_like_a_plain_str(
    column_type: sa.types.TypeEngine,
    expected_driver_type: type,
    pg_async_engine: AsyncEngine,
    spike_value: str,
    driver_parameters: DriverParameters,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insert, select back, equality filter and ON CONFLICT all behave as for a plain str; the stored value is plain."""
    table = _temp_table(column_type)
    async with pg_async_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(table.metadata.create_all)
            observed = await _scenario(conn, table, spike_value)
        finally:
            await transaction.rollback()

    _print_block(
        capsys,
        f'H3 FIRST HALF [{type(column_type).__name__} -> {expected_driver_type.__name__}]',
        [
            f'driver received (statement, type of the bound value): {[(kw, t.__name__) for kw, t in driver_parameters.seen]}',
            f'stored (owner, octet_length): {observed.stored}; python types read back: {observed.stored_types}',
            f'equality filter matched: {observed.matched}',
            f'after ON CONFLICT DO UPDATE: {observed.after_upsert}',
            f'rows holding the marker: {observed.rows_holding_marker}; typed read: {observed.typed_read!r}',
        ],
    )

    assert driver_parameters.seen == [
        ('INSERT', expected_driver_type),
        ('SELECT', expected_driver_type),
        ('INSERT', expected_driver_type),
    ], 'the insert, the equality filter and the upsert must each hand the driver the value as this binding makes it'
    assert observed.stored == [(spike_value, len(spike_value.encode()))], 'the server must hold exactly the plain value'
    assert observed.stored_types == [str]
    assert observed.matched == [('first',)], 'an equality filter on the bound value must find the row'
    assert observed.after_upsert == [(spike_value, 'second')], (
        'ON CONFLICT on the column must match the stored row and update it, not insert a second row'
    )
    assert observed.rows_holding_marker == 0, 'the marker must never reach the server'
    assert observed.typed_read == spike_value
    assert type(observed.typed_read) is str


@pytest.mark.asyncio
@pytest.mark.parametrize(('column_type', 'expected_driver_type'), BINDINGS)
async def test_a_failed_statement_renders_what_the_binding_shows(
    column_type: sa.types.TypeEngine,
    expected_driver_type: type,
    pg_async_engine: AsyncEngine,
    spike_value: str,
    driver_parameters: DriverParameters,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A DBAPIError on a statement carrying the value, and what its rendered parameters show.

    The control must render the plain value -- that is what proves parameters are rendered at
    all. SensitiveString must render the marker in its place.
    """
    table = _temp_table(column_type)
    # Division by zero in a sibling column: the server's message echoes no parameter, so anything
    # of the value in the rendered text came from SQLAlchemy's parameter rendering.
    failing = sa.insert(table).values(
        owner=spike_value, note=sa.cast(sa.literal(1, sa.Integer) / sa.literal(0, sa.Integer), sa.Text)
    )
    async with pg_async_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(table.metadata.create_all)
            with pytest.raises(sa.exc.DBAPIError) as raised:
                await conn.execute(failing)
        finally:
            await transaction.rollback()

    rendered = str(raised.value)
    parameters_line = next((line for line in rendered.splitlines() if '[parameters:' in line), '<none>')
    _print_block(
        capsys,
        f'H3 SECOND HALF [{type(column_type).__name__} -> {expected_driver_type.__name__}]',
        [
            f'error class: {type(raised.value).__module__}.{type(raised.value).__name__}',
            f'driver received: {[(kw, t.__name__) for kw, t in driver_parameters.seen]}',
            f'rendered parameters: {parameters_line}',
            f'plain value present: {spike_value in rendered}; marker present: {REDACTED in rendered}',
        ],
    )

    assert driver_parameters.seen == [('INSERT', expected_driver_type)]
    assert '[parameters:' in rendered, 'the error must render its parameters, or nothing below is evidence'
    if expected_driver_type is str:
        assert spike_value in rendered, 'the control must render the plain value, or the redaction check is vacuous'
    else:
        assert spike_value not in rendered, 'the plain value must not appear where the error is rendered'
        assert REDACTED in rendered, 'the marker must stand where the value was'
