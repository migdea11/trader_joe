"""tj-iee6sn.30: the build verbs' compose files must LOAD under the step env the verbs really build.

Found live at the tj-iee6sn.11 sitting: build and test_build both failed in under a second with
"services.postgres.container_name '' does not match pattern '[a-zA-Z0-9][a-zA-Z0-9_.-]+'". The base
file says `container_name: ${DATABASE_NAME}`, and the build step env (D5: no env file is read) did not
set DATABASE_NAME. compose validates the loaded files against its schema before it builds anything,
so an empty value in such a field fails the whole verb.

These tests take everything from the code rather than restating it: the step comes from the real step
builder (stack.dev_build_step, stack.testmode_build_step); the file list is that step's own `-f`
arguments; the env is what the runner hands the subprocess (runner.DOCKER_ENV under the step env).
The files are read from the checkout: S6 refuses a verb whenever the baked copy differs from it.

WHAT THIS CANNOT PROVE: no docker runs here. The schema check is a stand-in written from the live
refusal and the compose-spec patterns. The host test_build at the sitting is the only real evidence.
"""

import itertools
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import REPO_ROOT
from tools.agent_mcp.tests.test_dev_build import _interpolate, _Unset


HOST = Path('/home/someone/trader_joe')
STACK_DIR = Path('/stack')
DIGESTS = ('1' * 64, '2' * 64)
VERBS = ('build', 'test_build')

# `$$` is an escape; `${NAME}` and `$NAME` are plain; `${NAME:?m}` and `${NAME?m}` are required; the
# `-` forms carry a default. Group 1 or 3 is the name, group 2 the operator.
_REFERENCE = re.compile(r'\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)')
# compose-spec's pattern for container_name, quoted by the live refusal.
_CONTAINER_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]+')
_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*')
_PORT = re.compile(r'\d+(?:-\d+)?')
_IP = re.compile(r'(?:\d{1,3}\.){3}\d{1,3}')
# The keys the pre-fix placeholder env lacked (3e5c0c8 added them).
_PRE_FIX_MISSING = (
    'DATABASE_NAME',
    'DATABASE_PORT',
    'DATA_STORE_PORT',
    'DATA_INGEST_PORT',
    'POSTGRES_USER',
    'POSTGRES_DB_NAME',
)


def _step(verb: str) -> stack.Step:
    builder = {'build': stack.dev_build_step, 'test_build': stack.testmode_build_step}[verb]
    return builder(STACK_DIR, HOST, DIGESTS)


def _files(step: stack.Step) -> list[str]:
    """The step's `-f` arguments, each the trusted copy's name relative to TRUSTED_COMPOSE_DIR."""
    argv = list(step.argv)
    return [
        str(Path(value).relative_to(stack.TRUSTED_COMPOSE_DIR))
        for flag, value in itertools.pairwise(argv)
        if flag == '-f'
    ]


def _env(step: stack.Step) -> dict[str, str]:
    """What the runner gives the subprocess: DOCKER_ENV, the step env over it."""
    return {**runner.DOCKER_ENV, **dict(step.env)}


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [text for item in node for text in _strings(item)]
    if isinstance(node, dict):
        return [text for item in node.values() for text in _strings(item)]
    return []


