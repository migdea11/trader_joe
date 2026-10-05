"""A stand-in `docker` CLI for System Testing's Check gRPC Peer Reach and Check Network Lockdown (tj-3mk3u5.25).

No test functions here. test_grpc_peer_reach.py puts a two-line shell `docker` on PATH that runs this
file with the docker arguments, and describes the stack through the environment:

  STUB_SCENARIO         a JSON file:
                          container_env       data_ingest's container environment, a mapping
                          exec_fails          services whose exec fails, as on a container that is not running
                          ps_id, networks     as in grpc_bind_docker_stub: what `compose ps -q data_ingest`
                                              prints, and data_ingest's NetworkSettings.Networks
                          store_dns           {name: address}: what data_store's resolver answers for the host
                                              of a gRPC target. The channel dials that address on the step's own
                                              port, so a real local server answers the step's real Health.Check.
                          reach               {container: {"address port": errno}}: what a TCP connect from that
                                              container returns, 0 for connected
                          client_resolves     {name: [address, ...]}: what `getent hosts` answers in test_client;
                                              any other name is getent's not-found, exit 2
                          client_python_exit  when set, a test_client run of the venv interpreter fails with this
                                              status before running anything (a missing image, a compose error)
  STUB_LOG              each call's arguments, appended as one JSON array per line
  STUB_GRPC_LOG         each target data_store's code hands grpc.insecure_channel, before resolution
  STUB_CONNECT_LOG      each TCP connect a step makes: container, family, host, port
  STUB_POSTGRES_BIN     a directory holding a `timeout` shim that runs this file as postgres's network

WHAT IS NOT IMITATED. Every `-c CODE` a step runs -- `compose exec -T data_ingest|data_store
/code/.venv/bin/python -c CODE ARGS` and `compose run ... --entrypoint /code/.venv/bin/python test_client
-c CODE ARGS` -- is the step's own code, run in this process: what it prints and how it exits are the
step's. In data_store, the step's grpc channel is real and calls a real health server; only the name
in its target is resolved through store_dns. postgres's `bash -c CODE` runs under the real bash, with
this file standing in for its `timeout` (the /dev/tcp connect). What stands in is the network alone:
which names resolve where, which address a container can connect to. `docker inspect --format` renders
the step's own Go template with grpc_bind_docker_stub's renderer. Any call or option outside these
exits UNMODELLED, naming it, so a rewritten step turns the test red instead of meeting a canned answer.

What only Docker can show -- the embedded DNS, the internal networks' real routing, compose's exec and
run, test_client's real refusal -- is CI System Testing's run of the steps themselves.
"""

import json
import os
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from common.tests.grpc_bind_docker_stub import UNMODELLED, VENV_PYTHON, Unmodelled, render_template


DATA_INGEST = 'data_ingest'
DATA_STORE = 'data_store'
POSTGRES = 'postgres'
TEST_CLIENT = 'test_client'
STACK_FILES = ['docker-compose.yaml']
CLIENT_FILES = ['docker-compose.yaml', 'docker-compose.test-client.yaml']
CLIENT_RUN_FLAGS = frozenset({'--rm', '--no-deps', '-T'})
TIMEOUT_MODE = '__timeout__'

_DEV_TCP = re.compile(r'^exec 3<>/dev/tcp/([^/\s]+)/([0-9]+)$')
# This process's own STUB_* settings, read once at start: a step's code runs under the container's
# environment, which replaces os.environ before the code's connects are logged.
_SETTINGS = {name: value for name, value in os.environ.items() if name.startswith('STUB_') or name == 'PYTHONPATH'}


def _log(variable: str, record: object) -> None:
    path = _SETTINGS.get(variable)
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record) + '\n')


def _connect_result(container: str, host: str, port: int, family: int, scenario: Mapping[str, Any]) -> int:
    """The errno a connect from CONTAINER to HOST:PORT gets, as the scenario's network decides it."""
    _log('STUB_CONNECT_LOG', {'container': container, 'family': int(family), 'host': host, 'port': port})
    answers = (scenario.get('reach') or {}).get(container) or {}
    key = f'{host} {port}'
    if key not in answers:
        raise Unmodelled(f'a connect from {container} to {host}:{port}, which the scenario does not route')
    return int(answers[key])


