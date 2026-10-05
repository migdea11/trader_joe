"""The migration check with data: seed, upgrade, assert, downgrade, assert -- on scratch databases (tj-vhboky.62, Sys-8).

Design: decision tj-vhboky.55 (THE CHECK, the GENERIC RULE, EXPECTATIONS E1-E4, SEED FORMAT, and
addenda S1-S11); seed storage ruled (a) on tj-vhboky.56 -- every seed is read from
tests/system/seeds/ and from nowhere else; migration eec8f88a7443 and migrations/env.py.

EXCEPTION TO THE SUITE'S RULE "no test here drives a migration or a downgrade" (conftest.py), granted
by tj-vhboky.62 and scoped narrowly: every test here migrates ONLY a scratch database it creates
itself -- a name unique to the test and the run -- on the same Postgres server, reached through the
env contract conftest.py reads, and drops it in teardown. The stack's own database is never
migrated, read or written: the only statements sent to it are CREATE DATABASE and DROP DATABASE,
which act on the server, not on that database.

HOW ALEMBIC RUNS. As a subprocess, the way an operator runs it: `python -m alembic`, cwd
data/store, PYTHONPATH the image's (the repository root, then gen/proto/python), DATABASE_URI pointed at the scratch database. env.py
calls load_dotenv('.env'); python-dotenv's load_dotenv defaults to override=False, so it never
replaces a variable that is already set -- the DATABASE_URI given here wins over any data/store/.env
(checked: dotenv.main.load_dotenv(..., override: bool = False, ...), python-dotenv >= 1.2.3 as
pinned in pyproject.toml; test_the_scratch_database_is_the_one_migrated pins it against Postgres).
The config is the repository's own data/store/alembic.ini, which test_client mounts beside
data/store/migrations (docker-compose.test-client.yaml, tj-7294qb); its relative script_location
resolves against cwd data/store to the mounted migrations. The ini, env.py, the revisions and the
alembic CLI are all the repository's own -- no reduced copy.

SEEDS. tests/system/seeds/ holds pairs <name>.sql + <name>.json in data/store/seeds/dump.py's
rendering and manifest.py's manifest. <name> is <revision> for a revision's canonical seed (the one
the seed guard tj-irhy0a.4 requires and make seed-dump produces) or <revision>.<variant> for an
additional, hand-written bootstrap seed; the revision is the text before the first dot and must equal
the manifest's revision field. The five bootstrap seeds are synthetic and hand-written, because no
run ever produced a seed at 8f41c2d7a3b9:
  * 8f41c2d7a3b9                     entries plus bars (E1);
  * 8f41c2d7a3b9.entries-only        entries only, with NULL expiry_type and update_type (E2);
  * eec8f88a7443.collision-free      entries plus bars, no old-key collision (E3);
  * eec8f88a7443.collision-bars      the old four-column BAR key collides, nothing else (E4, bars);
  * eec8f88a7443.collision-entries   the old six-column ENTRY key collides, nothing else (E4, entries).

LOADING A SEED. The scratch database is upgraded to exactly the seed's revision (the schema always
comes from the migration chain; a seed is data only), then the .sql is executed line by line through
the psycopg2 driver in one transaction. A line starting with a backslash is REFUSED before anything
runs: a seed that loads only through psql is not a seed. An EMPTY seed -- no INSERT line, or a
manifest whose row counts are all zero -- FAILS the test that loads it (the user's ruling on
tj-vhboky.47), never passes.

THE DIGESTS ARE RECOMPUTED, NOT TRUSTED. data_digests() below is an independent implementation of
manifest.py's documented algorithm: Postgres renders each item (`'n'`, or `'v' || column::text`) in a
session at TimeZone UTC and extra_float_digits 1, rows ordered by id; Python joins the items with a
newline and hashes them with SHA-256. It does not call column_digest_sql, so the producer's query is
checked against it, not with it (decision tj-vhboky.55 S7/S9 (5)).

THE GENERIC RULE (test_generic_rule) is parametrised over every seed present whose revision has a
next revision in the graph read from data/store/migrations (never a literal list):
  * next revision WITHOUT the additive-exception marker: upgrade, every seed row survives on the
    seed revision's columns; downgrade, the rows equal the manifest again. Lossless both ways.
  * next revision WITH the marker: its expectations are looked up in EXPECTATIONS by revision id and
    by seed name. A marked revision with no entry, or a seed the entry does not cover, FAILS.
E1 and E2 are eec8f88a7443's expectations for its parent's seeds; E3 and the two E4 tests are the
downgrade checks on seeds AT eec8f88a7443. test_seed_loads_equal_to_its_manifest is the equal-loading
proof (S9): every committed seed, loaded at its own revision, recomputes to its manifest.

ATOMICITY IS ASSERTED, NOT ASSUMED: every refusal asserts a non-zero exit, the revision unchanged,
the data identical to what it was, and the SCHEMA identical too (columns, constraints, indexes, enum
types) -- eec8f88a7443 changes the entry table before the bar step fails, so a run that committed per
statement would leave an owner column behind.

Run by make test-system and the agent-stack MCP's run_system_tests; pytest.ini keeps tests/system out
of the PR gate (norecursedirs). FAIL, NEVER SKIP.
"""

import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- alembic as a subprocess is the point of the check (tj-vhboky.62)
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, Engine

from common.tests.image_path import image_pythonpath
from data.store.seeds.scenario import is_synthetic


