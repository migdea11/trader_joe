"""Static invariants over the prod network model and the dev-only reach (tj-ijpys9.4).

The design is decision record tj-q9ae5u, addendum 1: items 1' (four prod networks, no service on
default), 2' (prod publishes nothing), 3' (dev reach over one external network, devnet, declared in
the dev override) and item 4 (a prod launch never attaches devnet, which is what keeps a dev session
off a prod stack on the same machine).

These read committed files only. What they deliberately do NOT cover, because each needs a Docker
daemon: that `internal: true` really means no egress and no publish, that compose merges the
override's networks onto the base file's by key, and that the devcontainer starts in either order.
CI's network lockdown step (tj-ijpys9.5) and the host sittings (tj-ijpys9.7, tj-ijpys9.8) are the
evidence for those. A green run here means the files still say the right thing, nothing more.

A separate module from test_ci_invariants.py so this task and tj-ijpys9.6 never edit one file.
"""

import json
import re
import shlex
from itertools import pairwise
from pathlib import Path

import pytest

from common.tests.compose_model import AGENT_MCP_FILE, interpolate
from common.tests.test_ci_invariants import (
    COMPOSE_FILE,
    ENV_DEFAULT_FILE,
    MAKEFILE,
    OVERRIDE_FILE,
    REPO_ROOT,
    SERVER_ROOT,
    _env_file_values,
    _load_yaml,
    _make_recipe,
    _make_variable,
)


pytestmark = pytest.mark.build_infra

TOOLS_FILE = REPO_ROOT / 'docker-compose.tools.yaml'
DEVCONTAINER_COMPOSE = REPO_ROOT / '.devcontainer' / 'compose.yml'
DEVCONTAINER_JSON = REPO_ROOT / '.devcontainer' / 'devcontainer.json'
# SERVER_ROOT: data/store travels with the service trees (tj-iontkq.4), unlike the compose and
# devcontainer files above, which stay at the top of the repository.
RUN_MIGRATIONS = SERVER_ROOT / 'data' / 'store' / 'run_migrations.sh'

# Addendum 1, item 1'. The whole model, stated once; every test below is judged against it.
INTERNAL_NETWORKS = frozenset({'store_db', 'ingest_store', 'store_api'})
EGRESS_NETWORKS = frozenset({'ingest_egress'})
STORE_API_NAME = 'trader_joe_store_api'
PROD_MEMBERSHIP = {
    'postgres': {'store_db'},
    'data_store': {'store_db', 'ingest_store', 'store_api'},
    'data_ingest': {'ingest_store', 'ingest_egress'},
}
# Item 1': ingest is the only component with internet access.
EGRESS_SERVICES = {'data_ingest'}

# The agent-stack MCP's route (ADR tj-4rr0la addendum 1 (c)): the devcontainer joins it; it never
# joins the proxy's network, which only agent_mcp and socket_proxy share.
MCP_NETWORK = 'trader_joe_agent_mcp'
PROXY_NETWORK = 'agent_mcp_docker'

# Item 3'. The dev network's key in the compose files, and the loopback publishes moved into the
# override verbatim from the base file.
DEVNET_KEY = 'devnet'
LOOPBACK = '127.0.0.1'
DEV_PUBLISHES = {
    'postgres': ['127.0.0.1:${DATABASE_PORT}:5432'],
    'data_store': ['127.0.0.1:${DATA_STORE_PORT}:${APP_INTERNAL_PORT}'],
    'data_ingest': ['127.0.0.1:${DATA_INGEST_PORT}:${APP_INTERNAL_PORT}'],
}

# Item 3': the targets that bring a dev-side container up, each of which needs devnet first.
DEV_NETWORK_TARGET = 'dev-network'
TARGETS_NEEDING_DEVNET = ('dev-deps', 'dev-tools', 'agent-up')


def _service_networks(service: dict) -> list[str]:
    """Return a service's network keys, in either compose form (list or mapping).

    A service with no `networks:` key is on the implicit default network, so that is what this
    returns for it -- an absent key must never read as "on no network".
    """
    networks = service.get('networks')
    if networks is None:
        return ['default']
    return list(networks)


