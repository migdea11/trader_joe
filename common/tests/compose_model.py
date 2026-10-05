"""A small, faithful-enough model of compose interpolation and file merging, for the build_infra pins.

No test functions here, so a test module imports exactly what it uses (the agent-stack pins of
tj-c4mosr.5 and the network re-pins). The invariant tests parse compose files with yaml.safe_load and
never start a daemon, so the two things `docker compose config` would otherwise do are done here:

INTERPOLATION (compose-spec interpolation.md): ${VAR}, $VAR, ${VAR:-default}, ${VAR-default},
${VAR:?err}, ${VAR?err}, ${VAR:+alt}, ${VAR+alt}, defaults nested to any depth, and $$ for a literal
$. ':?' refuses an unset OR EMPTY value, '?' an unset one only -- the difference the overlay's guards
depend on (a generated 'ROOT_ENV_FILE=' line must stop compose).

MERGE (compose-spec merge.md), for the keys these pins read: mappings merge key by key (build,
labels, environment, top-level networks); `volumes` merge by container TARGET, so a later file's entry
with the same target REPLACES the earlier one; `networks` of a service merge by key; `env_file`,
`ports`, `security_opt`, `cap_add` and `cap_drop` append; any other scalar is overridden. Not modelled,
because no file here uses them: extends, include, profiles, !reset and !override. They fail LOUDLY,
never pass through: merge() raises NotModelled on a top-level `include` or a service-level `extends` or
`profiles` (each changes which services or keys compose renders, so copying it through would make the
model disagree with compose while every pin read the model), and yaml.safe_load already refuses the
!reset and !override tags.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from common.tests.roots import REPO_ROOT


# THE TRUE REPOSITORY ROOT (tj-iontkq.2). Every file below -- all five compose files and the two
# .devcontainer files -- stays at the top of the repository, so this is REPO_ROOT and never
# SERVER_ROOT.
BASE_FILE = REPO_ROOT / 'docker-compose.yaml'
TEST_CLIENT_FILE = REPO_ROOT / 'docker-compose.test-client.yaml'
AGENT_STACK_FILE = REPO_ROOT / 'docker-compose.agent-stack.yaml'
AGENT_MCP_FILE = REPO_ROOT / 'docker-compose.agent-mcp.yaml'
FAKE_FILE = REPO_ROOT / 'docker-compose.fake.yaml'
DEVCONTAINER_COMPOSE = REPO_ROOT / '.devcontainer' / 'compose.yml'
DEVCONTAINER_JSON = REPO_ROOT / '.devcontainer' / 'devcontainer.json'

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_OPERATORS = (':-', ':?', ':+', '-', '?', '+')


class InterpolationRefused(Exception):
    """A ${VAR:?...} or ${VAR?...} guard refused: what `docker compose config` would stop on."""

    def __init__(self, variable: str, message: str):
        super().__init__(f'{variable}: {message}')
        self.variable = variable


def load(path: Path) -> dict:
    with path.open(encoding='utf-8') as handle:
        return yaml.safe_load(handle) or {}


def _closing_brace(text: str, start: int) -> int:
    """The index of the '}' closing the '${' that opens at START, nested ${...} skipped."""
    depth, index = 0, start
    while index < len(text):
        if text.startswith('${', index):
            depth += 1
            index += 2
            continue
        if text[index] == '}':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError(f'unclosed ${{ in {text!r}')


def interpolate(text: str, env: Mapping[str, str]) -> str:
    """Interpolate one compose string under ENV, as compose does; a guard that refuses raises."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char != '$':
            out.append(char)
            index += 1
            continue
        if text.startswith('$$', index):
            out.append('$')
            index += 2
            continue
        if text.startswith('${', index):
            close = _closing_brace(text, index)
            out.append(_expression(text[index + 2 : close], env))
            index = close + 1
            continue
        match = _NAME.match(text, index + 1)
        if match:
            out.append(env.get(match.group(0), ''))
            index = match.end()
            continue
        out.append(char)
        index += 1
    return ''.join(out)


def _expression(inner: str, env: Mapping[str, str]) -> str:
    match = _NAME.match(inner)
    if not match:
        raise ValueError(f'not a variable expression: ${{{inner}}}')
    name, rest = match.group(0), inner[match.end() :]
    if not rest:
        return env.get(name, '')
    operator = next((op for op in _OPERATORS if rest.startswith(op)), None)
    if operator is None:
        raise ValueError(f'unknown operator in ${{{inner}}}')
    argument = rest[len(operator) :]
    is_set, value = name in env, env.get(name, '')
    if operator == ':-':
        return value if value else interpolate(argument, env)
    if operator == '-':
        return value if is_set else interpolate(argument, env)
    if operator == ':?':
        if not value:
            raise InterpolationRefused(name, argument)
        return value
    if operator == '?':
        if not is_set:
            raise InterpolationRefused(name, argument)
        return value
    if operator == ':+':
        return interpolate(argument, env) if value else ''
    return interpolate(argument, env) if is_set else ''


def interpolate_tree(node: Any, env: Mapping[str, str]) -> Any:
    """Interpolate every string value of a loaded compose document (keys are left as written)."""
    if isinstance(node, str):
        return interpolate(node, env)
    if isinstance(node, list):
        return [interpolate_tree(item, env) for item in node]
    if isinstance(node, dict):
        return {key: interpolate_tree(value, env) for key, value in node.items()}
    return node


