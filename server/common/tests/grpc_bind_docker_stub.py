"""A stand-in `docker` CLI for System Testing's Check gRPC Bind Network step (tj-3mk3u5.49).

No test functions here. test_grpc_bind_network.py puts a two-line shell `docker` on PATH that runs
this file with the docker arguments, and describes the stack through the environment:

  STUB_SCENARIO         a JSON file:
                          container_env  data_ingest's container environment, a mapping
                          resolves       {name: [address, ...]}: what getaddrinfo answers inside
                                         data_ingest. Any other name raises socket.gaierror.
                          ps_id          what `compose ps -q data_ingest` prints ('' for none)
                          exec_fails     true: every exec fails, as on a container that is not running
                          networks       {network name: {"IPAddress": ..., "GlobalIPv6Address": ...}},
                                         data_ingest's NetworkSettings.Networks, which inspect reads
  STUB_LOG              each call's arguments, appended as one JSON array per line
  STUB_GETADDRINFO_LOG  each getaddrinfo call the step's own code makes, normalised by
                        GETADDRINFO_FIELDS (the test compares it with refuse_wildcard's call)

WHAT IS NOT IMITATED. `compose exec -T data_ingest /code/.venv/bin/python -c CODE ARGS` runs the
step's own CODE, in this process, under the scenario's environment and getaddrinfo: what it prints
and how it fails are the step's, not a copy. `docker inspect --format` evaluates the step's own Go
template, with a renderer for exactly the constructs that template uses -- range over a map in
sorted key order (as Go ranges a map), variables, field access, println -- and refuses every other
construct and every field the scenario does not carry. Any call that is not one of these three
exits UNMODELLED, naming it, so a rewritten step turns the test red instead of meeting a canned
answer.

What only Docker can show -- the embedded DNS answering the alias with the ingest_store address,
inspect's real output, compose's exec -- is CI System Testing's run of the step itself.
"""

import json
import os
import re
import shlex
import socket
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


UNMODELLED = 97
DATA_INGEST = 'data_ingest'
STACK_FILES = ['docker-compose.yaml']
VENV_PYTHON = '/code/.venv/bin/python'
GETADDRINFO_FIELDS = ('host', 'port', 'family', 'type', 'proto', 'flags')

_ACTION = re.compile(r'\{\{(.*?)\}\}', re.DOTALL)
_RANGE = re.compile(r'^range\s+(\$\w+)\s*,\s*(\$\w+)\s*:=\s*(\$?\w*(?:\.\w+)+)$')
_PATH = re.compile(r'^(\$\w+)?((?:\.\w+)*)$')


class Unmodelled(Exception):
    """A call, option or template construct this stand-in does not implement."""


def recording_getaddrinfo(answers: Mapping[str, list[str]], log_path: str | None) -> Callable[..., list]:
    """A socket.getaddrinfo stand-in that answers from ANSWERS and logs every call it receives.

    socket.getaddrinfo's own signature, so a positional call (asyncio's loop.getaddrinfo) and a
    keyword call (the step's `type=`) log the same normalised record. With no socktype it answers
    one entry per socktype, as the real call does, so a step that forgot to deduplicate would show.
    """

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0) -> list:
        call = dict(zip(GETADDRINFO_FIELDS, (host, port, int(family), int(type), int(proto), int(flags)), strict=True))
        if log_path:
            with open(log_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(call) + '\n')
        socktypes = [int(type)] if type else [socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW]
        infos = []
        for address in answers.get(host, []):
            address_family = socket.AF_INET6 if ':' in address else socket.AF_INET
            if family and family != address_family:
                continue
            sockaddr = (address, port or 0, 0, 0) if address_family == socket.AF_INET6 else (address, port or 0)
            infos += [(address_family, socket.SocketKind(kind), 0, '', sockaddr) for kind in socktypes]
        if not infos:
            raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        return infos

    return getaddrinfo


