"""build_infra pins for the agent-stack MCP's container, its socket proxy, the devcontainer route and .mcp.json.

tj-c4mosr.5: body bullets 5-7 (the devcontainer has no socket or CLI; docker.sock is mounted by exactly
one service, read-only; agent_mcp publishes nothing and joins its two internal networks), 01:43 (e),
addendum 11's M1-M3 (as amended by P4), and the validator's P1 and P2 with the architect's (b) final
shape (06:28). Design: ADR tj-4rr0la addendum 1 (b)-(f), addenda 11 and 12 (F4).

Only what the committed files say is pinned here. That the proxy really starts under cap_drop ALL,
that the section list is enough for the verbs, and that the devcontainer reaches agent_mcp by name
are the host sitting's (tj-c4mosr.6 H0 (u), H1-H3).
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from common.tests.compose_model import (
    AGENT_MCP_FILE,
    DEVCONTAINER_COMPOSE,
    DEVCONTAINER_JSON,
    REPO_ROOT,
    interpolate,
    load,
    service_networks,
    volume,
)
from common.tests.test_ci_invariants import SYSTEM_SECRET_KEYS, _every_compose_file
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

SOCKET = '/var/run/docker.sock'
MCP_NETWORK = 'trader_joe_agent_mcp'
PROXY_NETWORK = 'agent_mcp_docker'
SHARE_TARGET = '/agent_mcp_share'
WORKSPACE = '/workspace'
MCP_INIT = REPO_ROOT / 'tools' / 'agent_mcp' / '__init__.py'
MCP_JSON = REPO_ROOT / '.mcp.json'
# Proxy settings that are not API sections (docker-socket-proxy's own switches).
PROXY_NON_SECTION_SETTINGS = {'DISABLE_IPV6': 1, 'ALLOW_START': 0, 'ALLOW_STOP': 0, 'ALLOW_RESTARTS': 0}


def _services() -> dict:
    return load(AGENT_MCP_FILE)['services']


def devcontainer_json() -> dict:
    """devcontainer.json is JSONC: whole-line // comments are dropped before parsing."""
    lines = DEVCONTAINER_JSON.read_text(encoding='utf-8').splitlines()
    return json.loads('\n'.join(line for line in lines if not line.lstrip().startswith('//')))


# --- the socket: one service, read-only --------------------------------------------------------


def _mentions_socket(value: object) -> bool:
    return 'docker.sock' in json.dumps(value)


def test_docker_sock_is_mounted_by_exactly_one_service_in_the_repository():
    """Body bullet 6 / addendum 1 (f): socket_proxy in docker-compose.agent-mcp.yaml, and nothing else anywhere.

    Every compose file (base, override, tools, test-client, agent-stack, agent-mcp, devcontainer), every
    service, every key -- volumes, environment (DOCKER_HOST=unix://...), devices, anything.
    """
    files = _every_compose_file()
    names = {path.name for path in files}
    assert {'docker-compose.agent-mcp.yaml', 'docker-compose.agent-stack.yaml', 'compose.yml'} <= names, names
    holders = [
        (path.relative_to(REPO_ROOT).as_posix(), name)
        for path in files
        for name, spec in (load(path).get('services') or {}).items()
        if _mentions_socket(spec)
    ]
    assert holders == [('docker-compose.agent-mcp.yaml', 'socket_proxy')], f'services reaching docker.sock: {holders}'


def test_the_proxy_mounts_the_socket_file_read_only():
    """The body's ':ro' pin: it guards the socket FILE, not the API.

    ':ro' stops the proxy chmod-ing or replacing the socket file. It does NOT narrow the API,
    which a connection through a read-only mount still reaches in full; P1's section list is what
    narrows the API (ADR tj-4rr0la addendum 12, F4).
    """
    mounts = [entry for entry in _services()['socket_proxy']['volumes'] if _mentions_socket(entry)]
    assert mounts == [f'{SOCKET}:{SOCKET}:ro'], (
        f'socket_proxy mounts {mounts}: the socket file must be mounted read-only, so the proxy cannot chmod or replace it'
    )


# --- P1: the proxy's section list is the one the server's docstring names ------------------------


