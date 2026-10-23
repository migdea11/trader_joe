"""dev_http_get(name): one GET of one of three fixed paths, by exec in the running dev data_store.

tj-iee6sn.13.6 (slice 1 of epic tj-iee6sn). Design: ADR tj-4rr0la addendum 22 (4) and (7) and addendum
24; the bead's WHAT and TESTS, the architect's 21:47, 22:34 and 23:33 notes, the 00:55 consolidated
work order and the 02:04 RE: (R1-R5: reaper_pending dropped, python -I, a 20 s overall deadline,
stderr discarded, the exact header wording).

WHAT IS PINNED:
    the argv        the data_store label lookup, then `docker exec <id> sh -c <script>` with NOTHING
                    between exec and the id, one fixed script per name, each a literal table row
    the input       a closed three-name enum at the schema AND at validate_dev_http_get_name; the
                    names map to the server's own route constants (parsed from source, not restated)
                    and each route is an open GET that takes no secret and records no usage
    the script      by its parts: redact embedded verbatim once, only an Accept header, no secret
                    name, no URL, no caller argument
    its behaviour   RUN under sh against a local stub HTTP server, with the image's python replaced
                    by a shim around this interpreter and redact() wrapped by a spy that records the
                    bytes it is handed and the truncated signal it sees: the 64 KB + 1 read bound,
                    the cut, the truncated mode, redaction, the status line and the exit codes
    the verb        closed schema, 60 s budget, ok/failed results, zero and several containers

NO DOCKER RUNS. The script runs under the host's sh (dash, as in the image) and awk, with an
environment built here from FAKE values; the real environment is never handed to the child.
"""

import ast
import contextlib
import inspect
import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import FakeDocker, default_response, forbid_real_subprocesses, make_rig


pytestmark = pytest.mark.build_infra

# --- the constants, from literals --------------------------------------------------------------
EXPECTED_DOCKER = '/usr/local/bin/docker'
EXPECTED_DEV_PROJECT = 'trader_joe'
CONTAINER_ID = 'c0ffee000001'
STACK_DIR = Path('/stack')
EXPECTED_PATHS = {'ping': '/ping', 'ui_config': '/ui/v1/config', 'usage_summary': '/ui/v1/stats/usage/summary'}
NAMES = list(EXPECTED_PATHS)
# DROPPED by ADR tj-4rr0la addendum 24: each /ui/v1/reaper/pending item is a full DatasetSummary whose
# owner, last_run.actor and usage.readByClients30d carry owner strings, which the in-container redact
# cannot know. It must be REFUSED like any other unknown name, at the schema and at the builder.
DROPPED_NAME = 'reaper_pending'
# 10 s per socket operation, 20 s overall (R3).
EXPECTED_SOCKET_TIMEOUT = 10
EXPECTED_OVERALL_DEADLINE = 20
# 64 KB: the most body bytes handed on; one more is read to learn the body was longer.
EXPECTED_LIMIT = 65536
EXPECTED_TIMEOUT_SECONDS = 60
# The image's interpreter (Dockerfile: `uv venv` in WORKDIR /code; the deploy stage copies /code),
# the one docker-compose.yaml's data_store healthcheck runs. The image has no curl.
EXPECTED_PYTHON = '/code/.venv/bin/python'
EXPECTED_REDACT_NAMES = (
    'INSTANCE_WRITE_SECRET',
    'POSTGRES_PASS',
    'POSTGRES_PASSWORD',
    'ALPACA_API_KEY',
    'ALPACA_API_SECRET',
)
REDACT_FUNCTION = stack._DEV_REDACT_FUNCTION
SERVER = Path(__file__).resolve().parents[3] / 'server'
MCP_CONFIG = Path(__file__).resolve().parents[3] / '.mcp.json'


def expected_lookup() -> list[str]:
    """The label lookup of data_store: running, project, service, not a one-off; ids only."""
    return [
        EXPECTED_DOCKER,
        'ps',
        '--filter',
        'status=running',
        '--filter',
        f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
        '--filter',
        'label=com.docker.compose.service=data_store',
        '--filter',
        'label=com.docker.compose.oneoff=False',
        '--format',
        '{{.ID}}',
    ]


def script_of(name: str) -> str:
    return stack.DEV_HTTP_GET_TAILS[name][2]


def expected_exec(name: str, container_id: str = CONTAINER_ID) -> list[str]:
    """`docker exec <id> sh -c <script>`: NO option -- no -u, no -w, no -e -- between exec and the id."""
    return [EXPECTED_DOCKER, 'exec', container_id, 'sh', '-c', script_of(name)]


def body_of(script: str) -> str:
    """The script after the embedded redact constant: the part this bead wrote."""
    assert script.startswith(REDACT_FUNCTION + '\n')
    return script[len(REDACT_FUNCTION) :]


# ===============================================================================================
# THE ARGV.
# ===============================================================================================


def test_the_lookup_is_exactly_the_label_filter_of_data_store():
    assert list(stack.dev_lookup_argv('data_store')) == expected_lookup()


@pytest.mark.parametrize('name', NAMES)
def test_the_exec_argv_is_exact_with_nothing_between_exec_and_the_id(name: str):
    steps = stack.dev_http_get_steps(STACK_DIR, CONTAINER_ID, name)
    assert [list(step.argv) for step in steps] == [expected_exec(name)]
    assert steps[0].argv[2] == CONTAINER_ID, 'an option sits between exec and the id'
    assert steps[0].cwd == STACK_DIR and steps[0].builds is False and steps[0].stdout_cap is None


@pytest.mark.parametrize('name', NAMES)
def test_the_builder_runs_check_dev_exec_argv(monkeypatch: pytest.MonkeyPatch, name: str):
    seen: list[tuple[str, ...]] = []

    def spy(argv):
        seen.append(tuple(argv))
        raise stack.Refused('spy refusal')

    monkeypatch.setattr(stack, 'check_dev_exec_argv', spy)
    with pytest.raises(stack.Refused, match='spy refusal'):
        stack.dev_http_get_steps(STACK_DIR, CONTAINER_ID, name)
    assert seen == [tuple(expected_exec(name))]


@pytest.mark.parametrize('container_id', ['data_store', 'C0FFEE000001', 'c0ffee00000', '', 'c0ffee000001\n', None])
def test_the_builder_refuses_a_non_id(container_id: object):
    with pytest.raises(stack.Refused, match='not a container id'):
        stack.dev_http_get_steps(STACK_DIR, container_id, 'ping')  # type: ignore[arg-type]


def test_each_name_has_its_own_script_differing_only_in_the_path():
    scripts = {name: script_of(name) for name in NAMES}
    assert len(set(scripts.values())) == 3
    for name, script in scripts.items():
        assert script.count(f'\nGET_PATH={EXPECTED_PATHS[name]}\n') == 1, name
        assert script.replace(f'\nGET_PATH={EXPECTED_PATHS[name]}\n', '\nGET_PATH=X\n') == scripts['ping'].replace(
            '\nGET_PATH=/ping\n', '\nGET_PATH=X\n'
        ), f'{name} differs from ping by more than its path'
    assert {name: tail[:2] for name, tail in stack.DEV_HTTP_GET_TAILS.items()} == dict.fromkeys(NAMES, ('sh', '-c'))


# --- the table: each tail runs with NO option, and with nothing else -------------------------------

_EXEC = [EXPECTED_DOCKER, 'exec']
_PG = ['-u', 'postgres', '-e', 'PGOPTIONS=-c default_transaction_read_only=on']
_OPTION_SETS = {
    'the db option set': _PG,
    'the migrate option set': ['-w', '/code'],
    '-u postgres': ['-u', 'postgres'],
    '-u root': ['-u', 'root'],
    '-w /': ['-w', '/'],
    '-e PGOPTIONS': _PG[2:],
    '-e another': ['-e', 'INSTANCE_WRITE_SECRET=x'],
    '-i': ['-i'],
    '-t': ['-t'],
    '-it': ['-it'],
    '--privileged': ['--privileged'],
    '--env-file': ['--env-file', '/workspace/.env'],
    '-d': ['-d'],
    'an empty word': [''],
}


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('options', list(_OPTION_SETS.values()), ids=list(_OPTION_SETS))
def test_an_http_get_tail_with_any_option_is_refused(monkeypatch: pytest.MonkeyPatch, name: str, options: list[str]):
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv([*_EXEC, *options, CONTAINER_ID, 'sh', '-c', script_of(name)])


@pytest.mark.parametrize('name', NAMES)
def test_check_dev_exec_argv_accepts_each_http_get_row_bare(name: str):
    stack.check_dev_exec_argv(expected_exec(name))
    stack.check_dev_exec_argv(tuple(expected_exec(name, 'f' * 64)))


_TAMPERED = {
    'another path': lambda s: s.replace('\nGET_PATH=/ping\n', '\nGET_PATH=/ui/v1/datasets\n'),
    'a URL path': lambda s: s.replace('\nGET_PATH=/ping\n', '\nGET_PATH=http://example.com/\n'),
    'a header added': lambda s: s.replace(
        '{"Accept": "application/json"}', '{"Accept": "*/*", "X-Instance-Secret": "x"}'
    ),
    'POST': lambda s: s.replace('conn.request("GET"', 'conn.request("POST"'),
    'a word appended': lambda s: s + '\nid',
    'a bigger read': lambda s: s.replace('LIMIT = 65536', 'LIMIT = 655360'),
}


