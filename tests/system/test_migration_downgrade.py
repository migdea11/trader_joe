"""The downgrade leg compares the schema with the parent revision's (bead tj-7zjov1.99, P33c; store CLAUDE.md m7 item 17).

Design: bead tj-7zjov1.99; the P33 architect gate on tj-7zjov1.25; decision tj-vhboky.55 (the generic rule); the
scratch-database exception tj-vhboky.62, which test_migration_with_data.py's docstring states and this file shares:
every test here migrates ONLY scratch databases it creates itself and drops in teardown, and the stack's own database
receives nothing but CREATE DATABASE and DROP DATABASE.

WHY THE GENERIC RULE WAS NOT ENOUGH. test_generic_rule (test_migration_with_data.py) downgrades each unmarked revision
and compares the two seeded tables' ROWS with the manifest. A downgrade() whose body is `return` passes it: the rows
are untouched, and every table, column, index and enum type the upgrade created stays behind. The validator showed
that on Postgres for f2b7d4a91c36 at the P33 gate; only the offline unit tests caught it.

THE RULE HERE, for every revision R in the line and its parent P (the empty database for the base):
  1. build-parent   a scratch database upgraded from empty to P, read through the catalogue;
  2. build-revision a second one upgraded from empty to R, read; it must differ from P's (else the read is blind, or
                    R changes no schema and this comparison proves nothing for it);
  3. downgrade      that database downgraded ONE step; it must equal P's catalogue exactly;
  4. re-upgrade     upgraded to R again; it must equal R's own catalogue exactly (the round trip).
The catalogue is pg_catalog, read by server/common/tests/schema_catalog.py, never the models of the code under test;
what it compares and leaves out (ordinal position, OIDs, sequence values, alembic_version) is in that module's
docstring. Its diff is unit-tested without a database in server/data/store/tests/test_schema_catalog_diff.py.

FAILURE OUTPUT IS COMPACT ON PURPOSE. The tool that runs this suite caps its output, so a failure is a pytest.fail
with no traceback whose first line is 'REVISION <id> <step>: <check>', followed by at most DIFF_LINES_SHOWN diff lines
(each cut to LINE_WIDTH characters) or the last ALEMBIC_LINES_SHOWN lines of alembic's output. Never a whole catalogue.

NON_REVERSIBLE_DOWNGRADES is the allowlist of revisions whose downgrade is documented NOT to restore the parent's
schema exactly. Each entry is the owner's ruling, never a way to make this green, and carries its written reason and
the EXACT facts that differ, each with its value on both sides (schema_catalog.AllowedDifference). An entry narrows
the downgrade check, it never skips anything: the downgraded catalogue must differ from the parent's by exactly those
facts -- one not seen, or any other difference, fails -- and the re-upgrade comparison then runs as for every other
revision. An entry with no facts is refused, and so is one that outlives its reason (the facts are no longer seen).

Run by make test-system and the agent-stack MCP's run_system_tests; pytest.ini keeps tests/system out of the PR gate.
FAIL, NEVER SKIP.
"""

import hashlib
import os
import subprocess  # nosec B404 -- alembic as a subprocess, as test_migration_with_data.py runs it (tj-vhboky.62)
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, Engine

from common.tests.image_path import image_pythonpath
from common.tests.migration_gate import read_line
from common.tests.roots import SERVER_ROOT
from common.tests.schema_catalog import (
    AllowedDifference,
    Catalogue,
    allowed_difference_problems,
    diff_catalogues,
    read_catalogue,
    unexplained_differences,
)


pytestmark = pytest.mark.data_store

# The same paths as test_migration_with_data.py; restated, not imported, because a test module is not importable by
# name from another (tests/system is not a package, and test_declared_imports refuses an undeclared import).
STORE_DIR = SERVER_ROOT / 'data' / 'store'
MIGRATIONS_DIR = STORE_DIR / 'migrations'
ALEMBIC_INI_PATH = STORE_DIR / 'alembic.ini'
ALEMBIC_TIMEOUT_SECONDS = 300

