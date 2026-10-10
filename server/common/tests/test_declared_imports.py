"""Every third-party package the repository's code imports is declared in a group that installs it.

tj-3mk3u5.37.15: data/ingest/app/brokers/alpaca/classify.py imported requests and
data/ingest/tests/test_alpaca_cut_connection.py imported urllib3, and neither was declared. requests
arrived through alpaca-py and urllib3 through requests. This repo declares what its code imports
rather than leaning on a transitive pin (pyproject.toml's data-store comment on sqlalchemy[asyncio]).
No other check enforces that: ruff does not, and a package installed transitively keeps every test
green until a re-lock or an upstream release drops it. Then the image fails when it imports the module.

One test per providing module, read from the source with ast, so an import inside a function body
counts too. Two rules:

1. Every file imports the module from a distribution declared in a group that its environment
   installs: the image or venv it runs in (ENVIRONMENTS below; the most specific path wins).
2. A module that only tests import is declared in testing and in no group an image installs.
   This is pyproject.toml's testing-group comment, which the architect applied to urllib3 on .37.15.
   The MCP image counts (tj-3mk3u5.59): it is an image like the services', so test-only packages
   stay out of agent-mcp too.

WHICH DISTRIBUTION PROVIDES AN IMPORT (tj-3mk3u5.59, the architect's finding on .37.15). Keying on
the top-level name is wrong for a namespace package. google has no __init__.py of its own (PEP 420),
and packages_distributions() maps it to EVERY distribution that ships something beneath it, so
declaring googleapis-common-protos would have satisfied an import of google.protobuf with protobuf
itself undeclared. So each import is keyed on the shallowest module on its dotted path that exactly
one installed distribution ships, read from the distributions' RECORD files: google.protobuf.message
keys on google.protobuf, which only protobuf ships. A regular package keys on its top-level name
exactly as before (yaml, starlette), so only namespaces change. The same rule passes a pkgutil-style
namespace, where several distributions each ship the shared __init__.py.

tools/ (tj-3mk3u5.59). tools/agent_mcp runs in the MCP image, which installs the agent-mcp group and
nothing else (tools/agent_mcp/Dockerfile), and copies only the server modules, never the tests. Those
modules are also imported by tools/agent_mcp/tests in the venv. They are mapped to the image, where
they run in production: the venv installs base as well, and base declares starlette and uvicorn, so a
venv mapping would let base satisfy the server and hide exactly the gap .58 fixed (both arrived in
the image only through mcp). The tests run in the venv (pytest.ini excludes only tests/system).
"""

import ast
import functools
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from importlib.metadata import distributions
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT, repo_relative, resolve_tree


pytestmark = pytest.mark.build_infra

# BOTH ROOTS, AND THAT IS NOT AN OVERSIGHT (tj-iontkq.2). REPO_ROOT is the true repository root and
# is right for everything this module reads by name -- pyproject.toml, uv.lock, Dockerfile,
# tools/agent_mcp/Dockerfile and the COPY globs under tools/, all of which stay at the top of the
# repository. SCANNED_ROOTS below is the other case: it is a tuple of repository-relative TREE names
# that spans both roots -- common/, routers/, schemas/ and data/ travel with the services, while
# gen/proto/python, tests/ and tools/ do not -- so those walks go through resolve_tree(), which
# hands each name the root that owns it, and their results are named with repo_relative().
PYPROJECT = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
DECLARED_GROUPS = PYPROJECT['dependency-groups']
MCP_DOCKERFILE = REPO_ROOT / 'tools' / 'agent_mcp' / 'Dockerfile'

# The repository's own top-level packages: ruff's isort list, plus the repo-root tests and tools packages.
FIRST_PARTY = frozenset(PYPROJECT['tool']['ruff']['lint']['isort']['known-first-party']) | {'tests', 'tools'}