@pytest.mark.parametrize('tamper', list(_TAMPERED.values()), ids=list(_TAMPERED))
def test_any_edit_of_the_script_is_another_tail_and_refused(monkeypatch: pytest.MonkeyPatch, tamper):
    forbid_real_subprocesses(monkeypatch)
    edited = tamper(script_of('ping'))
    assert edited != script_of('ping'), 'the tamper did not change the script'
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv([*_EXEC, CONTAINER_ID, 'sh', '-c', edited])
    with pytest.raises(stack.Refused):
        stack.dev_exec_steps(STACK_DIR, CONTAINER_ID, ('sh', '-c', edited))


@pytest.mark.parametrize('name', NAMES)
def test_an_extra_argument_after_the_script_is_refused(name: str):
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv([*expected_exec(name), '/ui/v1/datasets'])
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv([*_EXEC, CONTAINER_ID, 'bash', '-c', script_of(name)])


# ===============================================================================================
# THE INPUT: a closed enum of three names, mapped to the server's own route constants.
# ===============================================================================================


def test_the_name_to_path_map_is_exactly_these_three():
    assert stack.DEV_HTTP_GET_PATHS == EXPECTED_PATHS
    assert DROPPED_NAME not in stack.DEV_HTTP_GET_PATHS and DROPPED_NAME not in stack.DEV_HTTP_GET_TAILS
    assert '/ui/v1/reaper/pending' not in stack.DEV_HTTP_GET_PATHS.values()
    assert {name: stack.DEV_HTTP_GET_TAILS[name] for name in NAMES} == stack.DEV_HTTP_GET_TAILS
    assert stack.DEV_HTTP_GET_BODY_LIMIT == EXPECTED_LIMIT


# name -> (constants module, class, member, router module), all under server/.
_ROUTES = {
    'ping': ('routers/common/app_endpoints.py', 'InterfaceRest', 'PING', 'routers/common/ping.py'),
    'ui_config': (
        'routers/data_store/app_endpoints.py',
        'UiConfigInterface',
        'GET_UI_CONFIG',
        'routers/data_store/ui_config.py',
    ),
    'usage_summary': (
        'routers/data_store/app_endpoints.py',
        'UiStatsInterface',
        'GET_UI_USAGE_SUMMARY',
        'routers/data_store/ui_stats.py',
    ),
}


def _class_constant(relative: str, cls: str, member: str) -> str:
    """The string a class member is assigned in a source file, read by parsing it (never importing it)."""
    tree = ast.parse((SERVER / relative).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and [getattr(t, 'id', None) for t in item.targets] == [member]
                    and isinstance(item.value, ast.Constant)
                ):
                    return item.value.value
    raise AssertionError(f'{cls}.{member} not found in {relative}')


@pytest.mark.parametrize('name', NAMES)
def test_each_path_is_the_servers_own_route_constant(name: str):
    """Parsed from the server's source, not restated: a renamed route turns this red."""
    constants, cls, member, _ = _ROUTES[name]
    assert stack.DEV_HTTP_GET_PATHS[name] == _class_constant(constants, cls, member)


def _route_handler(relative: str, cls: str, member: str) -> tuple[ast.AsyncFunctionDef | ast.FunctionDef, ast.Call]:
    """The ONE handler decorated `@router.<method>(<cls>.<member>, ...)` in a router module, and the decorator."""
    tree = ast.parse((SERVER / relative).read_text(encoding='utf-8'))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.args
                    and ast.unparse(decorator.args[0]) == f'{cls}.{member}'
                ):
                    found.append((node, decorator))
    assert len(found) == 1, f'{cls}.{member} has {len(found)} handlers in {relative}'
    return found[0]


@pytest.mark.parametrize('name', NAMES)
def test_each_route_is_an_open_get_that_records_no_usage_and_publishes_nothing(name: str):
    """Addendum 22 (4): open GETs, no instance secret. And none counts as a read or changes anything.

    The handler is decorated `router.get` with no dependencies; it takes no Header (the client-id
    header is how a read is attributed); its module's router is a bare APIRouter(); and nothing in
    the handler records a read, publishes an event or commits. The module is registered on the app.
    """
    _, cls, member, module = _ROUTES[name]
    handler, decorator = _route_handler(module, cls, member)
    assert ast.unparse(decorator.func) == 'router.get', ast.unparse(decorator.func)
    assert [kw.arg for kw in decorator.keywords if kw.arg == 'dependencies'] == []
    source = ast.unparse(handler)
    for word in ('require_instance_secret', 'Header', 'record_read', 'usage_recorder', 'publish', 'commit', 'Secret'):
        assert word not in source, f'{name}: the handler mentions {word}'
    module_source = (SERVER / module).read_text(encoding='utf-8')
    assert 'router = APIRouter()\n' in module_source, f'{module} builds its router with arguments'
    assert 'require_instance_secret' not in module_source
    stem = Path(module).stem
    main = (SERVER / 'data' / 'store' / 'app' / 'main.py').read_text(encoding='utf-8')
    assert f'app.include_router({stem}.router)' in main, f'{stem} is not registered on the data_store app'


_BAD_NAMES = {
    'unknown': 'datasets',
    'empty': '',
    'a path': '/ping',
    'a ui path': '/ui/v1/config',
    'traversal': '../ping',
    'traversal after a name': 'ping/../../internal/asset-data/stock/x',
    'encoded traversal': 'ping%2F..%2F',
    'a full URL': 'http://127.0.0.1:80/ping',
    'another host': 'http://example.com/',
    'upper case': 'PING',
    'padded': ' ping',
    'a newline': 'ping\n',
    'a NUL': 'ping\x00',
    'two names': 'ping usage_summary',
    'a query': 'usage_summary?client_id=x',
    'a fragment': 'ping#x',
    # Dropped by addendum 24 because its response carries owner-type strings (owner, last_run.actor,
    # usage.readByClients30d): a name that once existed is refused exactly like one that never did.
    'the dropped reaper_pending': DROPPED_NAME,
}
_NON_STRINGS = {
    'None': None,
    'an int': 1,
    'a bool': True,
    'a list': ['ping'],
    'a dict': {'name': 'ping'},
    'bytes': b'ping',
}


@pytest.mark.parametrize('value', [*_BAD_NAMES.values(), *_NON_STRINGS.values()], ids=[*_BAD_NAMES, *_NON_STRINGS])
def test_validate_dev_http_get_name_refuses_anything_but_the_three(value: object):
    with pytest.raises(stack.Refused, match=r'^name must be one of: ping, ui_config, usage_summary$'):
        stack.validate_dev_http_get_name(value)
    with pytest.raises(stack.Refused):
        stack.dev_http_get_steps(STACK_DIR, CONTAINER_ID, value)  # type: ignore[arg-type]


@pytest.mark.parametrize('name', NAMES)
def test_validate_dev_http_get_name_accepts_the_three(name: str):
    assert stack.validate_dev_http_get_name(name) == name


def test_the_schema_is_closed_with_one_required_enum_name():
    schema = runner.VERB_SCHEMAS['dev_http_get']
    assert schema['type'] == 'object' and schema['additionalProperties'] is False
    assert schema['required'] == ['name']
    assert list(schema['properties']) == ['name']
    name = schema['properties']['name']
    assert name['type'] == 'string' and name['enum'] == NAMES
    assert set(name) == {'type', 'enum', 'description'}
    for key, path in EXPECTED_PATHS.items():
        assert f'{key} ({path})' in name['description']
    # reaper_pending was dropped (addendum 24: owner-type strings in its response); not advertised.
    assert DROPPED_NAME not in name['enum'] and DROPPED_NAME not in name['description']
    assert 'reaper' not in name['description']


@pytest.mark.parametrize(
    'arguments',
    [
        *({'name': value} for value in _BAD_NAMES.values()),
        *({'name': value} for value in _NON_STRINGS.values()),
        {},
        {'name': 'ping', 'path': '/ui/v1/datasets'},
        {'name': 'ping', 'url': 'http://example.com/'},
        {'name': 'ping', 'header': 'X-Instance-Secret: x'},
        {'path': '/ping'},
        'ping',
        ['ping'],
        None,
    ],
    ids=[
        *(f'name {key}' for key in _BAD_NAMES),
        *(f'name {key}' for key in _NON_STRINGS),
        'no name',
        'extra path',
        'extra url',
        'extra header',
        'path instead of name',
        'positional',
        'a list',
        'none',
    ],
)
def test_the_verb_refuses_every_other_input_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: object
):
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('dev_http_get', arguments)
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []


def test_the_budget_is_sixty_seconds_inside_the_mcp_client_timeout():
    """60 s for one lookup and one GET with a 10 s timeout; the client waits at least 30 s longer."""
    assert runner.VERB_TIMEOUT_SECONDS['dev_http_get'] == EXPECTED_TIMEOUT_SECONDS
    client_ms = json.loads(MCP_CONFIG.read_text(encoding='utf-8'))['mcpServers']['agent-stack']['timeout']
    assert client_ms >= EXPECTED_TIMEOUT_SECONDS * 1000 + 30_000


