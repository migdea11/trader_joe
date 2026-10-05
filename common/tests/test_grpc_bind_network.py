"""build_infra pins for data_ingest's gRPC bind name and port (T3a, tj-3mk3u5.49).

Design: ADR tj-q9ae5u addendum 3 -- data_ingest's gRPC server binds a NETWORK-SCOPED ALIAS,
data-ingest-grpc, declared on its ingest_store attachment only, because Docker's embedded DNS
answers an alias with the address on that network whatever order the networks attach in, while the
service name or hostname maps to whichever address /etc/hosts happened to get. The architect's
ruling on tj-3mk3u5.24 (05:28 UTC 2026-10-02) puts both values in the BASE compose file's
environment:, so every launch set and every env file written before T3a carries them:
common/rpc/server.py's BindAddress.from_env has no default, and once T2 (tj-3mk3u5.24) hosts the
server, a launch that lacks either crashes data_ingest at startup. Never a wildcard (tj-r6vcgv
addendum 1 A5; ADR tj-8konfu D6.5).

The bead's validator gate, one section each:

  1. Every Makefile compose set that runs data_ingest hands it both variables: set by the base file,
     declared by no overlay (so none blanks or overrides them), non-blank once rendered.
  2. The host is an alias declared on data_ingest's ingest_store attachment and on no other network
     or service; not a service, host or container name; a plain RFC 1123 name, not loopback and not
     an address literal (so not 0.0.0.0, :: or 0).
  3. The port renders to one integer 1..65535 with no DATA_INGEST_GRPC_PORT at all (an env file from
     before T3a; the agent stack's generated env), with it blank, and with .env.default's value --
     the same port each way -- and production's BindAddress.from_env accepts what compose renders.
  4. System Testing's Check gRPC Bind Network step, its own script run under bash with `docker`
     stubbed (grpc_bind_docker_stub.py), passes only when the name resolves inside data_ingest to
     exactly data_ingest's ingest_store address(es), and resolves it with the very call
     refuse_wildcard makes. Its place in the job -- after Smoke Test, so after the stack is up -- and
     its compose spelling are pinned with the rest of the job in test_ci_invariants.py.

The launch sets are read from the Makefile by make itself, never listed from memory, and a guard
test fails when the Makefile gains a set that runs data_ingest without it being pinned here.

What only Docker can show -- compose's real merge and render, the embedded DNS answering the alias
with the ingest_store address, docker inspect's real output -- is CI System Testing's run of the
step; nothing here needs Docker. CI runs this suite as root, so nothing here depends on mode bits
being enforced: the stub is made executable and that is all.
"""

import asyncio
import functools
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress, refuse_wildcard
from common.tests import grpc_bind_docker_stub as stub
from common.tests.compose_model import BASE_FILE, InterpolationRefused, interpolate, load, merge
from common.tests.test_ci_invariants import (
    ENV_DEFAULT_FILE,
    GRPC_BIND_STEP,
    MAKEFILE,
    REPO_ROOT,
    _compose_calls,
    _env_file_values,
    _expanded_make_variable,
    _subprocess_env,
    _system_step,
)


pytestmark = pytest.mark.build_infra

DATA_INGEST = 'data_ingest'
BIND_NETWORK = 'ingest_store'
BIND_VARIABLES = (GRPC_HOST_ENV, GRPC_PORT_ENV)
PORT_SETTING = 'DATA_INGEST_GRPC_PORT'
# Every Makefile compose set that runs data_ingest, in Makefile order. Derived, not trusted:
# test_every_makefile_set_that_runs_data_ingest_is_pinned reads them all from the Makefile.
DATA_INGEST_LAUNCH_SETS = (
    'PROD_COMPOSE',
    'DEV_COMPOSE',
    'TOOLS_COMPOSE',
    'AGENT_STACK_COMPOSE',
    'TEST_CLIENT_COMPOSE',
    'SYSTEM_COMPOSE',
    'SEED_DUMP_COMPOSE',
)
# The four the bead's gate names; a floor, so a parsing slip cannot shrink the set to nothing.
GATE_LAUNCH_SETS = frozenset({'PROD_COMPOSE', 'DEV_COMPOSE', 'SYSTEM_COMPOSE', 'AGENT_STACK_COMPOSE'})
_COMPOSE_SET = re.compile(r'^(\w+_COMPOSE)\s*[:?+]?=', re.MULTILINE)
# A plain RFC 1123 name: letters, digits and hyphens, no '_', no leading or trailing hyphen.
_RFC1123 = re.compile(
    r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
)