# Dockerfile service_build_image: `uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME}`.
INGEST_IMAGE = frozenset({'base', 'data-ingest'})
STORE_IMAGE = frozenset({'base', 'data-store'})
# Dockerfile system_test_image, which runs tests/system.
SYSTEM_TEST_IMAGE = frozenset({'base', 'data-store', 'testing'})
# tools/agent_mcp/Dockerfile: `uv sync --only-group agent-mcp`.
MCP_IMAGE = frozenset({'agent-mcp'})
# The venv that make test and CI's test jobs run in. test_agent_mcp_groups pins those installs to this set.
TEST_VENV = frozenset(DECLARED_GROUPS) - {'security', 'agent-mcp'}
# Everything an image can hold, a service's dev stage and the MCP image included. A test-only package
# belongs in none of them.
IMAGE_GROUPS = frozenset({'base', 'dev', 'data-ingest', 'data-store', 'agent-mcp'})

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
    # The MCP image copies these two paths' modules and nothing else under tools/ (the test below pins
    # it). No catch-all for tools: a new tools tree has no environment until someone maps it.
    'tools/__init__.py': (MCP_IMAGE, False),
    'tools/agent_mcp': (MCP_IMAGE, False),
    'tools/agent_mcp/tests': (TEST_VENV, True),
    # The error-catalogue generator (tj-3mk3u5.37.10). It runs only in the venv, under `make errors-doc`
    # and the CI step beside it; NO image copies it -- the MCP image takes tools/__init__.py and
    # tools/agent_mcp/*.py and nothing else (the Dockerfile test below pins that both ways), and the
    # service Dockerfile copies no tools tree at all. Not a test, so is_test is False.
    'tools/errors_doc.py': (TEST_VENV, False),
    'tools/tests': (TEST_VENV, True),
    'common/tests': (TEST_VENV, True),
    'data/ingest/tests': (TEST_VENV, True),
    'data/store/tests': (TEST_VENV, True),
    'routers/tests': (TEST_VENV, True),
    'schemas/tests': (TEST_VENV, True),
}
SCANNED_ROOTS = ('common', 'routers', 'schemas', 'gen/proto/python', 'data', 'tests', 'tools')

# Import name -> its distribution, for a module whose distribution the test venv does not install, so
# no RECORD names it. The venv leaves agent-mcp out (test_agent_mcp_groups), and only the MCP image
# installs it. When the distribution is installed after all (semgrep pulls mcp in, once make init adds
# the security group), its RECORD is used instead and must agree.
NOT_IN_THE_TEST_VENV = {'mcp': 'mcp'}

# Undeclared imports the survey found, keyed like IMPORTS, each a strict xfail until its fix lands.
# Empty since tj-3mk3u5.58 declared protobuf, packaging and PyYAML, and since 3c53437 declared pandas for
# data/store/app/freshness.py (found by the validator gating tj-grna9p.17).
FINDINGS: dict[str, str] = {}


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
def _shipped_modules() -> dict[str, frozenset[str]]:
    """Dotted name of every module and package an installed distribution ships -> the distributions that ship it.

    From each distribution's RECORD. A package counts through its own __init__ only, so a PEP 420
    namespace such as google, which no distribution ships an __init__.py for, maps to nothing.
    """
    shipped: dict[str, set[str]] = defaultdict(set)
    for dist in distributions():
        name = _canonical(dist.name)
        for file in dist.files or ():
            *package, leaf = file.parts
            if not leaf.endswith(('.py', '.so', '.pyd')):
                continue
            stem = leaf.partition('.')[0]
            dotted = package if stem == '__init__' else [*package, stem]
            if dotted and all(part.isidentifier() for part in dotted):
                shipped['.'.join(dotted)].add(name)
    return {module: frozenset(dists) for module, dists in shipped.items()}


def _providing_module(name: str, shipped: Mapping[str, frozenset[str]]) -> str:
    """The module on name's dotted path that decides which distribution provides it.

    The shallowest one exactly one distribution ships. Failing that, the deepest one any distribution
    ships, and failing that the top-level name, so that the test reports nothing providing it.
    """
    parts = name.split('.')
    levels = ['.'.join(parts[:depth]) for depth in range(1, len(parts) + 1)]
    known = [level for level in levels if level in shipped]
    single = [level for level in known if len(shipped[level]) == 1]
    return (single or known[::-1] or levels)[0]


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
    return sorted(str(repo_relative(path)) for root in SCANNED_ROOTS for path in resolve_tree(root).rglob('*.py'))