def _docstring_sections() -> tuple[set[str], set[str]]:
    """(needed, not needed) from tools/agent_mcp/__init__.py's 'DOCKER API SECTIONS THE VERBS NEED'."""
    text = MCP_INIT.read_text(encoding='utf-8')
    block = text.split('DOCKER API SECTIONS THE VERBS NEED', 1)[1]
    needed_block, not_block = block.split('NOT needed, keep off:', 1)
    needed = set()
    for line in needed_block.splitlines()[1:]:
        if not line.startswith('    ') or not line.strip():
            continue
        head = re.split(r'\s{2,}', line.strip())[0]
        needed |= {word.strip().removesuffix('=1') for word in head.split(',')}
    not_needed = {word.strip() for word in not_block.split('.', 1)[0].replace('\n', ' ').split(',')}
    assert needed and not_needed and not needed & not_needed, (needed, not_needed)
    return needed, not_needed


def test_the_proxy_allows_exactly_the_sections_the_verbs_need():
    """P1: 1 for exactly the docstring's NEED list, 0 for every section it names NOT needed."""
    needed, not_needed = _docstring_sections()
    assert needed == {
        'PING',
        'VERSION',
        'INFO',
        'CONTAINERS',
        'IMAGES',
        'NETWORKS',
        'VOLUMES',
        'BUILD',
        'SESSION',
        'GRPC',
        'EVENTS',
        'POST',
    }
    environment = {str(key): value for key, value in _services()['socket_proxy']['environment'].items()}
    sections = {key: value for key, value in environment.items() if key not in PROXY_NON_SECTION_SETTINGS}
    assert {key for key, value in sections.items() if value == 1} == needed, f'sections allowed: {sections}'
    assert {key for key, value in sections.items() if value == 0} == not_needed, f'sections refused: {sections}'
    assert set(sections.values()) <= {0, 1}
    for key, value in PROXY_NON_SECTION_SETTINGS.items():
        assert environment.get(key) == value, (key, environment.get(key))


def test_the_proxy_image_is_pinned_by_digest_and_joins_only_its_network():
    proxy = _services()['socket_proxy']
    assert re.fullmatch(r'tecnativa/docker-socket-proxy:[\w.]+@sha256:[0-9a-f]{64}', proxy['image']), proxy['image']
    assert service_networks(proxy) == [PROXY_NETWORK]


# --- P2 and (b): hardening -------------------------------------------------------------------------


def test_the_proxy_runs_with_no_capabilities_and_no_escalation():
    """(b), final shape: no-new-privileges, cap_drop [ALL], NO cap_add key at all, no user:, no privileged:."""
    proxy = _services()['socket_proxy']
    assert proxy.get('security_opt') == ['no-new-privileges:true']
    assert proxy.get('cap_drop') == ['ALL']
    assert 'cap_add' not in proxy, (
        f'socket_proxy adds capabilities back: {proxy.get("cap_add")} (addendum 12 F4 cited none)'
    )
    assert 'user' not in proxy and 'privileged' not in proxy
    assert 'ports' not in proxy and 'env_file' not in proxy


def test_agent_mcp_is_hardened_and_healthchecked_on_the_token_gate():
    """P2: cap_drop [ALL], no-new-privileges, no ports, no env_file, and a healthcheck expecting 401."""
    mcp = _services()['agent_mcp']
    assert mcp.get('cap_drop') == ['ALL'] and mcp.get('security_opt') == ['no-new-privileges:true']
    assert 'ports' not in mcp, 'agent_mcp publishes a port: reach is the internal network, nothing else'
    assert 'env_file' not in mcp and 'cap_add' not in mcp and 'privileged' not in mcp
    check = ' '.join(mcp['healthcheck']['test'])
    assert '401' in check and '127.0.0.1:8765/mcp' in check, check


# --- body bullet 7: agent_mcp's reach ------------------------------------------------------------


def test_agent_mcp_joins_only_its_two_internal_networks():
    """Neither devnet nor a stack network.

    agent_mcp_docker is internal here; trader_joe_agent_mcp is external, and created --internal by both of its creators (pinned in test_agent_mcp_make.py).
    """
    document = load(AGENT_MCP_FILE)
    assert set(service_networks(document['services']['agent_mcp'])) == {PROXY_NETWORK, MCP_NETWORK}
    networks = document['networks']
    assert networks[PROXY_NETWORK] == {'internal': True}
    assert networks[MCP_NETWORK] == {'external': True, 'name': MCP_NETWORK}
    assert set(networks) == {PROXY_NETWORK, MCP_NETWORK}
    assert document.get('name') == 'trader_joe_agent_mcp'


