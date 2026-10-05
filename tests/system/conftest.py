"""Shared fixtures for the system suite: tests/system/ against a live, migrated, DISPOSABLE stack.

Shared by every part of the suite (tj-vhboky.49 writes it; tj-vhboky.50 and tj-vhboky.51 build on
it). Run only by `make test-system` (tj-vhboky.48); pytest.ini keeps this directory out of the PR
gate through norecursedirs.

THE ENV CONTRACT IS THE ONLY CONFIGURATION. Every value below is read from a name the Makefile
comment above `test-system` defines, and from nothing else: no project env file, no defaults
that could quietly point somewhere, no fallback host. The make target reads the project env file
and hands these names over; this module never opens a file for configuration.

FAIL, NEVER SKIP (pytest.ini; ADR tj-fdb9gz section 3). A missing contract name, and a Postgres
that does not answer, are resources that SHOULD be there. Both call pytest.fail with a message
naming the variable, or the host and port -- never a skip, never an importorskip, never a
connect-and-skip. The password is never put in a message: it is bound into the URL object, whose
repr masks it, and psycopg2's connection errors carry the host, port and user but not the
password.

ISOLATION WITHOUT TRUNCATION. Every row a test writes is keyed by this run's identity:
  * RUN_ID, a short random hex string made once per session;
  * a synthetic symbol, ZZSYS plus RUN_ID in upper case, which no real ticker can collide with;
  * a per-run owner, system-test-<RUN_ID>, for the rows that carry owner.
Every entry a test creates goes through the `insert_entry` fixture, which records its id. At the
end of the session the registry deletes exactly those ids -- the ON DELETE CASCADE on
stock_market_activity.dataset_id takes their bars -- and then checks that no row carrying this
run's symbol survives in either table. Nothing is truncated, and nothing is deleted that this run
did not create, so the suite is safe to point at a database that holds other data and passes on
a second consecutive run (the symbol and owner are new each time).

NO DOCKER, NO MIGRATION, NO DOWNGRADE is driven from here. The stack is already up and migrated
when the suite starts; the make target says so and does neither.

WHY A SYNCHRONOUS psycopg2 ENGINE, when the service itself runs asyncpg. What the schema tests
assert is enforced by Postgres, not by the driver, and psycopg2 exposes the server's own error
fields directly: pgcode (the SQLSTATE) and diag.constraint_name / diag.column_name. The make venv
already carries psycopg2-binary in the data-store group (tj-vhboky.48, dependency check). A part
of the suite that must go through asyncpg -- tj-vhboky.51's asyncpg str-subclass spike, the crud
coroutines -- builds its own async engine from the same `pg_settings`.
"""

import hashlib
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.common.app_endpoints import InterfaceRest
from routers.common.instance_secret import INSTANCE_SECRET_HEADER
from tests.fakes.market_data import SCENARIO_PREFIXES


# ---------------------------------------------------------------------------------------------
# The env contract (Makefile, comment above test-system). Listed here so a reader can check this
# module against the Makefile in one place; every os.environ read below goes through _contract().
CONTRACT_DATABASE_HOST = 'DATABASE_NAME'  # data_store's own name for the Postgres HOST
CONTRACT_DATABASE_PORT = 'DATABASE_PORT'
CONTRACT_DATABASE_USER = 'POSTGRES_USER'
CONTRACT_DATABASE_PASS = 'POSTGRES_PASS'
CONTRACT_DATABASE_DB = 'POSTGRES_DB_NAME'
CONTRACT_DATABASE_TIMEOUT = 'DATABASE_CONN_TIMEOUT'
CONTRACT_DATA_STORE_URL = 'SYSTEM_TEST_DATA_STORE_URL'
CONTRACT_WRITE_SECRET = 'INSTANCE_WRITE_SECRET'


def _contract(name: str) -> str:
    """Read one env-contract name, failing -- never skipping -- when it is missing or empty."""
    value = os.environ.get(name, '')
    if not value:
        pytest.fail(
            f'system suite: env-contract variable {name} is missing or empty. Run the suite through '
            f'`make test-system SYSTEM_TEST_DISPOSABLE_DB=1`, which sets it (Makefile, comment above test-system).',
            pytrace=False,
        )
    return value


# ---------------------------------------------------------------------------------------------
# Run identity.

