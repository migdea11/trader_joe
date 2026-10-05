"""build_infra pins for data_store's gRPC target, the compose half of ADR tj-q9ae5u addendum 5 (tj-3mk3u5.8).

The design: data_store dials data_ingest's gRPC listener by the SAME alias the server binds, from ONE entry in
its own environment: in the BASE docker-compose.yaml,

    DATA_INGEST_GRPC_TARGET=data-ingest-grpc:${DATA_INGEST_GRPC_PORT:-50051}

read by common.rpc.channel.target_from_env with no default (the reader is pinned in
common/tests/rpc/test_rpc_target.py). The host is LITERAL -- network identity, declared literally under
networks: -- and the port half is the server's APP_INTERNAL_GRPC_PORT expression byte for byte, so compose
interpolates both ends from one source in one launch and any env file moves them together (item 2). No overlay
sets, restates or removes it; test_client carries none; the committed root env file does not carry it (item 5).

One section each:

  1. THE EFFECTIVE VALUE. For every launch set that runs data_store -- read from where each is spelled, the
     Makefile's *_COMPOSE variables as make expands them, tools/agent_mcp/stack.py, every `docker compose` in
     the workflows and data/store/run_migrations.sh (test_service_pythonpath.py's _launch_sets) --
     data_store's container value, under compose's precedence (image ENV < each env file's committed
     template < environment: as interpolated), EQUALS data_ingest's effective
     APP_INTERNAL_GRPC_HOST:APP_INTERNAL_GRPC_PORT. Under the committed root env file, one lacking
     DATA_INGEST_GRPC_PORT (a user's older .env, the never-regenerated agent-stack files), one with it blank,
     and one MOVING it, where both ends must have moved to the new port. Production's reader accepts each.
  2. AS WRITTEN. The host half carries no interpolation and is literally the server's APP_INTERNAL_GRPC_HOST,
     an alias on data_ingest's ingest_store attachment; the port half is the server's port expression, byte
     for byte. Item 1 alone cannot see an interpolated host: ${X:-data-ingest-grpc} renders the same under
     every env named there, and moves the first time an env file sets X.
  3. NO SECOND DEFINITION. No compose file but the base sets, restates, blanks or passes the variable through,
     for any service; in each launch set data_store is the only container that receives it, so test_client
     (not on ingest_store; Check Network Lockdown needs the alias not to resolve from it) does not; and the
     committed root env file does not carry it.
  4. NON-VACUITY. Each way the entry can go wrong, made in memory on the committed files, reds the section
     that exists to see it.

What only Docker can show -- compose's real render and precedence, the alias resolving from data_store, a
dial reaching the listener -- is CI System Testing's (Check gRPC Peer Reach, which tj-3mk3u5.62 changes to
dial this very value) and, for the agent stack, the batched MCP rebuild (addendum 5 item 8). Nothing here
needs Docker, and nothing depends on mode bits (CI runs the suite as root).
"""

import copy
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import pytest

from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, target_from_env
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.tests.compose_model import BASE_FILE, DEVCONTAINER_COMPOSE, FAKE_FILE, TEST_CLIENT_FILE, load, merge
from common.tests.test_ci_invariants import ENV_DEFAULT_FILE, REPO_ROOT, _container_env, _env_file_values, _image_env
from common.tests.test_grpc_bind_network import BIND_NETWORK, PORT_SETTING, _environment, _rendered
from common.tests.test_service_pythonpath import MAKEFILE_SOURCE, STACK_SOURCE, _documents, _image_target, _launch_sets


pytestmark = pytest.mark.build_infra

VARIABLE = DATA_INGEST_GRPC_TARGET_ENV
CLIENT = 'data_store'
SERVER = 'data_ingest'
TEST_CLIENT = 'test_client'
MOVED_PORT = '50777'
COMMITTED = 'the committed root env file'
MOVED = f'{PORT_SETTING} moved to {MOVED_PORT}'
# Floors, so a parsing slip cannot shrink a derived set to nothing. Never the list itself: the sets checked
# are whatever the files say.
KNOWN_CLIENT_SETS = frozenset(
    {
        f'{MAKEFILE_SOURCE} PROD_COMPOSE',
        f'{MAKEFILE_SOURCE} DEV_COMPOSE',
        f'{MAKEFILE_SOURCE} SYSTEM_COMPOSE',
        f'{MAKEFILE_SOURCE} AGENT_STACK_COMPOSE',
        f'{MAKEFILE_SOURCE} TEST_CLIENT_COMPOSE',
        STACK_SOURCE,
    }
)
KNOWN_OVERLAYS = frozenset(
    {
        'docker-compose.override.yaml',
        'docker-compose.fake.yaml',
        'docker-compose.test-client.yaml',
        'docker-compose.agent-stack.yaml',
        'docker-compose.tools.yaml',
    }
)