def test_the_mcp_file_loads_no_env_file_and_every_value_is_guarded():
    """Nothing reads the user's .env: no env_file, and every interpolated value is ':?' (the Makefile passes --env-file /dev/null)."""
    text = '\n'.join(
        line for line in AGENT_MCP_FILE.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('#')
    )
    assert 'env_file' not in text
    expressions = re.findall(r'\$\{([^}]*)\}', text)
    assert expressions and all(re.fullmatch(r'\w+:\?.+', expression) for expression in expressions), expressions


# --- M1: the repository at /workspace, read-only, agreeing with the devcontainer --------------------


def _mcp_mounts() -> list[dict]:
    return [volume(entry) for entry in _services()['agent_mcp']['volumes']]


def test_agent_mcp_sees_the_repository_read_only_at_the_devcontainers_workspace():
    """M1, addendum 11 R1: /workspace in four places that must agree."""
    mcp = _services()['agent_mcp']
    repo_mounts = [mount for mount in _mcp_mounts() if 'AGENT_MCP_REPO_HOST_PATH' in mount['source']]
    assert len(repo_mounts) == 1, _mcp_mounts()
    assert repo_mounts[0]['target'] == WORKSPACE and repo_mounts[0]['read_only'] is True, repo_mounts
    assert mcp['environment']['AGENT_MCP_REPO_ROOT'] == WORKSPACE
    agent = load(DEVCONTAINER_COMPOSE)['services']['agent']
    project_mounts = [volume(entry) for entry in agent['volumes'] if volume(entry)['source'] == '..']
    assert [mount['target'] for mount in project_mounts] == [WORKSPACE]
    assert agent['working_dir'] == WORKSPACE
    assert devcontainer_json()['workspaceFolder'] == WORKSPACE


def test_agent_mcp_mounts_exactly_three_paths_and_never_the_claude_config():
    """M2, addendum 11 R2: repo ro at /workspace, the stack dir at its own path, the share at /agent_mcp_share."""
    mounts = _mcp_mounts()
    shape = sorted((re.findall(r'\$\{(\w+)', mount['source']), mount['target'], mount['read_only']) for mount in mounts)
    assert shape == sorted(
        [
            (['AGENT_MCP_REPO_HOST_PATH'], WORKSPACE, True),
            (['AGENT_MCP_STACK_DIR'], '${AGENT_MCP_STACK_DIR:?set by make agent-mcp-up}', False),
            (['AGENT_MCP_SHARE_PATH'], SHARE_TARGET, False),
        ]
    ), mounts
    stack_mount = next(mount for mount in mounts if 'AGENT_MCP_STACK_DIR' in mount['source'])
    assert stack_mount['source'] == stack_mount['target'], 'the stack dir must be mounted at its own host path'
    mcp = _services()['agent_mcp']
    for value in [mount['source'] for mount in mounts] + [str(value) for value in mcp['environment'].values()]:
        assert 'AGENT_HOME_PATH' not in value and '.claude' not in value, (
            f'agent_mcp reaches the Claude config: {value}'
        )


# --- M3 / P4: the share directory agrees in every place --------------------------------------------


def test_both_containers_bind_the_share_at_the_same_path_and_the_server_is_pointed_at_it():
    agent = load(DEVCONTAINER_COMPOSE)['services']['agent']
    share = [volume(entry) for entry in agent['volumes'] if 'AGENT_MCP_SHARE_PATH' in str(entry)]
    assert [mount['target'] for mount in share] == [SHARE_TARGET]
    assert share[0]['source'] == '${AGENT_MCP_SHARE_PATH:-${XDG_RUNTIME_DIR}/trader_joe_agent_mcp}'
    assert (
        interpolate(share[0]['source'], {'XDG_RUNTIME_DIR': '/run/user/1000'}) == '/run/user/1000/trader_joe_agent_mcp'
    )
    assert interpolate(share[0]['source'], {'XDG_RUNTIME_DIR': '/x', 'AGENT_MCP_SHARE_PATH': '/chosen'}) == '/chosen'
    assert _services()['agent_mcp']['environment']['AGENT_HOME_PATH'] == SHARE_TARGET


def _headers_helper() -> str:
    return json.loads(MCP_JSON.read_text(encoding='utf-8'))['mcpServers']['agent-stack']['headersHelper']


