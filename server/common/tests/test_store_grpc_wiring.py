"""build_infra pins for the store's gRPC result path in compose: the data-store-grpc alias and its target (tj-7zjov1.16).

The design: owner ruling R1 on tj-26jt9w (2026-10-24, push) -- ingest pushes attempt results to a gRPC server
in data_store. The wiring mirrors data_ingest's (ADR tj-q9ae5u addendum 3, pinned by test_grpc_bind_network.py
and test_ingest_grpc_target.py) in the opposite direction:

  1. THE BIND. data_store's environment: in the BASE file sets APP_INTERNAL_GRPC_HOST to data-store-grpc,
     literally, and APP_INTERNAL_GRPC_PORT to ${DATA_STORE_GRPC_PORT:-50052}; no overlay touches either.
  2. THE ALIAS is declared on data_store's ingest_store attachment and nowhere else, in every launch set, and
     names no service, hostname or container; data_store's attachments are exactly store_db, ingest_store
     and store_api (the networks block moved to mapping form to carry the alias, and lost nothing).
  3. THE PORT renders to one valid port with or without DATA_STORE_GRPC_PORT, the same each way, accepted by
     BindAddress.from_env, and never data_ingest's own gRPC port.
  4. THE TARGET. data_ingest's DATA_STORE_GRPC_TARGET is data-store-grpc (literal) and the server's port
     expression byte for byte, so it equals what data_store binds in every launch set under every env file,
     a moved port included; only data_ingest receives it; no overlay and no committed env file carries it.
  5. NOTHING IS PUBLISHED. No launch set gives any service a ports: entry on the store's gRPC port.
  6. NON-VACUITY. Each way the wiring can go wrong, made in memory on the committed files, reds its check.

Docker's real render and the alias resolving are the CI System Testing job's, once P34 (tj-7zjov1.27) hosts
the servicer; nothing here needs Docker.
"""

import copy
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import pytest

from common.rpc.channel import DATA_STORE_GRPC_TARGET_ENV, target_from_env
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress
from common.tests.compose_model import BASE_FILE, FAKE_FILE, TEST_CLIENT_FILE, load, merge
from common.tests.test_ci_invariants import ENV_DEFAULT_FILE, _container_env, _env_file_values, _image_env
from common.tests.test_grpc_bind_network import _RFC1123, _environment, _is_address_literal, _name_uses, _rendered
from common.tests.test_ingest_grpc_target import _compose_files, _split_outside_braces
from common.tests.test_service_pythonpath import MAKEFILE_SOURCE, _documents, _image_target, _launch_sets


pytestmark = pytest.mark.build_infra

SERVER = 'data_store'
CLIENT = 'data_ingest'
VARIABLE = DATA_STORE_GRPC_TARGET_ENV
HOST = 'data-store-grpc'
BIND_NETWORK = 'ingest_store'
STORE_NETWORKS = frozenset({'store_db', 'ingest_store', 'store_api'})
PORT_SETTING = 'DATA_STORE_GRPC_PORT'
PORT_EXPRESSION = f'${{{PORT_SETTING}:-50052}}'
INGEST_PORT_SETTING = 'DATA_INGEST_GRPC_PORT'
MOVED_PORT = '50888'
COMMITTED = 'the committed root env file'
MOVED = f'{PORT_SETTING} moved to {MOVED_PORT}'
KNOWN_SERVER_SETS = frozenset({f'{MAKEFILE_SOURCE} PROD_COMPOSE', f'{MAKEFILE_SOURCE} DEV_COMPOSE'})

Replaced = Mapping[Path, dict]


def _interpolation_envs() -> dict[str, dict[str, str]]:
    committed = _env_file_values(ENV_DEFAULT_FILE)
    return {
        COMMITTED: committed,
        f'a root env file without {PORT_SETTING}': {name: v for name, v in committed.items() if name != PORT_SETTING},
        f'{PORT_SETTING} blank': {**committed, PORT_SETTING: ''},
        MOVED: {**committed, PORT_SETTING: MOVED_PORT},
    }


def _sets(replaced: Replaced | None = None) -> dict[str, tuple[dict, Path]]:
    """{label: (merged model, project dir)} for every launch set whose model runs data_store."""
    found = {}
    for label, files in _launch_sets().items():
        model = merge(_documents(files, replaced))
        if SERVER in (model.get('services') or {}):
            found[label] = (model, files[0].parent)
    return found