Replaced = Mapping[Path, dict]


def _interpolation_envs() -> dict[str, dict[str, str]]:
    """What compose interpolates from: the committed root env file, and the three ways an env file differs."""
    committed = _env_file_values(ENV_DEFAULT_FILE)
    return {
        COMMITTED: committed,
        f'a root env file without {PORT_SETTING}': {name: v for name, v in committed.items() if name != PORT_SETTING},
        f'{PORT_SETTING} blank': {**committed, PORT_SETTING: ''},
        MOVED: {**committed, PORT_SETTING: MOVED_PORT},
    }


def _effective(service: Mapping, project_dir: Path, interpolation: Mapping[str, str]) -> dict[str, str]:
    """One container's environment as compose builds it: image ENV < env files (committed templates) < environment:.

    environment: is interpolated as compose interpolates it, from the project env file; a ':?' guard that
    refuses gets a placeholder (the real launch supplies the value). A merged model holds a pass-through
    entry (a bare name) as '', so an overlay that passes the variable through reads here as an empty value:
    wrong, as it is in a launch whose shell does not set it.
    """
    stage = _image_target(service, project_dir)
    names = (VARIABLE, GRPC_HOST_ENV, GRPC_PORT_ENV)
    image = {name: value for name in names if stage and (value := _image_env(stage, name)) is not None}
    environment = {
        name: _rendered(value, interpolation)
        for name, value in _environment(service.get('environment')).items()
        if value is not None
    }
    return _container_env({**service, 'environment': environment}, image, project_dir)


def _client_sets(replaced: Replaced | None = None) -> dict[str, tuple[dict, Path]]:
    """{label: (merged model, project dir)} for every launch set whose model runs data_store."""
    sets = {}
    for label, files in _launch_sets().items():
        model = merge(_documents(files, replaced))
        if CLIENT in (model.get('services') or {}):
            sets[label] = (model, files[0].parent)
    return sets


def _split_outside_braces(text: str) -> tuple[str, str]:
    """TEXT split at its first ':' outside ${...}: (host half, port half); ('', TEXT) when there is none."""
    depth, index = 0, 0
    while index < len(text):
        if text.startswith('${', index):
            depth, index = depth + 1, index + 2
            continue
        if text[index] == '}' and depth:
            depth -= 1
        elif text[index] == ':' and not depth:
            return text[:index], text[index + 1 :]
        index += 1
    return '', text


# --- Section 1: the effective value, every launch set, every env ---------------------------------------


def _target_findings(replaced: Replaced | None = None) -> tuple[list[str], dict[str, dict[str, str]]]:
    """(findings, {label: {env: data_store's effective target}}) over every launch set that runs data_store."""
    findings, seen = [], {}
    for label, (model, project_dir) in _client_sets(replaced).items():
        services = model['services']
        if SERVER not in services:
            findings.append(f'{label}: runs {CLIENT} but not {SERVER}, so its target names no listener')
            continue
        seen[label] = {}
        for env_name, env in _interpolation_envs().items():
            client = _effective(services[CLIENT], project_dir, env).get(VARIABLE)
            server_env = _effective(services[SERVER], project_dir, env)
            server = f'{server_env.get(GRPC_HOST_ENV, "")}:{server_env.get(GRPC_PORT_ENV, "")}'
            seen[label][env_name] = client
            if client != server:
                findings.append(
                    f'{label} under {env_name}: {CLIENT} {VARIABLE}={client!r}, but {SERVER} binds {server!r}'
                )
            elif env_name == MOVED and server_env.get(GRPC_PORT_ENV) != MOVED_PORT:
                findings.append(
                    f'{label} under {env_name}: both ends read {server!r}; {PORT_SETTING} no longer moves the port'
                )
    return findings, seen


def test_the_launch_sets_that_run_data_store_are_derived():
    """Non-vacuity for section 1: today's sets that run data_store are among those read from the files."""
    sets = set(_client_sets())
    assert sets >= KNOWN_CLIENT_SETS, f'launch sets found running {CLIENT}: {sorted(sets)}'