RUN_ID = uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    symbol: str
    owner: str

    def alt_symbol(self, suffix: str) -> str:
        """A second synthetic symbol, still unique to this run."""
        return f'{self.symbol}{suffix.upper()}'

    def alt_owner(self, suffix: str) -> str:
        """A second owner, still unique to this run."""
        return f'{self.owner}-{suffix}'


@pytest.fixture(scope='session')
def run_identity() -> RunIdentity:
    return RunIdentity(run_id=RUN_ID, symbol=f'ZZSYS{RUN_ID.upper()}', owner=f'system-test-{RUN_ID}')


@pytest.fixture
def own_symbol(request: pytest.FixtureRequest, run_identity: RunIdentity) -> str:
    """A synthetic symbol unique to this test in this run: the run's symbol plus a digest of the test id.

    Tests share the run's owner, so two tests inserting the same default entry would collide on
    the entry identity constraint. Giving each test its own symbol keeps them independent of
    order and of each other, and it still starts with the run's symbol, so the cleanup's survivor
    check covers it.
    """
    digest = hashlib.blake2s(request.node.nodeid.encode(), digest_size=4).hexdigest().upper()
    return run_identity.alt_symbol(digest)


# ---------------------------------------------------------------------------------------------
# Postgres.


@dataclass(frozen=True)
class PgSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    connect_timeout: int

    def url(self, drivername: str = 'postgresql+psycopg2') -> URL:
        return URL.create(
            drivername,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )

    def describe(self) -> str:
        """Where the suite is pointed, for failure messages. Never includes the password."""
        return f'{self.user}@{self.host}:{self.port}/{self.database}'


@pytest.fixture(scope='session')
def pg_settings() -> PgSettings:
    port = _contract(CONTRACT_DATABASE_PORT)
    timeout = _contract(CONTRACT_DATABASE_TIMEOUT)
    try:
        port_number, timeout_seconds = int(port), int(timeout)
    except ValueError:
        pytest.fail(
            f'system suite: {CONTRACT_DATABASE_PORT} and {CONTRACT_DATABASE_TIMEOUT} must be integers', pytrace=False
        )
    return PgSettings(
        host=_contract(CONTRACT_DATABASE_HOST),
        port=port_number,
        user=_contract(CONTRACT_DATABASE_USER),
        password=_contract(CONTRACT_DATABASE_PASS),
        database=_contract(CONTRACT_DATABASE_DB),
        connect_timeout=timeout_seconds,
    )


@pytest.fixture(scope='session')
def pg_engine(pg_settings: PgSettings) -> Iterator[Engine]:
    """A synchronous engine on the contract's Postgres, proven reachable before any test uses it.

    An unreachable server FAILS here with the host and port in the message. The probe also reads
    server_version so the first line of a red run says which Postgres answered.
    """
    engine = sa.create_engine(
        pg_settings.url(), connect_args={'connect_timeout': pg_settings.connect_timeout}, pool_pre_ping=True
    )
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SHOW server_version')).scalar_one()
    except sa.exc.OperationalError as exc:
        engine.dispose()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig is not None else type(exc).__name__
        pytest.fail(
            f'system suite: Postgres at {pg_settings.host}:{pg_settings.port} '
            f'(database {pg_settings.database}, user {pg_settings.user}) is unreachable: {reason}. '
            'Bring the stack up and migrate it (make dev-launch, make migrate) before make test-system.',
            pytrace=False,
        )
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------------------------
# Rows: every entry a test writes goes through insert_entry, which records it for cleanup.

ENTRY_TABLE = StoreDatasetEntry.__table__
BAR_TABLE = StockMarketActivity.__table__

# Fixed, timezone-aware instants. Rows are kept apart across runs by the run's symbol and owner,
# not by time, so these never need to vary between runs.
BASE_START = datetime(2001, 2, 5, 14, 30, tzinfo=UTC)
BASE_END = datetime(2001, 2, 6, 21, 0, tzinfo=UTC)


class EntryRegistry:
    """The ids of every store_dataset_entry row this run created, and nothing else."""

    def __init__(self) -> None:
        self.ids: list[uuid.UUID] = []

    def add(self, entry_id: uuid.UUID) -> None:
        self.ids.append(entry_id)