def test_mcp_json_dials_agent_mcp_and_reads_the_token_from_the_share():
    server = json.loads(MCP_JSON.read_text(encoding='utf-8'))['mcpServers']['agent-stack']
    assert server['type'] == 'http' and server['url'] == 'http://agent_mcp:8765/mcp'
    assert f'{SHARE_TARGET}/agent_mcp_token' in _headers_helper()


@pytest.mark.parametrize('present', [True, False], ids=['token-present', 'token-absent'])
def test_the_headers_helper_emits_the_bearer_header_or_fails(tmp_path: Path, present: bool):
    """M3: run for real, with /agent_mcp_share pointed at a tmp directory."""
    token = 'T' * 43
    if present:
        (tmp_path / 'agent_mcp_token').write_text(token + '\n')
    command = _headers_helper().replace(SHARE_TARGET, str(tmp_path))
    result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, check=False)
    if present:
        assert result.returncode == 0 and json.loads(result.stdout) == {'Authorization': f'Bearer {token}'}
    else:
        assert result.returncode != 0 and token not in result.stdout


# --- the devcontainer: no socket, no docker, the MCP network and never the proxy's ----------------


# A docker CLI or engine package, an install script, a docker invocation, the docker group, or a
# devcontainer feature that brings docker in (docker-outside-of-docker, docker-in-docker).
_DOCKER_IN_CONTAINER = re.compile(
    r'\bdocker(?:\.io|-ce(?:-cli)?|-compose(?:-plugin)?|-buildx(?:-plugin)?|-cli)\b|\bmoby-|download\.docker\.com'
    r'|get\.docker\.com|\bdocker\s+[a-z]|\b(?:groupadd|usermod|adduser|gpasswd)\b[^\n]*\bdocker\b|-G\s*docker\b|group_add'
    r'|docker-(?:outside|in)-of-docker',
    re.IGNORECASE,
)

# devcontainer.json's top-level keys, exactly. Every key but initializeCommand configures the CONTAINER,
# and several can give it Docker in one line: features (docker-outside-of-docker mounts the socket and
# installs the CLI), postCreateCommand / postStartCommand / onCreateCommand / updateContentCommand
# (run an installer inside), privileged, capAdd, securityOpt, mounts, runArgs.
DEVCONTAINER_JSON_KEYS = ['name', 'dockerComposeFile', 'service', 'workspaceFolder', 'remoteUser', 'initializeCommand']
# The one key that runs on the HOST: it creates the networks and runs make agent-mcp-up, so it names docker.
HOST_SIDE_KEY = 'initializeCommand'

# The devcontainer compose service's keys, exactly. privileged, cap_add, group_add, devices, pid,
# userns_mode, security_opt, volumes_from and the like would each hand the agent the host or its daemon.
DEVCONTAINER_AGENT_KEYS = ['build', 'working_dir', 'volumes', 'environment', 'command', 'networks']

# THE AGENT SERVICE'S environment: BLOCK, EXACTLY -- every name it is allowed to carry (bead
# tj-ix1hbl, R1; decision record tj-izzqub clause 3). DEVCONTAINER_AGENT_KEYS above stops one layer
# ABOVE this, at the service's own keys, which is why adding a NAMED key here was green until this
# set existed. Compared as a set, not a list: reordering the file changes nothing about what the
# container holds, while an added or removed name is the whole point.
DEVCONTAINER_AGENT_ENVIRONMENT_KEYS = frozenset(
    {
        'CLAUDE_CONFIG_DIR',
        'BD_DISABLE_METRICS',
        'GIT_AUTHOR_NAME',
        'GIT_AUTHOR_EMAIL',
        'GIT_COMMITTER_NAME',
        'GIT_COMMITTER_EMAIL',
        'POSTGRES_ASYNC',
        'POSTGRES_SYNC',
        'UV_PROJECT_ENVIRONMENT',
        'UV_FROZEN',
        *SYSTEM_SECRET_KEYS,
    }
)
# The sentinel both credentials fall back to. Pinned as a literal because it is the string that
# turns up in whatever authentication error a missing credential goes on to cause: it must stay
# non-empty, and must stay unmistakable for a credential.
AGENT_CREDENTIAL_SENTINEL = 'UNSET-start-with-make-agent-up-from-the-repo-root'
# The broker family, from the one definition the repository already has (tools/agent_mcp/stack.py:231),
# never respelled here -- so a third broker key added THERE is refused HERE automatically. The vendor
# prefixes are derived from those same names, so ALPACA_ANYTHING is refused without naming 'ALPACA'.
BROKER_FAMILY = frozenset(stack.BROKER_CREDENTIALS)
BROKER_PREFIXES = tuple(sorted({f'{name.partition("_")[0]}_' for name in stack.BROKER_CREDENTIALS}))
# A value that is nothing but an interpolation of ONE named variable with a literal default.
_PURE_INTERPOLATION = re.compile(r'\$\{(\w+):-([^${}]+)\}')