def _effective(service: Mapping, project_dir: Path, env: Mapping[str, str]) -> dict[str, str]:
    stage = _image_target(service, project_dir)
    names = (VARIABLE, GRPC_HOST_ENV, GRPC_PORT_ENV)
    image = {name: value for name in names if stage and (value := _image_env(stage, name)) is not None}
    environment = {
        name: _rendered(value, env)
        for name, value in _environment(service.get('environment')).items()
        if value is not None
    }
    return _container_env({**service, 'environment': environment}, image, project_dir)


def _base_service(document: dict, name: str) -> dict:
    return (document.get('services') or {}).get(name) or {}


# --- 1. the bind, as written in the base file --------------------------------------------------------


def _bind_literal_findings(base: dict) -> list[str]:
    server = _base_service(base, SERVER)
    environment = _environment(server.get('environment'))
    findings = []
    if environment.get(GRPC_HOST_ENV) != HOST:
        findings.append(f'{SERVER} {GRPC_HOST_ENV}={environment.get(GRPC_HOST_ENV)!r}, not the literal {HOST!r}')
    if environment.get(GRPC_PORT_ENV) != PORT_EXPRESSION:
        findings.append(f'{SERVER} {GRPC_PORT_ENV}={environment.get(GRPC_PORT_ENV)!r}, not {PORT_EXPRESSION!r}')
    networks = server.get('networks')
    if not isinstance(networks, dict):
        return [*findings, f'{SERVER} networks is {type(networks).__name__}; an alias needs the mapping form']
    if set(networks) != STORE_NETWORKS:
        findings.append(f'{SERVER} attaches {sorted(networks)}, not {sorted(STORE_NETWORKS)}')
    aliases = [str(alias) for alias in (networks.get(BIND_NETWORK) or {}).get('aliases') or []]
    if HOST not in aliases or any('$' in alias for alias in aliases):
        findings.append(f'{SERVER}/{BIND_NETWORK} aliases {aliases} do not carry {HOST!r} literally')
    return findings


def test_the_base_file_binds_the_literal_alias_and_the_port_expression():
    """Sections 1 and 2, as written: the host literal, the port expression, the alias, three networks."""
    findings = _bind_literal_findings(load(BASE_FILE))
    assert not findings, '\n'.join(findings)


def _bind_overlay_findings(replaced: Replaced | None = None) -> list[str]:
    replaced = replaced or {}
    findings = []
    for path in _compose_files():
        if path == BASE_FILE:
            continue
        server = _base_service(replaced.get(path) or load(path), SERVER)
        if names := sorted({GRPC_HOST_ENV, GRPC_PORT_ENV} & set(_environment(server.get('environment')))):
            findings.append(f'{path.name} sets {SERVER} {names}, which only the base file may')
    return findings


def test_no_overlay_sets_the_stores_bind_variables():
    """Section 1: an overlay that blanked or moved them would strand the client."""
    findings = _bind_overlay_findings()
    assert not findings, '\n'.join(findings)


# --- 2. the alias is on ingest_store only, in every launch set -------------------------------------------


def _alias_findings(replaced: Replaced | None = None) -> list[str]:
    findings = []
    for label, (model, _) in _sets(replaced).items():
        for env_name, env in _interpolation_envs().items():
            aliases, others = _name_uses(model, HOST, env)
            if aliases != {(SERVER, BIND_NETWORK)}:
                findings.append(f'{label} under {env_name}: {HOST!r} is an alias on {sorted(aliases)}')
            if others:
                findings.append(f'{label} under {env_name}: {HOST!r} is also {others}')
        networks = model['services'][SERVER].get('networks')
        attached = set(networks) if isinstance(networks, dict) else set(networks or [])
        if not attached >= STORE_NETWORKS:
            findings.append(f'{label}: {SERVER} lost an attachment; it has {sorted(attached)}')
    return findings


def test_the_launch_sets_that_run_data_store_are_derived():
    """Non-vacuity: the sets checked below include the production and dev launches."""
    assert set(_sets()) >= KNOWN_SERVER_SETS, sorted(_sets())