@pytest.fixture(scope='session')
def entry_registry(pg_engine: Engine, run_identity: RunIdentity) -> Iterator[EntryRegistry]:
    """Deletes this run's entries at session end -- by id -- and checks nothing of the run survives.

    The DELETE names only ids this run inserted; the cascade removes their bars. The survivor
    check counts rows carrying this run's synthetic symbol (or a symbol derived from it) in both
    tables. A survivor means a test wrote a row outside insert_entry, or the cascade did not
    fire, and the teardown fails with the counts rather than leaving litter behind silently.
    """
    registry = EntryRegistry()
    yield registry
    symbol_prefix = f'{run_identity.symbol}%'
    with pg_engine.begin() as conn:
        if registry.ids:
            conn.execute(sa.delete(ENTRY_TABLE).where(ENTRY_TABLE.c.id.in_(registry.ids)))
    with pg_engine.connect() as conn:
        entries_left = conn.execute(
            sa.select(sa.func.count()).select_from(ENTRY_TABLE).where(ENTRY_TABLE.c.asset_symbol.like(symbol_prefix))
        ).scalar_one()
        bars_left = conn.execute(
            sa.select(sa.func.count()).select_from(BAR_TABLE).where(BAR_TABLE.c.asset_symbol.like(symbol_prefix))
        ).scalar_one()
    if entries_left or bars_left:
        pytest.fail(
            f'system suite cleanup: {entries_left} entries and {bars_left} bars with symbol '
            f"{run_identity.symbol}* survived the delete of this run's {len(registry.ids)} entries",
            pytrace=False,
        )


@pytest.fixture(scope='session')
def entry_values(run_identity: RunIdentity) -> Callable[..., dict[str, Any]]:
    """Build a complete, valid store_dataset_entry row for this run, as Core insert values.

    Keys are column names. expiry_type and update_type are plain Integer columns underneath the
    OrderedEnum custom type, so they take the enum's integer value, exactly as upsert_entry binds
    them. Keyword overrides replace a column's value; omit=('owner',) leaves a column out of the
    INSERT entirely, so its server default applies.
    """

    def _values(omit: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            'owner': run_identity.owner,
            'source': DataSource.ALPACA_API,
            'asset_symbol': run_identity.symbol,
            'asset_type': AssetType.STOCK,
            'data_type': DataType.MARKET_ACTIVITY,
            'granularity': Granularity.ONE_MINUTE,
            'start': BASE_START,
            'end': BASE_END,
            'expiry_type': ExpiryType.BULK.value,
            'update_type': UpdateType.STATIC.value,
        }
        unknown = (set(overrides) | set(omit)) - set(ENTRY_TABLE.c.keys())
        assert not unknown, f'not columns of {ENTRY_TABLE.name}: {sorted(unknown)}'
        values.update(overrides)
        return {key: value for key, value in values.items() if key not in omit}

    return _values


@pytest.fixture(scope='session')
def bar_values() -> Callable[..., dict[str, Any]]:
    """Build a complete, valid stock_market_activity row for one entry, as Core insert values.

    The natural-key columns other than dataset_id are copied from the entry row, so a bar always
    agrees with the entry it belongs to unless a test overrides a column on purpose. feed is the
    US-equity IEX tape, the tape an ALPACA_API entry is actually served. Bars are only ever
    written under an entry this run created, so the cascade from insert_entry's cleanup takes
    them.
    """

    def _values(entry: sa.Row, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            'dataset_id': entry.id,
            'source': entry.source,
            'asset_symbol': entry.asset_symbol,
            'feed': Feed.IEX,
            'granularity': entry.granularity,
            'timestamp': entry.start,
            'open': 10.0,
            'high': 12.5,
            'low': 9.25,
            'close': 11.0,
            'volume': 1_000,
            'trade_count': 42,
        }
        unknown = set(overrides) - set(BAR_TABLE.c.keys())
        assert not unknown, f'not columns of {BAR_TABLE.name}: {sorted(unknown)}'
        values.update(overrides)
        return values

    return _values


