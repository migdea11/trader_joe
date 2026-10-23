"""build_infra pin: the PR gate collects tests/system, so drift in it reds here, not at the system tier (tj-grna9p.195).

tests/system runs only in test mode, against an up and migrated stack (make test-system; pytest.ini keeps the
directory out of the PR gate through norecursedirs). So drift in it that has nothing to do with a stack -- an
import of a module that moved or was deleted (the freshness split took data.store.app.freshness away), a renamed
fixture, a syntax error -- used to surface only when someone next ran that tier. tj-grna9p.180's live check is where
one did. This module runs `pytest --collect-only -q tests/system` in a SUBPROCESS from the regular suite and holds:

  1. COLLECTION SUCCEEDS: exit status 0, and no `error` in the summary. Exit status, never the summary text alone:
     pytest.ini's --continue-on-collection-errors lets a run print a count with an error above it.
  2. AT LEAST TEST_FLOOR TESTS ARE COLLECTED, and at least FILE_FLOOR test files exist. Both are floors, a little
     below today's numbers, so a new test never breaks this and a silent collapse does. The file floor is there
     because the test floor alone could not catch a small file disappearing: test_http_pool_release.py holds one test.
  3. EVERY tests/system/test_*.py CONTRIBUTES AT LEAST ONE COLLECTED TEST. A module that imports cleanly but whose
     tests stopped being collected -- a renamed function prefix, a class pytest skips for its __init__ -- is a file
     that runs nothing at the system tier and says so nowhere.

A fixture that is renamed but still requested is NOT caught at collection: pytest resolves fixtures at setup. So a
second subprocess runs `pytest --setup-plan -q tests/system` (tj-grna9p.196), which resolves every requested fixture
name for every collected test and prints the plan WITHOUT RUNNING A FIXTURE BODY OR A TEST. That was measured on
pytest 9.1.1 with pytest-asyncio 1.4.0, not assumed: a probe fixture that wrote a marker file and raised was planned
and never called, no socket was opened (an audit hook on socket.connect and getaddrinfo logged nothing), and the run
exited 0 with the contract names entirely UNSET, which the autouse redacted_log_records would have failed on had its
body run. It holds:

  4. EVERY REQUESTED FIXTURE RESOLVES: exit status 0, no `fixture 'name' not found`, no ERROR line in the summary.
     Exit status first, as above; the plan's text carries parametrize ids such as error_type, so the word `error`
     alone is not a signal here.
  5. THE PLAN COVERS THE SUITE: at least TEST_FLOOR planned tests, every test file among them, and the conftest's
     fixtures named in it. A plan that planned nothing would otherwise pass point 4 vacuously.

Together the pins reach imports, module-level code, syntax, test discovery, and fixture names. Not fixture BODIES:
what a fixture does, and whether a stack answers, is the system tier's.

COLLECTION MUST NOT CONNECT, and the environment is built so that it could not quietly succeed if it did. Every
name of the suite's env contract (tests/system/conftest.py, CONTRACT_*; docker-compose.test-client.yaml sets them
for the real run) is stubbed with a dummy value, never a real one: the hosts are under .invalid (RFC 6761, never
resolves), the port is 1, the secrets are dummy-not-a-secret. Any value of those names this process inherited is
replaced, so a developer's own environment never reaches the child. As of this writing conftest reads the contract
only in fixture bodies, which collection never runs; the stubs mean that if a module ever reads it at import, the
read succeeds, and a connect at import fails loudly on the unresolvable host rather than reaching a real database.

THE IMPORT PATH mirrors the test_client container's PYTHONPATH=/code:/code/gen/proto/python, where /code is the
server root and tests/ is mounted beside the service trees. In the checkout those are two directories: the server
root (common, data, routers, schemas) and the repository root (tests.fakes, which conftest imports). pytest.ini's
pythonpath adds the repository root and the generated tree too; naming them here keeps the child independent of
that. POSTGRES_ASYNC and POSTGRES_SYNC are true, as the container and make test set them, because they are
import-time flags that change which drivers postgres_tools imports.
"""

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

from common.tests.roots import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.build_infra

SYSTEM_DIR = REPO_ROOT / 'tests' / 'system'
SYSTEM_ARG = 'tests/system'

# Today (feat/tj-grna9p-ui-data-retention at dd9bc1f): 345 tests in 23 files; the system tier passed 345 on its
# last run. The floors sit a little below, so adding tests never trips them; raise them when the suite grows.
TEST_FLOOR = 330
FILE_FLOOR = 23

COLLECT_TIMEOUT_SECONDS = 180