def _probe_socket(container: str, scenario: Mapping[str, Any]) -> type:
    """A socket.socket stand-in for CONTAINER: connect_ex answers from the scenario, nothing else is modelled."""

    class ProbeSocket:
        def __init__(self, family: int = socket.AF_INET, type: int = socket.SOCK_STREAM, proto: int = 0, fileno=None):
            if fileno is not None or type != socket.SOCK_STREAM:
                raise Unmodelled(f'a socket of type {type} or from a descriptor')
            self.family = socket.AddressFamily(family)

        def settimeout(self, value: float | None) -> None:
            self.timeout = value

        def connect_ex(self, address: tuple) -> int:
            host, port = str(address[0]), int(address[1])
            literal = socket.AF_INET6 if ':' in host else socket.AF_INET
            if self.family != literal:
                # What the real call raises for an address literal of the other family.
                raise socket.gaierror(socket.EAI_ADDRFAMILY, 'Address family for hostname not supported')
            return _connect_result(container, host, port, self.family, scenario)

        def connect(self, address: tuple) -> None:
            raise Unmodelled('socket.connect(); the steps probe with connect_ex')

        def close(self) -> None:
            pass

    return ProbeSocket


def _patch_grpc_resolution(scenario: Mapping[str, Any]) -> None:
    """Resolve the host of each insecure_channel target through store_dns; the channel itself stays real."""
    import grpc

    real = grpc.insecure_channel
    names = scenario.get('store_dns') or {}

    def insecure_channel(target: str, options=None, compression=None):
        _log('STUB_GRPC_LOG', {'target': target})
        host, separator, port = target.rpartition(':')
        if not separator or host not in names:
            raise Unmodelled(f'data_store resolving the gRPC target {target!r}, which the scenario does not name')
        return real(f'{names[host]}:{port}', options, compression)

    grpc.insecure_channel = insecure_channel


def _run_code(container: str, code: str, arguments: list[str], environment: Mapping[str, str], scenario) -> int:
    """Run a step's `python -c CODE ARGS` as CONTAINER would: its environment, its network."""
    if container == DATA_STORE:
        _patch_grpc_resolution(scenario)
    os.environ.clear()
    os.environ.update(environment)
    socket.socket = _probe_socket(container, scenario)
    sys.argv = ['-c', *arguments]
    # The step's own code, as `python -c` runs it: an uncaught exception prints its traceback and
    # exits 1, and SystemExit sets the status.
    exec(compile(code, '<string>', 'exec'), {'__name__': '__main__'})
    return 0