def _third_party_imports() -> dict[str, list[tuple[str, int]]]:
    """Providing module of each third-party import -> every (file, line) that imports it.

    `from x import y` is read as x.y, because y may be a submodule (`from google import protobuf`); the
    shallowest-first rule means an attribute never deepens the key of a regular package.
    """
    found: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for relative in _scanned_files():
        path = resolve_tree(relative)
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=relative)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [f'{node.module}.{alias.name}' for alias in node.names]
            else:
                continue
            for name in names:
                top = name.partition('.')[0]
                if top not in sys.stdlib_module_names and top not in FIRST_PARTY:
                    found[_providing_module(name, _shipped_modules())].append((relative, node.lineno))
    return found


IMPORTS = _third_party_imports()


def _params() -> list:
    return [
        pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=FINDINGS[name])) if name in FINDINGS else name
        for name in sorted(IMPORTS)
    ]


def _importers(name: str) -> set[str]:
    return {path for path, _ in IMPORTS[name]}


def test_the_scan_reaches_the_importers_tj_3mk3u5_37_15_declared():
    """A scan of the wrong directories would pass on nothing. These two files are where the gap was found."""
    assert 'data/ingest/app/brokers/alpaca/classify.py' in _importers('requests')
    assert 'data/ingest/tests/test_alpaca_cut_connection.py' in _importers('urllib3')


def test_the_scan_reaches_the_importers_tj_3mk3u5_58_declared():
    """tools/ is scanned, and the generated code's google.protobuf import keys past the google namespace."""
    assert 'tools/agent_mcp/server.py' in _importers('starlette')
    assert 'tools/agent_mcp/server.py' in _importers('uvicorn')
    assert 'gen/proto/python/trader_joe/proto/ping/v1/ping_pb2.py' in _importers('google.protobuf')
    assert 'google' not in IMPORTS, IMPORTS.get('google')


def test_the_scan_reaches_the_trading_calendar_importer_tj_grna9p_17():
    """The freshness rules import the calendar and pandas directly; both are keyed, so both are checked."""
    assert 'data/store/app/freshness.py' in _importers('exchange_calendars')
    assert 'data/store/app/freshness.py' in _importers('pandas')


def test_a_namespace_import_resolves_to_the_distribution_that_ships_its_subpackage():
    """Not to every distribution sharing the top-level name, as packages_distributions() reports it."""
    shipped = {
        'google.protobuf': frozenset({'protobuf'}),
        'google.api': frozenset({'googleapis-common-protos'}),
        'yaml': frozenset({'pyyaml'}),
        # pkgutil style: both distributions ship the namespace's __init__.py.
        'legacy': frozenset({'legacy-a', 'legacy-b'}),
        'legacy.a': frozenset({'legacy-a'}),
    }
    assert _providing_module('google.protobuf.internal.builder', shipped) == 'google.protobuf'
    assert _providing_module('yaml.safe_load', shipped) == 'yaml'
    assert _providing_module('legacy.a.thing', shipped) == 'legacy.a'
    # A bare namespace import names no provider, and the declaration test then fails saying so.
    assert _providing_module('google', shipped) == 'google'
    # In this venv, too: only protobuf ships google.protobuf, whatever else lives under google.
    assert _shipped_modules()['google.protobuf'] == {'protobuf'}


def test_every_distribution_named_outside_the_venv_is_locked_and_agrees_when_installed():
    """NOT_IN_THE_TEST_VENV stands in for RECORD files the venv lacks, so it must name real distributions."""
    lock = tomllib.loads((REPO_ROOT / 'uv.lock').read_text(encoding='utf-8'))
    locked = {_canonical(package['name']) for package in lock['package']}
    assert set(NOT_IN_THE_TEST_VENV.values()) <= locked, sorted(set(NOT_IN_THE_TEST_VENV.values()) - locked)
    for name, dist in NOT_IN_THE_TEST_VENV.items():
        assert name in IMPORTS, f'{name!r} is no longer imported; drop it from NOT_IN_THE_TEST_VENV'
        assert _shipped_modules().get(name, frozenset({dist})) == {dist}, _shipped_modules()[name]