DUMMY_SECRET = 'dummy-not-a-secret'
# Every name tests/system/conftest.py reads through _contract(), stubbed. Kept in step with conftest by
# test_the_stubbed_names_are_exactly_the_conftest_contract below.
CONTRACT_STUBS = {
    'DATABASE_NAME': 'collect-only-postgres.invalid',
    'DATABASE_PORT': '1',
    'POSTGRES_USER': 'dummy-not-a-user',
    'POSTGRES_PASS': DUMMY_SECRET,
    'POSTGRES_DB_NAME': 'dummy-not-a-database',
    'DATABASE_CONN_TIMEOUT': '1',
    'SYSTEM_TEST_DATA_STORE_URL': 'http://collect-only-data-store.invalid:1',
    'INSTANCE_WRITE_SECRET': DUMMY_SECRET,
}
# The rest of the client's environment that is not a contract name: the import-time driver flags, and the
# non-UTC zone the client's entrypoint insists on.
CLIENT_ENV = {'POSTGRES_ASYNC': 'true', 'POSTGRES_SYNC': 'true', 'TZ': 'America/Toronto'}

# One collected item per line under -q: path::name[params].
COLLECTED_ITEM = re.compile(r'^(tests/system/[^:\s]+\.py)::\S')
SUMMARY = re.compile(r'^(\d+) tests? collected\b.*$', re.MULTILINE)
# Under --setup-plan, one line per planned test: path::name[params] (fixtures used: a, b, ...). A parametrize id can
# hold spaces, so everything up to the last ' (fixtures used: ' is the node id.
PLANNED_ITEM = re.compile(r'^\s*(tests/system/[^:\s]+\.py)::.+ \(fixtures used: ([^)]*)\)$')
# pytest's own wording for a requested name that no fixture provides (FixtureLookupError).
FIXTURE_NOT_FOUND = re.compile(r"fixture '[^']+' not found")
SHORT_SUMMARY_ERROR = re.compile(r'^ERROR\b.*$', re.MULTILINE)
# Fixtures tests/system/conftest.py defines that the plan must name: the session's own spine. If these vanish from
# the plan, it stopped resolving the conftest at all, and point 4 would hold over nothing.
CONFTEST_FIXTURES_PLANNED = ('redacted_log_records', 'run_identity', 'pg_engine', 'data_store')