def _all_compose_files() -> list[Path]:
    """Every compose file in the repository: the root family and the devcontainer's.

    Globbed rather than listed, so a compose file added later (the test client of tj-ijpys9.10)
    is judged by the publish rule without anyone remembering to add it here.
    """
    files = sorted(REPO_ROOT.glob('docker-compose*.yaml')) + sorted(REPO_ROOT.glob('docker-compose*.yml'))
    files.append(DEVCONTAINER_COMPOSE)
    assert COMPOSE_FILE in files and OVERRIDE_FILE in files and TOOLS_FILE in files, files
    return files


def _published_host_ips(entry: object) -> str:
    """Return the host interface a `ports:` entry binds, '' when it names none (all interfaces)."""
    if isinstance(entry, dict):
        return str(entry.get('host_ip', ''))
    text = str(entry)
    # Short syntax: [HOST_IP:][HOST_PORT:]CONTAINER_PORT. Only a three-part form carries an IP;
    # an IPv6 host ip is bracketed, and none is expected here.
    parts = text.split(':')
    return parts[0] if len(parts) >= 3 else ''


def _devcontainer_json() -> dict:
    """Parse devcontainer.json, which is JSONC: whole-line // comments are dropped first."""
    lines = DEVCONTAINER_JSON.read_text(encoding='utf-8').splitlines()
    return json.loads('\n'.join(line for line in lines if not line.lstrip().startswith('//')))


def _initialize_command_create(variable: str) -> tuple[str, str]:
    """(network name, the options of its `docker network create`) for one of the initializeCommand's networks.

    The command ensures two networks, devnet as `n` and the MCP's as `m`, each with its own create; the
    options are judged per network, so one network's --internal never reads as the other's (tj-c4mosr.5
    re-pin: the MCP network's create is --internal, devnet's must not be).
    """
    command = _devcontainer_json()['initializeCommand']
    match = re.search(rf'\b{variable}=([\w.-]+);', command)
    assert match is not None, f'initializeCommand sets no network name ({variable}=...): {command}'
    creates = re.findall(rf'docker network create((?:\s+--?[\w-]+(?:\s+\w+)?)*)\s+"\${variable}"', command)
    assert len(creates) == 1, f'initializeCommand creates "${variable}" {len(creates)} times: {command}'
    return match.group(1), creates[0]


def _initialize_command_network() -> str:
    """Return the dev network's name in the initializeCommand, created as an ordinary bridge."""
    name, options = _initialize_command_create('n')
    assert options.split() == ['--driver', 'bridge'], (
        f'the initializeCommand creates devnet with {options!r}; devnet must be an ordinary bridge: the dev '
        f'publishes go out through it'
    )
    return name


def _make_prerequisites(target: str) -> list[str]:
    """Return a Makefile target's prerequisites, as written (variables unexpanded)."""
    text = MAKEFILE.read_text(encoding='utf-8')
    match = re.search(rf'^{re.escape(target)}\s*:(?!=)([^#\n]*)', text, re.MULTILINE)
    assert match is not None, f'{MAKEFILE.name} defines no {target!r} target'
    return match.group(1).split()


def _compose_files_named(command: list[str]) -> list[str]:
    """Return the value of every -f / --file in a compose command line, in order."""
    files = []
    for flag, value in pairwise(command):
        if flag in ('-f', '--file'):
            files.append(value)
    return files


# --- 1'. the four prod networks --------------------------------------------------------------