pytestmark = pytest.mark.data_store

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = REPO_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
# The repository's own ini, mounted into test_client (tj-7294qb); see the module docstring.
ALEMBIC_INI_PATH = STORE_DIR / 'alembic.ini'
SEEDS_DIR = Path(__file__).resolve().parent / 'seeds'

ENTRY_TABLE = 'store_dataset_entry'
BAR_TABLE = 'stock_market_activity'
SEED_TABLES = (ENTRY_TABLE, BAR_TABLE)
MANIFEST_KEYS = {'date', 'digests', 'producer', 'revision', 'row_counts'}

ADDITIVE_EXCEPTION_MARKER = re.compile(r'^# additive-exception: \S+', re.MULTILINE)
ALEMBIC_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------------------------
# The revision graph, read from data/store/migrations.


@dataclass(frozen=True)
class Revision:
    revision: str
    down_revision: str | None
    next_revision: str | None
    marked: bool


def _read_graph() -> dict[str, Revision]:
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    graph: dict[str, Revision] = {}
    for rev in script.walk_revisions():
        children = sorted(rev.nextrev)
        assert len(children) <= 1, f'revision {rev.revision} has {len(children)} children; the check expects a line'
        down = rev.down_revision
        assert down is None or isinstance(down, str), f'revision {rev.revision} is a merge point'
        graph[rev.revision] = Revision(
            revision=rev.revision,
            down_revision=down,
            next_revision=children[0] if children else None,
            marked=bool(ADDITIVE_EXCEPTION_MARKER.search(Path(rev.path).read_text(encoding='utf-8'))),
        )
    return graph


GRAPH = _read_graph()


# ---------------------------------------------------------------------------------------------
# Seeds, read from tests/system/seeds.


@dataclass(frozen=True)
class Seed:
    name: str
    revision: str
    sql_path: Path
    manifest: dict[str, Any]

    @property
    def row_counts(self) -> dict[str, int]:
        return self.manifest['row_counts']

    @property
    def digests(self) -> dict[str, dict[str, str]]:
        return self.manifest['digests']


def _seed_names() -> list[str]:
    stems = {path.name.removesuffix(path.suffix) for path in SEEDS_DIR.glob('*.sql')}
    stems |= {path.name.removesuffix(path.suffix) for path in SEEDS_DIR.glob('*.json')}
    return sorted(stems)


SEED_NAMES = _seed_names()


def read_seed(name: str) -> Seed:
    """Read one seed pair, failing on anything that would make the check prove less than it claims."""
    sql_path, json_path = SEEDS_DIR / f'{name}.sql', SEEDS_DIR / f'{name}.json'
    if not sql_path.is_file() or not json_path.is_file():
        pytest.fail(f'seed {name}: needs both {sql_path.name} and {json_path.name} in {SEEDS_DIR}', pytrace=False)
    manifest = json.loads(json_path.read_text(encoding='utf-8'))
    if set(manifest) != MANIFEST_KEYS:
        pytest.fail(f'seed {name}: manifest keys {sorted(manifest)}, expected {sorted(MANIFEST_KEYS)}', pytrace=False)
    revision = name.split('.', 1)[0]
    if manifest['revision'] != revision:
        pytest.fail(f'seed {name}: manifest revision {manifest["revision"]} differs from its name', pytrace=False)
    if revision not in GRAPH:
        pytest.fail(f'seed {name}: revision {revision} is not in {MIGRATIONS_DIR}', pytrace=False)
    if set(manifest['row_counts']) != set(SEED_TABLES) or set(manifest['digests']) != set(SEED_TABLES):
        pytest.fail(f'seed {name}: manifest must cover exactly {SEED_TABLES}', pytrace=False)
    if sum(manifest['row_counts'].values()) == 0:
        pytest.fail(f'seed {name}: EMPTY (every row count is zero); an empty seed is a failure', pytrace=False)
    return Seed(name=name, revision=revision, sql_path=sql_path, manifest=manifest)


def seed_statements(seed: Seed) -> list[str]:
    """The seed's statements, refusing a backslash line and an empty seed before anything runs."""
    text = seed.sql_path.read_text(encoding='utf-8')
    lines = [line for line in text.split('\n') if line.strip()]
    backslash = sum(1 for line in lines if line.lstrip().startswith('\\'))
    if backslash:
        pytest.fail(f'seed {seed.name}: {backslash} line(s) start with a backslash; refused', pytrace=False)
    if not any(line.startswith('INSERT INTO ') for line in lines):
        pytest.fail(f'seed {seed.name}: EMPTY (no INSERT line); an empty seed is a failure', pytrace=False)
    return lines


# ---------------------------------------------------------------------------------------------
# Scratch databases.


@dataclass
class AlembicRun:
    args: tuple[str, ...]
    returncode: int
    output: str