def _parse_template(template: str) -> list:
    """The template as nodes: ('text', s), ('value', path) and ('range', key, value, path, body)."""
    root: list = []
    stack = [root]
    position = 0
    for match in _ACTION.finditer(template):
        stack[-1].append(('text', template[position : match.start()]))
        position = match.end()
        raw = match.group(1)
        if raw.startswith('- ') or raw.endswith(' -'):
            raise Unmodelled(f'whitespace trimming in {{{{{raw}}}}}')
        action = raw.strip()
        if ranged := _RANGE.match(action):
            body: list = []
            stack[-1].append(('range', ranged.group(1), ranged.group(2), ranged.group(3), body))
            stack.append(body)
        elif action == 'end':
            if len(stack) == 1:
                raise Unmodelled('{{end}} with no open range')
            stack.pop()
        elif action == 'println':
            stack[-1].append(('text', '\n'))
        elif action and _PATH.match(action):
            stack[-1].append(('value', action))
        else:
            raise Unmodelled(f'template action {{{{{action}}}}}')
    if len(stack) != 1:
        raise Unmodelled('a {{range}} is never closed')
    root.append(('text', template[position:]))
    return root


def _lookup(path: str, dot: Any, variables: Mapping[str, Any]) -> Any:
    match = _PATH.match(path)
    if not match:
        raise Unmodelled(f'template path {path}')
    head, fields = match.group(1), [field for field in match.group(2).split('.') if field]
    if head and head not in variables:
        raise Unmodelled(f'undefined variable {head}')
    value = variables[head] if head else dot
    for field in fields:
        if not isinstance(value, Mapping) or field not in value:
            raise Unmodelled(f"can't evaluate field {field} in {path}: the scenario does not carry it")
        value = value[field]
    return value


def _evaluate(nodes: list, dot: Any, variables: Mapping[str, Any]) -> str:
    out = []
    for node in nodes:
        if node[0] == 'text':
            out.append(node[1])
        elif node[0] == 'value':
            value = _lookup(node[1], dot, variables)
            if isinstance(value, Mapping | list):
                raise Unmodelled(f'printing the composite {node[1]}')
            out.append(str(value))
        else:
            _, key_name, value_name, path, body = node
            collection = _lookup(path, dot, variables)
            if not isinstance(collection, Mapping):
                raise Unmodelled(f'range over {path}, which is not a map')
            for key in sorted(collection):
                item = collection[key]
                out.append(_evaluate(body, item, {**variables, key_name: key, value_name: item}))
    return ''.join(out)


def render_template(template: str, data: Mapping[str, Any]) -> str:
    """Render the subset of Go's text/template described in the module docstring."""
    return _evaluate(_parse_template(template), data, {})


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


def _exec(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, rest = _options(words, {'-T'}, set())
    if '-T' not in options:
        raise Unmodelled('exec without -T, which allocates a TTY a CI step does not have')
    if rest[:1] != [DATA_INGEST]:
        raise Unmodelled(f'exec into {rest[:1]}; the step reads and resolves inside {DATA_INGEST}')
    command = rest[1:]
    if command[:2] != [VENV_PYTHON, '-c'] or len(command) < 3:
        raise Unmodelled(f'exec of {command[:2]}; only the image interpreter running -c is modelled')
    if scenario.get('exec_fails'):
        print(f'service "{DATA_INGEST}" is not running', file=sys.stderr)
        return 1
    code, arguments = command[2], command[3:]
    log_path = os.environ.get('STUB_GETADDRINFO_LOG')
    os.environ.clear()
    os.environ.update(scenario.get('container_env') or {})
    socket.getaddrinfo = recording_getaddrinfo(scenario.get('resolves') or {}, log_path)
    sys.argv = ['-c', *arguments]
    # The step's own code, as `python -c` runs it: an uncaught exception prints its traceback and
    # exits 1, and SystemExit sets the status.
    exec(compile(code, '<string>', 'exec'), {'__name__': '__main__'})
    return 0


def _compose(words: list[str], scenario: Mapping[str, Any]) -> int:
    options, rest = _options(words, set(), {'-f', '--file'})
    files = options.get('-f', []) + options.get('--file', [])
    if files != STACK_FILES:
        raise Unmodelled(f'compose files {files}; the step reads the stack on its own spelling, {STACK_FILES}')
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
    scenario = json.loads(Path(os.environ['STUB_SCENARIO']).read_text(encoding='utf-8'))
    with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(argv) + '\n')
    try:
        if argv[:1] == ['compose']:
            return _compose(argv[1:], scenario)
        if argv[:1] == ['inspect']:
            return _inspect(argv[1:], scenario)
        raise Unmodelled(f'docker {argv[:1]}')
    except Unmodelled as unmodelled:
        print(f'docker stub: not modelled: {unmodelled}: docker {shlex.join(argv)}', file=sys.stderr)
        return UNMODELLED


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