@pytest.fixture(scope='session')
def insert_entry(
    pg_engine: Engine, entry_values: Callable[..., dict[str, Any]], entry_registry: EntryRegistry
) -> Callable[..., sa.Row]:
    """Insert one entry in its own committed transaction; return the full row it produced.

    The row is registered for cleanup before the caller sees it. Arguments are entry_values'.
    """

    def _insert(omit: tuple[str, ...] = (), **overrides: Any) -> sa.Row:
        with pg_engine.begin() as conn:
            row = conn.execute(
                sa.insert(ENTRY_TABLE).values(entry_values(omit, **overrides)).returning(*ENTRY_TABLE.c)
            ).one()
        entry_registry.add(row.id)
        return row

    return _insert


@pytest.fixture(scope='session')
def attempt(pg_engine: Engine) -> Callable[[sa.Executable], sa.exc.DBAPIError | None]:
    """Execute one statement in its own transaction; return the database error, or None if it committed.

    A refused statement aborts its transaction, so each attempt gets a fresh one and the refusal
    cannot poison the next statement. A statement that unexpectedly COMMITS an entry row is the
    caller's to register -- see the NULL-owner test, which registers anything it returns.
    """

    def _attempt(statement: sa.Executable) -> sa.exc.DBAPIError | None:
        try:
            with pg_engine.begin() as conn:
                conn.execute(statement)
        except sa.exc.DBAPIError as exc:
            return exc
        return None

    return _attempt


@pytest.fixture
def pg_conn(pg_engine: Engine) -> Iterator[Connection]:
    """A read connection for catalogue queries and read-backs."""
    with pg_engine.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def pg_async_engine(pg_settings: PgSettings) -> AsyncIterator[AsyncEngine]:
    """An asyncpg engine on the contract's Postgres -- the driver data_store itself runs (tj-vhboky.51).

    For tests whose question is about asyncpg or about the crud coroutines, which take an
    AsyncSession. Function-scoped because pytest.ini sets the asyncio fixture loop scope to
    function, and an async engine's pooled connections belong to the loop that opened them.
    Proven reachable before the test runs: an unreachable server FAILS here naming the host and
    port, never skips. asyncpg's refusals carry the host and port but not the password, and the
    URL object masks the password in its repr.
    """
    engine = create_async_engine(
        pg_settings.url('postgresql+asyncpg'), connect_args={'timeout': pg_settings.connect_timeout}
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text('SHOW server_version'))
    except (sa.exc.DBAPIError, OSError, TimeoutError) as exc:
        await engine.dispose()
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        pytest.fail(
            f'system suite: Postgres at {pg_settings.host}:{pg_settings.port} '
            f'(database {pg_settings.database}, user {pg_settings.user}) is unreachable over asyncpg: {reason}. '
            'Bring the stack up and migrate it (make dev-launch, make migrate) before make test-system.',
            pytrace=False,
        )
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------------------------
# data_store over HTTP (tj-vhboky.50). Resolved lazily: a test that does not ask for these does
# not need them set, but one that does and finds them missing fails.


@pytest.fixture(scope='session')
def data_store_url() -> str:
    return _contract(CONTRACT_DATA_STORE_URL).rstrip('/')


# Never request this from a TEST function: pytest prints a test function's arguments, values
# included, at the head of a failure traceback. Tests reach the secret only through `data_store`
# below, whose repr masks it.
@pytest.fixture(scope='session')
def write_secret() -> str:
    return _contract(CONTRACT_WRITE_SECRET)


# ---------------------------------------------------------------------------------------------
# No log record carries the write secret (tj-3mk3u5.47; tj-3mk3u5.41 finding F5).
#
# common/logging.py's basicConfig puts the root logger at DEBUG with a stderr handler, so
# httpcore's trace reaches both pytest's 'Captured log' and 'Captured stderr' sections. Its
# 'receive_response_headers.complete return_value=(...)' line prints every RAW response header
# value as a bytes literal, so a response that echoed the secret in a header printed it in full,
# in the report of the very test that caught the leak.
#
# So every record is redacted WHERE IT IS CREATED. A session-wide record factory, chained over
# the one it replaces and restored at teardown, rewrites each new record before any filter or
# handler sees it. It does not care which logger emits. httpcore creates its trace on five child
# loggers (httpcore.connection, .http11, .http2, .proxy, .socks), and a Filter on 'httpcore'
# would never run for them: a logger's filters see only the records created on that logger, and
# its children's records reach only its handlers. The trace keeps printing; only the value
# changes.