def test_the_handler_and_builder_take_only_the_name():
    assert list(inspect.signature(stack.dev_http_get_steps).parameters) == ['stack_dir', 'container_id', 'name']
    assert list(inspect.signature(runner.AgentStack._dev_http_get).parameters) == ['self', 'call', 'name']


# ===============================================================================================
# THE SCRIPT, by its parts.
# ===============================================================================================


@pytest.mark.parametrize('name', NAMES)
def test_the_script_embeds_the_redact_constant_verbatim_exactly_once(name: str):
    script = script_of(name)
    assert script.startswith(REDACT_FUNCTION + '\n')
    assert script.count(REDACT_FUNCTION) == 1
    assert script.count('redact() {') == 1 and script.count('awk ') == REDACT_FUNCTION.count('awk ')
    body = body_of(script)
    # Every printed byte goes through it: the body, and the error text of the no-response branch.
    assert body.count('| redact') == 2
    assert 'printf \'%s\\n\' "$body" | redact' in body and 'printf \'%s\\n\' "$res" | redact' in body


@pytest.mark.parametrize('name', NAMES)
def test_the_script_sends_no_secret_and_names_none(name: str):
    """No X-Instance-Secret; no secret variable named outside redact's own list; no other env read."""
    body = body_of(script_of(name))
    assert 'x-instance-secret' not in body.lower() and 'authorization' not in body.lower()
    for word in ('SECRET', 'PASS', 'ALPACA', 'DATABASE', 'TOKEN', 'KEY', 'DSN'):
        assert word not in body.upper(), f'the script names {word}'
    assert REDACT_FUNCTION.count(' '.join(EXPECTED_REDACT_NAMES)) == 1
    assert ' '.join(EXPECTED_REDACT_NAMES) == stack._DEV_REDACT_NAMES
    # The only variable the python reads is the port, and the only one the sh exports is the signal --
    # the literal 1, twice and nowhere else: once for the python's own cut flag (header `@@<s>@@1`) and
    # once for a python killed after a valid header (tj-iee6sn.13.11 N2: rc other than 0 or 1).
    assert body.count('os.environ') == 1 and 'os.environ["APP_INTERNAL_PORT"]' in body
    assert body.count('export ') == 2 and body.count('export REDACT_TRUNCATED=1;;') == 2
    assert '@@[0-9][0-9][0-9]@@1) status=${hdr#@@}; status=${status%@@1}; cut=1; export REDACT_TRUNCATED=1;;\n' in body
    assert '\ncase "$rc" in 0|1) ;; *) export REDACT_TRUNCATED=1;; esac\n' in body


@pytest.mark.parametrize('name', NAMES)
def test_the_script_sends_only_an_accept_header_by_get_without_a_url(name: str):
    body = body_of(script_of(name))
    assert body.count('conn.request(') == 1
    assert 'conn.request("GET", sys.argv[1], headers={"Accept": "application/json"})' in body
    assert 'headers=' in body and body.count('headers=') == 1
    assert 'putheader' not in body and 'urllib' not in body and 'requests' not in body
    for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
        assert f'"{method}"' not in body
    assert '://' not in body, 'a URL is spelled in the script'
    assert 'http.client.HTTPConnection("127.0.0.1", int(os.environ["APP_INTERNAL_PORT"]), timeout=10)' in body
    assert body.count(EXPECTED_PYTHON) == 1
    assert 'curl' not in body and 'wget' not in body


@pytest.mark.parametrize('name', NAMES)
def test_python_runs_isolated_with_its_stderr_discarded(name: str):
    """R2 and R4: `python -I` (no PYTHONPATH, no cwd, no user site) and stderr to /dev/null, never 2>&1.

    The whole command line, literally: -I first, then -c, the program and the path, and stderr goes
    nowhere. Only the python's stdout -- which the except fills before any body -- reaches redact.
    """
    body = body_of(script_of(name))
    python = stack._DEV_HTTP_GET_PYTHON
    assert f'res=$({EXPECTED_PYTHON} -I -c \'{python}\' "$GET_PATH" 2>/dev/null); rc=$?\n' in body
    assert body.count(' -I ') == 1 and body.count('2>/dev/null') == 1
    assert '2>&1' not in body and '2>' not in body.replace('2>/dev/null', '')
    assert f'{EXPECTED_PYTHON} -c' not in body, 'python runs without -I'


@pytest.mark.parametrize('name', NAMES)
def test_the_script_takes_no_caller_argument_and_leaves_no_placeholder(name: str):
    body = body_of(script_of(name))
    assert '$1' not in body and '$@' not in body and '$*' not in body
    for marker in ('@PATH@', '@PYTHON@', '@LIMIT@', '@NAMES@'):
        assert marker not in script_of(name)
    assert body.count('LIMIT = 65536\n') == 1
    assert 'body = resp.read(LIMIT + 1)' in body and 'sys.stdout.buffer.write(body[:LIMIT])' in body
    assert 'cut = len(body) > LIMIT' in body
    assert '== body cut at 65536 bytes ==' in body
    python = stack._DEV_HTTP_GET_PYTHON
    assert "'" not in python, 'a single quote would end the sh quoting of the python'
    assert f'-c \'{python}\' "$GET_PATH"' in body


def test_unset_precedes_every_use_of_the_signal():
    body = body_of(script_of('ping'))
    assert body.index('unset REDACT_TRUNCATED') < body.index('export REDACT_TRUNCATED=1')
    assert body.index('unset REDACT_TRUNCATED') < body.index('| redact')


# ===============================================================================================
# THE SCRIPT, RUN: a stub HTTP server, a python shim, a redact spy.
# ===============================================================================================

_HOLD = object()  # in a response's parts: wait for the server's release before sending the rest


@dataclass
class Trickle:
    """In a response's parts: send DATA one byte at a time, INTERVAL seconds apart (a slow drip)."""

    data: bytes
    interval: float = 1.0


class StubServer:
    """A loopback TCP server that records each request and answers with fixed parts, in order."""

    def __init__(self, parts: list[object]):
        self.parts = parts
        self.requests: list[bytes] = []
        self.release = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(30)
                data = b''
                try:
                    while b'\r\n\r\n' not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    self.requests.append(data)
                    for part in self.parts:
                        if part is _HOLD:
                            self.release.wait(30)
                            continue
                        if isinstance(part, Trickle):
                            for index in range(len(part.data)):
                                if self.release.wait(part.interval):
                                    break
                                conn.sendall(part.data[index : index + 1])
                            continue
                        conn.sendall(part)  # type: ignore[arg-type]
                except OSError:
                    pass

    def close(self) -> None:
        self.release.set()
        # shutdown, not only close: close() alone does not wake a thread blocked in accept() on Linux.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join(10)
        assert not self.thread.is_alive(), 'the stub server did not stop'


def response(status: int, body: bytes = b'', length: int | None = None, extra: str = '') -> bytes:
    head = (
        f'HTTP/1.1 {status} Whatever\r\nContent-Type: application/json\r\n'
        f'Content-Length: {len(body) if length is None else length}\r\n{extra}Connection: close\r\n\r\n'
    )
    return head.encode() + body


@pytest.fixture
def serve():
    servers: list[StubServer] = []

    def start(*parts: object) -> StubServer:
        server = StubServer(list(parts))
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