def _interpolation_envs() -> dict[str, dict[str, str]]:
    """The interpolation environments a launch renders under, as the bead's item 3 names them."""
    return {
        f'no {PORT_SETTING} (an env file from before T3a, or the agent stack generated env)': {},
        f'{PORT_SETTING} blank': {PORT_SETTING: ''},
        '.env.default': _env_file_values(ENV_DEFAULT_FILE),
    }


@functools.cache
def _set_files(variable: str) -> tuple[Path, ...]:
    """The compose files a Makefile set loads, in order, as make itself expands the variable."""
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    calls = _compose_calls(expanded)
    assert len(calls) == 1, f'{variable} is {expanded!r}; expected one docker compose invocation'
    files, rest = calls[0]
    assert files and not rest, f'{variable} is {expanded!r}; a compose set names files, not a subcommand'
    # AGENT_MCP_COMPOSE names its file under the root checkout's absolute path; the file that
    # matters is this checkout's, which shares the name.
    return tuple(REPO_ROOT / (PurePosixPath(file).name if file.startswith('/') else file) for file in files)


def _model(variable: str) -> dict:
    return merge([load(path) for path in _set_files(variable)])


def _environment(node: object) -> dict[str, str | None]:
    """An environment: block as a mapping, either form; None for a list entry that only passes a name through."""
    if node is None:
        return {}
    if isinstance(node, dict):
        return {str(name): None if value is None else str(value) for name, value in node.items()}
    entries: dict[str, str | None] = {}
    for entry in node:
        name, separator, value = str(entry).partition('=')
        entries[name.strip()] = value if separator else None
    return entries


def _data_ingest_environment(variable: str) -> dict[str, str | None]:
    services = _model(variable)['services']
    assert DATA_INGEST in services, f'{variable} does not run {DATA_INGEST}'
    return _environment(services[DATA_INGEST].get('environment'))


def _rendered(text: object, env: Mapping[str, str]) -> str:
    """TEXT interpolated under ENV. A ':?' guard that refuses gets a placeholder that names no service.

    The guard means the real launch supplies a value (the agent stack generates its own names), and
    no generated value is the bind alias, so a placeholder stands in for it.
    """
    env = dict(env)
    while True:
        try:
            return interpolate(str(text), env)
        except InterpolationRefused as refused:
            env[refused.variable] = f'placeholder-for-{refused.variable.lower()}'


def _bind_values(variable: str, env: Mapping[str, str]) -> tuple[str, str]:
    environment = _data_ingest_environment(variable)
    return tuple(_rendered(environment.get(name) or '', env).strip() for name in BIND_VARIABLES)


def _is_address_literal(host: str) -> bool:
    """True for anything the resolver reads as an address rather than a name: '0', '127.1', '::', '0x7f.1'."""
    try:
        ipaddress.ip_address(host.strip('[]'))
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


# --- Item 1: both variables in every launch set ----------------------------------------------------


def test_every_makefile_set_that_runs_data_ingest_is_pinned():
    """The launch sets below are exactly the Makefile's compose sets whose merged model runs data_ingest."""
    variables = sorted(set(_COMPOSE_SET.findall(MAKEFILE.read_text(encoding='utf-8'))))
    assert set(variables) >= GATE_LAUNCH_SETS, f'the Makefile no longer names {sorted(GATE_LAUNCH_SETS)}: {variables}'
    running = {variable for variable in variables if DATA_INGEST in _model(variable)['services']}
    assert running == set(DATA_INGEST_LAUNCH_SETS), (
        f'the Makefile sets that run {DATA_INGEST} are {sorted(running)}, but this module pins '
        f'{sorted(DATA_INGEST_LAUNCH_SETS)}. A new launch set must carry the gRPC bind values too.'
    )


