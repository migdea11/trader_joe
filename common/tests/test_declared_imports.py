"""Every third-party package the repository's code imports is declared in a group that installs it.

tj-3mk3u5.37.15: data/ingest/app/brokers/alpaca/classify.py imported requests and
data/ingest/tests/test_alpaca_cut_connection.py imported urllib3, and neither was declared. requests
arrived through alpaca-py and urllib3 through requests. This repo declares what its code imports
rather than leaning on a transitive pin (pyproject.toml's data-store comment on sqlalchemy[asyncio]).
No other check enforces that: ruff does not, and a package installed transitively keeps every test
green until a re-lock or an upstream release drops it. Then the image fails when it imports the module.

One test per third-party top-level import name, read from the source with ast, so an import inside a
function body counts too. Two rules:

1. Every file imports the name from a distribution declared in a group that its environment installs:
   the image or venv it runs in (ENVIRONMENTS below; the most specific path wins).
2. A name that only tests import is declared in testing and in no group a service image installs.
   This is pyproject.toml's testing-group comment, which the architect applied to urllib3 on .37.15.
"""

import ast
import functools
import re
import sys
import tomllib
from collections import defaultdict
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest


pytestmark = pytest.mark.build_infra

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
DECLARED_GROUPS = PYPROJECT['dependency-groups']

# The repository's own top-level packages: ruff's isort list, plus the repo-root tests and tools packages.
FIRST_PARTY = frozenset(PYPROJECT['tool']['ruff']['lint']['isort']['known-first-party']) | {'tests', 'tools'}

# Dockerfile service_build_image: `uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME}`.
INGEST_IMAGE = frozenset({'base', 'data-ingest'})
STORE_IMAGE = frozenset({'base', 'data-store'})
# Dockerfile system_test_image, which runs tests/system.
SYSTEM_TEST_IMAGE = frozenset({'base', 'data-store', 'testing'})
# The venv that make test and CI's test jobs run in. test_agent_mcp_groups pins those installs to this set.
TEST_VENV = frozenset(DECLARED_GROUPS) - {'security', 'agent-mcp'}
# Everything a service image can hold, its dev stage included. A test-only package belongs in none of them.
SERVICE_IMAGE_GROUPS = frozenset({'base', 'dev', 'data-ingest', 'data-store'})

# Path prefix -> (groups the environment installs, whether the code there is a test).
# Both images copy common, routers, schemas and gen/proto/python, so those default to base.
ENVIRONMENTS: dict[str, tuple[frozenset[str], bool]] = {
    'common': (frozenset({'base'}), False),
    # The shared library's only user of sqlalchemy, asyncpg and psycopg2; no ingest tree imports it.
    'common/database': (STORE_IMAGE, False),
    # Imports debugpy only under RunMode.DEV, which only the dev image sets; that stage adds the dev group.
    'common/app_lifecycle.py': (frozenset({'base', 'dev'}), False),
    'routers': (frozenset({'base'}), False),
    'routers/data_ingest': (INGEST_IMAGE, False),
    'routers/data_store': (STORE_IMAGE, False),
    'schemas': (frozenset({'base'}), False),
    'gen/proto/python': (frozenset({'base'}), False),
    'data': (frozenset({'base'}), False),
    'data/ingest/app': (INGEST_IMAGE, False),
    'data/store/app': (STORE_IMAGE, False),
    'data/store/migrations': (STORE_IMAGE, False),
    # The seed producer runs in the test_client container (make seed-dump); bundle.py in the host venv.
    'data/store/seeds': (SYSTEM_TEST_IMAGE, False),
    'tests': (frozenset({'base'}), False),
    # The fake-mode overlay mounts it into the data_ingest image (test_fake_read pins a stricter rule).
    'tests/fakes': (INGEST_IMAGE, False),
    'tests/system': (SYSTEM_TEST_IMAGE, True),
    'common/tests': (TEST_VENV, True),
    'data/ingest/tests': (TEST_VENV, True),
    'data/store/tests': (TEST_VENV, True),
    'routers/tests': (TEST_VENV, True),
    'schemas/tests': (TEST_VENV, True),
}
# tools/ is left out: tools/agent_mcp runs in its own image, which installs agent-mcp only.
SCANNED_ROOTS = ('common', 'routers', 'schemas', 'gen/proto/python', 'data', 'tests')

# Undeclared today, found by the validator's survey at the tj-3mk3u5.37.15 gate. Each marker comes off with its fix.
FINDINGS = {
    'google': (
        'FINDING (validator, tj-3mk3u5.37.15 gate): gen/proto/python/*_pb2.py imports google.protobuf at module '
        'level, and common/rpc loads it in both prod images. protobuf reaches base only through '
        'grpcio-health-checking. Fix: declare protobuf in base (builder-shared).'
    ),
    'packaging': (
        'FINDING (validator, tj-3mk3u5.37.15 gate): common/tests imports packaging (test_ci_invariants, '
        'test_agent_mcp_groups), and it arrives only through pytest. Fix: declare packaging in testing '
        '(builder-shared).'
    ),
    'yaml': (
        'FINDING (validator, tj-3mk3u5.37.15 gate): common/tests/compose_model.py imports yaml, and PyYAML arrives '
        'only through uvicorn[standard] in base. Fix: declare PyYAML in testing (builder-shared).'
    ),
}