class ScratchDb:
    """One scratch database: created by the fixture, migrated only by alembic subprocesses, dropped after."""

    def __init__(self, name: str, url: URL, ini_path: Path, password: str) -> None:
        self.name = name
        self.url = url
        self._ini_path = ini_path
        self._password = password
        # The digests' session settings (manifest.py SESSION_OPTIONS), on every connection.
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
        env['PYTHONPATH'] = image_pythonpath(REPO_ROOT)
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

    def load(self, seed: Seed) -> None:
        """Upgrade to exactly the seed's revision, then execute its statements through the driver."""
        statements = seed_statements(seed)
        self.must('upgrade', seed.revision)
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            for number, statement in enumerate(statements, start=1):
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    raw.rollback()
                    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                    pytest.fail(f'seed {seed.name}: line {number} did not load: {first}', pytrace=False)
            raw.commit()
        finally:
            raw.close()

    def row_counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {
                table: conn.execute(sa.text(f'SELECT count(*) FROM public.{table}')).scalar_one()  # nosec B608 -- module-constant table
                for table in SEED_TABLES
            }

    def data_digests(self, table: str, columns: list[str]) -> dict[str, str]:
        """Per column: SHA-256 of the items ('n', or 'v' plus the value's text) joined by newlines, rows by id."""
        live = set(self.columns(table))
        missing = sorted(set(columns) - live)
        assert not missing, f'{table} lost column(s) {missing}'
        quoted = [_quote(column) for column in sorted(columns)]
        items = ', '.join(f"CASE WHEN {q} IS NULL THEN 'n' ELSE 'v' || {q}::text END" for q in quoted)
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(f'SELECT {items} FROM public.{table} ORDER BY id')).all()  # nosec B608 -- module-constant table, quoted schema names
        return {
            column: hashlib.sha256('\n'.join(row[index] for row in rows).encode('utf-8')).hexdigest()
            for index, column in enumerate(sorted(columns))
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything a data assertion compares: row counts and every column's digest, per table."""
        return {
            'row_counts': self.row_counts(),
            'digests': {table: self.data_digests(table, self.columns(table)) for table in SEED_TABLES},
        }

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


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.fixture(scope='session')
def alembic_ini() -> Path:
    """The repository's data/store/alembic.ini; absent (mount missing) FAILS here, never skips."""
    assert ALEMBIC_INI_PATH.is_file(), f'{ALEMBIC_INI_PATH} is missing: is it mounted into test_client?'
    return ALEMBIC_INI_PATH


@pytest.fixture
def scratch(
    request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity, alembic_ini: Path
) -> Iterator[ScratchDb]:
    """A scratch database for this test alone: created here, dropped (WITH FORCE) in teardown."""
    digest = hashlib.blake2s(request.node.nodeid.encode(), digest_size=4).hexdigest()
    name = f'sys8_{run_identity.run_id}_{digest}'
    admin = pg_engine.execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE {_quote(name)}'))
    db = ScratchDb(name, pg_settings.url().set(database=name), alembic_ini, pg_settings.password)
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)'))


# ---------------------------------------------------------------------------------------------
# Shared assertions.


def assert_equals_manifest(db: ScratchDb, seed: Seed, *, skip: dict[str, set[str]] | None = None) -> None:
    """Row counts equal the manifest, and every manifest column not in `skip` digests to its manifest value."""
    skip = skip or {}
    assert db.row_counts() == seed.row_counts, f'seed {seed.name}: row counts {db.row_counts()} != {seed.row_counts}'
    for table in SEED_TABLES:
        wanted = {c: d for c, d in seed.digests[table].items() if c not in skip.get(table, set())}
        got = db.data_digests(table, list(wanted))
        differ = sorted(column for column in wanted if got[column] != wanted[column])
        assert not differ, f'seed {seed.name}: {table} column(s) {differ} differ from the manifest'


def assert_refused(
    run: AlembicRun, db: ScratchDb, revision: str, before: dict[str, Any], schema: dict[str, Any]
) -> None:
    """A refusal: non-zero exit, revision unchanged, data identical, schema identical."""
    assert run.returncode != 0, (
        f'alembic {" ".join(run.args)} was expected to REFUSE but exited 0:\n{run.output[-4000:]}'
    )
    assert db.revision() == revision, f'after the refusal the revision is {db.revision()}, not {revision}'
    assert db.snapshot() == before, 'after the refusal the data differs from before it'
    after = db.schema()
    differ = sorted(key for key in schema if after[key] != schema[key])
    assert not differ, f'after the refusal the schema differs ({differ}): the invocation was not atomic'


def _child(revision: str) -> str:
    child = GRAPH[revision].next_revision
    assert child is not None, f'{revision} has no next revision'
    return child


# ---------------------------------------------------------------------------------------------
# EXPECTATIONS for revisions carrying the additive-exception marker, by revision id, then by the
# name of a seed at that revision's PARENT. Each runs on a scratch database already holding the seed.


def expect_e1_bars_refuse(db: ScratchDb, seed: Seed) -> None:
    """E1: a bars-bearing seed at 8f41c2d7a3b9 -- upgrade REFUSES; revision, rows and schema unchanged."""
    assert seed.row_counts[BAR_TABLE] > 0, f'E1 needs bars; seed {seed.name} has none'
    target = _child(seed.revision)
    before, schema = db.snapshot(), db.schema()
    run = db.migrate('upgrade', target)
    assert_refused(run, db, seed.revision, before, schema)
    assert_equals_manifest(db, seed)
    assert re.search(r'column "feed" of relation "stock_market_activity" contains null values', run.output), (
        f'E1 refused, but not on the feed column:\n{run.output[-4000:]}'
    )