def test_the_base_file_declares_exactly_the_four_prod_networks():
    """Item 1': three internal networks, one ordinary bridge, store_api under its fixed name."""
    networks = _load_yaml(COMPOSE_FILE).get('networks') or {}
    assert set(networks) == INTERNAL_NETWORKS | EGRESS_NETWORKS, (
        f'{COMPOSE_FILE.name} declares networks {sorted(networks)}; the model is '
        f"{sorted(INTERNAL_NETWORKS | EGRESS_NETWORKS)} (tj-q9ae5u addendum 1, item 1')."
    )
    for name in sorted(INTERNAL_NETWORKS):
        assert (networks[name] or {}).get('internal') is True, (
            f'{name} is not internal: true, so everything on it gains a gateway -- egress and the '
            f'ability to publish -- which is exactly what the internal networks exist to deny.'
        )
    for name in sorted(EGRESS_NETWORKS):
        config = networks[name] or {}
        assert not config.get('internal'), f'{name} is the egress network and must not be internal'
        assert not config.get('external'), f'{name} belongs to the stack and must not be external'
    # tj-c4mosr.5 re-pin (ADR tj-4rr0la section 1): the name now reads STORE_API_NETWORK so the agent
    # stack can take its own, with today's name as the default. What prod renders is pinned: with the
    # variable unset, and under every value the committed .env.default sets, it is still the fixed name.
    spelled = str((networks['store_api'] or {}).get('name'))
    for environment in ({}, _env_file_values(ENV_DEFAULT_FILE)):
        assert interpolate(spelled, environment) == STORE_API_NAME, (
            f'store_api renders as {interpolate(spelled, environment)!r} ({spelled!r}); it must carry the fixed '
            f'name {STORE_API_NAME}, so another compose project can declare it external and join it.'
        )
    assert spelled in (STORE_API_NAME, f'${{STORE_API_NETWORK:-{STORE_API_NAME}}}'), (
        f'store_api is named {spelled!r}: the literal, or STORE_API_NETWORK defaulting to it, and nothing else'
    )


# --- 1'. membership --------------------------------------------------------------------------


def test_each_prod_service_is_on_exactly_its_designed_networks():
    """Item 1', by equality: a network gained is a reach gained, so a superset is a failure too."""
    services = _load_yaml(COMPOSE_FILE)['services']
    actual = {name: set(_service_networks(service)) for name, service in services.items()}
    assert actual == PROD_MEMBERSHIP, (
        f"prod network membership drifted from tj-q9ae5u addendum 1, item 1'.\n"
        f'  expected {PROD_MEMBERSHIP}\n  found    {actual}'
    )


def test_only_data_ingest_sits_on_a_network_with_a_gateway_and_nothing_is_on_default():
    """Derived, so a service added later is judged too: only ingest may have internet access."""
    document = _load_yaml(COMPOSE_FILE)
    networks = document.get('networks') or {}
    ordinary = {name for name, config in networks.items() if not (config or {}).get('internal')}
    on_default = set()
    with_gateway = set()
    for name, service in document['services'].items():
        joined = set(_service_networks(service))
        if 'default' in joined:
            on_default.add(name)
        if joined & (ordinary | {'default'}):
            with_gateway.add(name)
    assert not on_default, (
        f'{sorted(on_default)} sit on the implicit default network (declared, or by omitting '
        f'networks:). default is an ordinary bridge: egress and a publish path for every member.'
    )
    assert with_gateway == EGRESS_SERVICES, (
        f'services on a non-internal network in {COMPOSE_FILE.name}: {sorted(with_gateway)}. '
        f'Only {sorted(EGRESS_SERVICES)} may be -- ingest is the only component with internet '
        f'access (tj-q9ae5u addendum 1).'
    )


# --- 2'. publishes ---------------------------------------------------------------------------


def test_the_prod_file_publishes_nothing():
    """Item 2': no service in docker-compose.yaml has ports:, empty or not."""
    services = _load_yaml(COMPOSE_FILE)['services']
    publishing = sorted(name for name, service in services.items() if 'ports' in service)
    assert not publishing, (
        f'{publishing} carry ports: in {COMPOSE_FILE.name}. Prod publishes nothing; a dev-only '
        f'loopback publish belongs in {OVERRIDE_FILE.name}, beside devnet.'
    )