def _canonical(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _declaring_groups() -> dict[str, set[str]]:
    """Canonical distribution name -> the dependency groups that declare it."""
    declared: dict[str, set[str]] = defaultdict(set)
    for group, entries in DECLARED_GROUPS.items():
        for entry in entries:
            if isinstance(entry, str):
                declared[_canonical(re.match(r'[A-Za-z0-9][A-Za-z0-9._-]*', entry).group(0))].add(group)
    return declared


@functools.cache
def _providers() -> dict[str, list[str]]:
    """Top-level import name -> the installed distributions that provide it."""
    return packages_distributions()


def _environment(path: str) -> tuple[frozenset[str], bool]:
    """The ENVIRONMENTS entry whose prefix matches path most specifically."""
    matches = [prefix for prefix in ENVIRONMENTS if path == prefix or path.startswith(f'{prefix}/')]
    assert matches, f'{path} has no environment; add its tree to ENVIRONMENTS'
    return ENVIRONMENTS[max(matches, key=len)]


def _in_a_test_directory(path: str) -> bool:
    """Under a tests directory. The repo-root tests/ holds runtime fakes beside the system suite, so only tests/system."""
    parents = Path(path).parts[:-1]
    return 'tests' in parents[1:] or parents[:2] == ('tests', 'system')


def _scanned_files() -> list[str]:
    return sorted(
        str(path.relative_to(REPO_ROOT)) for root in SCANNED_ROOTS for path in (REPO_ROOT / root).rglob('*.py')
    )


def _third_party_imports() -> dict[str, list[tuple[str, int]]]:
    """Top-level third-party import name -> every (file, line) that imports it."""
    found: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for relative in _scanned_files():
        path = REPO_ROOT / relative
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=relative)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.partition('.')[0]
                if top not in sys.stdlib_module_names and top not in FIRST_PARTY:
                    found[top].append((relative, node.lineno))
    return found


IMPORTS = _third_party_imports()


def _params() -> list:
    return [
        pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=FINDINGS[name])) if name in FINDINGS else name
        for name in sorted(IMPORTS)
    ]


def test_the_scan_reaches_the_importers_tj_3mk3u5_37_15_declared():
    """A scan of the wrong directories would pass on nothing. These two files are where the gap was found."""
    assert 'data/ingest/app/brokers/alpaca/classify.py' in {path for path, _ in IMPORTS['requests']}
    assert 'data/ingest/tests/test_alpaca_cut_connection.py' in {path for path, _ in IMPORTS['urllib3']}


def test_test_code_and_only_test_code_resolves_to_a_test_environment():
    """Rule 2 depends on knowing which code is a test, so no test file may fall back to a production tree."""
    misfiled = [path for path in _scanned_files() if _in_a_test_directory(path) != _environment(path)[1]]
    assert not misfiled, f'files whose environment disagrees with whether they sit in a tests directory: {misfiled}'


def test_every_group_the_environments_name_is_declared():
    named = {group for groups, _ in ENVIRONMENTS.values() for group in groups} | SERVICE_IMAGE_GROUPS
    assert named <= set(DECLARED_GROUPS), sorted(named - set(DECLARED_GROUPS))


def test_the_image_environments_are_the_ones_the_dockerfile_installs():
    """INGEST_IMAGE, STORE_IMAGE and SYSTEM_TEST_IMAGE were read from these syncs. If one changes, re-read them."""
    lines = (REPO_ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()
    syncs = {line.removeprefix('RUN ').strip() for line in lines if line.startswith('RUN uv sync')}
    assert 'uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME} --frozen' in syncs, syncs
    assert 'uv sync --only-group base --only-group data-store --only-group testing --frozen' in syncs, syncs


def test_every_finding_is_still_imported():
    """A FINDINGS entry whose import is gone is a stale marker that no test carries."""
    assert set(FINDINGS) <= set(IMPORTS), sorted(set(FINDINGS) - set(IMPORTS))


@pytest.mark.parametrize('name', _params())
def test_every_import_is_declared_where_its_importers_run(name: str):
    providers = {_canonical(dist) for dist in _providers().get(name, [])}
    assert providers, f'no installed distribution provides {name!r}; importers: {IMPORTS[name]}'
    declared = _declaring_groups()
    groups = {group for dist in providers for group in declared.get(dist, set())}

    undeclared = [f'{path}:{line}' for path, line in IMPORTS[name] if not groups & _environment(path)[0]]
    assert not undeclared, (
        f'{name!r} (distribution {sorted(providers)}) is declared in {sorted(groups) or "no group"}, which these '
        f'importers do not install: {undeclared}'
    )

    if all(_environment(path)[1] for path, _ in IMPORTS[name]):
        assert 'testing' in groups and not groups & SERVICE_IMAGE_GROUPS, (
            f'{name!r} is imported only by tests, so it belongs in testing and in no service-image group; '
            f'it is declared in {sorted(groups)}'
        )
