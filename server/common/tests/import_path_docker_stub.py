"""A stand-in `docker` CLI for System Testing's Check Generated Code Import step (tj-3mk3u5.56).

No test functions here. test_service_pythonpath.py puts a two-line shell `docker` on PATH that runs this
file with the docker arguments, and describes the running stack through the environment:

  STUB_SCENARIO         a JSON file: {"containers": {service: {NAME: value, ...}}}, the container
                        environment of each RUNNING service, as the compose model computes it. A service
                        it does not name is not running, and an exec into it fails as compose's does.
  STUB_CODE_ROOT        the checkout, which stands in for the image's code root, /code
  STUB_LOG              each call's arguments, appended as one JSON array per line
  STUB_UNMODELLED_LOG   each call this file refuses

THE ONE CALL MODELLED: `docker compose -f docker-compose.yaml exec -T <service> /code/.venv/bin/python -c
CODE [ARGS]`. Anything else exits UNMODELLED, naming it, so a rewritten step turns the test red instead of
meeting a canned answer.

WHAT IS NOT IMITATED. CODE is the step's own, and it runs in a FRESH interpreter (this venv's, standing in
for the image's), as `python -c` runs it inside the container:
  * its os.environ is the container environment, and nothing of this process's;
  * its sys.path is the one the container's interpreter builds: the working directory (WORKDIR, /code),
    then every PYTHONPATH entry in order, a relative one read against /code, then the interpreter's own
    library paths -- each /code path read as the checkout. The interpreter starts with -E and -P, so no
    PYTHONPATH of this process's, and no directory of this file's, reaches it.
So whether CODE's imports resolve is decided by the container's PYTHONPATH alone, exactly the question
the step asks: a PYTHONPATH without /code/gen/proto/python cannot import trader_joe.proto here either.
What prints and how it exits are the step's code's, not a copy.

What only Docker can show -- the image's real /code, compose's real precedence over the real env file,
exec into a running container -- is CI System Testing's run of the step itself.
"""

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from common.tests.grpc_bind_docker_stub import UNMODELLED, VENV_PYTHON, Unmodelled
from common.tests.image_path import IMAGE_CODE_ROOT


STACK_FILES = ['docker-compose.yaml']
# This process's own STUB_* settings, read once at start.
_SETTINGS = {name: value for name, value in os.environ.items() if name.startswith('STUB_')}

# The child's first code: put the container's import path in place, then run the step's CODE as
# `python -c` would, with argv[0] '-c' and the rest its ARGS. argv[1] is the checkout, argv[2] CODE.
_BOOTSTRAP = """
import os, posixpath, sys
_checkout, _code = sys.argv[1], sys.argv[2]
sys.argv = ['-c', *sys.argv[3:]]
def _on_checkout(entry):
    path = posixpath.normpath(posixpath.join(CODE_ROOT, entry))
    if path == CODE_ROOT or path.startswith(CODE_ROOT + '/'):
        return _checkout + path[len(CODE_ROOT):]
    return path
sys.path[0:0] = [_on_checkout('.')] + [
    _on_checkout(entry) for entry in os.environ.get('PYTHONPATH', '').split(':') if entry
]
exec(compile(_code, '<string>', 'exec'), {'__name__': '__main__'})
""".replace('CODE_ROOT', repr(str(IMAGE_CODE_ROOT)))


def _log(variable: str, record: object) -> None:
    path = _SETTINGS.get(variable)
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record) + '\n')


def _exec(words: list[str], scenario: Mapping[str, Any]) -> int:
    if words[:1] != ['-T']:
        raise Unmodelled(f'exec options {words[:1]}; only -T, which a CI step needs, is modelled')
    service, command = (words[1], words[2:]) if len(words) > 1 else ('', [])
    if command[:2] != [VENV_PYTHON, '-c'] or len(command) < 3:
        raise Unmodelled(f'exec of {command[:2]} in {service!r}; only the image interpreter running -c is modelled')
    containers = scenario.get('containers') or {}
    if service not in containers:
        print(f'service "{service}" is not running', file=sys.stderr)
        return 1
    checkout = _SETTINGS['STUB_CODE_ROOT']
    argv = [sys.executable, '-E', '-P', '-c', _BOOTSTRAP, checkout, command[2], *command[3:]]
    # The container environment and nothing else: no PATH, no STUB_* setting, no PYTHONPATH of ours.
    environment = {str(name): str(value) for name, value in containers[service].items()}
    return subprocess.run(argv, env=environment, cwd=checkout, check=False).returncode


def _compose(words: list[str], scenario: Mapping[str, Any]) -> int:
    files, position = [], 0
    while position < len(words) and words[position] in ('-f', '--file') and position + 1 < len(words):
        files.append(words[position + 1])
        position += 2
    rest = words[position:]
    if files != STACK_FILES or rest[:1] != ['exec']:
        raise Unmodelled(f'compose files {files} for {rest[:1]}; only an exec on the base file alone is modelled')
    return _exec(rest[1:], scenario)


def main(argv: list[str]) -> int:
    scenario = json.loads(Path(_SETTINGS['STUB_SCENARIO']).read_text(encoding='utf-8'))
    _log('STUB_LOG', argv)
    try:
        if argv[:1] == ['compose']:
            return _compose(argv[1:], scenario)
        raise Unmodelled(f'docker {argv[:1]}')
    except Unmodelled as unmodelled:
        message = f'docker stub: not modelled: {unmodelled}: docker {shlex.join(argv)}'
        print(message, file=sys.stderr)
        _log('STUB_UNMODELLED_LOG', message)
        return UNMODELLED


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