def split_short_volume(entry: str) -> tuple[str, str, str]:
    """(source, target, mode) of a short-form volume, splitting on colons outside ${...}."""
    parts, depth, current = [], 0, []
    index = 0
    while index < len(entry):
        if entry.startswith('${', index):
            depth += 1
            current.append('${')
            index += 2
            continue
        char = entry[index]
        if char == '}' and depth:
            depth -= 1
        if char == ':' and not depth:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append(''.join(current))
    if len(parts) == 1:
        return '', parts[0], ''
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ''


def volume(entry: object) -> dict[str, Any]:
    """A volume entry as {'source', 'target', 'read_only', 'type'}, short or long form."""
    if isinstance(entry, dict):
        return {
            'type': entry.get('type', 'volume'),
            'source': str(entry.get('source', '')),
            'target': str(entry.get('target', '')),
            'read_only': entry.get('read_only') is True,
        }
    source, target, mode = split_short_volume(str(entry))
    is_bind = source.startswith(('.', '/', '~', '$'))
    return {
        'type': 'bind' if is_bind else 'volume',
        'source': source,
        'target': target,
        'read_only': 'ro' in mode.split(','),
    }


def _as_mapping(node: object) -> dict:
    """A labels or environment block, list form (KEY=VALUE) or mapping form, as a mapping."""
    if node is None:
        return {}
    if isinstance(node, dict):
        return dict(node)
    mapping = {}
    for item in node:
        key, _, value = str(item).partition('=')
        mapping[key] = value
    return mapping


def _merge_networks(base: object, over: object) -> dict:
    merged = dict.fromkeys(base) if isinstance(base, list) else dict(base or {})
    for name, config in (dict.fromkeys(over) if isinstance(over, list) else dict(over or {})).items():
        if isinstance(merged.get(name), dict) and isinstance(config, dict):
            merged[name] = {**merged[name], **config}
        else:
            merged[name] = config if config is not None else merged.get(name)
    return merged


_APPENDED = ('env_file', 'ports', 'security_opt', 'cap_add', 'cap_drop')


def merge_service(base: dict, over: dict) -> dict:
    merged = dict(base)
    for key, value in over.items():
        if key == 'volumes':
            by_target = {volume(entry)['target']: entry for entry in merged.get('volumes') or []}
            for entry in value or []:
                by_target[volume(entry)['target']] = entry
            merged['volumes'] = list(by_target.values())
        elif key == 'networks':
            merged['networks'] = _merge_networks(merged.get('networks'), value)
        elif key in ('labels', 'environment'):
            merged[key] = {**_as_mapping(merged.get(key)), **_as_mapping(value)}
        elif key == 'build':
            old = merged.get('build')
            old = {'context': old} if isinstance(old, str) else dict(old or {})
            new = {'context': value} if isinstance(value, str) else dict(value or {})
            merged['build'] = {**old, **new}
        elif key in _APPENDED:
            old = merged.get(key) or []
            merged[key] = ([old] if isinstance(old, str) else list(old)) + (
                [value] if isinstance(value, str) else list(value or [])
            )
        else:
            merged[key] = value
    return merged


class NotModelled(ValueError):
    """A compose key the model does not implement: refused, so the model never silently disagrees with compose."""


_REFUSED_TOP_LEVEL = ('include',)
_REFUSED_SERVICE = ('extends', 'profiles')


def _refuse_unmodelled(document: dict) -> None:
    for key in _REFUSED_TOP_LEVEL:
        if key in document:
            raise NotModelled(f'top-level {key!r} is not modelled by compose_model.merge')
    for name, service in (document.get('services') or {}).items():
        for key in _REFUSED_SERVICE:
            if key in (service or {}):
                raise NotModelled(f'service {name!r} uses {key!r}, which compose_model.merge does not model')


def merge(documents: list[dict]) -> dict:
    """Merge compose documents in order, as `docker compose -f a -f b ...` does for the keys modelled.

    Raises NotModelled on include, extends or profiles (see the module docstring).
    """
    result: dict[str, Any] = {'services': {}, 'networks': {}}
    for document in documents:
        _refuse_unmodelled(document)
        for name, service in (document.get('services') or {}).items():
            result['services'][name] = merge_service(result['services'].get(name, {}), service or {})
        for name, network in (document.get('networks') or {}).items():
            result['networks'][name] = {**(result['networks'].get(name) or {}), **(network or {})}
        for key, value in document.items():
            if key not in ('services', 'networks'):
                result[key] = value
    return result


def prod_model() -> dict:
    return merge([load(BASE_FILE)])


def client_model() -> dict:
    return merge([load(BASE_FILE), load(TEST_CLIENT_FILE)])


def agent_stack_model() -> dict:
    """The merged agent-stack model, in AGENT_STACK_COMPOSE's order.

    Base, test client, the agent-stack overlay, then the fake-mode overlay LAST (ADR tj-4rr0la
    addendum 3 (3); tj-vhboky.61) -- so every property pinned on this model holds for the stack the
    MCP really starts, fake data_ingest included.
    """
    return merge([load(BASE_FILE), load(TEST_CLIENT_FILE), load(AGENT_STACK_FILE), load(FAKE_FILE)])


def system_model() -> dict:
    """The merged fake-mode stack make system-launch starts: SYSTEM_COMPOSE, base then the fake overlay."""
    return merge([load(BASE_FILE), load(FAKE_FILE)])


def service_networks(service: dict) -> list[str]:
    networks = service.get('networks')
    if networks is None:
        return ['default']
    return list(networks)