def test_the_alias_is_on_data_stores_ingest_store_attachment_and_nowhere_else():
    """Section 2: on store_api or store_db the listener would land where data_ingest cannot reach it."""
    assert _RFC1123.match(HOST) and not _is_address_literal(HOST)
    findings = _alias_findings()
    assert not findings, '\n'.join(findings)


# --- 3. the port ------------------------------------------------------------------------------------------


def test_the_bind_port_renders_to_one_valid_port_and_from_env_accepts_it(monkeypatch: pytest.MonkeyPatch):
    """Section 3: the fallback is .env.default's value, and production's reader takes what compose renders."""
    committed = _env_file_values(ENV_DEFAULT_FILE)
    assert committed.get(PORT_SETTING) == '50052', (
        f'{ENV_DEFAULT_FILE.name} {PORT_SETTING}={committed.get(PORT_SETTING)!r}'
    )
    rendered = {
        name: _rendered(PORT_EXPRESSION, env).strip() for name, env in _interpolation_envs().items() if name != MOVED
    }
    assert set(rendered.values()) == {'50052'}, rendered
    assert _rendered(PORT_EXPRESSION, _interpolation_envs()[MOVED]) == MOVED_PORT

    monkeypatch.setenv(GRPC_HOST_ENV, HOST)
    monkeypatch.setenv(GRPC_PORT_ENV, '50052')
    assert BindAddress.from_env() == BindAddress(HOST, 50052)


def test_the_stores_grpc_port_is_not_ingests():
    """Two listeners on ingest_store; one port each so neither end can dial the other by mistake."""
    committed = _env_file_values(ENV_DEFAULT_FILE)
    assert committed.get(PORT_SETTING) != committed.get(INGEST_PORT_SETTING)


# --- 4. the target ----------------------------------------------------------------------------------------


def _target_findings(replaced: Replaced | None = None) -> tuple[list[str], dict[str, dict[str, str]]]:
    findings, seen = [], {}
    for label, (model, project_dir) in _sets(replaced).items():
        services = model['services']
        if CLIENT not in services:
            continue
        seen[label] = {}
        for env_name, env in _interpolation_envs().items():
            client = _effective(services[CLIENT], project_dir, env).get(VARIABLE)
            server_env = _effective(services[SERVER], project_dir, env)
            server = f'{server_env.get(GRPC_HOST_ENV, "")}:{server_env.get(GRPC_PORT_ENV, "")}'
            seen[label][env_name] = client
            if client != server:
                findings.append(f'{label} under {env_name}: {CLIENT} {VARIABLE}={client!r}, {SERVER} binds {server!r}')
            elif env_name == MOVED and server_env.get(GRPC_PORT_ENV) != MOVED_PORT:
                findings.append(f'{label} under {env_name}: {PORT_SETTING} no longer moves the port')
    return findings, seen


def test_data_ingests_target_is_the_address_data_store_binds_in_every_launch_set(monkeypatch: pytest.MonkeyPatch):
    """Section 4: equal under every env file, and moving the port moves both ends."""
    findings, seen = _target_findings()
    assert seen, f'no launch set runs both {SERVER} and {CLIENT}'
    assert not findings, '\n'.join(findings)
    for label, values in seen.items():
        for value in values.values():
            monkeypatch.setenv(VARIABLE, value)
            assert target_from_env(VARIABLE) == value, f'{label}: the reader changed {value!r}'


def _target_literal_findings(base: dict) -> list[str]:
    raw = _environment(_base_service(base, CLIENT).get('environment')).get(VARIABLE)
    if not raw:
        return [f'{BASE_FILE.name} does not set {CLIENT} {VARIABLE}']
    host, port = _split_outside_braces(raw)
    server = _environment(_base_service(base, SERVER).get('environment'))
    findings = []
    if host != HOST or host != server.get(GRPC_HOST_ENV):
        findings.append(f'{VARIABLE} host {host!r} is not the literal alias {SERVER} binds')
    if port != server.get(GRPC_PORT_ENV):
        findings.append(f'{VARIABLE} port {port!r} is not byte for byte {SERVER} {GRPC_PORT_ENV}')
    return findings


def test_the_target_is_written_as_the_literal_alias_and_the_servers_port_expression():
    """Section 4, as written: an interpolated host would render the same here and move under some env file."""
    findings = _target_literal_findings(load(BASE_FILE))
    assert not findings, '\n'.join(findings)