def _validated_fields(document: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """(path, kind, raw value) for every field compose's schema or loader checks at config time."""
    fields: list[tuple[str, str, Any]] = []
    for service, spec in (document.get('services') or {}).items():
        spec = spec or {}
        for key in ('container_name', 'hostname', 'image'):
            if key in spec:
                fields.append((f'services.{service}.{key}', key, spec[key]))
        for key in ('ports', 'expose', 'volumes'):
            for index, entry in enumerate(spec.get(key) or []):
                fields.append((f'services.{service}.{key}[{index}]', key, entry))
        env_file = spec.get('env_file')
        for index, entry in enumerate([env_file] if isinstance(env_file, str) else env_file or []):
            fields.append((f'services.{service}.env_file[{index}]', 'env_file', entry))
        build = spec.get('build')
        if isinstance(build, dict):
            for key in ('context', 'dockerfile'):
                if key in build:
                    fields.append((f'services.{service}.build.{key}', 'path', build[key]))
            args = build.get('args') or {}
            for arg, value in args.items() if isinstance(args, dict) else enumerate(args):
                fields.append((f'services.{service}.build.args.{arg}', 'arg', value))
    for top in ('networks', 'volumes'):
        for name, spec in (document.get(top) or {}).items():
            if isinstance(spec, dict) and 'name' in spec:
                fields.append((f'{top}.{name}.name', 'name', spec['name']))
    return fields


def _must_be_set(text: str) -> list[str]:
    """The names TEXT references with no default: plain `${X}`/`$X` and the `?` forms. `$${X}` is not one."""
    names = []
    for match in _REFERENCE.finditer(text):
        if match.group(0) == '$$':
            continue
        operator = match.group(2)
        if operator is None or operator in ('?', ':?'):
            names.append(match.group(1) or match.group(3))
    return names


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')) or {}


def _unset_in_validated_fields(files: list[str], env: dict[str, str]) -> list[tuple[str, str, str]]:
    """(file, field path, variable) for every no-default reference in a validated field that ENV leaves unset or empty."""
    missing = []
    for name in files:
        for path, _, raw in _validated_fields(_load(name)):
            for text in _strings(raw):
                missing += [(name, path, variable) for variable in _must_be_set(text) if not env.get(variable)]
    return missing


def _port_problems(raw: str, rendered: str) -> list[str]:
    """Short syntax [ip:][host:]container[/proto]: the container port, and any part a reference fills, must be valid."""
    # Each reference masked to '$' first, so a default word's ':' (`${WEB_PORT:-8088}`) splits nothing.
    masked = _REFERENCE.sub(lambda match: '' if match.group(0) == '$$' else '$', raw)
    raw_parts, parts = masked.split('/')[0].split(':'), rendered.split('/')[0].split(':')
    if len(raw_parts) != len(parts) or len(parts) > 3:
        return [f'shape {rendered!r}']
    problems = []
    for index, (raw_part, part) in enumerate(zip(raw_parts, parts, strict=True)):
        if index != len(parts) - 1 and '$' not in raw_part and not part:
            continue  # an empty literal host or ip is compose's 'any'
        is_ip = len(parts) == 3 and index == 0
        if is_ip:
            if not _IP.fullmatch(part):
                problems.append(f'ip {part!r}')
        elif not _PORT.fullmatch(part) or not all(1 <= int(p) <= 65535 for p in part.split('-')):
            problems.append(f'port {part!r}')
    return problems


def _invalid_values(files: list[str], env: dict[str, str]) -> list[str]:
    """Every validated field that renders, under ENV, to a value compose would refuse."""
    problems = []
    for name in files:
        for path, kind, raw in _validated_fields(_load(name)):
            where = f'{name}: {path}'
            try:
                if isinstance(raw, dict):
                    rendered = {key: _interpolate(str(value), env) for key, value in raw.items()}
                else:
                    rendered = _interpolate(str(raw), env)
            except _Unset as unset:
                problems.append(f'{where}: a :? guard fired: {unset}')
                continue
            if (
                (kind == 'container_name' and not _CONTAINER_NAME.fullmatch(rendered))
                or (kind in ('hostname', 'name') and not _NAME.fullmatch(rendered))
                or (kind == 'image' and (not rendered or rendered.startswith(':') or rendered.endswith(':')))
            ):
                problems.append(f'{where}: {rendered!r}')
            elif kind == 'ports' and isinstance(raw, str):
                problems += [f'{where}: {problem}' for problem in _port_problems(raw, rendered)]
            elif kind == 'ports' and isinstance(raw, dict):
                for key in ('target', 'published'):
                    if key in rendered and not _PORT.fullmatch(rendered[key]):
                        problems.append(f'{where}: {key} {rendered[key]!r}')
            elif kind == 'expose' and not _PORT.fullmatch(rendered.split('/')[0]):
                problems.append(f'{where}: {rendered!r}')
            elif kind in ('env_file', 'path') and not (
                rendered.get('path') if isinstance(rendered, dict) else rendered
            ):
                problems.append(f'{where}: empty')
            elif kind == 'volumes' and isinstance(raw, str):
                parts = rendered.split(':')
                if len(parts) < 2 or not parts[0] or not parts[1].startswith('/'):
                    problems.append(f'{where}: {rendered!r}')
    return problems


def test_each_build_verb_loads_the_files_its_code_names():
    """Non-vacuity: the -f list is the code's own set, every file exists, and each set holds the base file."""
    assert _files(_step('build')) == list(stack.DEV_COMPOSE_FILES)
    assert _files(_step('test_build')) == list(stack.TEST_BUILD_COMPOSE_FILES)
    for verb in VERBS:
        files = _files(_step(verb))
        assert files and files[0] == 'docker-compose.yaml', files
        assert all((REPO_ROOT / name).is_file() for name in files), files


@pytest.mark.parametrize('verb', VERBS)
def test_every_no_default_reference_in_a_validated_field_is_set_and_non_empty(verb: str):
    """THE PIN for tj-iee6sn.30: a validated field compose reads empty fails the verb before it builds."""
    step = _step(verb)
    files = _files(step)
    referenced = {
        variable
        for name in files
        for _, _, raw in _validated_fields(_load(name))
        for text in _strings(raw)
        for variable in _must_be_set(text)
    }
    # Non-vacuity: the field that failed live is among the ones this test reads.
    assert 'DATABASE_NAME' in referenced, referenced
    assert _unset_in_validated_fields(files, _env(step)) == []


@pytest.mark.parametrize('verb', VERBS)
def test_every_validated_field_renders_to_a_value_compose_accepts(verb: str):
    """Set is not enough: container_name matches compose's pattern, every port is numeric and in range."""
    step = _step(verb)
    assert _invalid_values(_files(step), _env(step)) == []


@pytest.mark.parametrize('verb', VERBS)
def test_every_no_default_reference_anywhere_in_the_files_is_set(verb: str):
    """The warnings too: an unset plain reference anywhere (environment included) warns on every build.

    3e5c0c8 sets POSTGRES_USER and POSTGRES_DB_NAME for exactly this reason; this keeps it so.
    """
    step = _step(verb)
    env = _env(step)
    unset = sorted(
        {
            (name, variable)
            for name in _files(step)
            for text in _strings(_load(name))
            for variable in _must_be_set(text)
            if not env.get(variable)
        }
    )
    assert unset == []


@pytest.mark.parametrize('verb', VERBS)
def test_the_pre_fix_env_reproduces_the_live_refusal(verb: str):
    """Regression oracle: without 3e5c0c8's keys the checks name the field compose refused at the sitting."""
    step = _step(verb)
    env = {name: value for name, value in _env(step).items() if name not in _PRE_FIX_MISSING}
    missing = _unset_in_validated_fields(_files(step), env)
    assert ('docker-compose.yaml', 'services.postgres.container_name', 'DATABASE_NAME') in missing, missing
    assert "docker-compose.yaml: services.postgres.container_name: ''" in _invalid_values(_files(step), env)
    if verb == 'build':
        # Only the dev set holds the override, whose published host ports these are.
        assert {variable for _, _, variable in missing} == {
            'DATABASE_NAME',
            'DATABASE_PORT',
            'DATA_STORE_PORT',
            'DATA_INGEST_PORT',
        }, missing
    else:
        assert {variable for _, _, variable in missing} == {'DATABASE_NAME'}, missing


def test_the_reference_reader_tells_plain_from_defaulted():
    """Non-vacuity for _must_be_set and _port_problems."""
    assert _must_be_set('${A} $B ${C:?m} ${D?m}') == ['A', 'B', 'C', 'D']
    assert _must_be_set('$${A} ${B:-x} ${C-x} $$B') == []
    assert _port_problems('127.0.0.1:${P}:80', '127.0.0.1::80') == ["port ''"]
    assert _port_problems('127.0.0.1::80', '127.0.0.1::80') == []
    assert _port_problems('127.0.0.1:${P}:80', '127.0.0.1:x:80') == ["port 'x'"]
    assert _port_problems('127.0.0.1:${P}:80', '127.0.0.1:70000:80') == ["port '70000'"]
    assert _port_problems('${P}', '') == ["port ''"]
    assert _port_problems('127.0.0.1:${W:-8088}:8080', '127.0.0.1:8088:8080') == []