def test_the_devcontainer_holds_no_socket_no_docker_cli_and_no_docker_group():
    """Body bullet 5 and 01:43 (e): nothing under .devcontainer/ mounts the socket or installs docker.

    devcontainer.json's initializeCommand runs on the HOST (it creates the networks and starts the
    MCP there), so it is the one value that may name docker; every other devcontainer.json value is
    scanned below (test_no_devcontainer_json_value_but_the_host_side_one_brings_in_docker).
    """
    directory = REPO_ROOT / '.devcontainer'
    for path in sorted(directory.iterdir()):
        text = path.read_text(encoding='utf-8')
        assert 'docker.sock' not in text, f'{path.name} names docker.sock'
        if path.name == 'devcontainer.json':
            continue
        code = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        docker = [line for line in code if _DOCKER_IN_CONTAINER.search(line)]
        assert not docker, f'{path.name} installs, groups or runs docker: {docker}'
    agent = load(DEVCONTAINER_COMPOSE)['services']['agent']
    assert 'group_add' not in agent and 'privileged' not in agent and 'devices' not in agent
    assert 'cap_add' not in agent and 'security_opt' not in agent


def test_devcontainer_json_has_exactly_todays_top_level_keys():
    """Architect gate R1 on 6b4ef02: a new key is how the devcontainer would be given Docker."""
    keys = list(devcontainer_json())
    assert keys == DEVCONTAINER_JSON_KEYS, (
        f'devcontainer.json keys changed to {keys}: a new key needs an architect ruling, because features, '
        'lifecycle commands, privileged, capAdd, mounts and runArgs can each give the devcontainer Docker '
        '(ADR tj-4rr0la addendum 1 (f))'
    )


def test_no_devcontainer_json_value_but_the_host_side_one_brings_in_docker():
    """Architect gate R1 on 6b4ef02: _DOCKER_IN_CONTAINER over every value except initializeCommand, keys included."""
    document = devcontainer_json()
    assert HOST_SIDE_KEY in document
    offenders = {
        key: value
        for key, value in document.items()
        if key != HOST_SIDE_KEY and _DOCKER_IN_CONTAINER.search(json.dumps({key: value}))
    }
    assert not offenders, f'devcontainer.json configures docker into the container: {offenders}'


def test_the_devcontainer_compose_service_has_exactly_todays_keys():
    """Architect gate R1 on 6b4ef02, the compose side: privileged, cap_add, devices, group_add, pid and the rest stay out."""
    agent = load(DEVCONTAINER_COMPOSE)['services']['agent']
    assert list(agent) == DEVCONTAINER_AGENT_KEYS, (
        f'.devcontainer/compose.yml agent keys changed to {list(agent)}: a new key needs an architect ruling, '
        'because several give the devcontainer the host or its Docker daemon (ADR tj-4rr0la addendum 1 (f))'
    )
    environment = {str(item).partition('=')[0] for item in agent['environment']}
    assert not {name for name in environment if name.startswith('DOCKER_')}, environment


# --- tj-ix1hbl: the agent container's environment, as an exact allowed key set -------------------
#
# WHY THESE TESTS EXIST, since no single assertion below says it: they are what makes the credential
# grant of tj-ywpuxx a PRECAUTION RATHER THAN A SHRUG. The user's ruling of 2026-10-04 (decision
# record tj-izzqub addendum 1 (a)) lets this container hold exactly POSTGRES_PASS and
# INSTANCE_WRITE_SECRET, and clause 3 of that record names the test below as the condition on which
# the grant is bounded at all -- until it existed, clause 3 described a guard that was not there
# (addendum 2).
#
# THE RISK MANAGED HERE IS NOT POSTGRES. A throwaway local database password and a dev write token
# are what the ruling judged affordable. The risk is THIS LIST GROWING LATER, one reasonable-looking
# line at a time, in a file whose comment blocks are long enough that an added entry reads as
# unremarkable. So the shape is two assertions and not one:
#   R1, the exact set, makes EVERY addition a deliberate act with a diff someone has to approve. It
#       is widenable, on purpose -- a legitimate new key edits the set in the same commit.
#   R2, the broker family, is the part that is NOT widenable by adding a name to a list. To defeat
#       it someone must delete an assertion whose message says the user ruled broker keys out, which
#       is a different act from appending a line.
# A superset check in place of R1 would be satisfied by a file that ALSO hands over the broker keys,
# and a prefix-family denial in place of R1 would not notice the set growing by anything else.