@pytest.mark.parametrize('launch_set', DATA_INGEST_LAUNCH_SETS)
def test_the_base_file_sets_both_bind_variables_and_no_overlay_touches_them(launch_set: str):
    """Item 1: the values live in the base file's environment:, which outranks every env_file.

    Declared by the base file, which every set loads, and by no other file in the set, so no overlay
    blanks, overrides or passes either through from the shell. The agent stack's env files are
    generated once and a user's .env predates these variables, so a value anywhere else would miss
    one of them (ruling on tj-3mk3u5.24).
    """
    files = _set_files(launch_set)
    assert BASE_FILE in files, f'{launch_set} does not load {BASE_FILE.name}: {[path.name for path in files]}'
    base = _environment(((load(BASE_FILE).get('services') or {}).get(DATA_INGEST) or {}).get('environment'))
    unset = [name for name in BIND_VARIABLES if base.get(name) is None]
    assert not unset, f'{BASE_FILE.name} does not set {unset} in {DATA_INGEST} environment:'

    touched = {}
    for path in files:
        if path == BASE_FILE:
            continue
        service = (load(path).get('services') or {}).get(DATA_INGEST) or {}
        if names := sorted(set(BIND_VARIABLES) & set(_environment(service.get('environment')))):
            touched[path.name] = names
    assert not touched, (
        f'{launch_set}: an overlay sets the gRPC bind variables, which only the base file may: {touched}'
    )


@pytest.mark.parametrize('launch_set', DATA_INGEST_LAUNCH_SETS)
def test_every_launch_set_renders_both_bind_variables_non_blank(launch_set: str):
    """Item 1, as the container receives them: merged, then interpolated under every env the bead names."""
    blank = {
        env_name: [name for name, value in zip(BIND_VARIABLES, _bind_values(launch_set, env), strict=True) if not value]
        for env_name, env in _interpolation_envs().items()
    }
    blank = {env_name: names for env_name, names in blank.items() if names}
    assert not blank, f'{launch_set} hands {DATA_INGEST} a blank gRPC bind value, so from_env refuses it: {blank}'


# --- Item 2: the host is an ingest_store-only alias ------------------------------------------------


def _name_uses(model: dict, host: str, env: Mapping[str, str]) -> tuple[set[tuple[str, str]], list[str]]:
    """Where HOST is declared: ({(service, network)} carrying it as an alias, [every other kind of use]).

    The other kinds are each a way a container could answer to, or be told, the name outside the
    ingest_store alias: a service name, hostname, container name, extra_hosts entry or link alias.
    """
    wanted = host.lower()
    aliases, others = set(), []
    for name, service in (model.get('services') or {}).items():
        service = service or {}
        if name.lower() == wanted:
            others.append(f'the service name {name!r}')
        for key in ('hostname', 'container_name'):
            if key in service and _rendered(service[key], env).lower() == wanted:
                others.append(f'{name} {key}')
        networks = service.get('networks')
        for network, config in networks.items() if isinstance(networks, dict) else []:
            for alias in (config or {}).get('aliases') or []:
                if _rendered(alias, env).lower() == wanted:
                    aliases.add((name, network))
        extra_hosts = service.get('extra_hosts') or []
        entries = (
            [f'{key}:{value}' for key, value in extra_hosts.items()] if isinstance(extra_hosts, dict) else extra_hosts
        )
        if any(re.split(r'[:=]', _rendered(entry, env), maxsplit=1)[0].lower() == wanted for entry in entries):
            others.append(f'{name} extra_hosts')
        for key in ('links', 'external_links'):
            if any(_rendered(link, env).split(':')[-1].lower() == wanted for link in service.get(key) or []):
                others.append(f'{name} {key}')
    return aliases, others


@pytest.mark.parametrize('launch_set', DATA_INGEST_LAUNCH_SETS)
def test_the_bind_host_is_an_alias_on_ingest_store_and_nowhere_else(launch_set: str):
    """Item 2 (ADR tj-q9ae5u addendum 3): the alias on data_ingest's ingest_store attachment, alone.

    An alias on any other network -- ingest_egress, or devnet in dev -- makes the name resolve to
    that address too, and the server could bind where data_store cannot reach it and still start
    cleanly. The service name, hostname or container name maps to whichever address /etc/hosts got
    by attach order. Literal: the same under every interpolation env, so no env file can move it.
    (Should tj-3mk3u5.25 take its optional dev reach -- the same alias on devnet, in the override --
    its validator re-points the DEV and TOOLS cases here.)
    """
    model = _model(launch_set)
    envs = _interpolation_envs()
    hosts = {_bind_values(launch_set, env)[0] for env in envs.values()}
    assert len(hosts) == 1, f'{launch_set}: {GRPC_HOST_ENV} renders differently by env file: {sorted(hosts)}'
    host = hosts.pop()
    assert _RFC1123.match(host), f'{launch_set}: {GRPC_HOST_ENV}={host!r} is not a plain RFC 1123 name'
    assert not _is_address_literal(host), f'{launch_set}: {GRPC_HOST_ENV}={host!r} is an address, not the alias'
    assert host.lower() != 'localhost' and not host.lower().endswith('.localhost'), (
        f'{launch_set}: {GRPC_HOST_ENV}={host!r} is loopback; the peer could never reach it'
    )
    for env_name, env in envs.items():
        aliases, others = _name_uses(model, host, env)
        assert aliases == {(DATA_INGEST, BIND_NETWORK)}, (
            f'{launch_set} under {env_name}: {host!r} must be an alias on {DATA_INGEST}/{BIND_NETWORK} only; '
            f'it is an alias on {sorted(aliases)}'
        )
        assert not others, f'{launch_set} under {env_name}: {host!r} is also {others}; the alias must be its only use'


