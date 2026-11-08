"""build_infra pin: no committed file turns the expiry reaper's deletions on (tj-grna9p.165).

The reaper is dry-run unless REAPER_DELETES_ENABLED is `true` in any letter case, surrounding whitespace ignored
(server/data/store/app/reaper.py, deletes_enabled); anything else, unset included, is dry-run. The UNSET half -- an absent variable reads as dry-run -- is already pinned by
server/data/store/tests/test_reaper.py::test_dry_run_deletes_nothing_and_logs_only_a_count, whose `flag`
parametrisation includes None; it is not repeated here. This module pins the other half: since unset is
dry-run, PROD stays dry-run only while nothing the repository ships sets the variable. A deployment turns
deletions on in its OWN env file, which is never committed.

  1. THE CONFIG SET NEVER NAMES IT. The root compose files (docker-compose*.yaml), the Makefile, .env.default,
     the Dockerfile and entrypoint.sh, .github/workflows, .devcontainer, deploy/** and tools/** -- every tracked
     file among them that is not a test or a Markdown document -- holds no mention of the variable at all, with
     any value, including an interpolation such as ${REAPER_DELETES_ENABLED:-...}. tools/dev_datasets/plan.json
     is read with its `purpose` prose removed: two rows' purposes explain when the reaper deletes, and that is
     text shown to a person, not a setting. tools/agent_mcp/stack.py's one entry on the agent MCP's NEVER list
     (ADR tj-4rr0la addendum 32 (2)) is exempt: it names the flag only so test_env can refuse it.
  2. NOWHERE ELSE ASSIGNS IT TRUE. Every other tracked text file -- the only ones exempt are reaper.py (which
     defines the name), Markdown documents and tests -- holds no assignment of `true` to it in any spelling:
     NAME=true, NAME: true, 'NAME': 'true', ENV NAME true, a ${NAME:-true} default.

The file list is `git ls-files`, read from the working tree, so an untracked operator .env never trips it and an
edited tracked file does.
"""

import ast
import fnmatch
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

VARIABLE = 'REAPER_DELETES_ENABLED'
MENTION = re.compile(re.escape(VARIABLE))
# The name, an optional closing quote or bracket, then a separator (make's :=, ?= and +=, =, :, :-, -, a comma or
# whitespace), then an optionally quoted true. Prose such as 'unless REAPER_DELETES_ENABLED is true' has a word
# between and is not one.
ASSIGNS_TRUE = re.compile(
    re.escape(VARIABLE) + r"""['"]?\]?\s*(?::=|\?=|\+=|=|:-?|-|,|\s)\s*['"]?true\b""", re.IGNORECASE
)

DEFINER = 'server/data/store/app/reaper.py'
PLAN = 'tools/dev_datasets/plan.json'

# The configuration a deployment or a dev stack is built from (repository-relative globs; * crosses '/').
CONFIG_GLOBS = (
    'docker-compose*.yaml',
    'Makefile',
    '.env.default',
    'Dockerfile',
    'entrypoint.sh',
    '.github/workflows/*',
    '.devcontainer/*',
    'deploy/*',
    'tools/*',
)
# One file of each kind that must be in the config set, so a broken listing cannot pass vacuously.
CONFIG_ANCHORS = frozenset(
    {
        'docker-compose.yaml',
        'docker-compose.fake.yaml',
        'Makefile',
        '.env.default',
        '.github/workflows/trader_joe_testing.yml',
        '.devcontainer/compose.yml',
        'deploy/web/Caddyfile',
        'tools/dev_datasets/request.py',
        PLAN,
    }
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ['git', '-C', str(REPO_ROOT), 'ls-files', '-z'], capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 0, f'git ls-files failed: {result.stderr!r}'
    files = [name for name in result.stdout.decode('utf-8').split('\0') if name]
    assert len(files) > 500, f'git ls-files listed only {len(files)} files'
    return files


def _is_test(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        'tests' in path.parts[:-1]
        or fnmatch.fnmatchcase(path.name, 'test_*.py')
        or path.name == 'conftest.py'
        or fnmatch.fnmatchcase(path.name, '*.spec.ts')
    )


def _is_doc(name: str) -> bool:
    return PurePosixPath(name).suffix == '.md'


def _is_config(name: str) -> bool:
    if '/' not in name and fnmatch.fnmatchcase(name, 'docker-compose*.yaml'):
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in CONFIG_GLOBS[1:])


def _text(name: str) -> str | None:
    """The file as the scan reads it: None for a file that is gone or not text; plan.json without its prose."""
    path = Path(REPO_ROOT, name)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None
    if name == PLAN:
        rows = json.loads(text)
        for row in rows:
            row.pop('purpose', None)
        return json.dumps(rows)
    return text


def _config_files() -> list[str]:
    return [name for name in _tracked_files() if _is_config(name) and not _is_test(name) and not _is_doc(name)]


# THE ONE REFUSAL-LIST EXEMPTION (tj-7zjov1.270). ADR tj-4rr0la addendum 32 (2) puts REAPER_DELETES_ENABLED on the
# agent MCP's NEVER list: test_env refuses it by name. That mention can never turn deletions on -- it is the reason
# test_env cannot -- so it is not what this module guards against (a committed file SETTING the flag). Spelling it
# without the literal instead would hide the refusal from exactly this kind of scan. The exemption is narrow: the
# line must be the bare string entry, inside the ENV_NEVER_NAMES assignment of that one file, found by AST. STALE
# RULE: test_the_never_list_exemption_is_live_and_is_the_only_mention goes red when the entry moves or goes, and
# then this exemption is deleted with it, not widened.
NEVER_LIST_FILE = 'tools/agent_mcp/stack.py'
NEVER_LIST_NAME = 'ENV_NEVER_NAMES'
NEVER_LIST_ENTRY = f"'{VARIABLE}',"