def expect_e2_entries_only_round_trip(db: ScratchDb, seed: Seed) -> None:
    """E2: entries-only seed at 8f41c2d7a3b9 with NULL expiry_type/update_type -- upgrade and downgrade succeed.

    Upgrade: ids and surviving columns preserved, owner 'unassigned', expiry NULL, the NULLs are 1.
    Downgrade: the rows equal the manifest except expiry_type/update_type, which STAY 1 -- the
    backfill is one-way, and this asserts it rather than papering over it.
    """
    assert seed.row_counts[BAR_TABLE] == 0, f'E2 needs an entries-only seed; {seed.name} has bars'
    backfilled = ('expiry_type', 'update_type')
    original = {column: db.values(ENTRY_TABLE, column) for column in backfilled}
    for column in backfilled:
        assert None in original[column].values(), f'E2 needs a NULL {column}; seed {seed.name} has none'
    expected = {
        column: {key: (1 if value is None else value) for key, value in original[column].items()}
        for column in backfilled
    }
    target = _child(seed.revision)

    db.must('upgrade', target)
    # The bar table is empty; its three columns this revision drops have nothing left to compare.
    assert_equals_manifest(
        db, seed, skip={ENTRY_TABLE: set(backfilled), BAR_TABLE: {'split_factor', 'dividends_factor', 'expiry'}}
    )
    owners = db.values(ENTRY_TABLE, 'owner')
    assert set(owners) == set(original['expiry_type']), 'entry ids changed across the upgrade'
    assert set(owners.values()) == {'unassigned'}, f'owner after the upgrade: {sorted(set(owners.values()))}'
    assert set(db.values(ENTRY_TABLE, 'expiry').values()) == {None}, 'expiry is not NULL after the upgrade'
    for column in backfilled:
        assert db.values(ENTRY_TABLE, column) == expected[column], f'{column} after the upgrade is not the backfill'

    db.must('downgrade', seed.revision)
    assert sorted(db.columns(ENTRY_TABLE)) == sorted(seed.digests[ENTRY_TABLE]), (
        'entry columns differ after the downgrade'
    )
    assert_equals_manifest(db, seed, skip={ENTRY_TABLE: set(backfilled)})
    for column in backfilled:
        assert db.values(ENTRY_TABLE, column) == expected[column], f'{column} after the downgrade is not still 1'
        assert db.data_digests(ENTRY_TABLE, [column])[column] != seed.digests[ENTRY_TABLE][column], (
            f'{column} equals the manifest after the round trip; the backfill was expected to be one-way'
        )


def expect_e5_feed_wipes_both_tables(db: ScratchDb, seed: Seed) -> None:
    """E5: c4a1f7b2e905 DESTROYS DATA BY DESIGN -- upgrade succeeds and empties both tables.

    THE ONE EXPECTATION FOR ALL FOUR SEEDS AT eec8f88a7443, which is itself the claim worth
    making. E1-E4 differ per seed because eec8f88a7443's outcome DEPENDS on the data it meets --
    bars present or absent, a collision on one old key or another. This revision does not look at
    the data at all: it DELETEs every bar and every entry, adds feed NOT NULL, and rebuilds the
    identity constraint around eleven columns. A collision-bearing seed and a collision-free seed
    are therefore indistinguishable to it, and binding the same function to all four says so --
    if any seed ever produced a different outcome here, that would be the defect.

    WHY A WIPE RATHER THAN A BACKFILL (user ruling, tj-3mk3u5.22 Q6, carried onto tj-3mk3u5.31):
    there was no dataset data worth preserving at this stage. The recommended alternative --
    derive each entry's feed from its own bars, give a bar-less entry iex, assert no entry holds
    two tapes -- was considered and SUPERSEDED. So "lossless" is the wrong generic rule for this
    revision, which is exactly why it carries the additive-exception marker and lands here.

    THE DELETES ARE WHAT MAKE THE COLUMN LEGAL, and that is the half only a real Postgres can
    show. ADD COLUMN feed ... NOT NULL with no server default is refused outright by a table that
    holds rows, so on a seeded database this revision either empties both tables first or fails at
    that statement. The unit tier (data/store/tests/test_head_revision_shape.py) asserts the op
    ORDER against a recording stand-in; this asserts the database accepted the result.

    DOWNGRADE IS ASSERTED TOO, AND IT SUCCEEDS ONLY BECAUSE THE TABLES ARE EMPTY. The revision's
    own docstring warns that recreating the ten-column constraint FAILS when two entries differ
    only by feed, which is routine once the application has run. After the upgrade's wipe there
    are no rows to collide, so the schema round-trips cleanly -- and the rows do NOT come back,
    which is the substance of the one-way door and is asserted rather than described.
    """
    assert seed.revision == 'eec8f88a7443', f'E5 is written for seeds at eec8f88a7443; got {seed.name}'
    target = _child(seed.revision)
    before = db.row_counts()
    entry_columns_before = set(db.columns(ENTRY_TABLE))

    db.must('upgrade', target)

    # The wipe, stated as both halves: the seed HAD rows (or the emptiness proves nothing), and
    # every one of them is gone.
    assert sum(before.values()) > 0, f'E5 needs a seed with rows; {seed.name} loaded empty'
    assert db.row_counts() == {ENTRY_TABLE: 0, BAR_TABLE: 0}, (
        f'the feed revision left rows behind: {db.row_counts()} (seed {seed.name} had {before})'
    )

    entry_columns_after = set(db.columns(ENTRY_TABLE))
    assert entry_columns_after - entry_columns_before == {'feed'}, (
        f'the entry gained {sorted(entry_columns_after - entry_columns_before)} rather than exactly feed'
    )
    # Neither created nor dropped by this revision -- eec8f88a7443 owns it for the bar's column
    # and the entry's new column reuses it, so it must simply still be here.
    assert db.enum_exists('feed'), 'the feed enum type is missing after the upgrade'

    db.must('downgrade', seed.revision)

    assert set(db.columns(ENTRY_TABLE)) == entry_columns_before, (
        'the downgrade did not restore the entry columns the seed revision had'
    )
    assert db.row_counts() == {ENTRY_TABLE: 0, BAR_TABLE: 0}, (
        'the downgrade restored rows, which it cannot do -- the upgrade deleted them and nothing stages them'
    )