def test_data_stores_target_is_the_address_data_ingest_binds_in_every_launch_set(monkeypatch: pytest.MonkeyPatch):
    """Section 1: equal to the server's host:port under every env, and the moved port moves both ends."""
    findings, seen = _target_findings()
    assert seen, f'no launch set runs {CLIENT}, so nothing was checked'
    assert not findings, '\n'.join(findings)
    for label, values in seen.items():
        for env_name, value in values.items():
            monkeypatch.setenv(VARIABLE, value)
            assert target_from_env(VARIABLE) == value, f'{label} under {env_name}: the reader changed {value!r}'
        assert values[MOVED].endswith(f':{MOVED_PORT}'), f'{label}: {values}'


# --- Section 2: as written in the base file ------------------------------------------------------------


def _literal_findings(base: dict) -> list[str]:
    services = base.get('services') or {}
    raw = _environment((services.get(CLIENT) or {}).get('environment')).get(VARIABLE)
    server = _environment((services.get(SERVER) or {}).get('environment'))
    if not raw:
        return [f'{BASE_FILE.name} does not set {CLIENT} {VARIABLE}']
    host, port = _split_outside_braces(raw)
    findings = []
    if '$' in host or not host:
        findings.append(f'{BASE_FILE.name} {CLIENT} {VARIABLE}={raw!r}: the host half {host!r} is not a literal')
    if host != server.get(GRPC_HOST_ENV):
        findings.append(f'{VARIABLE} host {host!r} is not {SERVER} {GRPC_HOST_ENV}={server.get(GRPC_HOST_ENV)!r}')
    if port != server.get(GRPC_PORT_ENV):
        findings.append(
            f'{VARIABLE} port {port!r} is not byte for byte {SERVER} {GRPC_PORT_ENV}={server.get(GRPC_PORT_ENV)!r}'
        )
    networks = (services.get(SERVER) or {}).get('networks')
    attachment = (networks.get(BIND_NETWORK) if isinstance(networks, dict) else None) or {}
    if host not in [str(alias) for alias in attachment.get('aliases') or []]:
        findings.append(f'{VARIABLE} host {host!r} is not an alias on {SERVER}/{BIND_NETWORK}')
    return findings


def test_the_host_is_written_literally_and_the_port_is_the_servers_expression():
    """Section 2 (item 2): host literal and the alias the server binds; port half the server's expression."""
    findings = _literal_findings(load(BASE_FILE))
    assert not findings, '\n'.join(findings)


# --- Section 3: no second definition -------------------------------------------------------------------


def _compose_files() -> list[Path]:
    """Every compose file a launch names, every docker-compose*.yaml at the root, and the devcontainer's."""
    files = {path for files in _launch_sets().values() for path in files}
    files |= set(REPO_ROOT.glob('docker-compose*.yaml')) | {DEVCONTAINER_COMPOSE}
    return sorted(files)


def _overlay_findings(replaced: Replaced | None = None) -> list[str]:
    """Every service in every compose file that names the variable in its environment:, but base data_store."""
    replaced = replaced or {}
    findings = []
    for path in _compose_files():
        document = replaced.get(path) or load(path)
        for name, service in (document.get('services') or {}).items():
            environment = _environment((service or {}).get('environment'))
            if VARIABLE in environment and (path, name) != (BASE_FILE, CLIENT):
                findings.append(f'{path.name} {name} sets {VARIABLE} to {environment[VARIABLE]!r}')
    return findings


def _receiver_findings(replaced: Replaced | None = None) -> tuple[list[str], dict[str, set[str]]]:
    """(findings, {label: services whose container receives the variable}) under the committed env file."""
    committed = _interpolation_envs()[COMMITTED]
    findings, receivers = [], {}
    for label, files in _launch_sets().items():
        model = merge(_documents(files, replaced))
        received = {
            name
            for name, service in (model.get('services') or {}).items()
            if VARIABLE in _effective(service or {}, files[0].parent, committed)
        }
        receivers[label] = received
        expected = {CLIENT} if CLIENT in (model.get('services') or {}) else set()
        if received != expected:
            findings.append(f'{label}: {VARIABLE} reaches {sorted(received)}, not {sorted(expected)}')
    return findings, receivers


def test_no_overlay_sets_restates_or_removes_the_target():
    """Section 3 (item 5): the base file's data_store entry is the only definition in any compose file."""
    names = {path.name for path in _compose_files()}
    assert names >= KNOWN_OVERLAYS | {BASE_FILE.name}, f'compose files scanned: {sorted(names)}'
    findings = _overlay_findings()
    assert not findings, '\n'.join(findings)