def _agent_service() -> dict:
    return load(DEVCONTAINER_COMPOSE)['services']['agent']


def _agent_environment() -> dict[str, str]:
    """The agent service's environment: block as {name: value-as-written}, interpolation unresolved."""
    entries = [str(item).partition('=') for item in _agent_service()['environment']]
    return {name: value for name, _, value in entries}


def test_the_agent_environment_block_is_exactly_the_allowed_key_set():
    """R1 and decision tj-izzqub clause 3: the names in agent.environment, as an EXACT set.

    test_the_devcontainer_compose_service_has_exactly_todays_keys pins the service's TOP-LEVEL keys,
    which is why an added `env_file:` reds there already. This is the layer BELOW it, which did not:
    adding ALPACA_API_KEY to the environment block passed the whole build_infra suite before this.

    Adding a key here reddens this test ON PURPOSE. Widen the set in the same diff, deliberately, or
    do not add the key -- and read the broker assertion below before widening it.
    """
    entries = [str(item) for item in _agent_service()['environment']]
    names = [entry.partition('=')[0] for entry in entries]
    assert set(names) == DEVCONTAINER_AGENT_ENVIRONMENT_KEYS, (
        f'.devcontainer/compose.yml agent environment keys are {sorted(names)}, not '
        f'{sorted(DEVCONTAINER_AGENT_ENVIRONMENT_KEYS)}: this container holds exactly the key set the '
        'user ruled on (tj-izzqub addendum 1 (a)), so an added key needs that set widened in the same, '
        'deliberate diff'
    )
    assert len(names) == len(set(names)), f'a name appears twice, so a later entry overrides an earlier: {names}'
    assert set(SYSTEM_SECRET_KEYS) == {'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET'}, (
        f'the allowed set builds its credential pair by reusing SYSTEM_SECRET_KEYS, which is now '
        f'{sorted(SYSTEM_SECRET_KEYS)}. Reuse is right -- it is the same pair -- but it must not become a '
        'way for this container to hold a third credential through an edit to another file. The user ruled '
        'on exactly two (tj-izzqub addendum 1 (a))'
    )
    assert all('=' in entry for entry in entries), (
        f'an environment entry with no value takes its value from the HOST environment, wholesale and '
        f'unnamed, which is what ruling (a) refuses: {[entry for entry in entries if "=" not in entry]}'
    )


def test_no_broker_family_key_reaches_the_agent_container():
    """R2: the broker exclusion as its OWN assertion, not as a consequence of the exact set above.

    Ruling (d) of tj-izzqub addendum 1: "Why should the agent have keys? They should use the
    framework API to have ingest run queries." An agent reaches a real vendor fetch by driving
    data_ingest over devnet, never by holding a vendor key.

    Keyed on the family, not on two spellings: the names come from tools/agent_mcp/stack.py's
    BROKER_CREDENTIALS, and the vendor prefixes are derived from them, so a third broker key added
    there is refused here with no edit. Scanned over the WHOLE service and not just the environment
    block -- a key smuggled in through command:, build.args or a volume is the same credential.
    """
    assert BROKER_FAMILY and BROKER_PREFIXES, (BROKER_FAMILY, BROKER_PREFIXES)
    widened = sorted(
        name
        for name in DEVCONTAINER_AGENT_ENVIRONMENT_KEYS
        if name in BROKER_FAMILY or name.startswith(BROKER_PREFIXES)
    )
    assert not widened, (
        f'the allowed key set itself was widened with broker credentials {widened}. Widening that set is '
        'the intended way to add a legitimate key, which is exactly why this assertion reads the set too: '
        'the broker family is the half that no widening reaches'
    )
    names = set(_agent_environment())
    offenders = sorted(names & BROKER_FAMILY) + sorted(name for name in names if name.startswith(BROKER_PREFIXES))
    assert not offenders, (
        f'the agent container is handed broker credentials {sorted(set(offenders))}: the user ruled the '
        'broker family out of this container entirely (tj-izzqub addendum 1 (d)). This is not an '
        'assertion to widen -- an agent drives data_ingest over devnet instead of holding a vendor key'
    )
    spelled_out = json.dumps(_agent_service())
    smuggled = sorted(name for name in BROKER_FAMILY if name in spelled_out)
    assert not smuggled, f'the agent service names broker credentials outside its environment block: {smuggled}'