def _never_list_lines() -> set[int]:
    """The line numbers of NEVER_LIST_FILE's ENV_NEVER_NAMES assignment whose whole text is the refusal entry."""
    text = Path(REPO_ROOT, NEVER_LIST_FILE).read_text(encoding='utf-8')
    lines = text.splitlines()
    spans = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Assign)
        and [getattr(target, 'id', None) for target in node.targets] == [NEVER_LIST_NAME]
    ]
    return {
        number
        for start, end in spans
        for number in range(start, end + 1)
        if lines[number - 1].strip() == NEVER_LIST_ENTRY
    }


def test_the_config_set_is_listed_and_holds_every_anchor():
    assert set(_config_files()) >= CONFIG_ANCHORS


def test_the_never_list_exemption_is_live_and_is_the_only_mention():
    """The exemption names a line that exists, holds the bare entry, and is the file's only mention of the flag."""
    exempt = _never_list_lines()
    assert len(exempt) == 1, f'{NEVER_LIST_FILE}: no single {NEVER_LIST_ENTRY} entry in {NEVER_LIST_NAME}: {exempt}'
    text = Path(REPO_ROOT, NEVER_LIST_FILE).read_text(encoding='utf-8')
    mentions = {number for number, line in enumerate(text.splitlines(), 1) if MENTION.search(line)}
    assert mentions == exempt, f'{NEVER_LIST_FILE} names {VARIABLE} outside its refusal entry: {sorted(mentions)}'


def test_the_never_list_entry_is_a_refusal_not_an_allowance():
    """The exempted entry is what test_env refuses; the flag is on no service's allow-list."""
    from tools.agent_mcp import stack

    assert VARIABLE in stack.ENV_NEVER_NAMES and stack.env_name_never_allowed(VARIABLE)
    assert not [service for service, names in stack.AGENT_ENV_ALLOW.items() if VARIABLE in names]


def test_no_committed_config_file_names_the_reaper_deletes_flag():
    offenders = {}
    exempt = _never_list_lines()
    for name in _config_files():
        text = _text(name)
        if text is None:
            continue
        lines = [
            number
            for number, line in enumerate(text.splitlines(), 1)
            if MENTION.search(line) and not (name == NEVER_LIST_FILE and number in exempt)
        ]
        if lines:
            offenders[name] = lines
    assert offenders == {}, (
        f'{VARIABLE} is set by a committed file; PROD must stay dry-run, so a deployment sets it only in its own '
        f'env file: {offenders}'
    )


def test_no_tracked_file_but_the_definer_docs_and_tests_assigns_it_true():
    offenders = {}
    for name in _tracked_files():
        if name == DEFINER or _is_doc(name) or _is_test(name):
            continue
        text = _text(name)
        if text is None:
            continue
        lines = [number for number, line in enumerate(text.splitlines(), 1) if ASSIGNS_TRUE.search(line)]
        if lines:
            offenders[name] = lines
    assert offenders == {}, f'{VARIABLE} is assigned true by a committed file: {offenders}'


def test_the_plan_exemption_is_only_its_purpose_prose():
    # The exemption is live (the raw plan does name the flag) and narrow (only inside a purpose string).
    raw = Path(REPO_ROOT, PLAN).read_text(encoding='utf-8')
    assert MENTION.search(raw)
    assert not MENTION.search(_text(PLAN) or '')
    for row in json.loads(raw):
        for key, value in row.items():
            if key != 'purpose':
                assert VARIABLE not in json.dumps(value), (row['id'], key)


@pytest.mark.parametrize(
    'line',
    [
        'REAPER_DELETES_ENABLED=true',
        'REAPER_DELETES_ENABLED: "true"',
        "REAPER_DELETES_ENABLED: 'TRUE'",
        "      'REAPER_DELETES_ENABLED': 'true',",
        'ENV REAPER_DELETES_ENABLED true',
        '${REAPER_DELETES_ENABLED:-true}',
        "os.environ['REAPER_DELETES_ENABLED'] = 'True'",
        '-e REAPER_DELETES_ENABLED=true',
        'export REAPER_DELETES_ENABLED := true',
        'REAPER_DELETES_ENABLED ?= true',
        'REAPER_DELETES_ENABLED+=true',
    ],
)
def test_the_assignment_pattern_catches_each_spelling(line):
    assert ASSIGNS_TRUE.search(line)


@pytest.mark.parametrize(
    'line',
    [
        '# dry-run unless REAPER_DELETES_ENABLED is true. One worker only.',
        'REAPER_DELETES_ENABLED=false',
        'REAPER_DELETES_ENABLED: "truee"',
        'REAPER_DELETES_ENABLED to be exactly `true`',
    ],
)
def test_the_assignment_pattern_leaves_prose_and_other_values_alone(line):
    assert not ASSIGNS_TRUE.search(line)


def test_the_definer_still_spells_the_name_this_scan_looks_for():
    # A rename of the flag in reaper.py would leave this scan looking for a name nothing reads.
    assert f"DELETES_ENABLED_ENV: Final = '{VARIABLE}'" in Path(REPO_ROOT, DEFINER).read_text(encoding='utf-8')