def test_only_data_store_receives_the_target_and_test_client_does_not():
    """Section 3: in every launch set the variable reaches data_store's container and no other."""
    findings, receivers = _receiver_findings()
    assert not findings, '\n'.join(findings)
    with_client = [
        label for label, files in _launch_sets().items() if TEST_CLIENT in merge(_documents(files))['services']
    ]
    assert with_client, f'no launch set runs {TEST_CLIENT}, so its absence was never checked'
    assert all(TEST_CLIENT not in receivers[label] for label in with_client), receivers


def test_the_committed_root_env_file_does_not_carry_the_target():
    """Section 3 (item 5): environment: outranks env_file:, so a copy there is a second value that can only drift."""
    assert VARIABLE not in _env_file_values(ENV_DEFAULT_FILE), f'{ENV_DEFAULT_FILE.name} assigns {VARIABLE}'


# --- Section 4: each way the entry goes wrong reds the section built to see it -------------------------


ENTRY = f'{VARIABLE}=data-ingest-grpc:${{{PORT_SETTING}:-50051}}'


def _with_entry(document: dict, service: str, entry: str | None) -> dict:
    """DOCUMENT with SERVICE's environment: entry for the variable replaced by ENTRY (None drops it), form kept."""
    document = copy.deepcopy(document)
    target = document.setdefault('services', {}).setdefault(service, {})
    environment = target.get('environment')
    name, separator, value = (entry or '').partition('=')
    if isinstance(environment, dict):
        environment.pop(VARIABLE, None)
        if entry is not None:
            environment[name] = value if separator else None
        return document
    kept = [item for item in environment or [] if str(item).partition('=')[0].strip() != VARIABLE]
    target['environment'] = kept if entry is None else [*kept, entry]
    return document


# mutation: (file, service, the entry written there, the checks that must red)
_MUTATIONS: dict[str, tuple[Path, str, str | None, frozenset[str]]] = {
    'entry dropped': (BASE_FILE, CLIENT, None, frozenset({'target', 'literal', 'receivers'})),
    'port hard-coded': (BASE_FILE, CLIENT, f'{VARIABLE}=data-ingest-grpc:50051', frozenset({'target', 'literal'})),
    'host interpolated': (
        BASE_FILE,
        CLIENT,
        f'{VARIABLE}=${{DATA_INGEST_GRPC_HOST:-data-ingest-grpc}}:${{{PORT_SETTING}:-50051}}',
        frozenset({'literal'}),
    ),
    'the hostname, not the alias': (
        BASE_FILE,
        CLIENT,
        f'{VARIABLE}=${{DATA_INGEST_NAME:-data_ingest}}:${{{PORT_SETTING}:-50051}}',
        frozenset({'target', 'literal'}),
    ),
    'another port variable': (
        BASE_FILE,
        CLIENT,
        f'{VARIABLE}=data-ingest-grpc:${{DATA_STORE_GRPC_PORT:-50051}}',
        frozenset({'target', 'literal'}),
    ),
    'restated by the fake overlay': (FAKE_FILE, CLIENT, ENTRY, frozenset({'overlay'})),
    'blanked by the fake overlay': (FAKE_FILE, CLIENT, f'{VARIABLE}=', frozenset({'overlay', 'target'})),
    'passed through by the fake overlay': (FAKE_FILE, CLIENT, VARIABLE, frozenset({'overlay'})),
    'given to test_client': (TEST_CLIENT_FILE, TEST_CLIENT, ENTRY, frozenset({'overlay', 'receivers'})),
}
_CHECKS: dict[str, Callable[[Replaced], Iterable[str]]] = {
    'target': lambda replaced: _target_findings(replaced)[0],
    'literal': lambda replaced: _literal_findings(replaced.get(BASE_FILE) or load(BASE_FILE)),
    'overlay': _overlay_findings,
    'receivers': lambda replaced: _receiver_findings(replaced)[0],
}


def test_every_check_is_green_on_the_committed_files():
    """The baseline the mutations below are read against."""
    findings = {name: list(check({})) for name, check in _CHECKS.items()}
    assert not any(findings.values()), findings


@pytest.mark.parametrize('mutation', list(_MUTATIONS))
def test_the_checks_see_the_entry_go_wrong(mutation: str):
    """Section 4: the mutation, made in memory on the committed file, reds each check named for it."""
    path, service, entry, must_red = _MUTATIONS[mutation]
    replaced = {path: _with_entry(load(path), service, entry)}
    reds = {name for name, check in _CHECKS.items() if list(check(replaced))}
    assert reds >= must_red, f'{mutation}: only {sorted(reds)} red, but {sorted(must_red)} must'