EXPECTATIONS: dict[str, dict[str, Callable[[ScratchDb, Seed], None]]] = {
    'eec8f88a7443': {
        '8f41c2d7a3b9': expect_e1_bars_refuse,
        '8f41c2d7a3b9.entries-only': expect_e2_entries_only_round_trip,
    },
    # E5. Every seed AT eec8f88a7443 maps to one function on purpose; see its docstring.
    'c4a1f7b2e905': {
        'eec8f88a7443': expect_e5_feed_wipes_both_tables,
        'eec8f88a7443.collision-free': expect_e5_feed_wipes_both_tables,
        'eec8f88a7443.collision-bars': expect_e5_feed_wipes_both_tables,
        'eec8f88a7443.collision-entries': expect_e5_feed_wipes_both_tables,
    },
}


def check_lossless(db: ScratchDb, seed: Seed, target: str) -> None:
    """The generic rule for an unmarked revision: lossless upgrade, lossless downgrade."""
    db.must('upgrade', target)
    assert_equals_manifest(db, seed)
    db.must('downgrade', seed.revision)
    assert_equals_manifest(db, seed)
    for table in SEED_TABLES:
        assert db.columns(table) == sorted(seed.digests[table]), f'{table} columns differ after the round trip'


# ---------------------------------------------------------------------------------------------
# The tests.

TRANSITIONS = [
    pytest.param(name, id=f'{name}->{GRAPH[name.split(".", 1)[0]].next_revision}')
    for name in SEED_NAMES
    if name.split('.', 1)[0] in GRAPH and GRAPH[name.split('.', 1)[0]].next_revision is not None
]


def test_seeds_are_present() -> None:
    """The seed directory holds seeds, and every name's revision is in the graph -- never an empty parametrisation."""
    assert SEED_NAMES, f'no seeds in {SEEDS_DIR}'
    unknown = sorted(name for name in SEED_NAMES if name.split('.', 1)[0] not in GRAPH)
    assert not unknown, f'seed(s) for revisions not in {MIGRATIONS_DIR}: {unknown}'
    assert TRANSITIONS, 'no seed has a next revision, so the generic rule checks nothing'


def test_every_expectation_is_for_a_marked_revision_and_a_present_seed() -> None:
    for revision, by_seed in EXPECTATIONS.items():
        assert revision in GRAPH, f'EXPECTATIONS names {revision}, which is not in the graph'
        assert GRAPH[revision].marked, f'EXPECTATIONS names {revision}, which carries no additive-exception marker'
        for name in by_seed:
            assert name in SEED_NAMES, f'EXPECTATIONS[{revision}] names seed {name}, which is not in {SEEDS_DIR}'
            assert GRAPH[name.split('.', 1)[0]].next_revision == revision, (
                f'seed {name} is not at the parent of {revision}'
            )


@pytest.mark.parametrize('name', SEED_NAMES)
def test_seed_loads_equal_to_its_manifest(scratch: ScratchDb, name: str) -> None:
    """S9's equal-loading proof: at its own revision, every committed seed recomputes to its manifest.

    Also: the manifest names exactly the table's live columns (a missing column would go unchecked by
    every other test), and every row is synthetic (the repo is public).
    """
    seed = read_seed(name)
    scratch.load(seed)
    assert scratch.revision() == seed.revision
    for table in SEED_TABLES:
        assert sorted(seed.digests[table]) == scratch.columns(table), (
            f"seed {name}: the manifest's {table} columns are not the live columns at {seed.revision}"
        )
    assert_equals_manifest(scratch, seed)
    for table in SEED_TABLES:
        has_owner = 'owner' in scratch.columns(table)
        owner = 'owner' if has_owner else 'NULL'
        with scratch.engine.connect() as conn:
            rows = conn.execute(sa.text(f'SELECT DISTINCT asset_symbol, {owner} FROM public.{table}')).all()  # nosec B608 -- module-constant table
        foreign = [row for row in rows if not is_synthetic(row[0], row[1] if has_owner else None)]
        assert not foreign, f'seed {name}: {len(foreign)} non-synthetic (symbol, owner) pair(s) in {table}'


@pytest.mark.parametrize('name', TRANSITIONS)
def test_generic_rule(scratch: ScratchDb, name: str) -> None:
    """Seed N, then its next revision: lossless both ways, or the marked revision's expectations by id."""
    seed = read_seed(name)
    target = _child(seed.revision)
    scratch.load(seed)
    assert_equals_manifest(scratch, seed)
    if not GRAPH[target].marked:
        check_lossless(scratch, seed, target)
        return
    by_seed = EXPECTATIONS.get(target)
    if by_seed is None:
        pytest.fail(
            f'revision {target} carries the additive-exception marker and has no EXPECTATIONS entry; '
            'write its expectations (decision tj-vhboky.55, GENERIC RULE)',
            pytrace=False,
        )
    expectation = by_seed.get(name)
    if expectation is None:
        pytest.fail(f'EXPECTATIONS[{target}] has no expectation for seed {name}', pytrace=False)
    expectation(scratch, seed)