def _child_env() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        # PYTEST_* (PYTEST_ADDOPTS, PYTEST_CURRENT_TEST, ...) belongs to this session, not the child's.
        if name not in CONTRACT_STUBS and not name.startswith('PYTEST_')
    }
    env.update(CONTRACT_STUBS)
    env.update(CLIENT_ENV)
    env['PYTHONPATH'] = os.pathsep.join(str(path) for path in (SERVER_ROOT, REPO_ROOT, REPO_ROOT / 'gen/proto/python'))
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def _run_system_pytest(mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-m', 'pytest', mode, '-q', '-p', 'no:cacheprovider', SYSTEM_ARG],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=COLLECT_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope='module')
def collection() -> subprocess.CompletedProcess:
    """One collect-only run of the system suite, shared by every check in this module."""
    return _run_system_pytest('--collect-only')


@pytest.fixture(scope='module')
def setup_plan() -> subprocess.CompletedProcess:
    """One setup-plan run of the system suite: every fixture name resolved, no fixture body and no test run."""
    return _run_system_pytest('--setup-plan')


def _shown(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\n--- stdout (tail) ---\n{result.stdout[-6000:]}\n--- stderr ---\n{result.stderr[-3000:]}'


def _per_file(result: subprocess.CompletedProcess) -> Counter:
    return Counter(match.group(1) for line in result.stdout.splitlines() if (match := COLLECTED_ITEM.match(line)))


def _system_files() -> list[str]:
    return sorted(PurePosixPath(path.relative_to(REPO_ROOT)).as_posix() for path in SYSTEM_DIR.glob('test_*.py'))


def test_the_system_suite_collects_without_error(collection):
    assert collection.returncode == 0, _shown(collection)
    summary = SUMMARY.search(collection.stdout)
    assert summary, f'no collected-count summary line\n{_shown(collection)}'
    assert 'error' not in summary.group(0).lower(), _shown(collection)


def test_the_system_suite_collects_at_least_the_floor(collection):
    summary = SUMMARY.search(collection.stdout)
    assert summary, f'no collected-count summary line\n{_shown(collection)}'
    collected = int(summary.group(1))
    assert collected >= TEST_FLOOR, (
        f'tests/system collected {collected} tests, below the floor of {TEST_FLOOR}; a file or a set of tests '
        f'stopped being collected\n{_shown(collection)}'
    )
    # The per-line items and the summary are the same collection read twice; a disagreement means the parse broke.
    assert sum(_per_file(collection).values()) == collected, _shown(collection)


def test_the_system_suite_has_at_least_the_floor_of_files():
    files = _system_files()
    assert len(files) >= FILE_FLOOR, (
        f'tests/system holds {len(files)} test files, below the floor of {FILE_FLOOR}: {files}'
    )


def test_every_system_file_contributes_a_collected_test(collection):
    per_file = _per_file(collection)
    empty = [name for name in _system_files() if per_file[name] == 0]
    assert empty == [], f'these tests/system files collected no test: {empty}\n{_shown(collection)}'


def _planned(result: subprocess.CompletedProcess) -> list[tuple[str, set[str]]]:
    return [
        (match.group(1), {name.strip() for name in match.group(2).split(',')})
        for line in result.stdout.splitlines()
        if (match := PLANNED_ITEM.match(line))
    ]


def test_every_fixture_the_system_suite_requests_resolves(setup_plan):
    missing = sorted(set(FIXTURE_NOT_FOUND.findall(setup_plan.stdout + setup_plan.stderr)))
    assert missing == [], (
        f'tests/system requests fixtures that no conftest, module or plugin defines: {missing}. A fixture was renamed '
        f'or removed while a test or fixture still asks for it by the old name\n{_shown(setup_plan)}'
    )
    errors = SHORT_SUMMARY_ERROR.findall(setup_plan.stdout)
    assert errors == [], f'setup-plan of tests/system reported errors: {errors}\n{_shown(setup_plan)}'
    assert setup_plan.returncode == 0, _shown(setup_plan)


def test_the_setup_plan_covers_the_system_suite(setup_plan):
    planned = _planned(setup_plan)
    assert len(planned) >= TEST_FLOOR, (
        f'setup-plan of tests/system planned {len(planned)} tests, below the floor of {TEST_FLOOR}; the fixture '
        f'check would hold over too little\n{_shown(setup_plan)}'
    )
    unplanned = sorted(set(_system_files()) - {path for path, _ in planned})
    assert unplanned == [], f'these tests/system files have no planned test: {unplanned}\n{_shown(setup_plan)}'
    used = set().union(*(fixtures for _, fixtures in planned))
    absent = [name for name in CONFTEST_FIXTURES_PLANNED if name not in used]
    assert absent == [], f'the plan never names these conftest fixtures: {absent}\n{_shown(setup_plan)}'


def test_the_plan_and_the_collection_count_the_same_tests(collection, setup_plan):
    # The two runs see one suite; a disagreement means the plan's line parse broke, or the plan dropped tests.
    summary = SUMMARY.search(collection.stdout)
    assert summary, f'no collected-count summary line\n{_shown(collection)}'
    assert len(_planned(setup_plan)) == int(summary.group(1)), _shown(setup_plan)


def test_the_conftest_fixtures_the_plan_must_name_exist():
    # Not vacuous: each name in CONFTEST_FIXTURES_PLANNED is a fixture tests/system/conftest.py defines today.
    conftest = (SYSTEM_DIR / 'conftest.py').read_text(encoding='utf-8')
    defined = set(re.findall(r'^(?:async )?def (\w+)\(', conftest, re.MULTILINE))
    assert set(CONFTEST_FIXTURES_PLANNED) <= defined, sorted(set(CONFTEST_FIXTURES_PLANNED) - defined)


def test_the_stubbed_names_are_exactly_the_conftest_contract():
    # The stubs replace the contract only while they name it: a contract name added to conftest and missing here
    # would reach the child from this process's environment, whatever it holds.
    conftest = (SYSTEM_DIR / 'conftest.py').read_text(encoding='utf-8')
    declared = set(re.findall(r"^CONTRACT_[A-Z_]+ = '([A-Z_]+)'", conftest, re.MULTILINE))
    assert declared, 'found no CONTRACT_* names in tests/system/conftest.py'
    assert declared == set(CONTRACT_STUBS)


def test_every_stubbed_value_is_a_dummy():
    # Never a real credential or a reachable host: every value is a dummy literal, an .invalid host, or a number.
    for name, value in CONTRACT_STUBS.items():
        assert value.isdigit() or 'dummy' in value or '.invalid' in value, (name, value)


def test_the_child_inherits_no_contract_value(monkeypatch):
    monkeypatch.setenv('INSTANCE_WRITE_SECRET', 'inherited-value')
    monkeypatch.setenv('DATABASE_NAME', 'inherited-host')
    env = _child_env()
    assert env['INSTANCE_WRITE_SECRET'] == DUMMY_SECRET
    assert env['DATABASE_NAME'] == CONTRACT_STUBS['DATABASE_NAME']


def test_the_paths_this_module_names_exist():
    # Not vacuous: the suite, its conftest and the generated tree the child imports through are where they are named.
    assert (SYSTEM_DIR / 'conftest.py').is_file()
    assert Path(REPO_ROOT, 'gen', 'proto', 'python').is_dir()
    assert (SERVER_ROOT / 'data' / 'store').is_dir()