@pytest.mark.parametrize('compose_file', _all_compose_files(), ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_every_published_port_in_every_compose_file_binds_loopback(compose_file: Path):
    """A published port bypasses the host firewall, so any publish anywhere is loopback only."""
    services = _load_yaml(compose_file).get('services') or {}
    for name, service in services.items():
        for entry in (service or {}).get('ports') or []:
            assert _published_host_ips(entry) == LOOPBACK, (
                f'{compose_file.name}: {name} publishes {entry!r}, which does not bind {LOOPBACK}. '
                f'Docker writes its own chain ahead of ufw/firewalld, so this is reachable from '
                f'every interface the binding names.'
            )


# --- 3'. the dev override --------------------------------------------------------------------


def test_the_override_declares_devnet_external_under_the_makefile_name():
    networks = _load_yaml(OVERRIDE_FILE).get('networks') or {}
    assert set(networks) == {DEVNET_KEY}, (
        f'{OVERRIDE_FILE.name} declares networks {sorted(networks)}; it adds devnet and nothing else.'
    )
    devnet = networks[DEVNET_KEY] or {}
    assert devnet.get('external') is True, (
        'devnet must be external: owned by neither compose project, so either side can start first '
        "and neither side's down removes it."
    )
    assert devnet.get('name') == _make_variable('DEV_NETWORK')


def test_the_override_attaches_every_stack_service_to_devnet_and_nothing_else():
    """Devnet ONLY per service: compose merges by key, so restating a prod network invites drift."""
    base_services = set(_load_yaml(COMPOSE_FILE)['services'])
    override_services = _load_yaml(OVERRIDE_FILE)['services']
    assert base_services <= set(override_services), (
        f'{sorted(base_services - set(override_services))} have no entry in {OVERRIDE_FILE.name}, '
        f'so a dev session cannot reach them over devnet.'
    )
    for name in sorted(base_services):
        networks = (override_services[name] or {}).get('networks')
        assert networks is not None and list(networks) == [DEVNET_KEY], (
            f'{OVERRIDE_FILE.name}: {name} lists networks {networks}; it must list devnet alone. '
            f'Its prod networks come from {COMPOSE_FILE.name} by the key merge.'
        )


def test_the_override_carries_exactly_the_three_loopback_publishes():
    services = _load_yaml(OVERRIDE_FILE)['services']
    actual = {name: list(service['ports']) for name, service in services.items() if 'ports' in (service or {})}
    assert actual == DEV_PUBLISHES, (
        f"{OVERRIDE_FILE.name} publishes {actual}; expected {DEV_PUBLISHES} -- the base file's "
        f'loopback publishes, moved verbatim.'
    )


def test_the_override_parses_as_plain_yaml():
    """No !reset / !override: yaml.safe_load refuses an unknown tag, and the invariants use it."""
    document = _load_yaml(OVERRIDE_FILE)
    assert isinstance(document, dict) and 'services' in document


def test_pgadmin_joins_devnet():
    """Postgres is on store_db and devnet only, so devnet is pgAdmin's only route to it."""
    pgadmin = _load_yaml(TOOLS_FILE)['services']['pgadmin']
    assert DEVNET_KEY in _service_networks(pgadmin), (
        f'pgadmin in {TOOLS_FILE.name} is on {_service_networks(pgadmin)}, not devnet, so it cannot reach postgres.'
    )


# --- 3'. one name in four places -------------------------------------------------------------


def test_the_four_places_naming_devnet_agree():
    """The override, the Makefile variable, the devcontainer compose and the initializeCommand."""
    devcontainer_networks = _load_yaml(DEVCONTAINER_COMPOSE).get('networks') or {}
    devcontainer_devnet = devcontainer_networks.get(DEVNET_KEY) or {}
    assert devcontainer_devnet.get('external') is True, (
        f'{DEVCONTAINER_COMPOSE} must declare devnet external: the stack does not own it either.'
    )
    names = {
        'Makefile DEV_NETWORK': _make_variable('DEV_NETWORK'),
        OVERRIDE_FILE.name: ((_load_yaml(OVERRIDE_FILE).get('networks') or {}).get(DEVNET_KEY) or {}).get('name'),
        '.devcontainer/compose.yml': devcontainer_devnet.get('name'),
        'devcontainer.json initializeCommand': _initialize_command_network(),
    }
    assert len(set(names.values())) == 1, (
        f'the dev network is named differently in different places, so one side creates or joins a '
        f'network the other never sees: {names}'
    )


def test_the_agent_devcontainer_joins_default_devnet_and_the_mcp_network_only():
    """Re-pinned (tj-c4mosr.5; ADR tj-4rr0la addendum 1 (c)/(f)): exactly default, devnet, trader_joe_agent_mcp.

    Was test_the_agent_devcontainer_joins_default_and_devnet. The MCP network is the devcontainer's one
    route to Docker; agent_mcp_docker, the socket proxy's network, it never joins.
    """
    agent = _load_yaml(DEVCONTAINER_COMPOSE)['services']['agent']
    joined = set(_service_networks(agent))
    assert PROXY_NETWORK not in joined, f"the devcontainer joins {PROXY_NETWORK}, the socket proxy's network"
    assert joined == {'default', DEVNET_KEY, MCP_NETWORK}, (
        f'the agent service is on {sorted(joined)}: default keeps its own egress, devnet is its reach into the '
        f'dev stack and {MCP_NETWORK} its route to the agent-stack MCP; anything else is reach nobody designed.'
    )


def test_the_four_places_naming_the_mcp_network_agree_and_it_is_internal():
    """The Makefile, docker-compose.agent-mcp.yaml, the devcontainer compose and the initializeCommand.

    Both creators make it --internal: only the devcontainer and agent_mcp join it, and neither needs
    a gateway through it (ADR tj-4rr0la addendum 1 (c)).
    """
    devcontainer = (_load_yaml(DEVCONTAINER_COMPOSE).get('networks') or {}).get(MCP_NETWORK) or {}
    mcp_file = (_load_yaml(AGENT_MCP_FILE).get('networks') or {}).get(MCP_NETWORK) or {}
    assert devcontainer.get('external') is True and mcp_file.get('external') is True
    name, options = _initialize_command_create('m')
    names = {
        'Makefile AGENT_MCP_NETWORK': _make_variable('AGENT_MCP_NETWORK'),
        AGENT_MCP_FILE.name: mcp_file.get('name'),
        '.devcontainer/compose.yml': devcontainer.get('name'),
        'devcontainer.json initializeCommand': name,
    }
    assert set(names.values()) == {MCP_NETWORK}, f'the MCP network is named differently in different places: {names}'
    assert options.split() == ['--driver', 'bridge', '--internal'], f'the initializeCommand creates it with {options!r}'
    recipe = ' '.join(_make_recipe('agent-mcp-network'))
    assert 'docker network create --driver bridge --internal $(AGENT_MCP_NETWORK)' in recipe, recipe


# --- item 4. prod never loads what attaches devnet -------------------------------------------


def test_prod_compose_names_the_base_file_alone():
    """Extends test_prod_compose_command_does_not_load_the_dev_override: no second file at all.

    That test forbids the override; this one forbids every other -f too (the tools file, the test
    client of tj-ijpys9.10), because any extra file can attach devnet or a publish to prod.
    """
    prod_compose = shlex.split(_make_variable('PROD_COMPOSE'))
    assert _compose_files_named(prod_compose) == [COMPOSE_FILE.name], (
        f'PROD_COMPOSE is {prod_compose}; it must load {COMPOSE_FILE.name} and nothing else.'
    )


def test_run_migrations_names_the_base_file_alone():
    text = RUN_MIGRATIONS.read_text(encoding='utf-8')
    match = re.search(r'^COMPOSE=\((.*)\)\s*$', text, re.MULTILINE)
    assert match is not None, f'{RUN_MIGRATIONS.name} defines no COMPOSE=(...) array'
    assert _compose_files_named(shlex.split(match.group(1))) == [COMPOSE_FILE.name], (
        f'{RUN_MIGRATIONS.name} runs compose as ({match.group(1)}); migrations run against the prod '
        f'data_store definition, {COMPOSE_FILE.name} alone.'
    )


# --- 3'. the Makefile's dev-network lifecycle ------------------------------------------------


@pytest.mark.parametrize('target', TARGETS_NEEDING_DEVNET)
def test_dev_side_targets_create_devnet_first(target: str):
    assert DEV_NETWORK_TARGET in _make_prerequisites(target), (
        f'`make {target}` does not depend on {DEV_NETWORK_TARGET}, so on a fresh host it starts a '
        f'container that joins an external network nobody has created, and compose refuses.'
    )


def test_dev_network_creates_an_ordinary_bridge_under_the_variable():
    recipe = ' '.join(_make_recipe(DEV_NETWORK_TARGET))
    assert 'docker network create --driver bridge $(DEV_NETWORK)' in recipe, recipe
    assert '--internal' not in recipe, 'devnet must be ordinary: the dev loopback publishes go out through it'


def test_no_recipe_removes_the_dev_network():
    """Devnet outlives every down: either side may still be using it."""
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    recipe_lines = [line.strip() for line in text.splitlines() if line.startswith('\t')]
    removing = [line for line in recipe_lines if re.search(r'\bnetwork\s+(rm|remove|prune)\b|\bsystem\s+prune\b', line)]
    assert not removing, f'these Makefile recipe lines remove docker networks: {removing}'