REDACTED = '<redacted>'


class SecretRedaction:
    """Replaces every spelling of one secret with '<redacted>': THE redaction (tj-3mk3u5.47 R3).

    DataStoreHttp.redact and the log-record factory both call this class, so the two cannot drift.
    The spellings are the secret as text, and as the inside of a bytes literal. httpcore prints
    header values with repr, which escapes a quote, a backslash and any non-ASCII character. A hex
    secret reads the same both ways. The longest spelling is replaced first, so no occurrence is
    left half-replaced.

    The secret lives only in here, behind a repr that masks it: pytest prints the arguments of
    every frame in a failure's traceback, and this object is the form the secret travels in.
    """

    def __init__(self, secret: str) -> None:
        # surrogateescape: a value read from os.environ always encodes back, so this cannot raise,
        # and a traceback from this frame would print `secret`.
        as_bytes_literal = repr(secret.encode(errors='surrogateescape'))[2:-1]
        self.__spellings = tuple(sorted({secret, as_bytes_literal} - {''}, key=len, reverse=True))

    def __repr__(self) -> str:
        return f'{type(self).__name__}(<redacted>)'

    def __call__(self, text: str) -> str:
        for spelling in self.__spellings:
            text = text.replace(spelling, REDACTED)
        return text


# Renders a record's traceback at creation, so it is redacted before any handler could render it
# from the live exception.
_TRACEBACK_FORMATTER = logging.Formatter()


def redact_log_record(record: logging.LogRecord, redaction: SecretRedaction) -> None:
    """Rewrite a new record in place so that nothing a handler reads from it carries the secret.

    The message is rendered here, once, and kept redacted, with no args left to render it again
    from. A record's exception is rendered too, into exc_text, which logging.Formatter prints in
    place of formatting exc_info, and exc_info is dropped (R5). A call whose args do not fit its
    message keeps both, rendered side by side and redacted. Otherwise the record would reach
    Handler.handleError, which prints msg and args raw.
    """
    try:
        message = record.getMessage()
    except Exception:  # a malformed logging call must not raise in the code that made it
        message = f'{record.msg} (logging args that did not fit: {record.args!r})'
    record.msg, record.args = redaction(message), ()
    if record.exc_info:
        record.exc_text = redaction(_TRACEBACK_FORMATTER.formatException(record.exc_info))
        record.exc_info = None


def redacting_record_factory(
    previous: Callable[..., logging.LogRecord], redaction: SecretRedaction
) -> Callable[..., logging.LogRecord]:
    """A log-record factory that builds each record with `previous`, then redacts it."""

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        redact_log_record(record, redaction)
        return record

    return factory


@contextmanager
def log_records_redacted(redaction: SecretRedaction) -> Iterator[None]:
    """Install the redacting factory over whichever factory is current, and put that one back on exit."""
    previous = logging.getLogRecordFactory()
    logging.setLogRecordFactory(redacting_record_factory(previous, redaction))
    try:
        yield
    finally:
        logging.setLogRecordFactory(previous)


@pytest.fixture(scope='session', autouse=True)
def redacted_log_records() -> Iterator[None]:
    """Every log record this session creates reaches its handlers with the write secret redacted.

    Autouse and session-scoped, so it is set up ahead of this suite's other fixtures for the first
    test and torn down after them at the end. The secret is read here, in the body, never taken
    as an argument (R4). The suite already requires it: make test-system refuses to start
    without it. So no test fails for a reason it did not have, and nothing here needs a running
    data_store.
    """
    with log_records_redacted(SecretRedaction(_contract(CONTRACT_WRITE_SECRET))):
        yield