# The line from the base to the head; each revision's parent is the one before it (read_line refuses a merge point).
LINE = [step.revision for step in read_line(MIGRATIONS_DIR)]
PARENT = {revision: (LINE[index - 1] if index else None) for index, revision in enumerate(LINE)}


@dataclass(frozen=True)
class NonReversibleDowngrade:
    reason: str
    differences: tuple[AllowedDifference, ...]


def _nullable_restored(table: str, column: str) -> AllowedDifference:
    """A column the parent has NOT NULL, which the downgrade restores nullable; every other fact of it must match."""
    return AllowedDifference('columns', (table, column), 'not_null', expected=True, actual=False)


NON_REVERSIBLE_DOWNGRADES: dict[str, NonReversibleDowngrade] = {
    # OWNER RULING 2026-10-25, option A, on bead tj-7zjov1.99 (architect gate note 22:30 UTC, (b2)): the revision is
    # on main, dated 2026-09-25, and its header records the owner's 2026-09-24 no-lossy-downgrade ruling; dropping
    # it was rejected because it rewrites chained history. Reason text verbatim from the architect's note.
    'eec8f88a7443': NonReversibleDowngrade(
        reason=(
            'eec8f88a7443 downgrade restores stock_market_activity split_factor and dividends_factor NULLABLE, '
            'where its parent 8f41c2d7a3b9 has them NOT NULL: the upgrade dropped their values and nothing can '
            'refill them, so NOT NULL would need invented factors or a refused downgrade over bars; ADR '
            'tj-vhboky.55 E3 requires the downgrade to succeed with bars and these columns NULL. The difference '
            'is exactly these two nullability facts.'
        ),
        differences=(
            _nullable_restored('stock_market_activity', 'split_factor'),
            _nullable_restored('stock_market_activity', 'dividends_factor'),
        ),
    )
}

DIFF_LINES_SHOWN = 25
ALEMBIC_LINES_SHOWN = 15
LINE_WIDTH = 140  # 25 full lines plus the header stay under 4 KB, so two failures fit beside a 6 KB build log


# ---------------------------------------------------------------------------------------------
# Two scratch databases per test, created, migrated and dropped as test_migration_with_data.py's `scratch` is.


@dataclass
class AlembicRun:
    returncode: int
    output: str


class ScratchDb:
    """One scratch database, migrated only by `python -m alembic` subprocesses with the repository's own ini."""

    def __init__(self, name: str, url: URL, password: str) -> None:
        self.name = name
        self.url = url
        self._password = password
        self.engine: Engine = sa.create_engine(url, poolclass=sa.pool.NullPool)

    def migrate(self, command: str, target: str) -> AlembicRun:
        env = dict(os.environ)
        env['DATABASE_URI'] = self.url.render_as_string(hide_password=False)
        env['PYTHONPATH'] = image_pythonpath(SERVER_ROOT)
        argv = [sys.executable, '-m', 'alembic', '-c', str(ALEMBIC_INI_PATH), command, target]
        done = subprocess.run(  # nosec B603 -- fixed argv, no shell
            argv, cwd=STORE_DIR, env=env, capture_output=True, text=True, timeout=ALEMBIC_TIMEOUT_SECONDS, check=False
        )
        output = done.stdout + done.stderr
        return AlembicRun(done.returncode, output.replace(self._password, '<redacted>') if self._password else output)

    def revision(self) -> str | None:
        with self.engine.connect() as conn:
            if not conn.execute(sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one():
                return None
            rows = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalars().all()
        assert len(rows) <= 1, f'alembic_version holds {len(rows)} rows'
        return rows[0] if rows else None


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _scratch_database(
    request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity, suffix: str
) -> Iterator[ScratchDb]:
    assert ALEMBIC_INI_PATH.is_file(), f'{ALEMBIC_INI_PATH} is missing: is it mounted into test_client?'
    digest = hashlib.blake2s(request.node.nodeid.encode(), digest_size=4).hexdigest()
    name = f'sys8_{run_identity.run_id}_{digest}_{suffix}'
    admin = pg_engine.execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE {_quote(name)}'))
    db = ScratchDb(name, pg_settings.url().set(database=name), pg_settings.password)
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)'))