def test_test_code_and_only_test_code_resolves_to_a_test_environment():
    """Rule 2 depends on knowing which code is a test, so no test file may fall back to a production tree."""
    misfiled = [path for path in _scanned_files() if _in_a_test_directory(path) != _environment(path)[1]]
    assert not misfiled, f'files whose environment disagrees with whether they sit in a tests directory: {misfiled}'


def test_every_group_the_environments_name_is_declared():
    named = {group for groups, _ in ENVIRONMENTS.values() for group in groups} | IMAGE_GROUPS
    assert named <= set(DECLARED_GROUPS), sorted(named - set(DECLARED_GROUPS))


def test_the_image_environments_are_the_ones_the_dockerfile_installs():
    """INGEST_IMAGE, STORE_IMAGE and SYSTEM_TEST_IMAGE were read from these syncs. If one changes, re-read them."""
    lines = (REPO_ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()
    syncs = {line.removeprefix('RUN ').strip() for line in lines if line.startswith('RUN uv sync')}
    assert 'uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME} --frozen' in syncs, syncs
    assert 'uv sync --only-group base --only-group data-store --only-group testing --frozen' in syncs, syncs


def test_the_mcp_image_environment_is_the_one_its_dockerfile_installs():
    """MCP_IMAGE was read from this sync, the image's only one. If it changes, re-read it."""
    lines = MCP_DOCKERFILE.read_text(encoding='utf-8').splitlines()
    syncs = {line.removeprefix('RUN ').strip() for line in lines if line.startswith('RUN uv sync')}
    assert syncs == {'uv sync --only-group agent-mcp --frozen'}, syncs


def test_the_files_mapped_to_the_mcp_image_are_the_ones_it_copies():
    """The image takes tools/agent_mcp's server modules and not its tests. ENVIRONMENTS must agree, both ways."""
    lines = MCP_DOCKERFILE.read_text(encoding='utf-8').splitlines()
    sources = [source for line in lines if line.startswith('COPY tools/') for source in line.split()[1:-1]]
    copied = {str(path.relative_to(REPO_ROOT)) for source in sources for path in REPO_ROOT.glob(source)}
    mapped = {path for path in _scanned_files() if _environment(path)[0] == MCP_IMAGE}
    assert 'tools/agent_mcp/server.py' in mapped, sorted(mapped)
    assert copied == mapped, (
        f'copied, not mapped: {sorted(copied - mapped)}; mapped, not copied: {sorted(mapped - copied)}'
    )


def test_every_finding_is_still_imported():
    """A FINDINGS entry whose import is gone is a stale marker that no test carries."""
    assert set(FINDINGS) <= set(IMPORTS), sorted(set(FINDINGS) - set(IMPORTS))


@pytest.mark.parametrize('name', _params())
def test_every_import_is_declared_where_its_importers_run(name: str):
    providers = _shipped_modules().get(name) or {NOT_IN_THE_TEST_VENV.get(name)} - {None}
    assert providers, f'no installed distribution provides {name!r}; importers: {IMPORTS[name]}'
    declared = _declaring_groups()
    groups = {group for dist in providers for group in declared.get(dist, set())}

    undeclared = [f'{path}:{line}' for path, line in IMPORTS[name] if not groups & _environment(path)[0]]
    assert not undeclared, (
        f'{name!r} (distribution {sorted(providers)}) is declared in {sorted(groups) or "no group"}, which these '
        f'importers do not install: {undeclared}'
    )

    if all(_environment(path)[1] for path, _ in IMPORTS[name]):
        assert 'testing' in groups and not groups & IMAGE_GROUPS, (
            f'{name!r} is imported only by tests, so it belongs in testing and in no group an image installs; '
            f'it is declared in {sorted(groups)}'
        )