HEAD_SEED_COLLISION_FREE = 'eec8f88a7443.collision-free'
HEAD_SEED_BAR_COLLISION = 'eec8f88a7443.collision-bars'
HEAD_SEED_ENTRY_COLLISION = 'eec8f88a7443.collision-entries'


def test_e3_collision_free_downgrade_succeeds_then_upgrade_refuses(scratch: ScratchDb) -> None:
    """E3: on a collision-free seed at eec8f88a7443, downgrade SUCCEEDS, then upgrade REFUSES.

    Downgrade: surviving columns equal, restored columns NULL, feed and its enum type gone, owner and
    expiry gone. Then the re-upgrade REFUSES (bars present), atomically.
    """
    seed = read_seed(HEAD_SEED_COLLISION_FREE)
    assert seed.row_counts[BAR_TABLE] > 0 and seed.row_counts[ENTRY_TABLE] > 0, 'E3 needs entries and bars'
    parent = GRAPH[seed.revision].down_revision
    assert parent is not None
    scratch.load(seed)
    entry_before, bar_before = set(scratch.columns(ENTRY_TABLE)), set(scratch.columns(BAR_TABLE))

    scratch.must('downgrade', parent)
    entry_after, bar_after = set(scratch.columns(ENTRY_TABLE)), set(scratch.columns(BAR_TABLE))
    assert entry_before - entry_after == {'owner', 'expiry'}, f'entry lost {sorted(entry_before - entry_after)}'
    assert entry_after - entry_before == set(), f'entry gained {sorted(entry_after - entry_before)}'
    assert bar_before - bar_after == {'feed'}, f'bar lost {sorted(bar_before - bar_after)}'
    restored = bar_after - bar_before
    assert restored == {'split_factor', 'dividends_factor', 'expiry'}, f'bar gained {sorted(restored)}'
    assert not scratch.enum_exists('feed'), 'the feed enum type survived the downgrade'
    assert_equals_manifest(scratch, seed, skip={ENTRY_TABLE: {'owner', 'expiry'}, BAR_TABLE: {'feed'}})
    for column in sorted(restored):
        assert set(scratch.values(BAR_TABLE, column).values()) == {None}, f'restored bar column {column} is not NULL'

    before, schema = scratch.snapshot(), scratch.schema()
    run = scratch.migrate('upgrade', seed.revision)
    assert_refused(run, scratch, parent, before, schema)
    assert 'column "feed" of relation "stock_market_activity" contains null values' in run.output, (
        f'the re-upgrade refused, but not on the feed column:\n{run.output[-4000:]}'
    )


@pytest.mark.parametrize(
    ('name', 'constraint_pattern'),
    [
        pytest.param(
            HEAD_SEED_BAR_COLLISION,
            r'could not create unique index "uq_stock_market_activity_natural_key"',
            id='old-bar-key',
        ),
        pytest.param(
            HEAD_SEED_ENTRY_COLLISION,
            r'could not create unique index "store_dataset_entry_asset_symbol_[a-z_]*key"',
            id='old-entry-key',
        ),
    ],
)
def test_e4_collision_downgrade_refuses(scratch: ScratchDb, name: str, constraint_pattern: str) -> None:
    """E4: a collision on one old key -- downgrade REFUSES; revision still head; every column identical.

    One case per old constraint, each seed colliding on that one only, and the refusal is attributed
    to it by the constraint Postgres names.
    """
    seed = read_seed(name)
    parent = GRAPH[seed.revision].down_revision
    assert parent is not None
    scratch.load(seed)
    before, schema = scratch.snapshot(), scratch.schema()
    run = scratch.migrate('downgrade', parent)
    assert_refused(run, scratch, seed.revision, before, schema)
    assert_equals_manifest(scratch, seed)
    assert re.search(constraint_pattern, run.output), (
        f'the downgrade refused, but not on that constraint:\n{run.output[-4000:]}'
    )


def test_the_scratch_database_is_the_one_migrated(scratch: ScratchDb) -> None:
    """DATABASE_URI given to the subprocess wins over env.py's load_dotenv: the scratch database moves."""
    assert scratch.revision() is None
    head = next(rev.revision for rev in GRAPH.values() if rev.next_revision is None)
    scratch.must('upgrade', head)
    run = scratch.alembic('current')
    assert run.returncode == 0, run.output
    assert head in run.output, f'alembic current did not report {head}:\n{run.output}'


