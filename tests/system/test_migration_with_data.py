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


EXPECTATIONS: dict[str, dict[str, Callable[[ScratchDb, Seed], None]]] = {
    'eec8f88a7443': {
        '8f41c2d7a3b9': expect_e1_bars_refuse,
        '8f41c2d7a3b9.entries-only': expect_e2_entries_only_round_trip,
    }
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
