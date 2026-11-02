"""build_infra pins: no task-store address, host or password reaches a tracked file (tj-e1k9b8.5, plan C3).

The task store (bd) runs against a Dolt sql-server on the home server. Its connection settings --
host, port, user, password -- are per machine and live only in the git-ignored per-project settings
file inside .beads/ (user ruling A, 2026-11-01). The tracked config carries the mode and nothing that
locates or opens the server. This repository is public, so one careless `bd dolt set host` or
`bd init --server-host`, both of which write the TRACKED .beads/metadata.json (risk K11), publishes
the address. These tests are the guard the plan names for that risk, next to the working-directory
rule.

WHAT IS READ. Only files git tracks: every path comes from `git ls-files`, so an untracked or ignored
file -- above all the per-machine settings file -- is never opened. The ignore check asks git about
the path; it does not read the file.

WHAT IS HELD.
  1. No IPv4 address in any tracked file under .beads/ or .devcontainer/, or in .claude/settings.json
     (whose env block bd reads, and where BEADS_DIR already lives). No loopback allowance: none of
     these files carries any IPv4 literal today, placeholder or otherwise, so any one is new.
  2. No task-store connection setting is assigned there: no BEADS_DOLT_* / DOLT_REMOTE_* /
     dolt_server_* key with a value, no host:3306 (the Dolt sql-server port), no user:password@ in a
     URL, and no *PASS* / *PASSWORD* key with a literal value. A ${VAR:-...} interpolation is not a
     literal: .devcontainer/compose.yml's two dev credentials are interpolated with a named sentinel
     default and are not the store's.
  3. .beads/metadata.json has none of dolt_server_host, dolt_server_port, dolt_server_user (nor any
     other dolt_server_* key), and says server mode on database tj, so a clone with no settings file
     fails closed instead of opening a local store.
  4. The per-project settings file inside .beads/ is git-ignored and not tracked.

NO TOOLCHAIN PIN CHECK (architect gate, 2026-11-01). A "bd and dolt pins equal main's" test was here
and was removed: it is not a leak check, it reds every deliberate bump PR until merge, and its answer
depends on how fresh the local main ref is.

NOTHING HERE NAMES A REAL ADDRESS OR HOST. The patterns match shapes and key names only.
"""

import ipaddress
import json
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest


pytestmark = pytest.mark.build_infra

SCANNED_TREES = ('.beads', '.devcontainer')
SCANNED_FILES = ('.claude/settings.json',)
METADATA = '.beads/metadata.json'
# The per-project connection-settings file bd 1.3.0 reads (listed in .beads/.gitignore, GH#2520).
# Spelled only here: a project hook refuses any agent shell command that names it.
STORE_SETTINGS_FILE = '.beads/.env'
FORBIDDEN_METADATA_KEYS = ('dolt_server_host', 'dolt_server_port', 'dolt_server_user')

DOTTED_QUAD = re.compile(r'(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])')
# Key names that configure the store connection, followed by an assignment with a value.
STORE_KEY_ASSIGNED = re.compile(
    r"""(?ix) (?<![{\w-]) (beads_dolt_\w+ | dolt_remote_\w+ | dolt_server_\w+ | server[-_]host)
        ["']? \s* [:=] \s* (?!\s*$) \S"""
)
SQL_PORT_ENDPOINT = re.compile(r'[\w.-]+:3306\b')
URL_WITH_CREDENTIALS = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@', re.IGNORECASE)
# A *PASS* or *PASSWORD* key given a literal value; `${...}` interpolation and empty values are not literals.
# The lookbehind keeps the variable NAME inside `${NAME:-default}` from reading as a key assigned `-default`.
PASSWORD_LITERAL = re.compile(
    r"""(?ix) (?<![{\w-]) ([a-z0-9_.-]*pass(?:word)?[a-z0-9_]*) ["']? \s* [:=] \s* ["']? (?!\$\{) (?!["']?\s*$) \S"""
)


def _repo_root() -> Path:
    """The checkout root, from git: the test sits at a fixed depth today, but git, not a parents[N], says so."""
    result = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f'not inside a git checkout: {result.stderr}'
    return Path(result.stdout.strip())