# SHIM_CRASH_RC, when set, stands for a python that dies before it prints a header: it prints
# SHIM_CRASH_OUT (maybe nothing) and exits with that status. SHIM_STDERR, when set, is written to
# stderr AFTER the python's stdout -- the place a traceback or a warning would land after a body.
_PYTHON_SHIM = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done >> "$SHIM_ARGV_LOG"
if [ -n "$SHIM_CRASH_RC" ]; then printf '%s' "$SHIM_CRASH_OUT"; exit "$SHIM_CRASH_RC"; fi
"$SHIM_REAL_PYTHON" "$@" > "$SHIM_PY_OUT"
rc=$?
cat "$SHIM_PY_OUT"
[ -z "$SHIM_STDERR" ] || printf '%s' "$SHIM_STDERR" >&2
exit $rc
"""
# Wraps the embedded redact: records the truncated signal it sees and every byte it is handed, then
# runs the real one (renamed, otherwise byte-identical).
_SPY = '\nredact() { printf \'%s\\n\' "${REDACT_TRUNCATED-unset}" >> "$SPY_FLAGS"; tee -a "$SPY_IN" | _real_redact; }'


@dataclass
class Run:
    completed: subprocess.CompletedProcess
    python_stdout: bytes  # what the python printed: the header line and the body bytes it handed on
    redact_in: bytes  # every byte redact() was handed
    flags: list[str]  # REDACT_TRUNCATED as each redact() call saw it ('unset' when unset)
    python_argv: list[str]
    elapsed: float

    @property
    def stdout(self) -> str:
        return self.completed.stdout.decode('utf-8', errors='replace')


def run_get(
    tmp_path: Path, name: str, port: int, env: dict[str, str] | None = None, timeout: float = 40, edit=None
) -> Run:
    """NAME's real script under sh, the image's python replaced by a shim, redact() wrapped by the spy.

    EDIT, when given, maps the real script to the one run (each caller asserts its edit took).
    """
    script = script_of(name) if edit is None else edit(script_of(name))
    assert script.count(EXPECTED_PYTHON) == 1 and script.count(REDACT_FUNCTION) == 1
    shim = tmp_path / 'python'
    shim.write_text(_PYTHON_SHIM)
    shim.chmod(0o755)
    renamed = REDACT_FUNCTION.replace('redact() {', '_real_redact() {', 1)
    assert renamed != REDACT_FUNCTION
    script = script.replace(EXPECTED_PYTHON, str(shim)).replace(REDACT_FUNCTION, renamed + _SPY)
    files = {key: tmp_path / key.lower() for key in ('SHIM_ARGV_LOG', 'SHIM_PY_OUT', 'SPY_FLAGS', 'SPY_IN')}
    full_env = {
        'PATH': '/usr/bin:/bin',
        'APP_INTERNAL_PORT': str(port),
        'SHIM_REAL_PYTHON': sys.executable,
        **{key: str(path) for key, path in files.items()},
        **(env or {}),
    }
    started = time.monotonic()
    completed = subprocess.run(
        ['sh', '-c', script], env=full_env, cwd=tmp_path, capture_output=True, timeout=timeout, check=False
    )
    elapsed = time.monotonic() - started

    def read(key: str) -> bytes:
        return files[key].read_bytes() if files[key].exists() else b''

    return Run(
        completed=completed,
        python_stdout=read('SHIM_PY_OUT'),
        redact_in=read('SPY_IN'),
        flags=read('SPY_FLAGS').decode().splitlines(),
        python_argv=read('SHIM_ARGV_LOG').decode().splitlines(),
        elapsed=elapsed,
    )


def closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def header_lines(request: bytes) -> tuple[str, dict[str, str]]:
    head = request.split(b'\r\n\r\n', 1)[0].decode('latin-1').split('\r\n')
    return head[0], {line.split(':', 1)[0].strip().lower(): line.split(':', 1)[1].strip() for line in head[1:]}


# --- the request ---------------------------------------------------------------------------------------


@pytest.mark.parametrize('name', NAMES)
def test_the_request_is_one_get_of_the_fixed_path_with_only_an_accept_header(tmp_path: Path, serve, name: str):
    """The stub's view: `GET <path> HTTP/1.1`, and only an Accept header beside http.client's own two.

    Those two are Host and Accept-Encoding: identity. No secret header, and no FAKE secret value
    anywhere in the request bytes.
    """
    server = serve(response(200, b'{}'))
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-iws-sent-0001', 'POSTGRES_PASS': 'dummy-pgpass-sent-0002'}
    run = run_get(tmp_path, name, server.port, env=secrets)
    assert run.completed.returncode == 0, run.completed
    assert len(server.requests) == 1
    request_line, headers = header_lines(server.requests[0])
    assert request_line == f'GET {EXPECTED_PATHS[name]} HTTP/1.1'
    assert set(headers) == {'host', 'accept-encoding', 'accept'}, headers
    assert headers['accept'] == 'application/json' and headers['accept-encoding'] == 'identity'
    assert headers['host'] == f'127.0.0.1:{server.port}'
    assert server.requests[0].split(b'\r\n\r\n', 1)[1] == b'', 'the GET carried a body'
    for value in secrets.values():
        assert value.encode() not in server.requests[0]


def test_no_fake_secret_and_no_url_is_on_any_command_line(tmp_path: Path, serve):
    server = serve(response(200, b'{"message":"pong"}'))
    secret = 'dummy-cmdline-secret-0001'
    run = run_get(tmp_path, 'ping', server.port, env={'INSTANCE_WRITE_SECRET': secret})
    assert run.completed.returncode == 0
    # the python's argv: -I, -c, the program, and the path -- nothing else
    assert run.python_argv[:2] == ['-I', '-c'] and run.python_argv[-1] == '/ping'
    joined = '\n'.join(run.python_argv)
    assert secret not in joined and '://' not in joined and str(server.port) not in joined


# --- the status line and the exit codes ------------------------------------------------------------


@pytest.mark.parametrize(
    ('status', 'rc'),
    [(200, 0), (201, 0), (204, 0), (299, 0), (301, 1), (302, 1), (304, 1), (400, 1), (404, 1), (422, 1), (500, 1)],
)
def test_the_exit_is_zero_for_2xx_and_one_otherwise_with_the_status_first(tmp_path: Path, serve, status: int, rc: int):
    body = b'' if status in (204, 304) else b'{"detail":"x"}'
    server = serve(response(status, body, extra='Location: http://127.0.0.1:1/elsewhere\r\n'))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == rc, run.completed
    first = run.stdout.split('\n', 1)[0]
    assert first == f'== GET /ping -> HTTP {status} =='
    assert stack.parse_http_get_status(run.completed.stdout) == status
    assert len(server.requests) == 1, 'a redirect was followed'
    if body:
        assert run.stdout == f'{first}\n{body.decode()}\n'


def test_a_200_prints_the_status_line_then_the_body(tmp_path: Path, serve):
    server = serve(response(200, b'{"message":"pong"}'))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 0 and run.completed.stderr == b''
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n{"message":"pong"}\n'
    assert run.flags == ['unset']


def test_a_refused_connection_exits_two_with_no_http_response(tmp_path: Path):
    run = run_get(tmp_path, 'ping', closed_port())
    assert run.completed.returncode == 2, run.completed
    lines = run.stdout.split('\n')
    assert lines[0] == '== GET /ping: no HTTP response =='
    assert lines[1].startswith('ConnectionRefusedError: ')
    assert stack.parse_http_get_status(run.completed.stdout) is None
    assert run.flags == ['unset']
    assert '@@' not in run.stdout, 'the python header leaked into the output'


def test_a_body_that_never_comes_is_cut_off_by_the_ten_second_timeout(tmp_path: Path, serve):
    """Headers, ten body bytes, then silence: the socket timeout ends it as a transport failure."""
    server = serve(response(200, b'0123456789', length=1000), _HOLD)
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 2, run.completed
    assert run.stdout.startswith('== GET /ping: no HTTP response ==\nTimeoutError: ')
    assert 9 <= run.elapsed < 20, run.elapsed


# --- the bound: at most 64 KB + 1 read, at most 64 KB handed to redact ---------------------------------

_MB = 1024 * 1024


def test_a_one_megabyte_body_hands_redact_at_most_64_kb(tmp_path: Path, serve):
    """The 23:33 note: measure what reaches redact, not only what is printed."""
    body = b'a' * _MB
    server = serve(response(200, body))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 0, run.completed
    # The python hands on its header line and exactly the first 64 KB of the body, no more.
    assert run.python_stdout == b'@@200@@1\n' + b'a' * EXPECTED_LIMIT
    # redact is handed those 64 KB and printf's line terminator: 65537 bytes on the pipe, of which at
    # most 65536 are body bytes.
    assert run.redact_in == b'a' * EXPECTED_LIMIT + b'\n'
    assert len(run.redact_in) <= EXPECTED_LIMIT + 1 and len(run.redact_in.rstrip(b'\n')) <= EXPECTED_LIMIT
    assert run.flags == ['1']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + 'a' * EXPECTED_LIMIT + '\n== body cut at 65536 bytes ==\n'


def test_the_read_stops_at_64_kb_plus_one_byte(tmp_path: Path, serve):
    """The server sends 65537 bytes of a 1 MB body and then holds the connection open.

    A read of more than LIMIT + 1 would block on the held socket until the 10 s timeout; the bound
    returns at once.
    """
    server = serve(response(200, b'b' * (EXPECTED_LIMIT + 1), length=_MB), _HOLD, b'b' * (_MB - EXPECTED_LIMIT - 1))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 0, run.completed
    assert run.elapsed < 8, f'the read waited for more than 65537 bytes ({run.elapsed:.1f} s)'
    assert run.python_stdout == b'@@200@@1\n' + b'b' * EXPECTED_LIMIT
    assert run.flags == ['1']


def test_exactly_64_kb_is_not_marked_truncated(tmp_path: Path, serve):
    body = b'c' * (EXPECTED_LIMIT - 9) + b'dummy-str'  # ends in a prefix of a secret: NOT redacted here
    server = serve(response(200, body))
    run = run_get(tmp_path, 'ping', server.port, env={'INSTANCE_WRITE_SECRET': 'dummy-straddle-0001'})
    assert run.completed.returncode == 0, run.completed
    assert run.python_stdout == b'@@200@@0\n' + body
    assert run.redact_in == body + b'\n'
    assert run.flags == ['unset']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + body.decode() + '\n'
    assert 'cut' not in run.stdout


def test_64_kb_plus_one_is_marked_truncated(tmp_path: Path, serve):
    body = b'd' * (EXPECTED_LIMIT + 1)
    server = serve(response(200, body))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 0, run.completed
    assert run.python_stdout == b'@@200@@1\n' + b'd' * EXPECTED_LIMIT
    assert run.redact_in == b'd' * EXPECTED_LIMIT + b'\n'
    assert run.flags == ['1']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + 'd' * EXPECTED_LIMIT + '\n== body cut at 65536 bytes ==\n'


def test_a_cut_non_2xx_body_is_still_cut_and_exits_one(tmp_path: Path, serve):
    server = serve(response(500, b'e' * (2 * EXPECTED_LIMIT)))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 1
    assert run.flags == ['1'] and run.stdout.endswith('\n== body cut at 65536 bytes ==\n')
    assert run.stdout.startswith('== GET /ping -> HTTP 500 ==\n')


@pytest.mark.parametrize('signal', ['1', '0', 'yes'])
def test_the_containers_own_truncated_variable_is_unset_before_the_decision(tmp_path: Path, serve, signal: str):
    """REDACT_TRUNCATED in the container's environment never reaches redact.

    The script unsets it, and only its own length test sets it. A short body ending in a secret
    prefix is printed as it is.
    """
    body = b'{"x":"short"} dummy-stra'
    server = serve(response(200, body))
    env = {'INSTANCE_WRITE_SECRET': 'dummy-straddle-0001', 'REDACT_TRUNCATED': signal}
    run = run_get(tmp_path, 'ping', server.port, env=env)
    assert run.completed.returncode == 0, run.completed
    assert run.flags == ['unset']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + body.decode() + '\n'


_STRADDLE = 'dummy-straddle-SECRET-xyz'


@pytest.mark.parametrize('before_cut', [1, 2, 12, len(_STRADDLE) - 1])
def test_a_secret_straddling_the_cut_prints_no_part_of_itself(tmp_path: Path, serve, before_cut: int):
    """The 22:34 note: a body of 64 KB + n whose secret starts BEFORE_CUT bytes before the cut.

    This is about THE 64 KB CUT, the one place the script itself splits a body: the secret straddles
    byte 65536, so the bytes before the cut are a prefix of it, and truncated mode hides them. A SHORT
    body is NOT a truncated one -- see the next test.
    """
    filler = EXPECTED_LIMIT - before_cut
    body = b'.' * filler + _STRADDLE.encode() + b'.' * 100
    server = serve(response(200, body))
    run = run_get(tmp_path, 'ping', server.port, env={'ALPACA_API_SECRET': _STRADDLE})
    assert run.completed.returncode == 0, run.completed
    assert run.flags == ['1']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + '.' * filler + '<redacted>\n== body cut at 65536 bytes ==\n'


def test_a_short_body_that_is_a_secret_prefix_is_not_a_truncated_body(tmp_path: Path, serve):
    """A short body is NOT a truncated one: the script did not cut it, so truncated mode stays off.

    The server sent exactly these 7 bytes, and they are printed as they are, even though they happen to
    be a prefix of a secret. Truncation is flagged only by the length test at the 64 KB limit, never by
    the content. A body the server itself cut short is the server's output, outside this script's
    contract (the builder's 02:26 note; ruled out of scope -- the cut this design guards is its own).
    """
    server = serve(response(200, b'FAKESEC'))
    run = run_get(tmp_path, 'ping', server.port, env={'INSTANCE_WRITE_SECRET': 'FAKESECRET-0001'})
    assert run.completed.returncode == 0, run.completed
    assert run.python_stdout == b'@@200@@0\nFAKESEC'
    assert run.flags == ['unset']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\nFAKESEC\n'


# --- R4: python's stderr never reaches redact, and a crash keeps its exit status --------------------


def test_stderr_after_a_cut_body_with_a_straddling_secret_prints_nothing_of_either(tmp_path: Path, serve):
    """R4: text on python's stderr AFTER a cut body would sit after the straddling prefix.

    Merged (2>&1), it would follow the body in the bytes handed to redact, so the prefix would no longer
    be a suffix and truncated mode could not hide it -- and the stderr text itself, here a FAKE
    fragment of the same secret, would print. Discarded, neither appears anywhere.
    """
    before_cut = 12
    filler = EXPECTED_LIMIT - before_cut
    body = b'.' * filler + _STRADDLE.encode() + b'.' * 100
    server = serve(response(200, body))
    fragment = 'stderr-says dummy-straddle-SEC'
    env = {'ALPACA_API_SECRET': _STRADDLE, 'SHIM_STDERR': fragment}
    run = run_get(tmp_path, 'ping', server.port, env=env)
    assert run.completed.returncode == 0, run.completed
    assert run.flags == ['1']
    # redact is handed the cut body and printf's newline -- nothing after it.
    assert run.redact_in == body[:EXPECTED_LIMIT] + b'\n'
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + '.' * filler + '<redacted>\n== body cut at 65536 bytes ==\n'
    everything = run.completed.stdout + run.completed.stderr
    assert b'stderr-says' not in everything and b'dummy-' not in everything
    assert _STRADDLE[:before_cut].encode() not in everything


@pytest.mark.parametrize('rc', [3, 9, 127, 137])
@pytest.mark.parametrize(
    'printed', ['', 'Traceback dummy-iws-0001 half', '@@20'], ids=['nothing', 'text', 'half a header']
)
def test_a_python_crash_without_a_header_says_so_and_keeps_its_status(tmp_path: Path, rc: int, printed: str):
    """R4: an exit other than 0/1/2 with no valid header -> no HTTP response, `python exited <rc>`, rc kept."""
    env = {'SHIM_CRASH_RC': str(rc), 'SHIM_CRASH_OUT': printed, 'INSTANCE_WRITE_SECRET': 'dummy-iws-0001'}
    run = run_get(tmp_path, 'ping', closed_port(), env=env)
    assert run.completed.returncode == rc, run.completed
    shown = printed.replace('dummy-iws-0001', '<redacted>')
    assert run.stdout == f'== GET /ping: no HTTP response ==\n{shown}\npython exited {rc}\n'
    assert run.redact_in == printed.encode() + b'\n', 'the crash text did not go through redact'
    assert stack.parse_http_get_status(run.completed.stdout) is None
    assert run.flags == ['unset']


@pytest.mark.parametrize(('rc', 'final'), [(0, 2), (1, 2), (2, 2)])
def test_a_zero_one_or_two_exit_without_a_header_names_no_python_exit(tmp_path: Path, rc: int, final: int):
    """0, 1 and 2 are the python's own codes: no `python exited` line, and all three fail as 2.

    tj-iee6sn.13.11 N1: a header-less 1 is a traceback (e.g. the alarm landing inside the except), not
    an HTTP status, so it must not report rc 1, the code for 'HTTP non-2xx'.
    """
    run = run_get(tmp_path, 'ping', closed_port(), env={'SHIM_CRASH_RC': str(rc), 'SHIM_CRASH_OUT': 'x'})
    assert run.completed.returncode == final, run.completed
    assert run.stdout == '== GET /ping: no HTTP response ==\nx\n'


# --- tj-iee6sn.13.11: the exit-status table, read from rc alone ---------------------------------------
#
# The shim stands for a python that printed OUT and then exited RC (SHIM_CRASH_*: the real python does
# not run). The sh sees only those bytes and that status, exactly as after a real kill: a process the
# kernel killed with SIGKILL and a shell `exit 137` are the same $? to it.

_KILLED_SECRET = 'dummy-killed-secret-0001'
_INCOMPLETE = 'python exited {rc}, body incomplete'


def run_shim(tmp_path: Path, out: str, rc: int, env: dict[str, str] | None = None) -> Run:
    full = {'SHIM_CRASH_RC': str(rc), 'SHIM_CRASH_OUT': out, 'INSTANCE_WRITE_SECRET': _KILLED_SECRET}
    return run_get(tmp_path, 'ping', closed_port(), env={**full, **(env or {})})


@pytest.mark.parametrize(
    ('header', 'rc', 'final', 'flags', 'tail'),
    [
        # valid header: 0 and 1 are the python's own HTTP codes -> body as is, nothing after it
        ('@@200@@0', 0, 0, ['unset'], ''),
        ('@@404@@0', 1, 1, ['unset'], ''),
        # valid header, any other status: killed mid-write -> truncated scrub, the incomplete line, rc kept
        ('@@200@@0', 2, 2, ['1'], _INCOMPLETE.format(rc=2) + '\n'),
        ('@@200@@0', 3, 3, ['1'], _INCOMPLETE.format(rc=3) + '\n'),
        ('@@200@@0', 137, 137, ['1'], _INCOMPLETE.format(rc=137) + '\n'),
        ('@@500@@0', 143, 143, ['1'], _INCOMPLETE.format(rc=143) + '\n'),
    ],
    ids=['200 rc0', '404 rc1', '200 rc2', '200 rc3', '200 rc137', '500 rc143'],
)
def test_a_valid_header_keeps_rc_and_flags_any_exit_but_zero_or_one(
    tmp_path: Path, header: str, rc: int, final: int, flags: list[str], tail: str
):
    status = header[2:5]
    run = run_shim(tmp_path, f'{header}\n{{"x":"body"}}', rc)
    assert run.completed.returncode == final, run.completed
    assert run.flags == flags
    assert run.stdout == f'== GET /ping -> HTTP {status} ==\n{{"x":"body"}}\n{tail}'
    assert stack.parse_http_get_status(run.completed.stdout) == int(status)


@pytest.mark.parametrize(
    ('out', 'rc', 'final', 'shown', 'exit_line'),
    [
        ('', 0, 2, '', ''),
        ('', 1, 2, '', ''),
        ('', 2, 2, '', ''),
        ('', 137, 137, '', 'python exited 137\n'),
        # the python's own transport-failure header, then a traceback in the handler (N1): rc 1 -> 2
        (
            '@@000@@0\nTimeoutError: overall deadline of 20 s exceeded',
            1,
            2,
            'TimeoutError: overall deadline of 20 s exceeded',
            '',
        ),
        ('@@000@@0\nConnectionRefusedError: x', 2, 2, 'ConnectionRefusedError: x', ''),
        ('@@000@@0\nConnectionRefusedError: x', 137, 137, 'ConnectionRefusedError: x', 'python exited 137\n'),
    ],
    ids=['none rc0', 'none rc1', 'none rc2', 'none rc137', '000 rc1', '000 rc2', '000 rc137'],
)
def test_no_valid_header_is_no_http_response_and_only_a_status_above_two_survives(
    tmp_path: Path, out: str, rc: int, final: int, shown: str, exit_line: str
):
    """N1: header-less 0 and 1 (and 2) report rc 2; above 2 keeps its code and its `python exited` line.

    Never the incomplete line, and never the truncated scrub: that belongs to a valid header only.
    """
    run = run_shim(tmp_path, out, rc)
    assert run.completed.returncode == final, run.completed
    assert run.stdout == f'== GET /ping: no HTTP response ==\n{shown}\n{exit_line}'
    assert 'body incomplete' not in run.stdout and 'body cut' not in run.stdout
    assert run.flags == ['unset']
    assert stack.parse_http_get_status(run.completed.stdout) is None


@pytest.mark.parametrize('prefix_len', [1, 6, len(_KILLED_SECRET) - 1])
@pytest.mark.parametrize('rc', [137, 9, 3])
def test_a_body_killed_after_the_header_ending_in_a_secret_prefix_prints_none_of_it(
    tmp_path: Path, rc: int, prefix_len: int
):
    """N2: a valid header, a body ending inside a FAKE secret, then a kill. The prefix is scrubbed.

    The python reported no cut (`@@200@@0`), so only rc says the body is incomplete: the truncated
    scrub is on, `python exited <rc>, body incomplete` follows the body, no `body cut` line, rc kept.
    """
    body = '{"message":"pong","x":"' + _KILLED_SECRET[:prefix_len]
    run = run_shim(tmp_path, f'@@200@@0\n{body}', rc)
    assert run.completed.returncode == rc, run.completed
    assert run.flags == ['1']
    assert run.redact_in == body.encode() + b'\n'
    assert run.stdout == (
        '== GET /ping -> HTTP 200 ==\n{"message":"pong","x":"<redacted>\n' + _INCOMPLETE.format(rc=rc) + '\n'
    )
    assert 'body cut' not in run.stdout
    assert 'dummy-' not in run.stdout


def test_the_same_body_exiting_zero_is_not_scrubbed_as_truncated(tmp_path: Path):
    """The control for the test above: same bytes, rc 0 -> the prefix prints, as for any short body."""
    body = '{"message":"pong","x":"' + _KILLED_SECRET[:6]
    run = run_shim(tmp_path, f'@@200@@0\n{body}', 0)
    assert run.completed.returncode == 0 and run.flags == ['unset']
    assert run.stdout == f'== GET /ping -> HTTP 200 ==\n{body}\n'


def test_a_cut_body_then_a_kill_prints_the_cut_line_then_the_incomplete_line(tmp_path: Path):
    """`@@200@@1` and rc 137: both lines, cut first, the prefix scrubbed, rc kept."""
    body = 'f' * 40 + _KILLED_SECRET[:10]
    run = run_shim(tmp_path, f'@@200@@1\n{body}', 137)
    assert run.completed.returncode == 137, run.completed
    assert run.flags == ['1']
    assert run.stdout == (
        '== GET /ping -> HTTP 200 ==\n' + 'f' * 40 + '<redacted>\n'
        '== body cut at 65536 bytes ==\n' + _INCOMPLETE.format(rc=137) + '\n'
    )


def test_a_cut_body_exiting_cleanly_prints_only_the_cut_line(tmp_path: Path):
    run = run_shim(tmp_path, '@@200@@1\n' + 'g' * 40, 0)
    assert run.completed.returncode == 0 and run.flags == ['1']
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n' + 'g' * 40 + '\n== body cut at 65536 bytes ==\n'


@pytest.mark.parametrize('value', ['1', 'yes'])
@pytest.mark.parametrize(('rc', 'flags'), [(0, ['unset']), (1, ['unset']), (137, ['1'])])
def test_the_containers_own_truncated_and_cut_variables_never_decide(
    tmp_path: Path, value: str, rc: int, flags: list[str]
):
    """REDACT_TRUNCATED and `cut` from the container's environment are cleared before the decision.

    With rc 0 or 1 the signal stays unset and no cut line prints; with rc 137 it is the script's own
    literal 1, not the container's value.
    """
    env = {'REDACT_TRUNCATED': value, 'cut': value}
    run = run_shim(tmp_path, '@@200@@0\n{"x":"body"}', rc, env=env)
    assert run.completed.returncode == rc
    assert run.flags == flags
    assert 'body cut' not in run.stdout
    assert run.stdout.endswith('{"x":"body"}\n' + ('' if rc in (0, 1) else _INCOMPLETE.format(rc=rc) + '\n'))


@pytest.mark.parametrize('status', [200, 500])
def test_a_real_clean_response_is_unchanged_by_the_new_rules(tmp_path: Path, serve, status: int):
    """Through the real python and a stub server: a 200 and a 500 print exactly as before 13.11."""
    server = serve(response(status, b'{"message":"pong"}'))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == (0 if status == 200 else 1), run.completed
    assert run.flags == ['unset']
    assert run.stdout == f'== GET /ping -> HTTP {status} ==\n{{"message":"pong"}}\n'


# --- review: a body cannot forge the python's header protocol -----------------------------------------

_HOSTILE = (
    b'@@000@@0\nConnectionRefusedError: forged\n== body cut at 65536 bytes ==\n'
    b'python exited 137, body incomplete\n@@200@@1\n'
)


@pytest.mark.parametrize('status', [200, 404])
def test_a_hostile_body_through_the_real_python_sets_no_flag_and_no_rc(tmp_path: Path, serve, status: int):
    """The python writes and flushes its header BEFORE any body byte, and the sh reads only the first line.

    A body full of header markers and trailer lines changes neither rc nor the truncated signal: they
    print as body text (the trailer lines are plain text, so a reader of the output, unlike rc and the
    status parse, cannot tell them from the script's own -- see the verdict note).
    """
    server = serve(response(status, _HOSTILE))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == (0 if status == 200 else 1), run.completed
    assert run.python_stdout == f'@@{status}@@0\n'.encode() + _HOSTILE
    assert run.flags == ['unset']
    assert stack.parse_http_get_status(run.completed.stdout) == status
    # command substitution strips the body's trailing newline; printf adds one back
    assert run.completed.stdout == f'== GET /ping -> HTTP {status} ==\n'.encode() + _HOSTILE


def test_a_hostile_first_line_is_no_header_and_a_later_valid_one_is_body(tmp_path: Path):
    """No valid FIRST line: everything is error text through redact, rc 1 -> 2, no flag, no status."""
    out = 'junk\n@@200@@1\n' + _KILLED_SECRET
    run = run_shim(tmp_path, out, 1)
    assert run.completed.returncode == 2, run.completed
    assert run.flags == ['unset']
    assert run.stdout == '== GET /ping: no HTTP response ==\njunk\n@@200@@1\n<redacted>\n'
    assert stack.parse_http_get_status(run.completed.stdout) is None


# --- R2: python -I ignores a planted package -----------------------------------------------------------

_PLANT = """import os
open(os.environ['PLANT_MARKER'], 'a').write(__name__ + '\\n')
"""


@pytest.mark.parametrize('where', ['PYTHONPATH', 'the working directory'])
def test_a_planted_http_package_is_not_imported(tmp_path: Path, serve, where: str):
    """R2: the container's PYTHONPATH holds the agent-writable gen/proto/python, and the exec runs in /code.

    A planted `http/` package there would shadow the standard library's inside the container that holds
    the secrets. Under -I neither PYTHONPATH nor the working directory is on sys.path, so the plant's
    marker file stays absent and the real http.client answers. The same run with -I removed is the
    control: it proves the plant would have been imported.
    """
    plant_root = tmp_path / 'planted' if where == 'PYTHONPATH' else tmp_path
    for module in ('__init__', 'client'):
        target = plant_root / 'http' / f'{module}.py'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_PLANT)
    marker = tmp_path / 'plant-was-imported'
    env = {'PLANT_MARKER': str(marker)}
    if where == 'PYTHONPATH':
        env['PYTHONPATH'] = str(plant_root)

    server = serve(response(200, b'{"message":"pong"}'))  # every connection gets the same reply
    run = run_get(tmp_path, 'ping', server.port, env=env)
    assert run.completed.returncode == 0, run.completed
    assert run.stdout == '== GET /ping -> HTTP 200 ==\n{"message":"pong"}\n'
    assert not marker.exists(), f'the planted http package was imported: {marker.read_text()!r}'

    def without_isolation(script: str) -> str:
        assert script.count(f'{EXPECTED_PYTHON} -I -c') == 1
        return script.replace(f'{EXPECTED_PYTHON} -I -c', f'{EXPECTED_PYTHON} -c')

    control = run_get(tmp_path, 'ping', server.port, env=env, edit=without_isolation)
    assert marker.exists() and 'http' in marker.read_text(), 'the control did not import the plant'
    assert control.completed.returncode != 0 or control.stdout != run.stdout


# --- R3: 10 s per socket operation, 20 s overall --------------------------------------------------------


def test_the_overall_deadline_is_twenty_seconds_on_top_of_a_ten_second_socket_timeout():
    """The constants as written. The run below shortens the alarm to stay fast; this pins the real one."""
    python = stack._DEV_HTTP_GET_PYTHON
    # Three calls: arm at 20, disarm after the body, and disarm first in the except (13.11 N1 (b),
    # pinned by statement position in the next test).
    assert f'signal.alarm({EXPECTED_OVERALL_DEADLINE})\n' in python and python.count('signal.alarm(') == 3
    assert python.count('signal.alarm(0)') == 2
    assert f'timeout={EXPECTED_SOCKET_TIMEOUT})' in python and python.count('timeout=') == 1
    assert 'raise TimeoutError("overall deadline of 20 s exceeded")' in python
    # Installed before the connect, cleared only after the body is read and the status taken.
    assert python.index('signal.signal(signal.SIGALRM, deadline)') < python.index('signal.alarm(20)')
    assert python.index('signal.alarm(20)') < python.index('try:') < python.index('http.client.HTTPConnection(')
    assert python.index('body = resp.read(LIMIT + 1)') < python.index('status = resp.status')
    assert python.index('status = resp.status') < python.index('signal.alarm(0)') < python.index('except Exception')
    # Well inside the verb's 60 s, so the in-container process ends before the docker client is killed.
    assert runner.VERB_TIMEOUT_SECONDS['dev_http_get'] > EXPECTED_OVERALL_DEADLINE + 10


def _is_alarm_zero(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == 'signal.alarm'
        and len(node.value.args) == 1
        and not node.value.keywords
        and isinstance(node.value.args[0], ast.Constant)
        and node.value.args[0].value == 0
    )


def test_the_except_clears_the_alarm_before_anything_else():
    """13.11 N1 (b): `signal.alarm(0)` is the FIRST statement of the one except, parsed, not grepped.

    The try's last statement disarms it on success; the handler's first disarms it on failure, so a
    socket timeout just before the 20 s deadline cannot have SIGALRM raise again inside the handler
    (which would die by traceback, exit 1, and print no header). The sh's rc mapping is the fix; this
    narrows the window.
    """
    tree = ast.parse(stack._DEV_HTTP_GET_PYTHON)
    tries = [node for node in tree.body if isinstance(node, ast.Try)]
    assert len(tries) == 1 and len(tries[0].handlers) == 1
    (handler,) = tries[0].handlers
    assert ast.unparse(handler.type) == 'Exception'
    assert _is_alarm_zero(handler.body[0]), ast.unparse(handler.body[0])
    assert _is_alarm_zero(tries[0].body[-1]), ast.unparse(tries[0].body[-1])
    # the rest of the handler is unchanged: the transport-failure header and error text, then exit 2
    assert [ast.unparse(node) for node in handler.body[1:]] == [
        "sys.stdout.write('@@000@@0\\n' + type(exc).__name__ + ': ' + str(exc) + '\\n')",
        'sys.exit(2)',
    ]


_SHORT_DEADLINE = 3


def _short_deadline(script: str) -> str:
    assert script.count('signal.alarm(20)') == 1, 'the 20 s alarm is gone'
    return script.replace('signal.alarm(20)', f'signal.alarm({_SHORT_DEADLINE})')


@pytest.mark.parametrize(
    'parts',
    [
        # Status line and headers dripped: getresponse() never finishes.
        (Trickle(response(200, b'Z' * 40)),),
        # Headers at once, then the body dripped: read() never finishes.
        (response(200, b'', length=40), Trickle(b'Z' * 40)),
    ],
    ids=['dripped headers', 'dripped body'],
)
def test_a_trickle_one_byte_per_second_ends_at_the_overall_deadline_with_no_body(tmp_path: Path, serve, parts: tuple):
    """R3: each byte arrives inside the 10 s socket timeout, so only the overall deadline can end it.

    The alarm is shortened from 20 s to 3 s by editing the script text (the real value is pinned above).
    The call ends in the transport-failure path: exit 2, TimeoutError, and none of the body.
    """
    server = serve(*parts)
    run = run_get(tmp_path, 'ping', server.port, edit=_short_deadline, timeout=30)
    assert run.completed.returncode == 2, run.completed
    assert run.stdout == '== GET /ping: no HTTP response ==\nTimeoutError: overall deadline of 20 s exceeded\n'
    assert b'Z' not in run.completed.stdout and b'Z' not in run.redact_in
    assert run.python_stdout == b'@@000@@0\nTimeoutError: overall deadline of 20 s exceeded\n'
    assert _SHORT_DEADLINE - 0.5 <= run.elapsed < _SHORT_DEADLINE + 4, run.elapsed
    assert run.flags == ['unset']


def test_a_response_inside_the_deadline_is_untouched_by_the_alarm(tmp_path: Path, serve):
    """A slow reply that finishes inside the deadline is a normal GET, not a timeout.

    With the alarm at 3 s, a body dripped over about 2 s prints as usual and exits 0: the deadline
    bounds the whole request, it does not shorten an ordinary one.
    """
    server = serve(response(200, b'', length=2), Trickle(b'ok', interval=0.9))
    run = run_get(tmp_path, 'ping', server.port, edit=_short_deadline)
    assert run.completed.returncode == 0, run.completed
    assert run.stdout == '== GET /ping -> HTTP 200 ==\nok\n'


# --- redaction of the body and of the error text, FAKE values only -----------------------------------

_FAKE_ENV = {
    'INSTANCE_WRITE_SECRET': 'dummy-iws-0001',
    'POSTGRES_PASS': 'dummy-pgpass-0002',
    'POSTGRES_PASSWORD': 'dummy-pgpassword-0003',
    'ALPACA_API_KEY': 'dummy-alpaca-key-0004',
    'ALPACA_API_SECRET': 'dummy-alpaca-secret-0005',
    'DATABASE_URI': 'postgresql://u:dummy-dsn-pw-0006@db:5432/x',
}
_MULTI = 'dummy-line-one-0007\ndummy-line-two-0008'


def _leaks(text: str) -> list[str]:
    values = [*_FAKE_ENV.values(), *_MULTI.split('\n'), 'dummy-']
    return [value for value in values if value in text]


def test_every_fake_secret_in_a_body_is_redacted_whole_and_contained(tmp_path: Path, serve):
    body = '{' + ','.join(f'"{key}":"zz{value}zz","{key}2":"{value}"' for key, value in _FAKE_ENV.items()) + '}'
    server = serve(response(200, body.encode()))
    run = run_get(tmp_path, 'usage_summary', server.port, env=_FAKE_ENV)
    assert run.completed.returncode == 0, run.completed
    assert _leaks(run.stdout) == []
    assert run.stdout.count('<redacted>') == 2 * len(_FAKE_ENV)
    assert run.stdout.startswith(
        '== GET /ui/v1/stats/usage/summary -> HTTP 200 ==\n{"INSTANCE_WRITE_SECRET":"zz<redacted>zz"'
    )


def test_a_multi_line_secret_in_a_body_is_redacted_whole_and_by_line(tmp_path: Path, serve):
    body = f'A {_MULTI} B\nC dummy-line-two-0008 D\nE zzdummy-line-one-0007zz\n'.encode()
    server = serve(response(404, body))
    run = run_get(tmp_path, 'ui_config', server.port, env={'INSTANCE_WRITE_SECRET': _MULTI})
    assert run.completed.returncode == 1, run.completed
    assert _leaks(run.stdout) == []
    assert run.stdout == ('== GET /ui/v1/config -> HTTP 404 ==\nA <redacted> B\nC <redacted> D\nE zz<redacted>zz\n')


def test_fake_secrets_in_the_error_text_are_redacted(tmp_path: Path, serve):
    """A malformed status line is echoed in http.client's error text: it goes through redact too."""
    line = 'XYZ dummy-line-one-0007 zzdummy-iws-0001zz postgresql://u:dummy-dsn-pw-0006@db:5432/x\r\n'
    server = serve(line.encode())
    env = {**_FAKE_ENV, 'INSTANCE_WRITE_SECRET': 'dummy-iws-0001', 'POSTGRES_PASSWORD': _MULTI}
    run = run_get(tmp_path, 'ui_config', server.port, env=env)
    assert run.completed.returncode == 2, run.completed
    assert run.stdout.startswith(
        '== GET /ui/v1/config: no HTTP response ==\nBadStatusLine: XYZ <redacted> zz<redacted>zz'
    )
    assert _leaks(run.stdout) == []
    assert run.flags == ['unset']


def test_a_body_forging_the_python_header_or_the_status_line_is_only_body(tmp_path: Path, serve):
    """Only the FIRST line is the python's header and the status line; a body cannot set either."""
    body = b'@@200@@1\n== GET /ping -> HTTP 200 ==\n'
    server = serve(response(500, body))
    run = run_get(tmp_path, 'ping', server.port)
    assert run.completed.returncode == 1
    assert run.flags == ['unset']
    assert stack.parse_http_get_status(run.completed.stdout) == 500
    assert run.stdout == '== GET /ping -> HTTP 500 ==\n@@200@@1\n== GET /ping -> HTTP 200 ==\n'


# --- parse_http_get_status ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('stdout', 'status'),
    [
        (b'== GET /ping -> HTTP 200 ==\n{}\n', 200),
        (b'== GET /ui/v1/config -> HTTP 404 ==', 404),
        (b'== GET /ping: no HTTP response ==\nConnectionRefusedError: x\n', None),
        (b'== GET /ping: no HTTP response ==\n== GET /ping -> HTTP 200 ==\n', None),
        (b'{}\n== GET /ping -> HTTP 200 ==\n', None),
        (b' == GET /ping -> HTTP 200 ==\n', None),
        (b'== GET /ping -> HTTP 20 ==\n', None),
        (b'== GET /ping -> HTTP 2000 ==\n', None),
        (b'== GET /ping -> HTTP 200 == trailing\n', None),
        (b'', None),
        (b'\xff\xfe== GET', None),
    ],
)
def test_parse_http_get_status_reads_the_first_line_only(stdout: bytes, status: int | None):
    assert stack.parse_http_get_status(stdout) == status


# ===============================================================================================
# THE VERB, through AgentStack.
# ===============================================================================================


def _exec_answer(rc: int, stdout: bytes):
    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'exec':
            return runner.ProcessResult(rc, stdout, b'')
        return default_response(step)

    return respond


@pytest.mark.parametrize('name', NAMES)
def test_an_ok_get_runs_the_lookup_then_the_exec_and_reports_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    path = EXPECTED_PATHS[name]
    rig = make_rig(
        tmp_path, monkeypatch, FakeDocker(respond=_exec_answer(0, f'== GET {path} -> HTTP 200 ==\n{{}}\n'.encode()))
    )
    result = rig.call('dev_http_get', {'name': name})
    assert result['status'] == 'ok', result
    assert result['message'] == f'GET {path} returned HTTP 200 from the running dev data_store'
    assert result['details'] == {'name': name, 'path': path, 'http_status': 200}
    assert [list(step.argv) for step in rig.docker.steps] == [expected_lookup(), expected_exec(name)]
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps)
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['verb'] == 'dev_http_get' and audit['status'] == 'ok' and audit['arguments'] == {'name': name}


