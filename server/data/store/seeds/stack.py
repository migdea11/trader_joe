"""How the producer reaches the stack: over the network, from test_client's own environment.

The producer runs inside test_client, launched per call, on the stack's networks and with no Docker
access (ADR tj-4rr0la addendum 10; decision tj-vhboky.55 addendum S9). It needs no other server and
no new setting: everything comes from the variables test_client already carries
(docker-compose.test-client.yaml):

    DATABASE_NAME             the Postgres host
    DATABASE_PORT             its port
    POSTGRES_USER, POSTGRES_PASS, POSTGRES_DB_NAME
    SYSTEM_TEST_DATA_STORE_URL  data_store's base URL, no trailing path
    INSTANCE_WRITE_SECRET     sent as the write-secret header and nowhere else

A variable that is missing or empty fails the run (StackError, exit 1) and the message names the
VARIABLE, never a value.

SQL goes through the sync driver the data-store group already ships (psycopg2), one connection per
run, opened with SESSION_OPTIONS (TimeZone=UTC, extra_float_digits=1) because the manifest digests
and the dump's quoting depend on them. The connection autocommits: NORMALISE_SQL carries its own
BEGIN and COMMIT. Results come back as rows.

ERROR TEXT. A failed query raises StackError naming the STEP and the SQLSTATE only, never the
driver's message, which can quote a row value (the synthetic check runs before the rows are known to
be synthetic). A failed connect names the step and the exception class only, because the driver's
message can carry connection details. The chained exception is dropped for the same reason.

THE SECRET is read from the environment at each POST and sent only as the header. It is not copied
into this object: the object keeps a reference to the environment mapping and reads the secret from
it at each POST. It is never put in an argument, a file, a log line, an exception message or the
bundle.
"""

import contextlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx
import psycopg2

from data.store.seeds.manifest import SESSION_OPTIONS


# The variables read, by role. Names only; values never appear in a message.
ENV_DB_HOST = 'DATABASE_NAME'
ENV_DB_PORT = 'DATABASE_PORT'
ENV_DB_USER = 'POSTGRES_USER'
ENV_DB_AUTH = 'POSTGRES_PASS'
ENV_DB_NAME = 'POSTGRES_DB_NAME'
ENV_STORE_URL = 'SYSTEM_TEST_DATA_STORE_URL'
ENV_WRITE_KEY = 'INSTANCE_WRITE_SECRET'

REQUEST_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 10
# How much of a reply body is kept, for the caller's data_points parse.
REPLY_BODY_LIMIT = 400