def test_the_agent_service_loads_no_env_file_at_all():
    """Ruling (a), by name: `env_file:` is the single edit that would silently defeat the set above.

    It injects EVERY key the named file holds without naming one, which would hand this container
    ALPACA_API_KEY and ALPACA_API_SECRET in the same stroke while the environment block still read
    as the ruled twelve. The service's top-level key list already reds on it; this says so, so a
    reader looking for the assertion the ruling describes finds it rather than inferring it.
    """
    assert 'env_file' not in _agent_service(), (
        'the agent service loads an env_file: wholesale, which ruling (a) of tj-izzqub addendum 1 '
        'refuses by name. The two allowed credentials are interpolated one by one, on the host'
    )
    code = [
        line
        for line in DEVCONTAINER_COMPOSE.read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    offenders = [line for line in code if 'env_file' in line]
    assert not offenders, f'.devcontainer/compose.yml names an env file outside its comments: {offenders}'


@pytest.mark.parametrize('key', SYSTEM_SECRET_KEYS)
def test_each_agent_credential_falls_back_to_the_sentinel_when_unset_or_empty(key: str):
    """R4, both halves, resolved through compose_model.interpolate so the BEHAVIOUR is pinned, not the spelling.

    An unset or empty value must never pass through as a set one: an empty password is a config that
    looks like it nearly works, and compose only warns. `${VAR:-X}` fires on empty as well as unset;
    `${VAR-X}` does not, and a bare `${VAR}` resolves to nothing at all. Both of those were green
    before this test, and neither is visible in a diff that only widens a key set.

    Not `${VAR:?}`, deliberately: that would make the devcontainer REFUSE TO START on the IDE path
    for anyone who has not exported both secrets into the environment their IDE was launched from
    (.devcontainer/compose.yml:140-148). The sentinel starts the container and names itself in
    whatever authentication error it goes on to cause.
    """
    expression = _agent_environment()[key]
    assert interpolate(expression, {}) == AGENT_CREDENTIAL_SENTINEL, f'{key} unset resolves to {expression!r}'
    assert interpolate(expression, {key: ''}) == AGENT_CREDENTIAL_SENTINEL, (
        f'{key} set to EMPTY passes through as a real value: {expression!r} needs ${{{key}:-...}}, not ${{{key}-...}}'
    )
    assert interpolate(expression, {key: 'a-real-dev-value'}) == 'a-real-dev-value', expression
    assert AGENT_CREDENTIAL_SENTINEL.startswith('UNSET'), (
        f'the fallback {AGENT_CREDENTIAL_SENTINEL!r} must stay non-empty and unmistakable for a credential'
    )


def test_neither_agent_credential_carries_a_literal_value():
    """R5: both entries are pure interpolations of their own name -- no secret VALUE in this file.

    Cheap, and the assertion a reader of that file will look for. `${OTHER_NAME:-...}` under one
    key's name is refused too: the value of POSTGRES_PASS comes from POSTGRES_PASS or from the
    sentinel, and from nowhere else.
    """
    environment = _agent_environment()
    for key in SYSTEM_SECRET_KEYS:
        value = environment[key]
        match = _PURE_INTERPOLATION.fullmatch(value)
        assert match, (
            f'{key}={value!r} is not a bare ${{{key}:-<sentinel>}} interpolation: a literal secret value '
            'must never appear in .devcontainer/compose.yml, which is committed to a public repository'
        )
        assert match.group(1) == key, f'{key} takes its value from {match.group(1)}, not from its own name'
        assert match.group(2) == AGENT_CREDENTIAL_SENTINEL, f'{key} defaults to {match.group(2)!r}'


def test_the_devcontainer_compose_defines_the_agent_alone():
    """01:43 (e): no agent_mcp and no socket_proxy in the devcontainer's project (addendum 4, REJECTED)."""
    services = load(DEVCONTAINER_COMPOSE)['services']
    assert list(services) == ['agent'], list(services)