class SecretLogProbe:
    """Puts the deployment's write secret into log records, and says whether text carries it (R7).

    For tests/system/test_log_redaction.py, which proves that the INSTALLED factory redacts the
    REAL secret. The secret stays in here, behind a repr that masks it, so no test function holds
    it (R4). carries_secret looks for both spellings by itself, not through SecretRedaction, so a
    fault in the redaction cannot also blind the check meant to catch it.
    """

    def __init__(self, secret: str) -> None:
        self.__secret = secret

    def __repr__(self) -> str:
        return f'{type(self).__name__}(secret=<redacted>)'

    def emit_header_trace(self, logger_name: str, level: int = logging.DEBUG) -> None:
        """Log one record shaped like httpcore's trace of a 401 that echoed the secret in a header."""
        return_value = (
            b'HTTP/1.1',
            401,
            b'Unauthorized',
            [(b'content-length', b'53'), (b'www-authenticate', self.__secret.encode())],
        )
        logging.getLogger(logger_name).log(level, f'receive_response_headers.complete return_value={return_value!r}')

    def emit_failure_with_traceback(self, logger_name: str) -> None:
        """Log one record whose exc_info is an exception with the secret in its message."""
        try:
            raise ConnectionError(f'the peer sent back {self.__secret}')
        except ConnectionError:
            logging.getLogger(logger_name).exception('receive_response_headers.failed')

    def carries_secret(self, text: str) -> bool:
        """Whether `text` holds the secret, as text or as a bytes literal's inside. Assert on the bool."""
        return self.__secret in text or repr(self.__secret.encode())[2:-1] in text


@pytest.fixture(scope='session')
def secret_log_probe() -> SecretLogProbe:
    """The probe, holding the deployment's secret, read in the body (R4). It needs no data_store."""
    return SecretLogProbe(_contract(CONTRACT_WRITE_SECRET))


@dataclass(frozen=True)
class RedactionKit:
    """The redaction's parts, for test_log_redaction.py to build with SYNTHETIC secrets.

    tests/system is not a package, so a test module cannot import this conftest by name; this
    fixture hands the parts over instead. Nothing in it holds the deployment's secret.
    """

    redaction: type[SecretRedaction]
    data_store_http: type['DataStoreHttp']
    records_redacted: Callable[[SecretRedaction], AbstractContextManager[None]]


@pytest.fixture(scope='session')
def redaction_kit() -> RedactionKit:
    return RedactionKit(SecretRedaction, DataStoreHttp, log_records_redacted)


# ---------------------------------------------------------------------------------------------
# One HTTP client for data_store that holds the write secret and never shows it (tj-vhboky.50).
#
# THE SECRET STAYS INSIDE DataStoreHttp. A test names what a write carries -- WriteAuth.ABSENT,
# EMPTY, WRONG or RIGHT -- and never the value. What a failure can print is kept clean in five
# places: the client's repr masks the secret (pytest prints fixture arguments in a traceback);
# describe() and every transport-failure message pass through redact(), which is the shared
# SecretRedaction above; a transport failure is a pytest.fail with pytrace=False, so no httpx
# frame -- whose arguments include the headers -- is printed; leaks_secret() and
# response_leaks_secret() return a bool, so a test asserts on a plain name rather than on an
# expression pytest would expand, response body included, into the failure message; and the
# captured log, where every record is created already redacted by the session-wide factory above
# (tj-3mk3u5.47), so httpcore's trace of a header that echoed the secret prints '<redacted>' in
# both 'Captured log' and 'Captured stderr'.

HTTP_TIMEOUT_SECONDS = 30.0


class WriteAuth(StrEnum):
    """What a request carries in the instance-secret header."""

    ABSENT = 'absent'  # no header at all
    EMPTY = 'empty'  # the header, with an empty value
    WRONG = 'wrong'  # a value that is not the deployment's secret
    RIGHT = 'right'  # the deployment's secret