class StackError(Exception):
    """A call against the stack failed. The message never carries a query result, a driver message or a secret."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, '')
    if not value:
        raise StackError(f'the environment variable {name} is missing or empty')
    return value


@dataclass(frozen=True)
class StackSettings:
    """Where the stack is, read from the environment.

    Args:
        host (str): The Postgres host (DATABASE_NAME).
        port (int): Its port (DATABASE_PORT).
        user (str): POSTGRES_USER.
        password (str): POSTGRES_PASS; never shown in a repr.
        database (str): POSTGRES_DB_NAME.
        store_url (str): data_store's base URL (SYSTEM_TEST_DATA_STORE_URL), no trailing slash.
    """

    host: str
    port: int
    user: str
    database: str
    store_url: str
    password: str = field(repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        """Read every setting, each required and non-empty.

        Args:
            environ (Mapping[str, str]): The process environment.

        Returns:
            StackSettings: The settings.

        Raises:
            StackError: If a variable (including INSTANCE_WRITE_SECRET, read later) is missing or
                empty, or the port is not an integer. The message names the variable only.
        """
        host = _required(environ, ENV_DB_HOST)
        raw_port = _required(environ, ENV_DB_PORT)
        user = _required(environ, ENV_DB_USER)
        password = _required(environ, ENV_DB_AUTH)
        database = _required(environ, ENV_DB_NAME)
        store_url = _required(environ, ENV_STORE_URL)
        _required(environ, ENV_WRITE_KEY)
        if not (raw_port.isascii() and raw_port.isdigit()):
            raise StackError(f'the environment variable {ENV_DB_PORT} is not an integer')
        return cls(
            host=host,
            port=int(raw_port),
            user=user,
            database=database,
            store_url=store_url.rstrip('/'),
            password=password,
        )


Connect = Callable[..., Any]
HttpPost = Callable[..., Any]


class Stack:
    """Runs SQL over one driver connection and POSTs the scenario over HTTP.

    Args:
        environ (Mapping[str, str] | None): The environment to read; os.environ when None.
        connect (Connect): psycopg2.connect, unless a test injects its own.
        http_post (HttpPost): httpx.post, unless a test injects its own.

    Raises:
        StackError: If a setting is missing or empty (see StackSettings.from_environment).
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        connect: Connect = psycopg2.connect,
        http_post: HttpPost = httpx.post,
    ) -> None:
        self.__environ = os.environ if environ is None else environ
        self.__settings = StackSettings.from_environment(self.__environ)
        self.__connect = connect
        self.__http_post = http_post
        self.__connection: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection, if one was opened."""
        connection, self.__connection = self.__connection, None
        if connection is not None:
            connection.close()

    def __cursor(self, what: str) -> Any:
        if self.__connection is None:
            settings = self.__settings
            try:
                connection = self.__connect(
                    host=settings.host,
                    port=settings.port,
                    user=settings.user,
                    password=settings.password,
                    dbname=settings.database,
                    options=SESSION_OPTIONS,
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except Exception as error:
                raise StackError(f'{what}: could not connect ({type(error).__name__})') from None
            try:
                connection.autocommit = True
            except Exception as error:
                # A failing close must not replace the StackError, nor carry a driver message out.
                with contextlib.suppress(Exception):
                    connection.close()
                raise StackError(f'{what}: could not set autocommit ({type(error).__name__})') from None
            self.__connection = connection
        try:
            return self.__connection.cursor()
        except psycopg2.Error as error:
            raise StackError(f'{what}: could not open a cursor ({type(error).__name__})') from None

    def query(self, sql: str, what: str) -> list[tuple[Any, ...]]:
        """Run one statement and return its rows.

        Args:
            sql (str): The statement.
            what (str): What this is, for the error message.

        Returns:
            list[tuple[Any, ...]]: The rows.

        Raises:
            StackError: If the statement fails; the message holds the step and the SQLSTATE only.
        """
        cursor = self.__cursor(what)
        try:
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        except psycopg2.Error as error:
            raise StackError(f'{what} failed (SQLSTATE {error.pgcode or "unknown"})') from None
        finally:
            cursor.close()

    def script(self, sql: str, what: str) -> None:
        """Run a script that returns no rows, as one execute (it carries its own BEGIN and COMMIT).

        Args:
            sql (str): The script.
            what (str): What this is, for the error message.

        Raises:
            StackError: If the script fails; the message holds the step and the SQLSTATE only.
        """
        cursor = self.__cursor(what)
        try:
            cursor.execute(sql)
        except psycopg2.Error as error:
            raise StackError(f'{what} failed (SQLSTATE {error.pgcode or "unknown"})') from None
        finally:
            cursor.close()

    def post(self, path: str, body: dict[str, str], secret_header: str) -> dict[str, object]:
        """POST one request to data_store.

        Args:
            path (str): The route, appended to SYSTEM_TEST_DATA_STORE_URL.
            body (dict[str, str]): The JSON body.
            secret_header (str): The name of the write-secret header; its value is read from the
                environment here, at the call.

        Returns:
            dict[str, object]: {'status': int, 'body': str}, the body truncated.

        Raises:
            StackError: If INSTANCE_WRITE_SECRET is gone, or the request fails to complete. The
                message names the step and the exception class only.
        """
        secret = _required(self.__environ, ENV_WRITE_KEY)
        try:
            response = self.__http_post(
                self.__settings.store_url + path,
                json=body,
                headers={secret_header: secret},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            return {'status': int(response.status_code), 'body': str(response.text)[:REPLY_BODY_LIMIT]}
        except Exception as error:
            raise StackError(f'POST {path} failed ({type(error).__name__})') from None
