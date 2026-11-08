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


# The module globals app_depends.lifespan assigns and never clears (tj-7zjov1.130). Module scope, so no name mangling:
# the attribute names are literally these.
LIFESPAN_GLOBALS = ('__INGEST_CHANNEL', '__INGEST_FETCH_CLIENT', '__INGEST_SUBMIT_CLIENT')


@pytest.fixture(autouse=True)
def restore_lifespan_globals() -> Iterator[None]:
    """Put back the gRPC channel, fetch client and submit client globals a lifespan test may have left set.

    app_depends.lifespan assigns both and does not reset them on exit (in production one lifespan runs per process,
    so nothing needs to). A test that enters the lifespan without patching them leaves its fake client in the module
    for the rest of the session: test_reaper's lifespan test did, and in random order the two tests that assert no
    lifespan has run (test_http_smoke test_the_routes_are_driven_without_a_lifespan and test_dataset_fetch_transaction
    test_get_ingest_fetch_client_hands_out_the_client_the_lifespan_built) then failed their own guard. Restoring here
    covers every lifespan test, present and future, so none has to remember to patch them. The import is inside the
    fixture so that collecting a test module never imports the app.
    """
    from data.store.app import app_depends

    saved = {name: getattr(app_depends, name) for name in LIFESPAN_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(app_depends, name, value)


@pytest.fixture(autouse=True)
def inert_panel_manager_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No lifespan test starts the REAL panel manager loop (tj-7zjov1.227, finding (e) of the P22v verdict).

    Since b03edd4 app_depends.lifespan starts the panel manager (tj-7zjov1.34). Every lifespan test fakes the
    database's initialize, so the real loop's first tick, which runs at once, opened own_session on a session handle
    that was never initialised and failed RuntimeError ('periodic job failed: name=panel-manager error=RuntimeError'),
    alongside whatever the test was about, after building every trading calendar off the event loop. Autouse, like
    restore_lifespan_globals, so every lifespan test present and future is covered without remembering. Only the names
    the LIFESPAN calls are replaced (app_depends'); panel_manager's own start and stop, which test_panel_manager_loop
    drives, are untouched. The start answers None, what the real one answers when the loop is off, and the stop is a
    no-op. A test about the lifespan's wiring of the loop patches these two again with recorders
    (test_store_grpc_host.py).
    """
    from data.store.app import app_depends

    async def start(_client):
        return None

    async def stop(_task) -> None:
        return None

    monkeypatch.setattr(app_depends, 'start_panel_manager_loop', start)
    monkeypatch.setattr(app_depends, 'stop_panel_manager_loop', stop)