@pytest.fixture
def revision_db(request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity) -> Iterator[ScratchDb]:
    """The database built to the revision, downgraded and upgraded again."""
    yield from _scratch_database(request, pg_settings, pg_engine, run_identity, 'r')


@pytest.fixture
def parent_db(request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity) -> Iterator[ScratchDb]:
    """The database built from empty to the parent, read once."""
    yield from _scratch_database(request, pg_settings, pg_engine, run_identity, 'p')


# ---------------------------------------------------------------------------------------------
# Compact failures.


def _cut(line: str) -> str:
    return line if len(line) <= LINE_WIDTH else line[: LINE_WIDTH - 3] + '...'


def _fail(revision: str, step: str, check: str, detail: list[str]) -> None:
    shown = [_cut(line) for line in detail[:DIFF_LINES_SHOWN]]
    if len(detail) > DIFF_LINES_SHOWN:
        shown.append(f'... and {len(detail) - DIFF_LINES_SHOWN} more difference(s)')
    pytest.fail('\n'.join([f'REVISION {revision} {step}: {check}', *shown]), pytrace=False)


def _alembic_tail(run: AlembicRun) -> list[str]:
    lines = [line for line in run.output.splitlines() if line.strip()]
    return ['alembic output, last lines:', *lines[-ALEMBIC_LINES_SHOWN:]]


def _migrate(db: ScratchDb, revision: str, step: str, command: str, target: str, lands_on: str | None) -> None:
    run = db.migrate(command, target)
    if run.returncode != 0:
        _fail(revision, step, f'alembic {command} {target} exited {run.returncode}', _alembic_tail(run))
    landed = db.revision()
    if landed != lands_on:
        _fail(revision, step, f'alembic {command} {target} left the revision {landed}, not {lands_on}', [])


def _catalogue(db: ScratchDb) -> Catalogue:
    with db.engine.connect() as conn:
        return read_catalogue(conn)


# ---------------------------------------------------------------------------------------------
# The tests.


def _entry_problems(revision: str, entry: NonReversibleDowngrade) -> list[str]:
    problems = []
    if revision not in PARENT:
        problems.append(f'names {revision}, which is not in {MIGRATIONS_DIR}')
    if not entry.reason.strip():
        problems.append('gives no reason')
    problems.extend(allowed_difference_problems(entry.differences))
    return [f'NON_REVERSIBLE_DOWNGRADES[{revision}] {problem}' for problem in problems]


def test_every_non_reversible_downgrade_entry_names_a_revision_a_reason_and_its_exact_differences() -> None:
    problems = [
        line for revision, entry in NON_REVERSIBLE_DOWNGRADES.items() for line in _entry_problems(revision, entry)
    ]
    assert not problems, '\n'.join(problems)


@pytest.mark.parametrize(
    ('revision', 'entry', 'problem'),
    [
        pytest.param(LINE[-1], NonReversibleDowngrade('a reason', ()), 'no allowed differences', id='no-differences'),
        pytest.param(LINE[-1], NonReversibleDowngrade('  ', ()), 'gives no reason', id='no-reason'),
        pytest.param('000000000000', NonReversibleDowngrade('a reason', ()), 'which is not in', id='unknown-revision'),
    ],
)
def test_a_malformed_entry_is_refused(revision: str, entry: NonReversibleDowngrade, problem: str) -> None:
    """The check above is not vacuous: an entry with no facts, no reason or an unknown revision is named."""
    assert any(problem in line for line in _entry_problems(revision, entry)), _entry_problems(revision, entry)