def _options(words: list[str], flags: set[str], valued: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Split leading options from the rest; any option not in FLAGS or VALUED is unmodelled."""
    found: dict[str, list[str]] = {}
    position = 0
    while position < len(words) and words[position].startswith('-'):
        option = words[position]
        if option in valued and position + 1 < len(words):
            found.setdefault(option, []).append(words[position + 1])
            position += 2
        elif option in flags:
            found.setdefault(option, []).append('')
            position += 1
        else:
            raise Unmodelled(f'option {option}')
    return found, words[position:]


def _postgres(command: list[str]) -> int:
    """Run postgres's `bash -c CODE ARGS` under the real bash, with `timeout` (its connect) standing in."""
    if command[:2] != ['bash', '-c'] or len(command) < 3:
        raise Unmodelled(f'exec of {command[:2]} in {POSTGRES}; only bash -c is modelled')
    environment = {'PATH': f'{_SETTINGS["STUB_POSTGRES_BIN"]}{os.pathsep}/usr/bin{os.pathsep}/bin', **_SETTINGS}
    return subprocess.run(['bash', '--noprofile', '--norc', *command[1:]], env=environment, check=False).returncode


def _timeout(words: list[str], scenario: Mapping[str, Any]) -> int:
    """Stand in for postgres's `timeout SECONDS bash -c "exec 3<>/dev/tcp/HOST/PORT"`: 0 on connect, else 1."""
    target = _DEV_TCP.match(words[3]) if len(words) == 4 and words[1:3] == ['bash', '-c'] else None
    if not target or not words[0].isdigit():
        raise Unmodelled(f'timeout {words}; only a bounded /dev/tcp connect is modelled')
    host, port = target.group(1), int(target.group(2))
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    return 0 if _connect_result(POSTGRES, host, port, family, scenario) == 0 else 1


def _exec(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, rest = _options(words, {'-T'}, set())
    if '-T' not in options:
        raise Unmodelled('exec without -T, which allocates a TTY a CI step does not have')
    service, command = (rest[0], rest[1:]) if rest else ('', [])
    if service not in (DATA_INGEST, DATA_STORE, POSTGRES):
        raise Unmodelled(f'exec into {service!r}')
    if service in (scenario.get('exec_fails') or []):
        print(f'service "{service}" is not running', file=sys.stderr)
        return 1
    if service == POSTGRES:
        return _postgres(command)
    if command[:2] != [VENV_PYTHON, '-c'] or len(command) < 3:
        raise Unmodelled(f'exec of {command[:2]} in {service}; only the image interpreter running -c is modelled')
    environment = (scenario.get('container_env') or {}) if service == DATA_INGEST else {}
    return _run_code(service, command[2], command[3:], environment, scenario)


def _run(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, rest = _options(words, set(CLIENT_RUN_FLAGS), {'--pull', '--entrypoint'})
    if not set(options) >= CLIENT_RUN_FLAGS or options.get('--pull') != ['never'] or rest[:1] != [TEST_CLIENT]:
        raise Unmodelled(f'run with {sorted(options)} of {rest[:1]}; only a removed, no-deps, no-TTY test_client')
    entrypoint, arguments = (options.get('--entrypoint') or [''])[-1], rest[1:]
    if entrypoint == 'getent':
        if len(arguments) != 2 or arguments[0] != 'hosts':
            raise Unmodelled(f'getent {arguments}')
        addresses = (scenario.get('client_resolves') or {}).get(arguments[1]) or []
        for address in addresses:
            print(f'{address}      {arguments[1]}')
        return 0 if addresses else 2
    if entrypoint == VENV_PYTHON and arguments[:1] == ['-c'] and len(arguments) >= 2:
        if (status := scenario.get('client_python_exit')) is not None:
            print(f'test_client: the interpreter did not run (exit {status})', file=sys.stderr)
            return int(status)
        return _run_code(TEST_CLIENT, arguments[1], arguments[2:], {}, scenario)
    raise Unmodelled(f'test_client entrypoint {entrypoint!r} with {arguments[:1]}')


def _compose(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, rest = _options(words, set(), {'-f', '--file'})
    files = options.get('-f', []) + options.get('--file', [])
    if files == CLIENT_FILES and rest[:1] == ['run']:
        return _run(rest[1:], scenario)
    if files != STACK_FILES:
        raise Unmodelled(f'compose files {files} for {rest[:1]}')
    if rest == ['ps', '-q', DATA_INGEST]:
        if scenario.get('ps_id'):
            print(scenario['ps_id'])
        return 0
    if rest[:1] == ['exec']:
        return _exec(rest[1:], scenario)
    raise Unmodelled(f'compose {rest[:1]}')


def _inspect(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, objects = _options(words, set(), {'--format', '-f'})
    formats = options.get('--format', []) + options.get('-f', [])
    if len(formats) != 1 or not objects:
        raise Unmodelled(f'inspect with formats {formats} of {objects}')
    data = {'Id': scenario.get('ps_id'), 'NetworkSettings': {'Networks': scenario.get('networks') or {}}}
    for name in objects:
        if not scenario.get('ps_id') or name != scenario['ps_id']:
            print(f'Error: No such object: {name}', file=sys.stderr)
            return 1
        print(render_template(formats[0], data))
    return 0


def main(argv: list[str]) -> int:
    scenario = json.loads(Path(_SETTINGS['STUB_SCENARIO']).read_text(encoding='utf-8'))
    try:
        # postgres's `timeout` is not a docker call, so it is not logged as one.
        if argv[:1] == [TIMEOUT_MODE]:
            return _timeout(argv[1:], scenario)
        _log('STUB_LOG', argv)
        if argv[:1] == ['compose']:
            return _compose(argv[1:], scenario)
        if argv[:1] == ['inspect']:
            return _inspect(argv[1:], scenario)
        raise Unmodelled(f'docker {argv[:1]}')
    except Unmodelled as unmodelled:
        message = f'docker stub: not modelled: {unmodelled}: docker {shlex.join(argv)}'
        print(message, file=sys.stderr)
        # Also on file: the postgres probe sends its `timeout`'s stderr to /dev/null, and a step
        # that folds every failure into its own exit status would hide the 97.
        _log('STUB_UNMODELLED_LOG', message)
        return UNMODELLED


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
