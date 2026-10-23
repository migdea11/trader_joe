"""build_infra pins on the base compose file and the merged-model reader that outlive the agent stack.

Moved from test_agent_stack_compose.py when the agent stack was retired (tj-iee6sn.21; ADR tj-4rr0la
addendum 26). That module pinned the agent-stack overlay, which B12b (tj-iee6sn.27) deletes, but some of
its pins protected OTHER files: the base file's env_file lists that prod has always loaded, the MCP's own
compose file staying out of every Makefile set and out of run_migrations.sh, no committed file naming a
compose project, and compose_model's refusal of what it does not model. Those are kept here, re-pointed at
the sets that remain; the overlay-only pins went with the overlay.
"""

import pytest

from common.tests.compose_model import (
    AGENT_MCP_FILE,
    BASE_FILE,
    FAKE_FILE,
    TEST_CLIENT_FILE,
    NotModelled,
    client_model,
    interpolate,
    load,
    merge,
    prod_model,
    system_model,
)
from common.tests.test_ci_invariants import (
    MIGRATIONS_SCRIPT,
    REPO_ROOT,
    SERVER_ROOT,
    _expanded_make_variable,
    _subprocess_env,
)


pytestmark = pytest.mark.build_infra

# The Makefile's compose sets: none may load the MCP's own compose file (ADR tj-4rr0la 5(b)).
COMPOSE_VARIABLES = ('PROD_COMPOSE', 'DEV_COMPOSE', 'TOOLS_COMPOSE', 'TEST_CLIENT_COMPOSE', 'AGENT_COMPOSE')
ENV_FILE_VARIABLES = ('ROOT_ENV_FILE', 'STORE_ENV_FILE', 'INGEST_ENV_FILE')
# Today's env_file lists with the three variables unset, in order: root first, then the service
# file -- the order decides which value wins in the container (ADR F1). Spelled out, not derived.
PROD_ENV_FILES = {
    'postgres': ['.env', './server/data/store/.env'],
    'data_store': ['.env', './server/data/store/.env'],
    'data_ingest': ['.env', './server/data/ingest/.env'],
}
ENV_DEFAULTS = (
    REPO_ROOT / '.env.default',
    SERVER_ROOT / 'data' / 'store' / '.env.default',
    SERVER_ROOT / 'data' / 'ingest' / '.env.default',
)


@pytest.mark.parametrize('variable', COMPOSE_VARIABLES)
def test_no_makefile_compose_set_loads_the_mcp_file(variable: str):
    """The MCP's own file (docker-compose.agent-mcp.yaml) is loaded by its own targets only."""
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    assert AGENT_MCP_FILE.name not in expanded, f'{variable} ({expanded}) loads {AGENT_MCP_FILE.name}'


def test_run_migrations_never_names_the_mcp_file():
    assert AGENT_MCP_FILE.name not in MIGRATIONS_SCRIPT.read_text(encoding='utf-8')


def test_no_committed_compose_or_env_default_names_a_project():
    """A top-level name: or COMPOSE_PROJECT_NAME would move every make target off the checkout's project."""
    for path in (BASE_FILE, TEST_CLIENT_FILE, FAKE_FILE):
        assert 'name' not in load(path), f'{path.name} sets a top-level project name'
    for path in ENV_DEFAULTS:
        assert 'COMPOSE_PROJECT_NAME' not in path.read_text(encoding='utf-8'), path


def test_the_base_env_files_resolve_to_todays_lists_with_the_variables_unset():
    """Addendum-2 pins (1) and (1a): same services, same entries, SAME ORDER, plain short-form strings."""
    actual = {}
    for name, spec in load(BASE_FILE)['services'].items():
        entries = spec.get('env_file')
        if entries is None:
            continue
        assert isinstance(entries, list) and all(isinstance(entry, str) for entry in entries), (
            f'{name}: env_file must stay short form, plain strings (a missing prod env file still stops compose)'
        )
        actual[name] = [interpolate(entry, {}) for entry in entries]
    assert actual == PROD_ENV_FILES, f'the base env_file lists drifted from what prod has always loaded: {actual}'


def test_each_base_env_file_entry_reads_its_own_variable_with_todays_path_as_default():
    services = load(BASE_FILE)['services']
    service_variable = {'postgres': 'STORE_ENV_FILE', 'data_store': 'STORE_ENV_FILE', 'data_ingest': 'INGEST_ENV_FILE'}
    for name, expected in PROD_ENV_FILES.items():
        spelled = services[name]['env_file']
        assert spelled == [f'${{ROOT_ENV_FILE:-{expected[0]}}}', f'${{{service_variable[name]}:-{expected[1]}}}'], (
            name,
            spelled,
        )


def test_no_committed_env_default_redirects_an_env_file():
    """A committed default copied to .env must not move prod's env files or its store_api network."""
    for path in ENV_DEFAULTS:
        keys = {
            line.split('=', 1)[0].strip()
            for line in path.read_text(encoding='utf-8').splitlines()
            if '=' in line and not line.lstrip().startswith('#')
        }
        leaked = keys & {'STORE_API_NETWORK', *ENV_FILE_VARIABLES}
        assert not leaked, f'{path.relative_to(REPO_ROOT)} sets {leaked}'


@pytest.mark.parametrize(
    'document',
    [
        {'include': ['other.yaml'], 'services': {}},
        {'services': {'data_ingest': {'extends': {'service': 'data_store'}}}},
        {'services': {'data_ingest': {'profiles': ['x']}}},
    ],
    ids=['include', 'extends', 'profiles'],
)
def test_merge_refuses_include_extends_and_profiles(document: dict):
    """Each changes what compose renders; copied through, the model would disagree with compose silently."""
    with pytest.raises(NotModelled):
        merge([load(BASE_FILE), document])


def test_every_merged_model_is_built_from_files_the_model_can_read():
    """The models the pins read load without refusal: no committed file uses include, extends or profiles."""
    for model in (prod_model, client_model, system_model):
        assert model()['services'], model.__name__