def _receiver_findings(replaced: Replaced | None = None) -> list[str]:
    committed = _interpolation_envs()[COMMITTED]
    findings = []
    for label, files in _launch_sets().items():
        model = merge(_documents(files, replaced))
        services = model.get('services') or {}
        received = {name for name, s in services.items() if VARIABLE in _effective(s or {}, files[0].parent, committed)}
        expected = {CLIENT} if CLIENT in services else set()
        if received != expected:
            findings.append(f'{label}: {VARIABLE} reaches {sorted(received)}, not {sorted(expected)}')
    for path in _compose_files():
        document = (replaced or {}).get(path) or load(path)
        for name, service in (document.get('services') or {}).items():
            if VARIABLE in _environment((service or {}).get('environment')) and (path, name) != (BASE_FILE, CLIENT):
                findings.append(f'{path.name} {name} sets {VARIABLE}')
    return findings


def test_only_data_ingest_receives_the_target_and_no_overlay_or_env_file_restates_it():
    """Section 4: one definition, one receiver."""
    findings = _receiver_findings()
    assert not findings, '\n'.join(findings)
    assert VARIABLE not in _env_file_values(ENV_DEFAULT_FILE)


# --- 5. nothing is published --------------------------------------------------------------------------------


def _container_ports(entry: object, env: Mapping[str, str]) -> set[str]:
    """The container-side port(s) a ports: entry publishes, short or long syntax."""
    if isinstance(entry, dict):
        return {_rendered(entry.get('target', ''), env).strip()}
    text = _rendered(entry, env).strip().split('/')[0]
    container = text.rsplit(':', 1)[-1]
    if '-' in container:
        low, high = container.split('-', 1)
        if low.isdigit() and high.isdigit():
            return {str(port) for port in range(int(low), int(high) + 1)}
    return {container}


def _published_findings(replaced: Replaced | None = None) -> list[str]:
    findings = []
    for label, (model, _) in _sets(replaced).items():
        for env_name, env in _interpolation_envs().items():
            grpc_port = _rendered(PORT_EXPRESSION, env).strip()
            for entry in (model['services'][SERVER] or {}).get('ports') or []:
                if grpc_port in _container_ports(entry, env):
                    findings.append(f'{label} under {env_name}: {SERVER} publishes the gRPC port: {entry!r}')
    return findings


def test_no_launch_set_publishes_the_stores_grpc_port():
    """Section 5: the listener is reached over ingest_store only; a host port would bypass the network model."""
    findings = _published_findings()
    assert not findings, '\n'.join(findings)
    base_ports = _base_service(load(BASE_FILE), SERVER).get('ports')
    assert not base_ports, f'{BASE_FILE.name} {SERVER} publishes {base_ports}'


# --- 6. each way it goes wrong reds the check built to see it -----------------------------------------------


def _with_store_environment(entry_name: str, value: str | None) -> dict:
    document = copy.deepcopy(load(BASE_FILE))
    server = document['services'][SERVER]
    environment = server.get('environment')
    if isinstance(environment, dict):
        environment.pop(entry_name, None)
        if value is not None:
            environment[entry_name] = value
    else:
        kept = [item for item in environment if str(item).partition('=')[0].strip() != entry_name]
        server['environment'] = kept if value is None else [*kept, f'{entry_name}={value}']
    return document


def _with_alias_on(network: str) -> dict:
    document = copy.deepcopy(load(BASE_FILE))
    networks = document['services'][SERVER]['networks']
    networks[BIND_NETWORK] = {}
    networks[network] = {'aliases': [HOST]}
    return document


def _with_networks_as_a_list() -> dict:
    document = copy.deepcopy(load(BASE_FILE))
    document['services'][SERVER]['networks'] = sorted(STORE_NETWORKS)
    return document


def _with_target(value: str) -> dict:
    document = copy.deepcopy(load(BASE_FILE))
    environment = document['services'][CLIENT]['environment']
    assert isinstance(environment, dict), 'data_ingest environment: is expected in mapping form'
    environment[VARIABLE] = value
    return document


def _with_published_port() -> dict:
    document = copy.deepcopy(load(BASE_FILE))
    document['services'][SERVER]['ports'] = [f'127.0.0.1:{PORT_EXPRESSION}:{PORT_EXPRESSION}']
    return document


