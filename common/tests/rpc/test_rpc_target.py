"""The peer-target reader in common/rpc/channel.py, target_from_env (tj-3mk3u5.8; ADR tj-q9ae5u addendum 5 item 3).

The design: one environment variable holds data_ingest's gRPC host:port, set by one entry in data_store's
base compose environment (pinned in common/tests/test_ingest_grpc_target.py), and target_from_env is its one
reader. It is read when called, never at import, with NO DEFAULT. Every refusal is a ValueError naming the
variable, so a misconfiguration fails data_store's startup saying what to fix, never later as a
DEADLINE_EXCEEDED against a peer that is up. It checks the string only and never resolves the name, because
nothing resolves the peer at startup (ADR tj-8konfu D6.5, O1). It loads no generated code, so callers outside
common/rpc -- routers/common/latency.py imports it at module level in both services -- stay on the right side
of the TID251 seam whether the harness is on or off.

The two refusals the builder added beyond item 3, an empty host and an unbracketed IPv6 host, are pinned too:
both name an address gRPC cannot dial as written.

Each property that a fresh interpreter can see differently from this one -- what an import loads, whether a
call reaches the resolver -- is checked in a child process, which reads no pytest.ini, so it is handed the
image's import path (common/tests/image_path.py). That keeps the generated tree IMPORTABLE there, so 'loads
no generated module' is a finding, not an accident of a path that could not have found it.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import common.rpc.channel as channel_module
from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, target_from_env
from common.tests.image_path import REPO_ROOT, image_pythonpath


pytestmark = pytest.mark.common

VARIABLE = DATA_INGEST_GRPC_TARGET_ENV
# A second name, so a message that names the variable is shown to name the one it was GIVEN.
OTHER_VARIABLE = 'TJ_TEST_SOME_OTHER_GRPC_TARGET'
GENERATED_PACKAGE = 'trader_joe'
CHANNEL_SOURCE = Path(channel_module.__file__)
CHILD_TIMEOUT_S = 60

# Each refusal of item 3 and of the builder's two additions, as (id, value). None is unset.
REFUSED = [
    ('unset', None),
    ('empty', ''),
    ('blank', '   '),
    ('whitespace', '\t\n'),
    ('no port', 'data-ingest-grpc'),
    ('empty port', 'data-ingest-grpc:'),
    ('non-integer port', 'data-ingest-grpc:abc'),
    ('signed port', 'data-ingest-grpc:+5'),
    ('negative port', 'data-ingest-grpc:-1'),
    ('exponent port', 'data-ingest-grpc:5e4'),
    ('padded port', 'data-ingest-grpc: 50051'),
    # 50051 in Arabic-Indic digits: str.isdigit() and int() both accept them; a port is ASCII.
    ('non-ascii digits', 'data-ingest-grpc:' + ''.join(chr(0x0660 + int(digit)) for digit in '50051')),
    ('port 0', 'data-ingest-grpc:0'),
    ('port 65536', 'data-ingest-grpc:65536'),
    ('huge port', 'data-ingest-grpc:99999999999999999999'),
    ('ipv4 wildcard', '0.0.0.0:50051'),
    ('ipv6 wildcard', '[::]:50051'),
    ('ipv6 wildcard spelled out', '[0:0:0:0:0:0:0:0]:50051'),
    ('ipv6 wildcard with a zone', '[::%eth0]:50051'),
    ('bare ipv6 wildcard', '::'),
    ('bracketed wildcard, no port', '[::]'),
    ('empty host', ':50051'),
    ('empty brackets', '[]:50051'),
    ('unbracketed ipv6 loopback', '::1:50051'),
    ('unbracketed ipv6', 'fd00::4:50051'),
    ('unbracketed ipv6 wildcard', '::50051'),
    ('unclosed bracket', '[fd00::4:50051'),
    ('bracketed, no port', '[fd00::4]'),
    ('bracketed, no colon', '[fd00::4]50051'),
]
# The wildcard refusals alone: item 3's 'literal unspecified host', each with a valid port, so nothing but
# the wildcard check can refuse it.
WILDCARDS = ['0.0.0.0:50051', '[::]:50051', '[0:0:0:0:0:0:0:0]:50051', '[::%eth0]:50051']

# (value as set, what the reader returns)
ACCEPTED = [
    ('data-ingest-grpc:50051', 'data-ingest-grpc:50051'),
    ('  data-ingest-grpc:50051 \n', 'data-ingest-grpc:50051'),
    ('data-ingest-grpc:1', 'data-ingest-grpc:1'),
    ('data-ingest-grpc:65535', 'data-ingest-grpc:65535'),
    ('10.0.0.5:50051', '10.0.0.5:50051'),
    ('[fd00::4]:50051', '[fd00::4]:50051'),
    ('[fe80::1%eth0]:50051', '[fe80::1%eth0]:50051'),
    # A name that cannot resolve is still accepted: resolving it is the first call's job (D6.5 O1).
    ('no-such-host.invalid:50051', 'no-such-host.invalid:50051'),
]


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def _child(script: str, *, env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Run SCRIPT in a fresh interpreter on the image's import path, from the repository root."""
    env = {key: value for key, value in os.environ.items() if key != 'PYTEST_ADDOPTS'}
    env['PYTHONPATH'] = image_pythonpath()
    for name, value in env_overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return subprocess.run(
        [sys.executable, '-c', script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )


# --- The refusals --------------------------------------------------------------------------------------


@pytest.mark.parametrize(('case', 'value'), REFUSED, ids=[case for case, _ in REFUSED])
def test_every_malformed_or_missing_target_is_refused_naming_the_variable(
    case: str, value: str | None, monkeypatch: pytest.MonkeyPatch
):
    """Item 3: unset, blank, no port, a port that is not 1-65535, a wildcard, an empty or unbracketed host."""
    for name in (VARIABLE, OTHER_VARIABLE):
        _set(monkeypatch, name, value)
        with pytest.raises(ValueError, match=name) as raised:
            target_from_env(name)
        assert name in str(raised.value), f'{case}: the refusal does not name {name}: {raised.value}'


@pytest.mark.parametrize('value', WILDCARDS)
def test_a_wildcard_host_is_refused_although_its_port_is_valid(value: str, monkeypatch: pytest.MonkeyPatch):
    """Item 3's literal unspecified host: a client 'dialing' 0.0.0.0 or :: reaches whatever listens locally."""
    monkeypatch.setenv(VARIABLE, value)
    with pytest.raises(ValueError, match=VARIABLE):
        target_from_env(VARIABLE)


def test_there_is_no_default_when_the_variable_is_unset(monkeypatch: pytest.MonkeyPatch):
    """No default in code (addendum 5 items 3-4; option B rejected): unset is a refusal, never a fallback address."""
    monkeypatch.delenv(VARIABLE, raising=False)
    with pytest.raises(ValueError, match=f'{VARIABLE} is not set'):
        target_from_env(VARIABLE)


# --- The accepted forms ---------------------------------------------------------------------------------


@pytest.mark.parametrize(('value', 'expected'), ACCEPTED, ids=[value.strip() for value, _ in ACCEPTED])
def test_a_valid_target_comes_back_stripped(value: str, expected: str, monkeypatch: pytest.MonkeyPatch):
    """The alias:port, an IPv4 literal, a bracketed (and zoned) IPv6 literal; surrounding whitespace removed."""
    monkeypatch.setenv(VARIABLE, value)
    assert target_from_env(VARIABLE) == expected


def test_the_variable_is_the_one_compose_sets():
    """The name data_store's compose entry assigns (test_ingest_grpc_target.py looks the entry up by this constant)."""
    assert DATA_INGEST_GRPC_TARGET_ENV == 'DATA_INGEST_GRPC_TARGET'


# --- Read at call time, never at import -----------------------------------------------------------------


def test_the_variable_is_read_when_called_not_when_imported(monkeypatch: pytest.MonkeyPatch):
    """The module is already imported; each call sees the environment as it is at that call (common pitfall 1)."""
    monkeypatch.delenv(VARIABLE, raising=False)
    with pytest.raises(ValueError, match=VARIABLE):
        target_from_env(VARIABLE)
    monkeypatch.setenv(VARIABLE, 'data-ingest-grpc:50051')
    assert target_from_env(VARIABLE) == 'data-ingest-grpc:50051'
    monkeypatch.setenv(VARIABLE, 'data-ingest-grpc:50777')
    assert target_from_env(VARIABLE) == 'data-ingest-grpc:50777'


# A fresh interpreter: import the module under test with the variable unset, then print every module the
# import added under the generated package. A control import shows the generated tree IS importable here.
_IMPORT_PROBE = """
import json, sys
before = set(sys.modules)
import {module}
added = sorted(m for m in set(sys.modules) - before if m == {package!r} or m.startswith({package!r} + '.'))
print(json.dumps(added))
"""


def test_importing_the_reader_loads_no_generated_module_and_reads_nothing():
    """The TID251 seam (addendum 5 item 3): common.rpc.channel imports no trader_joe.proto, and needs no variable."""
    result = _child(
        _IMPORT_PROBE.format(module='common.rpc.channel', package=GENERATED_PACKAGE), env_overrides={VARIABLE: None}
    )
    assert result.returncode == 0, f'importing common.rpc.channel with {VARIABLE} unset failed:\n{result.stderr}'
    assert json.loads(result.stdout) == [], f'importing common.rpc.channel loaded generated code: {result.stdout}'


def test_the_import_probe_sees_generated_code_when_a_module_loads_it():
    """Non-vacuity for the probe above: common.rpc.ping does load trader_joe.proto, and the probe reports it."""
    result = _child(
        _IMPORT_PROBE.format(module='common.rpc.ping', package=GENERATED_PACKAGE), env_overrides={VARIABLE: None}
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert f'{GENERATED_PACKAGE}.proto.ping.v1.ping_pb2' in loaded, f'the probe saw {loaded}'


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    """(line, module) for every import statement in PATH, at any depth: a function-level import included."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, '.' * node.level + (node.module or '')))
    return found


def test_the_reader_module_imports_no_generated_code_at_any_depth():
    """What importing cannot show: an import inside target_from_env runs only when it is called."""
    imports = _imported_modules(CHANNEL_SOURCE)
    assert imports, f'no imports parsed from {CHANNEL_SOURCE}'
    generated = [f'line {line}: {name}' for line, name in imports if name.split('.')[0] == GENERATED_PACKAGE]
    assert not generated, f'{CHANNEL_SOURCE.name} imports generated code: {generated}'


# --- No DNS ---------------------------------------------------------------------------------------------

# A fresh interpreter with an audit hook (PEP 578) recording every resolver and connect event: CPython raises
# these from the C socket functions themselves, so the hook sees a lookup however it is reached -- socket's
# Python wrappers, a name bound by 'from socket import ...', asyncio's executor -- and cannot be bypassed by a
# module-attribute patch going stale. Hooks cannot be removed, which is why this runs in a child. The target
# calls are recorded on their own, then a control lookup shows the hook does see one.
_RESOLVER_EVENTS = (
    'socket.getaddrinfo',
    'socket.gethostbyname',
    'socket.gethostbyaddr',
    'socket.getnameinfo',
    'socket.connect',
)
_DNS_PROBE = """
import json, os, socket, sys
from common.rpc.channel import target_from_env

EVENTS = {events!r}
seen = {{'target': [], 'control': []}}
phase = None

def hook(event, args):
    if phase is not None and event in EVENTS:
        seen[phase].append(event)

sys.addaudithook(hook)
phase = 'target'
for value in {values!r}:
    os.environ[{variable!r}] = value
    target_from_env({variable!r})
phase = 'control'
try:
    socket.getaddrinfo('localhost', 50051)
except OSError:
    pass
phase = None
print(json.dumps(seen))
"""


def test_the_reader_never_resolves_the_name():
    """Item 3: a string check only. No getaddrinfo, gethostbyname or connect, for a name or an address literal."""
    values = [value for value, _ in ACCEPTED]
    script = _DNS_PROBE.format(events=_RESOLVER_EVENTS, values=values, variable=VARIABLE)
    result = _child(script, env_overrides={})
    assert result.returncode == 0, f'the probe failed:\n{result.stderr}'
    seen = json.loads(result.stdout)
    assert seen['control'], f'the audit hook saw no event for a real lookup, so it proves nothing: {seen}'
    assert seen['target'] == [], f'target_from_env reached the resolver or the network: {seen["target"]}'