REPO_ROOT = _repo_root()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


@cache
def scanned_files() -> tuple[str, ...]:
    """Every tracked file the address checks read, as repository-relative paths."""
    result = _git('ls-files', '-z', '--', *SCANNED_TREES, *SCANNED_FILES)
    assert result.returncode == 0, result.stderr
    paths = tuple(sorted(path for path in result.stdout.split('\0') if path))
    # A floor, so a renamed tree or a broken ls-files cannot turn every check below into a pass over nothing.
    assert METADATA in paths, f'{METADATA} is not tracked; ls-files returned {paths}'
    assert SCANNED_FILES[0] in paths, f'{SCANNED_FILES[0]} is not tracked'
    assert any(path.startswith('.devcontainer/') for path in paths), 'no tracked file under .devcontainer/'
    return paths


def _findings(pattern: re.Pattern) -> list[str]:
    found = []
    for path in scanned_files():
        text = (REPO_ROOT / path).read_text(encoding='utf-8')
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                found.append(f'{path}:{number}')
    return found


def test_scanned_set_is_the_tracked_store_and_container_config():
    paths = scanned_files()
    assert '.beads/config.yaml' in paths
    assert STORE_SETTINGS_FILE not in paths


def test_no_ipv4_address_in_tracked_store_or_container_files():
    found = []
    for path in scanned_files():
        text = (REPO_ROOT / path).read_text(encoding='utf-8')
        for number, line in enumerate(text.splitlines(), start=1):
            for match in DOTTED_QUAD.finditer(line):
                try:
                    ipaddress.IPv4Address(match.group(1))
                except ValueError:
                    continue  # e.g. a four-part version number with an octet over 255
                found.append(f'{path}:{number}')
    # Locations only: the message must not repeat the address it found into a CI log.
    assert not found, f'IPv4 address in a tracked file (connection settings belong in {STORE_SETTINGS_FILE}): {found}'


def test_no_store_connection_key_is_assigned_in_tracked_files():
    found = _findings(STORE_KEY_ASSIGNED)
    assert not found, f'task-store connection key assigned in a tracked file: {found}'


def test_no_sql_server_endpoint_in_tracked_files():
    found = _findings(SQL_PORT_ENDPOINT)
    assert not found, f'host:3306 endpoint in a tracked file: {found}'


def test_no_credentials_in_a_url_in_tracked_files():
    found = _findings(URL_WITH_CREDENTIALS)
    assert not found, f'user:password@ URL in a tracked file: {found}'


def test_no_literal_password_value_in_tracked_files():
    found = _findings(PASSWORD_LITERAL)
    assert not found, f'password-like key with a literal value in a tracked file: {found}'


@cache
def metadata() -> dict:
    return json.loads((REPO_ROOT / METADATA).read_text(encoding='utf-8'))


def test_metadata_has_no_server_connection_keys():
    present = sorted(key for key in metadata() if key in FORBIDDEN_METADATA_KEYS or key.startswith('dolt_server_'))
    assert not present, (
        f'{METADATA} carries server connection keys {present}; `bd dolt set host` or `bd init --server-host` '
        f'wrote them. Remove them; they belong in {STORE_SETTINGS_FILE}.'
    )


def test_metadata_selects_server_mode_on_the_project_database():
    assert metadata().get('dolt_mode') == 'server'
    assert metadata().get('dolt_database') == 'tj'


def test_store_settings_file_is_ignored_and_untracked():
    # --no-index answers from the ignore rules alone, so a force-added file still reds here.
    ignored = _git('check-ignore', '-q', '--no-index', '--', STORE_SETTINGS_FILE)
    assert ignored.returncode == 0, (
        f'{STORE_SETTINGS_FILE} is not git-ignored (exit {ignored.returncode}) {ignored.stderr}'
    )
    tracked = _git('ls-files', '--', STORE_SETTINGS_FILE)
    assert tracked.returncode == 0, tracked.stderr
    assert tracked.stdout.strip() == '', f'{STORE_SETTINGS_FILE} is tracked; untrack it with git rm --cached'
