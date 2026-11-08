"""Shared fixtures for the data_store SQLite tier (validator, tj-grna9p.85 round 2).

POSTGRES'S to_regclass, ON EVERY SQLITE CONNECTION. The usage catalog asks `to_regclass('dataset_read')` before it
reads usage (usage_tables_exist in app/database/crud/dataset_read.py, the same probe the audit writes and the reaper
use). On Postgres it returns the table's name, or NULL when the table does not exist, and never raises. SQLite has
no such function, so until tj-grna9p.85's round 2 the probe raised there and a catch in the catalog turned that into
"not ready". The catch is gone (a real probe failure must propagate), so the SQLite tier needs the function itself.

It is registered ONCE, here, for every SQLite connection any test opens: a listener on the Engine class's connect
event, autouse for the whole session, removed at the end. It answers like Postgres: the name when the table exists
on that connection's database, else NULL. So a fixture that creates the usage tables gets "ready", and one that does
not (most of the route tier) gets the zero view and the one-time warning exactly as an unmigrated Postgres would.

LOGGING STATE IS PROCESS-WIDE (tj-7zjov1.92, finding F5 on tj-7zjov1.75). app_depends' lifespan calls
common.app_lifecycle.startup_logs, which installs the broker sink once per process (propagate False on alpaca,
requests, urllib3, httpx and httpcore, a handler on each) and wraps root's and uvicorn's handler formatters in
ScrubFormatter; migrations/env.py wraps root's too. Unrestored, that outlives the test that did it: a later test
capturing httpx on root passes vacuously, and test order decides what passes. restore_logging_state runs every
test inside common.tests.logging_isolation.guarded_isolated_logging, imported and not copied: isolated_logging,
then a CHECK that the restore held. A touched logger with another propagate flag, level or handler list, a new
module sink, or a root handler with a different formatter fails the test's teardown, naming what leaked. Root's
handler LIST is not compared: pytest swaps its own capture handlers there per phase.
test_logging_state_restored.py proves the gap.
"""

import sqlite3
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from common.tests.logging_isolation import guarded_isolated_logging


def _to_regclass_on(connection: sqlite3.Connection) -> Callable[[str | None], str | None]:
    """Postgres's to_regclass(name) on one SQLite connection: the name if that table exists, else NULL."""

    def to_regclass(name: str | None) -> str | None:
        if name is None:
            return None
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name.split('.')[-1],)
        ).fetchone()
        return name if found else None

    return to_regclass


def register_to_regclass(dbapi_connection, _connection_record) -> None:
    """The connect hook: aiosqlite's adapter runs the registration on its own thread; plain sqlite3 directly.

    Under aiosqlite, run_async hands over the aiosqlite.Connection, whose execute is a coroutine; the function runs
    on aiosqlite's worker thread inside a statement, so it queries the sqlite3 connection that thread owns
    (aiosqlite keeps it as `_conn`; it has no public accessor).
    """
    if hasattr(dbapi_connection, 'run_async'):
        dbapi_connection.run_async(lambda raw: raw.create_function('to_regclass', 1, _to_regclass_on(raw._conn)))
    elif isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function('to_regclass', 1, _to_regclass_on(dbapi_connection))


@pytest.fixture(scope='session', autouse=True)
def sqlite_to_regclass() -> Iterator[None]:
    """Register to_regclass on every SQLite connection opened during the session."""
    event.listen(Engine, 'connect', register_to_regclass)
    try:
        yield
    finally:
        event.remove(Engine, 'connect', register_to_regclass)


@pytest.fixture(autouse=True)
def restore_logging_state() -> Iterator[None]:
    """Run the test against a clean broker sink; fail if any logging state survives the restore."""
    with guarded_isolated_logging():
        yield