@pytest.mark.parametrize('revision', LINE)
def test_a_downgrade_leaves_exactly_the_parents_schema_and_a_re_upgrade_rebuilds_the_revisions(
    revision_db: ScratchDb, parent_db: ScratchDb, revision: str
) -> None:
    parent = PARENT[revision]
    parent_name = parent or 'base'
    if revision_db.revision() is not None or parent_db.revision() is not None:
        _fail(revision, 'setup', 'a scratch database is not empty', [])

    if parent is not None:
        _migrate(parent_db, revision, 'build-parent', 'upgrade', parent, parent)
    parent_schema = _catalogue(parent_db)

    _migrate(revision_db, revision, 'build-revision', 'upgrade', revision, revision)
    revision_schema = _catalogue(revision_db)
    if not diff_catalogues(parent_schema, revision_schema):
        _fail(
            revision,
            'build-revision',
            f'reads the same catalogue as its parent {parent_name}: the read is blind or the revision changes no schema',
            [],
        )

    _migrate(revision_db, revision, 'downgrade', 'downgrade', parent_name, parent)
    labels = {'expected_label': f'parent {parent_name}', 'actual_label': 'downgraded'}
    entry = NON_REVERSIBLE_DOWNGRADES.get(revision)
    if entry is not None:
        # Exactly the listed facts, each as stated; a fact not seen (the entry outlived its reason) or anything else
        # fails. No early return: the re-upgrade below runs for an allowlisted revision too.
        left = unexplained_differences(parent_schema, _catalogue(revision_db), entry.differences, **labels)
        if left:
            _fail(
                revision,
                'downgrade',
                f'allowlisted, but the catalogue does not differ from parent {parent_name} by exactly its '
                f'{len(entry.differences)} listed fact(s) ({len(left)})',
                left,
            )
    elif left := diff_catalogues(parent_schema, _catalogue(revision_db), **labels):
        _fail(
            revision,
            'downgrade',
            f'the catalogue after one downgrade differs from parent {parent_name} built from empty ({len(left)})',
            left,
        )

    _migrate(revision_db, revision, 're-upgrade', 'upgrade', revision, revision)
    again = diff_catalogues(
        revision_schema, _catalogue(revision_db), expected_label='built from empty', actual_label='round trip'
    )
    if again:
        _fail(
            revision,
            're-upgrade',
            f'the catalogue after downgrade and re-upgrade differs from {revision} built from empty ({len(again)})',
            again,
        )


# ---------------------------------------------------------------------------------------------
# tj-7zjov1.123 (P34c): what b8e4c1f7a923 adds, read off the catalogue. The generic test above proves the downgrade
# restores the parent exactly; it does not say WHAT the revision built, so a revision that added an extra object (and
# dropped it again) would pass it. The design's objects: seven columns and one CHECK, each with its facts, and nothing
# else in any category, so the downgrade's inverse is exactly these eight objects.

PUSH_PATH_REVISION = 'b8e4c1f7a923'
PUSH_PATH_OBJECTS = {
    # (category, identity): the catalogue's facts (type, NOT NULL, default, identity, generated) / (kind, definition).
    ('columns', ('ingest_run', 'served_start')): ('timestamp with time zone', False, None, '', ''),
    ('columns', ('ingest_run', 'served_end')): ('timestamp with time zone', False, None, '', ''),
    ('columns', ('ingest_request', 'trigger')): ('ingest_run_trigger', False, None, '', ''),
    ('columns', ('ingest_request', 'expiry')): ('timestamp with time zone', False, None, '', ''),
    ('columns', ('ingest_request', 'collect')): ('boolean', False, None, '', ''),
    ('columns', ('ingest_request', 'spend_unknown_attempts')): ('integer', True, '0', '', ''),
    ('columns', ('store_dataset_entry', 'stopped_cause')): ('character varying', False, None, '', ''),
    ('constraints', ('store_dataset_entry', 'ck_store_dataset_entry_stopped_cause_needs_stop')): (
        'c',
        'CHECK (((stopped_cause IS NULL) OR (collect IS FALSE)))',
    ),
}