# --- Item 3: the port, with or without an env file value -------------------------------------------


@pytest.mark.parametrize('launch_set', DATA_INGEST_LAUNCH_SETS)
def test_the_bind_port_renders_to_one_valid_port_with_or_without_an_env_file_value(
    launch_set: str, monkeypatch: pytest.MonkeyPatch
):
    """Item 3: an integer 1..65535 under every interpolation env, and the same one each way.

    The fallback exists for env files written before DATA_INGEST_GRPC_PORT did, and names the real
    in-network port, so it must be the port .env.default sets: a fallback that differed would bind
    one port under an old env file and another under a fresh one. Then production's own reader is
    handed what compose renders, as the container would hand it.
    """
    rendered = {env_name: _bind_values(launch_set, env) for env_name, env in _interpolation_envs().items()}
    invalid = {
        env_name: port
        for env_name, (_, port) in rendered.items()
        if not re.fullmatch(r'[0-9]+', port) or not 1 <= int(port) <= 65535
    }
    assert not invalid, f'{launch_set}: {GRPC_PORT_ENV} does not render to a port 1..65535: {invalid}'
    ports = {port for _, port in rendered.values()}
    assert len(ports) == 1, f'{launch_set}: the fallback and .env.default disagree on the port: {rendered}'

    for host, port in rendered.values():
        monkeypatch.setenv(GRPC_HOST_ENV, host)
        monkeypatch.setenv(GRPC_PORT_ENV, port)
        bind = BindAddress.from_env()
        assert (bind.host, bind.port) == (host, int(port)), bind


# --- Item 4: System Testing's Check gRPC Bind Network ----------------------------------------------

HOST = 'data-ingest-grpc'
PORT = '50051'
STORE_V4, STORE_V6 = '172.20.0.3', 'fd00:20::3'
EGRESS_V4, EGRESS_V6 = '172.21.0.2', 'fd00:21::2'
DEVNET_V4 = '172.30.0.5'
CONTAINER_ID = '3f2a9c'
STORE_NET, EGRESS_NET, DEVNET = 'trader_joe_ingest_store', 'trader_joe_ingest_egress', 'trader_joe_devnet'
DOCKER_SHIM = '#!/bin/sh\nexec "$STUB_PYTHON" -I "$STUB_IMPL" "$@"\n'


def _net(ipv4: str = '', ipv6: str = '') -> dict[str, str]:
    return {'IPAddress': ipv4, 'GlobalIPv6Address': ipv6}


def _scenario(**changes: object) -> dict:
    """A healthy prod-shaped stack: the alias answers with data_ingest's ingest_store address alone."""
    scenario = {
        'container_env': {GRPC_HOST_ENV: HOST, GRPC_PORT_ENV: PORT, 'PATH': '/usr/local/bin:/usr/bin:/bin'},
        'resolves': {HOST: [STORE_V4]},
        'ps_id': CONTAINER_ID,
        'exec_fails': False,
        'networks': {STORE_NET: _net(STORE_V4), EGRESS_NET: _net(EGRESS_V4)},
    }
    scenario.update(changes)
    return scenario


def _without(name: str) -> dict[str, str]:
    return {key: value for key, value in _scenario()['container_env'].items() if key != name}


def _with(name: str, value: str) -> dict[str, str]:
    return {**_scenario()['container_env'], name: value}