class DataStoreHttp:
    """A client for the running data_store, reached over real HTTP through the prod image's uvicorn."""

    def __init__(self, base_url: str, secret: str) -> None:
        self.base_url = base_url
        self.__secret = secret
        self.__redaction = SecretRedaction(secret)
        wrong = f'wrong-{uuid.uuid4().hex}'
        while wrong == secret:  # astronomically unlikely; cheap to rule out
            wrong = f'wrong-{uuid.uuid4().hex}'
        self.__wrong = wrong
        self._client = httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS)

    def __repr__(self) -> str:
        return f'DataStoreHttp({self.base_url!r}, secret=<redacted>)'

    def _headers(self, auth: WriteAuth) -> dict[str, str]:
        match auth:
            case WriteAuth.ABSENT:
                return {}
            case WriteAuth.EMPTY:
                return {INSTANCE_SECRET_HEADER: ''}
            case WriteAuth.WRONG:
                return {INSTANCE_SECRET_HEADER: self.__wrong}
            case WriteAuth.RIGHT:
                return {INSTANCE_SECRET_HEADER: self.__secret}
        raise AssertionError(f'unhandled WriteAuth {auth!r}')

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: WriteAuth = WriteAuth.ABSENT,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Send one request. A data_store that does not answer FAILS the test, naming the URL; never a skip."""
        try:
            return self._client.request(method, path, params=params, json=json, headers=self._headers(auth))
        except httpx.TransportError as exc:
            reason = self.redact(f'{type(exc).__name__}: {exc}')
        pytest.fail(
            f'system suite: data_store at {self.base_url} did not answer {method} {path}: {reason}. '
            'Bring the stack up (make dev-launch, make migrate) before make test-system.',
            pytrace=False,
        )

    def get(self, path: str, *, params: dict[str, Any] | None = None, auth: WriteAuth = WriteAuth.ABSENT):
        return self.request('GET', path, params=params, auth=auth)

    def post(self, path: str, *, json: Any, auth: WriteAuth) -> httpx.Response:
        return self.request('POST', path, json=json, auth=auth)

    def delete(self, path: str, *, params: dict[str, Any] | None = None, auth: WriteAuth) -> httpx.Response:
        return self.request('DELETE', path, params=params, auth=auth)

    def leaks_secret(self, text: str) -> bool:
        """Whether `text` contains the deployment's secret. Assert on the returned bool, never on this call."""
        return self.__secret in text

    def response_leaks_secret(self, response: httpx.Response) -> bool:
        """Whether the response's body, or the value of any header it carries, contains the secret.

        Header values are read RAW, from multi_items(), and never through str(response.headers):
        that string is httpx's Headers repr, which prints the values of Authorization and
        Proxy-Authorization as '[secure]' (httpx SENSITIVE_HEADERS) and so cannot see a secret
        sent back under exactly the two names a credential is most likely to travel in
        (tj-3mk3u5.41). multi_items() also keeps a repeated header's values apart rather than
        joining them. Assert on the returned bool, never on this call: a bool keeps pytest from
        expanding the body into the failure message.
        """
        if self.leaks_secret(response.text):
            return True
        return any(self.leaks_secret(value) for _, value in response.headers.multi_items())

    def redact(self, text: str) -> str:
        """`text` with the secret, as text or as a bytes literal's inside, replaced: SecretRedaction (R3)."""
        return self.__redaction(text)

    def describe(self, response: httpx.Response) -> str:
        """The request and its answer, for an assertion message, with the secret redacted."""
        request = response.request
        return self.redact(f'{request.method} {request.url} -> {response.status_code} {response.text[:2000]}')

    def close(self) -> None:
        self._client.close()


@pytest.fixture(scope='session')
def data_store(data_store_url: str) -> Iterator[DataStoreHttp]:
    """The running data_store, proven reachable before any test uses it.

    The secret is read in the body, not taken as a fixture argument, for the traceback reason
    above. A data_store that does not answer, or answers the ping with anything but 200, FAILS
    here naming the URL.
    """
    client = DataStoreHttp(data_store_url, _contract(CONTRACT_WRITE_SECRET))
    response = client.get(InterfaceRest.PING.value)
    if response.status_code != 200:
        client.close()
        pytest.fail(
            f'system suite: data_store at {data_store_url} answered {InterfaceRest.PING.value} with '
            f'{response.status_code}, not 200. Is the stack up?',
            pytrace=False,
        )
    yield client
    client.close()


@pytest.fixture(scope='session')
def adopt_entries(
    pg_engine: Engine, entry_registry: EntryRegistry, run_identity: RunIdentity
) -> Callable[[str], list[uuid.UUID]]:
    """Register for cleanup every entry carrying `symbol`, and return their ids, oldest first.

    For a test whose HTTP request could create an entry the test did not insert itself: a
    dataset POST. None of this suite's POSTs should create one -- each is refused before the
    upsert -- but if a regression lets one through, the entry is adopted rather than left
    behind. The symbol must be one of this run's (own_symbol, or an alt_symbol), so this never
    claims a row another run or test owns.
    """

    def _adopt(symbol: str) -> list[uuid.UUID]:
        assert symbol.startswith(run_identity.symbol), f'{symbol} is not one of this run'
        with pg_engine.connect() as conn:
            ids = list(
                conn.execute(
                    sa.select(ENTRY_TABLE.c.id)
                    .where(ENTRY_TABLE.c.asset_symbol == symbol)
                    .order_by(ENTRY_TABLE.c.created_at, ENTRY_TABLE.c.id)
                ).scalars()
            )
        for entry_id in ids:
            if entry_id not in entry_registry.ids:
                entry_registry.add(entry_id)
        return ids

    return _adopt