def test_alembic_check_is_clean_at_head(scratch: ScratchDb) -> None:
    """MIG-1's measurement: at head, the models and the migrated schema have NO drift between them.

    IT CARRIED A STRICT xfail UNTIL tj-o3af47, AND THAT IS THE EVIDENCE IT WORKS. MIG-1
    (tj-3mk3u5.38) wired the out-of-model exclusion so this could be clean; its builder could not
    run it, because the only sanctioned route was a throwaway under tests/system and a validator
    was live in that tree. Run here for the first time, it reported one real drift -- a redundant
    unique=True on the entry's primary key, a model defect rather than a migration one, invisible
    to every other test in the repo. tj-o3af47 dropped that unique=True and removed the marker in
    the same commit, which is why this now asserts rather than expects a failure.

    WHAT THIS CATCHES THAT NOTHING ELSE DOES. `alembic current` reports the version table and
    nothing about the shape of the database -- tj-5h30md is the case in point: head was
    eec8f88a7443, uq_stock_market_activity_natural_key was simply missing, and `current` showed
    nothing wrong. Every model-against-revision test in the repo compares two things people
    WROTE; this compares what the migrations BUILD against what the models DECLARE, which is the
    only check that notices a revision that forgot something.

    ON A SCRATCH DATABASE, NOT THE STACK'S. The suite's standing exception (tj-vhboky.62) is
    narrow: a test here migrates only a database it creates and drops itself. Running `check`
    against the stack's own database would need no migration and so look harmless, but it would
    read a database other tests are concurrently writing, and its answer would depend on whoever
    migrated it last. A scratch database upgraded to head here answers the same question
    hermetically and is the honest form of the measurement.

    IT IS LOAD-BEARING FOR tj-3mk3u5.38's EXCLUSION. env.py's include_object hides exactly one
    out-of-model table, stock_market_activity_superseded_8f41c2d7a3b9, which 8f41c2d7a3b9 creates
    and no model declares. Without the exclusion this test fails on every correctly migrated
    database, with the exclusion it passes -- so a green result here is also the evidence that
    the exclusion is doing its job and is not, say, swallowing a real drop.

    tj-3mk3u5.39 (MIG-V) owns this measurement formally; it is taken here because the machinery
    to take it already exists in this file and a number now is worth more than one deferred.
    """
    head = next(rev.revision for rev in GRAPH.values() if rev.next_revision is None)
    scratch.must('upgrade', head)

    run = scratch.alembic('check')

    assert run.returncode == 0, (
        'alembic check reports drift between the models and a database migrated to head. Either a '
        'revision does not build what the models declare, or a table outside the models needs '
        f'adding to OUT_OF_MODEL_TABLES in data/store/migrations/env.py:\n{run.output[-4000:]}'
    )
    assert 'No new upgrade operations detected' in run.output, (
        f'alembic check exited 0 without reporting a clean comparison, so it may not have run:\n{run.output[-4000:]}'
    )


# ---------------------------------------------------------------------------------------------
# MIG-V (tj-3mk3u5.39): what `alembic check` is for, pinned against a real Postgres.
#
# THE TEST ABOVE IS NOT ENOUGH ON ITS OWN, and that is the whole reason this section exists. A
# check that exits zero proves nothing about whether it is LOOKING: a filter that excluded every
# object, or a comparison that silently did nothing, would pass it exactly as a correct one does.
# The three tests below produce the forbidden states and watch the check find them, so the green
# above means "no drift" rather than "no eyes".
#
# WHICH TARGET RUNS THIS. `make migrate-check`, not `make migrate-status` (MIG-2 tj-o82yyu as
# shipped, and the 2026-10-03 addendum on ADR tj-x3ig38 that superseded item 8). migrate-status
# runs `current` and `history` only and keeps the read-only promise its approval was conditional
# on. These tests drive `alembic check` directly on a scratch database rather than through make,
# because make cannot run in this container; what they pin is the BEHAVIOUR that target exposes.


BAR_NATURAL_KEY_CONSTRAINT = 'uq_stock_market_activity_natural_key'

# A table whose name only RESEMBLES the one OUT_OF_MODEL_TABLES excludes. The exclusion is an
# exact-name list on purpose, so this must show as drift (tj-3mk3u5.38; ADR tj-x3ig38 addendum
# item 1, "never a prefix or pattern, so a stray table still shows").
STRAY_TABLE = 'stock_market_activity_superseded_x'

ARCHIVE_TABLE = 'stock_market_activity_superseded_8f41c2d7a3b9'


def _head_revision() -> str:
    return next(rev.revision for rev in GRAPH.values() if rev.next_revision is None)


def _execute(db: ScratchDb, statement: str) -> None:
    """Run one DDL statement on the scratch database, outside alembic."""
    with db.engine.begin() as conn:
        conn.execute(sa.text(statement))


def test_alembic_check_reds_when_the_bar_natural_key_is_dropped(scratch: ScratchDb) -> None:
    """tj-5h30md's exact state, reproduced and caught: the permanent form of that bug's demonstration.

    THE BUG THIS AUTOMATES. The user's live database sat at head while
    uq_stock_market_activity_natural_key was missing -- dropped by hand during a host verification
    and never restored. Nothing showed it until every bar upsert returned 500, because a manual
    DROP leaves alembic_version untouched and `alembic current` reports the revision, not the
    shape. This is the one failure mode `check` was adopted for, so it is pinned by reproducing
    it rather than by trusting that it would be noticed.

    THE PRECONDITION IS LOAD-BEARING. Asserting the constraint exists before dropping it stops
    this passing for the wrong reason: against a head that never created it, the DROP would error
    or the check would red for an unrelated cause, and either way the test would look like it had
    demonstrated something it had not.
    """
    scratch.must('upgrade', _head_revision())
    clean = scratch.alembic('check')
    assert clean.returncode == 0, f'the scratch database is not clean before the drop:\n{clean.output[-2000:]}'

    _execute(scratch, f'ALTER TABLE {BAR_TABLE} DROP CONSTRAINT {BAR_NATURAL_KEY_CONSTRAINT}')

    run = scratch.alembic('check')

    assert run.returncode != 0, (
        'alembic check passed against a database missing uq_stock_market_activity_natural_key, '
        f'which is tj-5h30md exactly:\n{run.output[-4000:]}'
    )
    assert BAR_NATURAL_KEY_CONSTRAINT in run.output, (
        f'check failed but did not name the missing constraint, so it would not tell an operator '
        f'what to restore:\n{run.output[-4000:]}'
    )