def _run_step(tmp_path: Path, scenario: dict) -> tuple[subprocess.CompletedProcess, list[list[str]], list[dict]]:
    """Run the step's own script as GitHub runs a `run:` with no shell (bash -e), docker stubbed."""
    bash = shutil.which('bash')
    assert bash, 'bash is not on PATH, so the step cannot be exercised'
    script = _system_step(GRPC_BIND_STEP).get('run') or ''
    assert '${{' not in script, 'the step now uses a workflow expression, which this test cannot evaluate'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    shim = bin_dir / 'docker'
    shim.write_text(DOCKER_SHIM, encoding='utf-8')
    shim.chmod(0o755)
    (tmp_path / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
    (tmp_path / 'step.sh').write_text(script, encoding='utf-8')
    env = {
        'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}',
        'STUB_PYTHON': sys.executable,
        'STUB_IMPL': stub.__file__,
        'STUB_SCENARIO': str(tmp_path / 'scenario.json'),
        'STUB_LOG': str(tmp_path / 'docker.log'),
        'STUB_GETADDRINFO_LOG': str(tmp_path / 'getaddrinfo.log'),
    }
    result = subprocess.run(
        [bash, '--noprofile', '--norc', '-e', str(tmp_path / 'step.sh')],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    def lines(name: str) -> list:
        path = tmp_path / name
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    return result, lines('docker.log'), lines('getaddrinfo.log')


def _report(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


_PASSING = {
    'prod: the alias answers with the ingest_store address': _scenario(),
    'dual-stack: both ingest_store families': _scenario(
        resolves={HOST: [STORE_V4, STORE_V6]},
        networks={STORE_NET: _net(STORE_V4, STORE_V6), EGRESS_NET: _net(EGRESS_V4, EGRESS_V6)},
    ),
    'dev: devnet attached, the alias still on ingest_store alone': _scenario(
        networks={STORE_NET: _net(STORE_V4), EGRESS_NET: _net(EGRESS_V4), DEVNET: _net(DEVNET_V4)}
    ),
    'another project prefix (the agent stack)': _scenario(
        networks={
            'trader_joe_agent_stack_ingest_store': _net(STORE_V4),
            'trader_joe_agent_stack_ingest_egress': _net(EGRESS_V4),
        }
    ),
    'the resolver repeats an address': _scenario(resolves={HOST: [STORE_V4, STORE_V4]}),
}


@pytest.mark.parametrize('scenario', list(_PASSING.values()), ids=list(_PASSING))
def test_the_bind_check_passes_when_the_name_resolves_to_the_ingest_store_address_alone(tmp_path: Path, scenario: dict):
    result, docker, _ = _run_step(tmp_path, scenario)
    assert result.returncode == 0, _report(result)
    execs = [call for call in docker if call[:4] == ['compose', '-f', 'docker-compose.yaml', 'exec']]
    assert len(execs) == 3, f'expected two env reads and one resolution inside {DATA_INGEST}, saw {docker}'
    assert [docker[-1][0], docker[-1][-1]] == ['inspect', CONTAINER_ID], (
        f'the last call must inspect {CONTAINER_ID}: {docker}'
    )


# Each must exit 1: the step's own refusal, never a stub that met a call it does not model (97).
_FAILING = {
    # The alias design gone wrong, which only a running stack could show.
    'the name answers with the egress address': (_scenario(resolves={HOST: [EGRESS_V4]}), [EGRESS_V4, STORE_V4]),
    'the alias on ingest_egress too': (_scenario(resolves={HOST: [STORE_V4, EGRESS_V4]}), [EGRESS_V4, STORE_V4]),
    'the alias on devnet too (dev)': (
        _scenario(
            resolves={HOST: [STORE_V4, DEVNET_V4]},
            networks={STORE_NET: _net(STORE_V4), EGRESS_NET: _net(EGRESS_V4), DEVNET: _net(DEVNET_V4)},
        ),
        [DEVNET_V4, STORE_V4],
    ),
    'dual-stack, the name answers IPv4 only': (
        _scenario(networks={STORE_NET: _net(STORE_V4, STORE_V6), EGRESS_NET: _net(EGRESS_V4)}),
        [STORE_V4, STORE_V6],
    ),
    # A host the server must never bind.
    'the wildcard 0.0.0.0': (
        _scenario(container_env=_with(GRPC_HOST_ENV, '0.0.0.0'), resolves={'0.0.0.0': ['0.0.0.0']}),
        ['0.0.0.0', STORE_V4],
    ),
    'loopback': (
        _scenario(container_env=_with(GRPC_HOST_ENV, 'localhost'), resolves={'localhost': ['127.0.0.1']}),
        ['127.0.0.1', STORE_V4],
    ),
    # The base file's environment: block not reaching the launch.
    'host unset': (_scenario(container_env=_without(GRPC_HOST_ENV)), []),
    'host empty': (_scenario(container_env=_with(GRPC_HOST_ENV, '')), []),
    'host blank': (_scenario(container_env=_with(GRPC_HOST_ENV, '   ')), []),
    'port unset': (_scenario(container_env=_without(GRPC_PORT_ENV)), []),
    'port empty': (_scenario(container_env=_with(GRPC_PORT_ENV, '')), []),
    # Nothing to compare against, or nothing to read from.
    'the name does not resolve': (_scenario(resolves={}), []),
    'data_ingest is not running': (_scenario(exec_fails=True), []),
    'no data_ingest container to inspect': (_scenario(ps_id=''), []),
    'not on any *_ingest_store network': (_scenario(networks={EGRESS_NET: _net(EGRESS_V4)}), []),
    'two *_ingest_store networks': (
        _scenario(networks={STORE_NET: _net(STORE_V4), 'other_ingest_store': _net('172.22.0.4')}),
        [],
    ),
    'inspect reports no ingest_store address': (
        _scenario(networks={STORE_NET: _net(), EGRESS_NET: _net(EGRESS_V4)}),
        [],
    ),
}


@pytest.mark.parametrize(('scenario', 'printed'), list(_FAILING.values()), ids=list(_FAILING))
def test_the_bind_check_fails_on_anything_else_and_prints_both_sides(
    tmp_path: Path, scenario: dict, printed: list[str]
):
    """Item 4: a mismatch, a missing variable or a missing fact fails the step; a mismatch prints both sets."""
    result, _, _ = _run_step(tmp_path, scenario)
    assert result.returncode == 1, _report(result)
    assert 'not modelled' not in result.stderr, f'the step made a call the stub does not model:\n{_report(result)}'
    unprinted = [address for address in printed if address not in result.stdout]
    assert not unprinted, f'the step failed without printing {unprinted}:\n{_report(result)}'


def test_the_bind_check_resolves_with_the_call_refuse_wildcard_makes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The step proves the name the way the server will use it: getaddrinfo with refuse_wildcard's arguments.

    Both calls go through the same recording stand-in, so a step that resolved differently (another
    socktype, a family filter, a port) would compare one lookup's answer with another's.
    """
    result, _, step_calls = _run_step(tmp_path, _scenario())
    assert result.returncode == 0, _report(result)

    server_log = tmp_path / 'server-getaddrinfo.log'
    monkeypatch.setattr(socket, 'getaddrinfo', stub.recording_getaddrinfo({HOST: [STORE_V4]}, str(server_log)))
    asyncio.run(refuse_wildcard(HOST))
    server_calls = [json.loads(line) for line in server_log.read_text(encoding='utf-8').splitlines()]
    assert server_calls, 'refuse_wildcard made no getaddrinfo call through the patched socket module'
    assert step_calls == server_calls, f'the step resolves with {step_calls}, refuse_wildcard with {server_calls}'


# The stand-in's own template renderer, checked against output Go's text/template gives for the
# same template and data, so the inspect half of the step is judged on Go's semantics.
_TEMPLATE = '{{range $name, $net := .NetworkSettings.Networks}}{{$name}} {{$net.IPAddress}}{{println}}{{end}}'


def test_the_stub_renders_go_template_ranges_in_sorted_key_order():
    data = {'NetworkSettings': {'Networks': {'b_net': {'IPAddress': '10.0.0.2'}, 'a_net': {'IPAddress': '10.0.0.1'}}}}
    assert stub.render_template(_TEMPLATE, data) == 'a_net 10.0.0.1\nb_net 10.0.0.2\n'


@pytest.mark.parametrize(
    'template',
    [
        '{{json .NetworkSettings.Networks}}',
        '{{range $name, $net := .NetworkSettings.Networks}}{{$net.MacAddress}}{{end}}',
        '{{- range $name, $net := .NetworkSettings.Networks}}{{end}}',
        '{{range $name, $net := .NetworkSettings.Networks}}',
    ],
    ids=['a function', 'a field the scenario lacks', 'trimming', 'an unclosed range'],
)
def test_the_stub_refuses_what_it_does_not_model(template: str):
    data = {'NetworkSettings': {'Networks': {'a_net': {'IPAddress': '10.0.0.1'}}}}
    with pytest.raises(stub.Unmodelled):
        stub.render_template(template, data)