def _given_to_test_client() -> dict:
    document = copy.deepcopy(load(TEST_CLIENT_FILE))
    service = document.setdefault('services', {}).setdefault('test_client', {})
    environment = service.get('environment')
    entry = f'{VARIABLE}={HOST}:{PORT_EXPRESSION}'
    if isinstance(environment, dict):
        environment[VARIABLE] = f'{HOST}:{PORT_EXPRESSION}'
    else:
        service['environment'] = [*(environment or []), entry]
    return document


def _overlay_moves_the_bind() -> dict:
    document = copy.deepcopy(load(FAKE_FILE))
    service = document.setdefault('services', {}).setdefault(SERVER, {})
    environment = service.get('environment')
    if isinstance(environment, dict):
        environment[GRPC_PORT_ENV] = '50999'
    else:
        service['environment'] = [*(environment or []), f'{GRPC_PORT_ENV}=50999']
    return document


_MUTATIONS: dict[str, tuple[Path, Callable[[], dict], frozenset[str]]] = {
    'alias moved to store_api': (BASE_FILE, lambda: _with_alias_on('store_api'), frozenset({'literal', 'alias'})),
    'networks back to a list': (BASE_FILE, _with_networks_as_a_list, frozenset({'literal', 'alias'})),
    'bind host interpolated': (
        BASE_FILE,
        lambda: _with_store_environment(GRPC_HOST_ENV, f'${{STORE_GRPC_HOST:-{HOST}}}'),
        frozenset({'literal'}),
    ),
    'bind port hard-coded': (
        BASE_FILE,
        lambda: _with_store_environment(GRPC_PORT_ENV, '50052'),
        frozenset({'literal', 'target', 'target_literal'}),
    ),
    'bind host dropped': (BASE_FILE, lambda: _with_store_environment(GRPC_HOST_ENV, None), frozenset({'target'})),
    'target port on ingest variable': (
        BASE_FILE,
        lambda: _with_target(f'{HOST}:${{{INGEST_PORT_SETTING}:-50051}}'),
        frozenset({'target', 'target_literal'}),
    ),
    'target host is the service name': (
        BASE_FILE,
        lambda: _with_target(f'data_store:{PORT_EXPRESSION}'),
        frozenset({'target', 'target_literal'}),
    ),
    'grpc port published': (BASE_FILE, _with_published_port, frozenset({'published'})),
    'target given to test_client': (TEST_CLIENT_FILE, _given_to_test_client, frozenset({'receivers'})),
    'an overlay moves the bind port': (FAKE_FILE, _overlay_moves_the_bind, frozenset({'overlay'})),
}
_CHECKS: dict[str, Callable[[Replaced], Iterable[str]]] = {
    'literal': lambda replaced: _bind_literal_findings(replaced.get(BASE_FILE) or load(BASE_FILE)),
    'overlay': _bind_overlay_findings,
    'alias': _alias_findings,
    'target': lambda replaced: _target_findings(replaced)[0],
    'target_literal': lambda replaced: _target_literal_findings(replaced.get(BASE_FILE) or load(BASE_FILE)),
    'receivers': _receiver_findings,
    'published': _published_findings,
}


def test_every_check_is_green_on_the_committed_files():
    """The baseline the mutations are read against."""
    findings = {name: list(check({})) for name, check in _CHECKS.items()}
    assert not any(findings.values()), findings


@pytest.mark.parametrize('mutation', list(_MUTATIONS))
def test_the_checks_see_the_wiring_go_wrong(mutation: str):
    """Section 6: the mutation, made in memory, reds each check named for it.

    Args:
        mutation: The named way the wiring goes wrong.
    """
    path, build, must_red = _MUTATIONS[mutation]
    replaced = {path: build()}
    reds = {name for name, check in _CHECKS.items() if list(check(replaced))}
    assert reds >= must_red, f'{mutation}: only {sorted(reds)} red, but {sorted(must_red)} must'


def test_the_port_parser_reads_both_syntaxes():
    """The published-port check is only as good as its reading of a ports: entry."""
    assert _container_ports('127.0.0.1:8080:50052', {}) == {'50052'}
    assert _container_ports('50052/tcp', {}) == {'50052'}
    assert _container_ports({'target': 50052, 'published': 9}, {}) == {'50052'}
    assert '50052' in _container_ports('50050-50060', {})
    assert re.fullmatch(r'[0-9]+', _rendered(PORT_EXPRESSION, {}))