def test_a_non_2xx_is_failed_with_its_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=_exec_answer(1, b'== GET /ping -> HTTP 404 ==\n{}\n')))
    result = rig.call('dev_http_get', {'name': 'ping'})
    assert result['status'] == 'failed' and result['exit_status'] == 1, result
    assert result['message'] == 'GET /ping returned HTTP 404 from the running dev data_store; see the steps'
    assert result['details'] == {'name': 'ping', 'path': '/ping', 'http_status': 404}


@pytest.mark.parametrize('rc', [2, 127])
def test_no_http_response_is_failed_with_no_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int):
    stdout = b'== GET /ping: no HTTP response ==\nConnectionRefusedError: [Errno 111]\n'
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=_exec_answer(rc, stdout)))
    result = rig.call('dev_http_get', {'name': 'ping'})
    assert result['status'] == 'failed' and result['exit_status'] == rc, result
    assert result['message'] == 'GET /ping got no HTTP response from the running dev data_store; see the steps'
    assert result['details'] == {'name': 'ping', 'path': '/ping', 'http_status': None}


def test_the_status_is_read_from_the_whole_stdout_not_the_capped_step_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The reported status comes from the first line of what the exec printed.

    A 64 KB body pushes the status line out of the step's capped text (the LAST OUTPUT_CAP_BYTES).
    """
    stdout = b'== GET /ping -> HTTP 200 ==\n' + b'a' * EXPECTED_LIMIT + b'\n== body cut at 65536 bytes ==\n'
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=_exec_answer(0, stdout)))
    result = rig.call('dev_http_get', {'name': 'ping'})
    assert result['status'] == 'ok' and result['details']['http_status'] == 200, result
    step = result['steps'][-1]
    assert step['stdout']['truncated'] is True and step['stdout']['text'].endswith('== body cut at 65536 bytes ==\n')


_LOOKUP_ANSWERS = {
    'no running container': (runner.ProcessResult(0, b'', b''), 'no running data_store container'),
    'blank lines': (runner.ProcessResult(0, b'\n \n', b''), 'no running data_store container'),
    'two running containers': (runner.ProcessResult(0, b'c0ffee000001\nc0ffee000002\n', b''), '2 running data_store'),
    'three running containers': (
        runner.ProcessResult(0, b'c0ffee000001\nc0ffee000002\nc0ffee000003\n', b''),
        '3 running data_store',
    ),
    'a non-hex answer': (runner.ProcessResult(0, b'trader_joe-data_store-1\n', b''), 'not a container id'),
    'the lookup failed': (runner.ProcessResult(1, b'', b'403 Forbidden'), 'the container lookup failed'),
}


@pytest.mark.parametrize('case', list(_LOOKUP_ANSWERS), ids=list(_LOOKUP_ANSWERS))
def test_zero_or_several_containers_are_refused_with_no_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    answer, match = _LOOKUP_ANSWERS[case]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'ps':
            return answer
        return default_response(step)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    result = rig.call('dev_http_get', {'name': 'usage_summary'})
    assert result['status'] == 'refused', result
    assert match in result['message'], result
    assert [list(step.argv) for step in rig.docker.steps] == [expected_lookup()], 'an exec ran'
    names_fallback = 'use dev_db_tables, which works while data_store is down' in result['message']
    assert names_fallback is match.startswith('no running'), result['message']
    assert 'c0ffee' not in result['message']
    assert 'details' not in result
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['verb'] == 'dev_http_get' and audit['status'] == 'refused', audit


# ===============================================================================================
# THE DOCSTRINGS (work order (5), 00:55 note).
# ===============================================================================================


def test_the_migrate_status_docstring_scopes_writes_nothing_to_the_database():
    doc = runner.AgentStack._dev_migrate_status.__doc__ or ''
    assert (
        'Takes no argument and writes nothing to the database (Python may write bytecode caches into the '
        'bind-mounted checkout, as make migrate-status does);'
    ) in doc
    assert 'Takes no argument, writes nothing,' not in doc


def test_the_http_get_docstring_names_the_three_paths_and_the_bound():
    doc = runner.AgentStack._dev_http_get.__doc__ or ''
    assert doc.startswith('READ-ONLY: ONE GET of a fixed path,'), 'F4: READ-ONLY stays, unscoped'
    for name, path in EXPECTED_PATHS.items():
        assert f'{name} ({path})' in doc
    assert DROPPED_NAME not in doc and 'reaper' not in doc
    assert '64 KB' in doc and 'use dev_db_tables' in doc
    # R5, in one clause; R3's two bounds by name.
    assert (
        'GET only; Accept is the only header the script sets (http.client adds Host and Accept-Encoding: '
        'identity itself), nothing else is sent, no X-Instance-Secret;'
    ) in doc
    assert '10 s per socket operation, 20 s overall' in doc


def _comment_before(source: str, anchor: str) -> str:
    """The `#` comment block directly above ANCHOR in SOURCE, its lines joined by single spaces."""
    lines = source[: source.index(anchor)].split('\n')[:-1]
    block: list[str] = []
    while lines and lines[-1].startswith('#'):
        block.insert(0, lines.pop()[1:].strip())
    return ' '.join(block)


def test_the_stack_comment_states_exactly_what_is_sent_and_both_bounds():
    """R5's exact wording, and R2-R4 named where the script is defined."""
    source = Path(stack.__file__).read_text(encoding='utf-8')
    python_comment = _comment_before(source, '_DEV_HTTP_GET_PYTHON = ')
    assert (
        'Accept is the only header the script sets; http.client adds Host and Accept-Encoding: identity '
        'itself; nothing else is sent, no X-Instance-Secret'
    ) in python_comment
    assert 'only an Accept header' not in python_comment
    assert '10 s per socket operation, 20 s overall' in python_comment
    assert '`python -I` with its stderr discarded' in python_comment
    sh_comment = _comment_before(source, '_DEV_HTTP_GET_TEMPLATE = ')
    assert 'python runs with -I' in sh_comment and 'its stderr goes to /dev/null' in sh_comment
    paths_comment = _comment_before(source, 'DEV_HTTP_GET_PATHS: dict[str, str] = {')
    assert 'one of three fixed paths' in paths_comment and 'REAPER' not in paths_comment
    package_doc = sys.modules['tools.agent_mcp'].__doc__ or ''
    assert 'dev_http_get(name) does one GET of one of three fixed paths' in package_doc