def test_the_out_of_model_filter_is_an_exact_name_list_not_a_pattern(scratch: ScratchDb) -> None:
    """The archive table is ignored; a table whose name merely RESEMBLES it is not.

    BOTH HALVES IN ONE TEST, because either alone is misleading. That the archive table is ignored
    is already implied by the clean check above -- every database migrated through 8f41c2d7a3b9
    carries it, and without the exclusion that check could never pass. What is NOT implied, and is
    the thing the design actually promises, is that the exclusion is an exact-name list: a filter
    written as a prefix or a pattern would hide this stray table too, and the check would go on
    reporting clean while the database grew tables nobody declared.

    IT IS ALSO THE ANTI-VACUITY TEST FOR THE WHOLE SECTION. A check that had stopped comparing, or
    a filter that excluded everything, passes test_alembic_check_is_clean_at_head unchanged. This
    is the case that reds when the filter is too wide, which is what makes that green mean
    something.

    The name used is the one the design names as the hazard: the archive table's name with a
    different suffix. A misspelling behaves identically and for the same reason -- neither matches
    exactly, so neither is excluded.
    """
    scratch.must('upgrade', _head_revision())
    live = set(scratch.columns(ARCHIVE_TABLE))
    assert live, f'{ARCHIVE_TABLE} is absent at head, so this test is not exercising the exclusion at all'

    clean = scratch.alembic('check')
    assert clean.returncode == 0, (
        f'the archive table trips the check, so the exclusion is not working:\n{clean.output[-4000:]}'
    )

    _execute(scratch, f'CREATE TABLE {STRAY_TABLE} (id integer PRIMARY KEY)')

    run = scratch.alembic('check')

    assert run.returncode != 0, (
        f'a stray table named {STRAY_TABLE} did not trip alembic check, so OUT_OF_MODEL_TABLES is '
        f'matching by prefix or pattern rather than by exact name (tj-3mk3u5.38):\n{run.output[-4000:]}'
    )
    assert STRAY_TABLE in run.output, f'check failed but did not name the stray table:\n{run.output[-4000:]}'


def test_alembic_check_refuses_when_the_database_is_behind_head(scratch: ScratchDb) -> None:
    """Behind head is its own answer, and a different one from drift.

    An operator who has not finished migrating gets "Target database is not up to date." rather
    than a drift report, which is the useful distinction: the models legitimately describe a
    schema the database has not reached yet, so comparing them would produce a diff that means
    "you have not migrated" dressed up as "your schema is wrong".

    The parent of head is used rather than a named revision so this does not need editing on every
    new migration.
    """
    head = _head_revision()
    parent = GRAPH[head].down_revision
    assert parent is not None, f'head {head} has no parent, so there is no behind-head state to make'
    scratch.must('upgrade', parent)

    run = scratch.alembic('check')

    assert run.returncode != 0, f'alembic check passed against a database behind head:\n{run.output[-4000:]}'
    assert 'Target database is not up to date' in run.output, (
        f'check refused, but not with the behind-head message, so an operator cannot tell this '
        f'apart from real drift:\n{run.output[-4000:]}'
    )


def test_alembic_check_writes_nothing_to_an_already_migrated_database(scratch: ScratchDb) -> None:
    """Read-only here -- and the reason it is, is the reason it is not always.

    THE CONDITION IS THE POINT (ADR tj-x3ig38, 2026-10-03 addendum superseding item 8). alembic's
    guard reads `if not self.as_sql and not heads and not dont_mutate: self._ensure_version_table()`.
    command.check() passes no dont_mutate, unlike command.current(), so the only term saving it is
    NOT HEADS: against a database that already has a revision, heads exist and the version table is
    left alone. Against a never-migrated one it issues CREATE TABLE alembic_version, which is why
    `make migrate-check` is labelled as writing it and why the loose claim "check writes" would be
    just as wrong as the old claim that it never does.

    SO THIS TEST PINS THE BRANCH THE AUTOMATED CALLER ACTUALLY TAKES. Every scratch database here,
    and the stack's own, is migrated before anything runs a check, so the write branch is
    unreachable in this suite. That is not a gap being papered over -- it is the condition that
    makes the honest label on migrate-check true in practice, and it is asserted rather than
    assumed by taking the version table's contents and both row counts either side of a run.

    NOT PINNED HERE, deliberately: the mutating branch on a fresh database. Reaching it means a
    database with no alembic_version, which this fixture cannot produce without abandoning the
    scratch-is-migrated-first pattern every other test in this file depends on. It is measured in
    the alembic source and recorded on the ADR; a test that created an empty database purely to
    watch alembic write to it would pin alembic's behaviour, not ours.
    """
    scratch.must('upgrade', _head_revision())
    with scratch.engine.connect() as conn:
        before_version = sorted(conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalars())
    before_counts = scratch.row_counts()

    run = scratch.alembic('check')
    assert run.returncode == 0, f'check did not pass, so this says nothing about what it wrote:\n{run.output[-2000:]}'

    with scratch.engine.connect() as conn:
        after_version = sorted(conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalars())
    assert after_version == before_version, f'check changed alembic_version from {before_version} to {after_version}'
    assert scratch.row_counts() == before_counts, 'check changed a row count'