@pytest.fixture(scope='session')
def scenario_entries(pg_engine: Engine, run_identity: RunIdentity) -> Iterator[Callable[[str], list[uuid.UUID]]]:
    """adopt_entries for a symbol that carries a FakeRead scenario prefix (tj-vhboky.63, Sys-7).

    FakeRead picks its scenario from the symbol's PREFIX (tests/fakes/market_data.py), so a
    scenario symbol is <PREFIX><one of this run's symbols> -- which the run's symbol% pattern that
    adopt_entries and entry_registry's survivor check use cannot match. This fixture is their
    counterpart for those symbols: it returns the ids carrying `symbol`, oldest first, remembers
    them, and at session end deletes exactly those ids (the cascade takes their bars) and fails if
    any entry or bar with a scenario-prefixed run symbol survives. Nothing is truncated, and
    nothing is deleted that this run did not create.
    """
    patterns = [f'{prefix}{run_identity.symbol}%' for prefix in SCENARIO_PREFIXES]
    ids: list[uuid.UUID] = []

    def _adopt(symbol: str) -> list[uuid.UUID]:
        assert any(symbol.startswith(f'{prefix}{run_identity.symbol}') for prefix in SCENARIO_PREFIXES), (
            f'{symbol} is not a scenario symbol of this run'
        )
        with pg_engine.connect() as conn:
            found = list(
                conn.execute(
                    sa.select(ENTRY_TABLE.c.id)
                    .where(ENTRY_TABLE.c.asset_symbol == symbol)
                    .order_by(ENTRY_TABLE.c.created_at, ENTRY_TABLE.c.id)
                ).scalars()
            )
        ids.extend(entry_id for entry_id in found if entry_id not in ids)
        return found

    yield _adopt
    with pg_engine.begin() as conn:
        if ids:
            conn.execute(sa.delete(ENTRY_TABLE).where(ENTRY_TABLE.c.id.in_(ids)))
    with pg_engine.connect() as conn:
        entries_left = conn.execute(
            sa.select(sa.func.count())
            .select_from(ENTRY_TABLE)
            .where(sa.or_(*(ENTRY_TABLE.c.asset_symbol.like(pattern) for pattern in patterns)))
        ).scalar_one()
        bars_left = conn.execute(
            sa.select(sa.func.count())
            .select_from(BAR_TABLE)
            .where(sa.or_(*(BAR_TABLE.c.asset_symbol.like(pattern) for pattern in patterns)))
        ).scalar_one()
    if entries_left or bars_left:
        pytest.fail(
            f'system suite cleanup: {entries_left} entries and {bars_left} bars with a scenario-prefixed '
            f"{run_identity.symbol}* symbol survived the delete of this run's {len(ids)} scenario entries",
            pytrace=False,
        )


@pytest.fixture(scope='session')
def bar_create_body() -> Callable[..., dict[str, Any]]:
    """Build a JSON body for the internal single-bar POST, for one bar under a seeded entry.

    The identifying fields are copied from the entry row, as bar_values does for SQL. Keyword
    overrides replace OHLCV values only.
    """

    def _body(entry: sa.Row, timestamp: datetime, feed: Feed = Feed.IEX, **data: float | int) -> dict[str, Any]:
        values: dict[str, float | int] = {
            'open': 10.0,
            'high': 12.5,
            'low': 9.25,
            'close': 11.0,
            'volume': 1_000,
            'trade_count': 42,
        }
        unknown = set(data) - set(values)
        assert not unknown, f'not bar data fields: {sorted(unknown)}'
        values.update(data)
        return {
            'dataset_id': str(entry.id),
            'asset_symbol': entry.asset_symbol,
            'source': entry.source.value,
            'granularity': entry.granularity.value,
            'feed': feed.value,
            'timestamp': timestamp.isoformat(),
            'data': values,
        }

    return _body