def test_the_push_path_revision_builds_exactly_its_designed_objects_and_its_downgrade_removes_them(
    revision_db: ScratchDb, parent_db: ScratchDb
) -> None:
    parent = PARENT[PUSH_PATH_REVISION]
    assert parent == 'f2b7d4a91c36', f'{PUSH_PATH_REVISION} sits on {parent}'
    _migrate(parent_db, PUSH_PATH_REVISION, 'build-parent', 'upgrade', parent, parent)
    _migrate(revision_db, PUSH_PATH_REVISION, 'build-revision', 'upgrade', PUSH_PATH_REVISION, PUSH_PATH_REVISION)
    before, after = _catalogue(parent_db), _catalogue(revision_db)

    added = {
        (category, key): facts
        for category, facts_by_key in after.items()
        for key, facts in facts_by_key.items()
        if key not in before.get(category, {})
    }
    removed = [(category, key) for category, facts in before.items() for key in facts if key not in after[category]]
    changed = [
        (category, key)
        for category, facts in before.items()
        for key in facts
        if key in after[category] and after[category][key] != facts[key]
    ]
    assert added == PUSH_PATH_OBJECTS, sorted(set(added) ^ set(PUSH_PATH_OBJECTS), key=repr)
    assert (removed, changed) == ([], []), 'an additive revision removed or changed an existing object'

    _migrate(revision_db, PUSH_PATH_REVISION, 'downgrade', 'downgrade', parent, parent)
    assert not diff_catalogues(before, _catalogue(revision_db)), 'the downgrade left or lost an object'


# ---------------------------------------------------------------------------------------------
# tj-7zjov1.153 (P35r): what 39c28a39b290 adds, read off the catalogue, as for P34c above: exactly one column,
# ingest_request.expiry_type, integer, nullable, NO default (a default would write a guessed entry identity), and
# nothing else in any category; its downgrade removes exactly that column.

EXPIRY_TYPE_REVISION = '39c28a39b290'
EXPIRY_TYPE_OBJECTS = {('columns', ('ingest_request', 'expiry_type')): ('integer', False, None, '', '')}


def test_the_expiry_type_revision_builds_exactly_its_one_column_and_its_downgrade_removes_it(
    revision_db: ScratchDb, parent_db: ScratchDb
) -> None:
    parent = PARENT[EXPIRY_TYPE_REVISION]
    assert parent == PUSH_PATH_REVISION, f'{EXPIRY_TYPE_REVISION} sits on {parent}'
    _migrate(parent_db, EXPIRY_TYPE_REVISION, 'build-parent', 'upgrade', parent, parent)
    _migrate(revision_db, EXPIRY_TYPE_REVISION, 'build-revision', 'upgrade', EXPIRY_TYPE_REVISION, EXPIRY_TYPE_REVISION)
    before, after = _catalogue(parent_db), _catalogue(revision_db)

    added = {
        (category, key): facts
        for category, facts_by_key in after.items()
        for key, facts in facts_by_key.items()
        if key not in before.get(category, {})
    }
    removed = [(category, key) for category, facts in before.items() for key in facts if key not in after[category]]
    changed = [
        (category, key)
        for category, facts in before.items()
        for key in facts
        if key in after[category] and after[category][key] != facts[key]
    ]
    assert added == EXPIRY_TYPE_OBJECTS, sorted(set(added) ^ set(EXPIRY_TYPE_OBJECTS), key=repr)
    assert (removed, changed) == ([], []), 'an additive revision removed or changed an existing object'

    _migrate(revision_db, EXPIRY_TYPE_REVISION, 'downgrade', 'downgrade', parent, parent)
    assert not diff_catalogues(before, _catalogue(revision_db)), 'the downgrade left or lost an object'
