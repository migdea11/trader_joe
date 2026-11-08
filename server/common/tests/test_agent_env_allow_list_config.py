"""build_infra pin: no name test_env may set is a secret or a value the committed config pins (tj-7zjov1.270).

Design: ADR tj-4rr0la addendum 32 (2)-(3). The agent MCP's test_env writes allow-listed environment overrides into
a generated overlay that is LAST in the test file set, so an override silently outranks the same name anywhere
earlier. Two things must therefore stay true of the committed configuration:

  1. NO ALLOWED NAME IS IN A SECRET-BEARING POSITION. No .env.default line that assigns a name containing KEY,
     SECRET, PASS, TOKEN or CREDENTIAL (commented-out examples included) mentions an allowed name, and no allowed
     name contains one of those words (the structural half; stack.py also refuses at import).
  2. NO ALLOWED NAME IS SET LITERALLY under environment: in docker-compose.yaml or docker-compose.fake.yaml, the
     two files the overlay follows. Such a literal would be a value the test mode pins on purpose, and an
     override would fight it with nothing saying so.

The allow-list is read from tools/agent_mcp/stack.py on purpose: this is a cross-check of whatever the list holds
against the files, so it must follow the list as it grows. The literal list itself is pinned in
tools/agent_mcp/tests/test_test_service_verbs.py.
"""

import re

import pytest

from common.tests import compose_model
from common.tests.roots import REPO_ROOT, SERVER_ROOT
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

SECRET_WORDS = ('KEY', 'SECRET', 'PASS', 'TOKEN', 'CREDENTIAL')
ENV_DEFAULTS = (
    REPO_ROOT / '.env.default',
    SERVER_ROOT / 'data' / 'store' / '.env.default',
    SERVER_ROOT / 'data' / 'ingest' / '.env.default',
)
OVERLAID_COMPOSE_FILES = (REPO_ROOT / 'docker-compose.yaml', REPO_ROOT / 'docker-compose.fake.yaml')
# An assignment, possibly commented out or exported: `NAME=`, `# NAME="..."`, `export NAME=`.
ASSIGNMENT = re.compile(r'^\s*(?:#\s*)?(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')


def _allowed_names() -> set[str]:
    names = {name for rules in stack.AGENT_ENV_ALLOW.values() for name in rules}
    assert len(names) >= 7, names
    return names


def _secret_lines(text: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = ASSIGNMENT.match(line)
        if match and any(word in match.group(1).upper() for word in SECRET_WORDS):
            found.append((number, line))
    return found


def _environment_names(service: dict) -> set[str]:
    environment = service.get('environment') or {}
    if isinstance(environment, dict):
        return set(environment)
    return {str(entry).split('=', 1)[0] for entry in environment}


def test_no_allowed_name_contains_a_secret_word():
    assert not [name for name in _allowed_names() if any(word in name.upper() for word in SECRET_WORDS)]


@pytest.mark.parametrize('path', ENV_DEFAULTS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_no_secret_bearing_env_default_line_mentions_an_allowed_name(path):
    text = path.read_text(encoding='utf-8')
    lines = _secret_lines(text)
    offenders = [(number, name) for number, line in lines for name in _allowed_names() if name in line]
    assert offenders == [], f'{path}: an allow-listed name sits in a secret-bearing line: {offenders}'


def test_the_secret_line_scan_sees_the_committed_secrets():
    """Non-vacuous: the scan finds the secret-bearing lines the defaults are known to hold."""
    names = {
        ASSIGNMENT.match(line).group(1)
        for path in ENV_DEFAULTS
        for _, line in _secret_lines(path.read_text(encoding='utf-8'))
    }
    assert {'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET', 'ALPACA_API_KEY'} <= names, names


@pytest.mark.parametrize('path', OVERLAID_COMPOSE_FILES, ids=lambda path: path.name)
def test_no_allowed_name_is_set_literally_under_environment(path):
    services = compose_model.load(path).get('services') or {}
    assert services, f'{path} defines no service, so this pin would guard nothing'
    offenders = {
        name: sorted(_environment_names(service) & _allowed_names())
        for name, service in services.items()
        if _environment_names(service) & _allowed_names()
    }
    assert offenders == {}, f'{path.name} pins a value test_env can override: {offenders}'


@pytest.mark.parametrize(
    'environment', [{'ALPACA_MAX_ATTEMPTS': '3'}, ['PANEL_MANAGER_ENABLED=true'], ['ALPACA_STALL_TIMEOUT_S']]
)
def test_the_environment_reader_sees_both_compose_spellings(environment):
    assert _environment_names({'environment': environment}) & _allowed_names()
