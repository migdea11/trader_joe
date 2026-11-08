"""Scratch databases for the system tests that must migrate or build a schema of their own (tj-vhboky.62).

No test functions here. Moved out of test_migration_with_data.py when that file was deleted with the old migration chain
(ADR tj-kg521d): the scratch database outlived the seed checks, because test_usage_record.py (and through its fixture
the usage, stats, reaper and refresh tests), test_dev_seed_reset_pg.py and test_alpha_baseline_schema_pg.py still need
a database nobody else touches.

THE EXCEPTION TO THE SUITE'S RULE "no test here drives a migration" (conftest.py), granted by tj-vhboky.62 and scoped
narrowly: a test migrates ONLY a scratch database it creates itself, with a name unique to the test and the run, on the
same Postgres server, reached through the env contract conftest.py reads, and drops it in teardown. The stack's own
database is never migrated, read or written: the only statements sent to it are CREATE DATABASE and DROP DATABASE, which
act on the server, not on that database.

HOW ALEMBIC RUNS. As a subprocess, the way an operator runs it: `python -m alembic`, cwd data/store, PYTHONPATH the
image's (the repository root, then gen/proto/python), DATABASE_URI pointed at the scratch database. env.py calls
load_dotenv('.env'), whose default override=False never replaces a variable already set, so the DATABASE_URI given here
wins over any data/store/.env. The config is the repository's own data/store/alembic.ini, which test_client mounts beside
data/store/migrations (docker-compose.test-client.yaml); its relative script_location resolves against cwd data/store.
"""

import hashlib
import os
import subprocess  # nosec B404 -- alembic as a subprocess is the point (tj-vhboky.62)
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import URL, Engine

from common.tests.image_path import image_pythonpath
from common.tests.roots import SERVER_ROOT


STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
ALEMBIC_INI_PATH = STORE_DIR / 'alembic.ini'
ALEMBIC_TIMEOUT_SECONDS = 300


@dataclass
class AlembicRun:
    args: tuple[str, ...]
    returncode: int
    output: str


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class ScratchDb:
    """One scratch database: created by a fixture, migrated only by alembic subprocesses, dropped after."""

    def __init__(self, name: str, url: URL, ini_path: Path, password: str) -> None:
        self.name = name
        self.url = url
        self._ini_path = ini_path
        self._password = password
        self.engine: Engine = sa.create_engine(
            url, connect_args={'options': '-c TimeZone=UTC -c extra_float_digits=1'}, poolclass=sa.pool.NullPool
        )

    def _redact(self, text: str) -> str:
        return text.replace(self._password, '<redacted>') if self._password else text

    def alembic(self, *args: str) -> AlembicRun:
        """Run `python -m alembic -c <ini> <args>` against this database; return its exit and output."""
        env = dict(os.environ)
        env['DATABASE_URI'] = self.url.render_as_string(hide_password=False)
        # The image's path model (decision tj-3mk3u5.42 F1): the root, then the generated gRPC code's.
        env['PYTHONPATH'] = image_pythonpath(SERVER_ROOT)
        argv = [sys.executable, '-m', 'alembic', '-c', str(self._ini_path), *args]
        done = subprocess.run(  # nosec B603 -- fixed argv, no shell
            argv, cwd=STORE_DIR, env=env, capture_output=True, text=True, timeout=ALEMBIC_TIMEOUT_SECONDS, check=False
        )
        return AlembicRun(args, done.returncode, self._redact(done.stdout + done.stderr))

    def migrate(self, command: str, revision: str) -> AlembicRun:
        return self.alembic(command, revision)

    def must(self, command: str, revision: str) -> None:
        """Run an alembic command that has to succeed and land exactly on `revision`."""
        run = self.migrate(command, revision)
        assert run.returncode == 0, f'alembic {command} {revision} on {self.name} failed:\n{run.output[-6000:]}'
        assert self.revision() == revision, f'alembic {command} {revision} left {self.revision()}'

    def revision(self) -> str | None:
        with self.engine.connect() as conn:
            exists = conn.execute(sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
            if not exists:
                return None
            rows = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalars().all()
        assert len(rows) <= 1, f'alembic_version holds {len(rows)} rows'
        return rows[0] if rows else None

    def columns(self, table: str) -> list[str]:
        with self.engine.connect() as conn:
            return sorted(
                conn.execute(
                    sa.text(
                        'SELECT column_name FROM information_schema.columns '
                        "WHERE table_schema = 'public' AND table_name = :table"
                    ),
                    {'table': table},
                ).scalars()
            )

    def schema(self) -> dict[str, Any]:
        """The schema facts a part-applied migration would change: columns, constraints, indexes, enum types."""
        with self.engine.connect() as conn:
            columns = conn.execute(
                sa.text(
                    'SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default '
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    'ORDER BY table_name, column_name'
                )
            ).all()
            constraints = conn.execute(
                sa.text(
                    'SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint '
                    "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
                )
            ).all()
            indexes = conn.execute(
                sa.text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2"
                )
            ).all()
            enums = conn.execute(
                sa.text(
                    'SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid '
                    "WHERE t.typnamespace = 'public'::regnamespace ORDER BY 1, e.enumsortorder"
                )
            ).all()
        return {
            'columns': [tuple(row) for row in columns],
            'constraints': [tuple(row) for row in constraints],
            'indexes': [tuple(row) for row in indexes],
            'enums': [tuple(row) for row in enums],
        }

    def values(self, table: str, column: str) -> dict[Any, Any]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(f'SELECT id, {_quote(column)} FROM public.{table} ORDER BY id')).all()  # nosec B608 -- module-constant table, quoted schema name
        return {row[0]: row[1] for row in rows}

    def enum_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = :name AND typtype = 'e')"), {'name': name}
            ).scalar_one()


def scratch_name(prefix: str, run_id: str, nodeid: str, extra: str = '') -> str:
    """A database name unique to the test (its node id, plus `extra` for a second database) and the run."""
    digest = hashlib.blake2s(f'{nodeid}{extra}'.encode(), digest_size=4).hexdigest()
    return f'{prefix}_{run_id}_{digest}'


@contextmanager
def scratch_database(pg_settings: Any, pg_engine: Engine, name: str) -> Iterator[ScratchDb]:
    """Create the database `name` on the stack's server, yield it, and drop it WITH (FORCE) whatever happened.

    The alembic.ini absent (the test_client mount missing) FAILS here, never skips.
    """
    assert ALEMBIC_INI_PATH.is_file(), f'{ALEMBIC_INI_PATH} is missing: is it mounted into test_client?'
    admin = pg_engine.execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE {_quote(name)}'))
    db = ScratchDb(name, pg_settings.url().set(database=name), ALEMBIC_INI_PATH, pg_settings.password)
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)'))
