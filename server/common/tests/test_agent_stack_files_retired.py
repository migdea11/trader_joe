"""build_infra pins for B12b (tj-iee6sn.27): the agent stack's files and config are gone, for good.

Design: ADR tj-4rr0la addendum 26 (the separate agent stack is retired; modes of the one project replace it)
and addendum 27 (test mode is the base file plus docker-compose.fake.yaml, as make system-launch runs it),
with the architect's release note on tj-iee6sn.27 (12:25 UTC 2026-10-19). The MCP code half (B12a,
tj-iee6sn.21) is pinned by tools/agent_mcp/tests/test_agent_stack_retired.py; this module pins the files half.

  1. THE FAKE OVERLAY'S LOADERS, ENUMERATED (the release note's (C), REQUIRED). Every place a compose file set
     loads docker-compose.fake.yaml is read from where it is spelled and must be EXACTLY: the Makefile's
     SYSTEM_COMPOSE (and SEED_DUMP_COMPOSE, which is $(SYSTEM_COMPOSE) plus the client), used by the recipes
     of system-launch, test-build and seed-dump alone; run_migrations.sh's TEST definition; the MCP's
     test_up and test_build sets in tools/agent_mcp/stack.py (the TEST migrate one-off runs on the first);
     and CI's lines through the test pair -- the render check, Migrate Database, the Instance Secret
     Lifecycle's recreate, and the System Testing job's `make system-launch` and `make seed-dump`. Nothing
     else in the repository's tracked files names the overlay outside a comment, a docstring or a message,
     but the MCP image's bake of it (tools/agent_mcp/Dockerfile and its ignore file). A new loader anywhere
     -- a dev target, the DEV set, a script -- reds, and so does a listed one that goes missing.
  2. THE RETIRED NAMES ARE GONE. No tracked file but README.md (the scribe's, tj-iee6sn.12) names the
     agent stack's project, its overlay file or the Makefile's variables of that prefix, except the lines
     listed in ALLOWED_RETIRED_LINES: the constants of the test files that assert the names' ABSENCE (the
     MCP server label, the last production line, was renamed by tj-iee6sn.26). Exact equality both ways: a
     stale exception reds too. The MCP server KEY agent-stack in
     .mcp.json (and so the mcp__agent-stack__ tool prefix) is not retired and must not trip it.
  3. The default AGENT_MCP_STACK_DIR (trader_joe_agent_mcp) is pinned in test_agent_mcp_make.py, run for real.
  4. CI. The Check Compose Renders step renders exactly the surviving sets, spelled out here; the retired one
     is not among them. test_ci_invariants.py's test_image_build_renders_every_compose_set_quietly holds the
     same step equal to the Makefile's sets; this holds it to a literal list as well, so a set dropped from
     both the Makefile and the workflow still reds.

Every retired name in this module is spelled in pieces, so the module holds none of them itself and needs no
exception for its own lines.
"""

import ast
import json
import re
import subprocess
from collections import Counter
from pathlib import PurePosixPath

import pytest

from common.tests.compose_model import FAKE_FILE
from common.tests.test_ci_invariants import (
    MAKEFILE,
    MIGRATIONS_SCRIPT,
    RENDER_STEP,
    REPO_ROOT,
    TESTING_WORKFLOW,
    _compose_calls,
    _compose_projects,
    _load_yaml,
    _run_lines,
    _subprocess_env,
    _workflow_files,
)
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

FAKE = FAKE_FILE.name
BASE = 'docker-compose.yaml'
CLIENT = 'docker-compose.test-client.yaml'
MCP_TEST_OVERLAY = 'docker-compose.agent-test.yaml'
TEST_PAIR = (BASE, FAKE)

# The retired names, in pieces (see the module docstring).
RETIRED_PROJECT = 'trader_joe_agent' + '_stack'
RETIRED_OVERLAY = 'docker-compose.agent' + '-stack.yaml'
RETIRED_PREFIX = 'AGENT' + '_STACK_'
RETIRED = re.compile('|'.join(re.escape(name) for name in (RETIRED_PROJECT, RETIRED_OVERLAY, RETIRED_PREFIX)))

# --- 1. the loaders of the fake overlay -----------------------------------------------------------------

# Every place a compose file set loads the overlay, with the files it loads in order. Spelled out, not read:
# the oracle must not be the files under test.
EXPECTED_FAKE_SETS = Counter(
    {
        ('Makefile', 'SYSTEM_COMPOSE', TEST_PAIR): 1,
        ('Makefile', 'SEED_DUMP_COMPOSE', (*TEST_PAIR, CLIENT)): 1,
        ('run_migrations.sh', 'TEST', TEST_PAIR): 1,
        ('stack.py', 'TEST_COMPOSE_FILES', TEST_PAIR): 1,
        ('stack.py', 'TEST_BUILD_COMPOSE_FILES', (*TEST_PAIR, MCP_TEST_OVERLAY)): 1,
        ('trader_joe_testing.yml', 'Image Build / Check Compose Renders: config', TEST_PAIR): 1,
        ('trader_joe_testing.yml', 'System Testing / Migrate Database: run', TEST_PAIR): 2,
        ('trader_joe_testing.yml', 'System Testing / Instance Secret Lifecycle: up', TEST_PAIR): 1,
    }
)
# The Makefile targets whose recipes run a fake-loading set, and the CI steps that run those targets.
EXPECTED_FAKE_TARGETS = frozenset({'system-launch', 'test-build', 'seed-dump'})
EXPECTED_CI_FAKE_TARGETS = Counter(
    {
        ('trader_joe_testing.yml', 'System Testing / Start System', 'system-launch'): 1,
        ('trader_joe_testing.yml', 'System Testing / Seed Dump', 'seed-dump'): 1,
    }
)
# Every tracked file that names the overlay outside a comment, a docstring or a message: the five places
# above, and the MCP image's bake of the file (its COPY and its ignore file's allow line), which loads no set.
EXPECTED_FAKE_NAMING_FILES = frozenset(
    {
        'Makefile',
        'server/data/store/run_migrations.sh',
        'tools/agent_mcp/stack.py',
        '.github/workflows/trader_joe_testing.yml',
        'tools/agent_mcp/Dockerfile',
        'tools/agent_mcp/Dockerfile.dockerignore',
    }
)
# The module-level names in the MCP's code whose value is exactly the overlay's file name: a set entry, and
# the mode classifier's constant (a label read, not a set loaded).
EXPECTED_FAKE_CONSTANTS = frozenset({'TEST_COMPOSE_FILES', 'TEST_FAKE_FILE'})

_VARIABLE_MARK = re.compile(r'^<<<([A-Za-z0-9_.]+)>>> ', re.MULTILINE)
_RULE = re.compile(r'^([A-Za-z0-9_%/][A-Za-z0-9_.%/-]*(?:\s+[A-Za-z0-9_.%/-]+)*)\s*:(?!=)(.*)$')


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ['git', '-C', str(REPO_ROOT), 'ls-files', '-z'], capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 0, f'git ls-files failed: {result.stderr!r}'
    files = [name for name in result.stdout.decode('utf-8').split('\0') if name]
    assert len(files) > 500, f'git ls-files listed only {len(files)} files'
    return files


def _makefile_variables() -> dict[str, str]:
    """Every variable the Makefile itself defines, as make expands it once the whole file is parsed.

    One make run prints them all: $(.VARIABLES) filtered to origin 'file' (or 'override'), so a variable
    defined under any spelling -- :=, =, ?=, +=, define -- is read, never a list kept here.
    """
    rule = (
        'print-every-variable: ; @: $(foreach v,$(sort $(.VARIABLES)),'
        '$(if $(filter file override,$(origin $(v))),$(info <<<$(v)>>> $($(v)))))'
    )
    command = ['make', '-s', '--no-print-directory', '-C', str(REPO_ROOT), '-f', str(MAKEFILE), '--eval', rule]
    result = subprocess.run(
        [*command, 'print-every-variable'],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f'make could not print its variables: {result.stderr}'
    marks = list(_VARIABLE_MARK.finditer(result.stdout))
    values = {
        mark.group(1): result.stdout[mark.end() : marks[index + 1].start() if index + 1 < len(marks) else None]
        for index, mark in enumerate(marks)
    }
    assert {'SYSTEM_COMPOSE', 'DEV_COMPOSE', 'PROD_COMPOSE'} <= set(values), sorted(values)
    return values


def _set_names(files: list[str]) -> tuple[str, ...]:
    return tuple(PurePosixPath(file).name for file in files)


def _makefile_fake_sets() -> tuple[list[tuple[str, str, tuple[str, ...]]], set[str]]:
    sites, loaders = [], set()
    for name, value in _makefile_variables().items():
        if FAKE not in value:
            continue
        loaders.add(name)
        calls = _compose_calls(value)
        assert len(calls) == 1 and not calls[0][1], f'{name} names the overlay but is not one compose file set: {value}'
        sites.append(('Makefile', name, _set_names(calls[0][0])))
    return sites, loaders


def _makefile_rules() -> list[tuple[list[str], list[str], list[str]]]:
    """(targets, prerequisites, recipe lines) of every Makefile rule, continuations folded."""
    rules: list[tuple[list[str], list[str], list[str]]] = []
    for line in MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines():
        if line.startswith('\t'):
            if rules:
                rules[-1][2].append(line.strip())
            continue
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = _RULE.match(line)
        if match is None or line.startswith('.'):
            rules.append(([], [], []))
            continue
        prerequisites = match.group(2).split('##')[0].split('|')[0]
        if re.match(r'^\s*(?:export\s+)?\w+\s*[:?+]?=', prerequisites):
            prerequisites = ''  # a target-specific variable, not prerequisites
        rules.append((match.group(1).split(), prerequisites.split(), []))
    return rules


def _migrations_fake_sets() -> list[tuple[str, str, tuple[str, ...]]]:
    """The TEST definition's set; every other line naming the overlay is a case pattern or a message."""
    lines = _run_lines(MIGRATIONS_SCRIPT.read_text(encoding='utf-8'))
    sites, unclassified = [], []
    for index, line in enumerate(lines):
        if FAKE not in line:
            continue
        if line.startswith('COMPOSE=(') and line.endswith(')'):
            calls = _compose_calls(line[len('COMPOSE=(') : -1])
            assert len(calls) == 1 and not calls[0][1], line
            arm = lines[index - 1] if index else ''
            sites.append(('run_migrations.sh', arm.rstrip(')'), _set_names(calls[0][0])))
        elif re.match(rf'^{re.escape(FAKE)}\)\s', line) or line.startswith('echo '):
            continue
        else:
            unclassified.append(line)
    assert not unclassified, f'{MIGRATIONS_SCRIPT.name} names the overlay outside its TEST definition: {unclassified}'
    return sites


def _mcp_fake_sets() -> list[tuple[str, str, tuple[str, ...]]]:
    """Every module-level tuple of file names in stack.py that holds the overlay."""
    return [
        ('stack.py', name, value)
        for name, value in sorted(vars(stack).items())
        if isinstance(value, tuple) and value and all(isinstance(item, str) for item in value) and FAKE in value
    ]


def _workflow_fake_sites() -> tuple[list[tuple[str, str, tuple[str, ...]]], list[tuple[str, str, str]]]:
    """(compose calls through a set holding the overlay, make runs of a fake-loading target) in every workflow."""
    sets, targets = [], []
    for workflow in _workflow_files():
        for job_id, job in ((_load_yaml(workflow) or {}).get('jobs') or {}).items():
            job_name = (job or {}).get('name') or job_id
            for step in (job or {}).get('steps') or []:
                where = f'{job_name} / {step.get("name")}'
                for line in _run_lines(step.get('run') or ''):
                    for files, rest in _compose_calls(line):
                        if FAKE in _set_names(files):
                            sets.append((workflow.name, f'{where}: {rest[0] if rest else ""}', _set_names(files)))
                    words = line.split()
                    if words[:1] == ['make']:
                        targets += [(workflow.name, where, word) for word in words[1:] if word in EXPECTED_FAKE_TARGETS]
    return sets, targets


def test_every_compose_file_set_that_loads_the_fake_overlay_is_exactly_the_enumerated_one():
    """The release note's (C), REQUIRED: the loaders of docker-compose.fake.yaml, read from source, are exactly these.

    Makefile variables as make expands them (every one the file defines), run_migrations.sh's COMPOSE=(...)
    definitions, stack.py's file-name tuples and every `docker compose` in every workflow. Count matters:
    a second CI line through the test pair is a new loader as much as a new file is.
    """
    makefile_sets, _ = _makefile_fake_sets()
    workflow_sets, _ = _workflow_fake_sites()
    found = Counter(makefile_sets + _migrations_fake_sets() + _mcp_fake_sets() + workflow_sets)
    assert found == EXPECTED_FAKE_SETS, (
        f'the file sets that load {FAKE} are {sorted(found.elements())}, expected {sorted(EXPECTED_FAKE_SETS.elements())}'
    )


def test_only_system_launch_test_build_and_seed_dump_run_a_fake_loading_set():
    """No other recipe names a fake-loading variable or the overlay; nothing reaches the three targets but CI.

    A dev target that ran $(SYSTEM_COMPOSE), or a rule that took system-launch as a prerequisite or re-entered
    make for it, would start the fake beside the owner's data from a target that does not say so.
    """
    _, loaders = _makefile_fake_sets()
    assert loaders == {'SYSTEM_COMPOSE', 'SEED_DUMP_COMPOSE'}, loaders
    users, chained = set(), []
    for targets, prerequisites, recipe in _makefile_rules():
        if any(FAKE in line or any(f'$({name})' in line for name in loaders) for line in recipe):
            users.update(targets)
        chained += [(targets, word) for word in prerequisites if word in EXPECTED_FAKE_TARGETS]
        chained += [
            (targets, line) for line in recipe if '$(MAKE)' in line and set(line.split()) & EXPECTED_FAKE_TARGETS
        ]
    assert users == EXPECTED_FAKE_TARGETS, f'the recipes that run a fake-loading set: {sorted(users)}'
    assert not chained, f'rules that reach a fake-loading target: {chained}'
    _, ci_targets = _workflow_fake_sites()
    assert Counter(ci_targets) == EXPECTED_CI_FAKE_TARGETS, sorted(ci_targets)


def test_the_mcp_test_sets_are_the_makefile_system_set_and_the_overlay_constant_sits_in_two_places():
    """test_up's set IS SYSTEM_COMPOSE's, and test_build's adds only the MCP-only build overlay, last.

    The overlay's file name appears as a whole string constant in the MCP's code exactly where
    EXPECTED_FAKE_CONSTANTS says -- the TEST_COMPOSE_FILES entry and the mode classifier's TEST_FAKE_FILE -- so
    a set spelled inline inside a function, which the module-level read above cannot see, reds here.
    """
    makefile_sets, _ = _makefile_fake_sets()
    system = next(files for _, name, files in makefile_sets if name == 'SYSTEM_COMPOSE')
    assert system == stack.TEST_COMPOSE_FILES, (stack.TEST_COMPOSE_FILES, system)
    assert (*system, MCP_TEST_OVERLAY) == stack.TEST_BUILD_COMPOSE_FILES, stack.TEST_BUILD_COMPOSE_FILES
    assert FAKE not in stack.SYSTEM_CLIENT_COMPOSE_FILES, stack.SYSTEM_CLIENT_COMPOSE_FILES
    holders = []
    for path in sorted((REPO_ROOT / 'tools' / 'agent_mcp').glob('*.py')):
        for statement in ast.parse(path.read_text(encoding='utf-8')).body:
            constants = [node for node in ast.walk(statement) if isinstance(node, ast.Constant) and node.value == FAKE]
            if constants:
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [getattr(statement, 'target', None)]
                )
                names = [target.id for target in targets if isinstance(target, ast.Name)] or [type(statement).__name__]
                holders += [f'{path.name}:{name}' for name in names for _ in constants]
    assert sorted(holders) == sorted(f'stack.py:{name}' for name in EXPECTED_FAKE_CONSTANTS), holders


def _code_mentions(path: str, text: str, needle: str) -> list[str]:
    """The lines of a file that name NEEDLE as code: a whole string constant in Python, a non-comment line elsewhere."""
    if path.endswith('.py'):
        return [
            str(node.lineno)
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and node.value == needle
        ]
    return [line for line in text.splitlines() if needle in line and not line.lstrip().startswith('#')]


def _is_test_or_doc(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (
        'tests' in parts
        or name.startswith('test_')
        or name.endswith('.spec.ts')
        or name.endswith('.md')
        or parts[0] == 'docs'
    )


def test_nothing_else_in_the_repository_names_the_overlay_as_code():
    """The fence around the enumeration: a new loader in a file none of the readers above parse still reds.

    Comments, docstrings and messages may name the overlay (they explain it); code may not, outside the files
    listed. Tests and documentation are out of scope: they load nothing.
    """
    naming = set()
    for path in _tracked_files():
        if _is_test_or_doc(path) or path == FAKE:
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding='utf-8')
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if FAKE in text and _code_mentions(path, text, FAKE):
            naming.add(path)
    assert naming == EXPECTED_FAKE_NAMING_FILES, f'tracked files naming {FAKE} as code: {sorted(naming)}'


# --- 2. the retired names ---------------------------------------------------------------------------------

# Every line, outside README.md, allowed to name the retired project, overlay or variable prefix, as
# (path, the stripped line): the constants and descriptions of tests that assert the names' ABSENCE, plus
# two network names that serve as a project prefix no live project uses. The MCP's server label was the one
# production line here; tj-iee6sn.26 renamed it to agent-stack and removed it.
ALLOWED_RETIRED_LINES = Counter(
    {
        # B12a's absence pin (tools/agent_mcp/tests/test_agent_stack_retired.py): its description and constants.
        (
            'tools/agent_mcp/tests/test_agent_stack_retired.py',
            f"* NO MCP SOURCE NAMES THE AGENT STACK: its project, its overlay, the Makefile's {RETIRED_PREFIX}* variables,",
        ): 1,
        (
            'tools/agent_mcp/tests/test_agent_stack_retired.py',
            f"r'{RETIRED_PROJECT}|AGENT_STACK|docker-compose\\.agent-stack\\.yaml|agent_stack[a-z_]*\\.env'",
        ): 1,
        ('tools/agent_mcp/tests/test_agent_stack_retired.py', f'"PROJECT = \'{RETIRED_PROJECT}\'",'): 1,
        ('tools/agent_mcp/tests/test_agent_stack_retired.py', f"'{RETIRED_PREFIX}COMPOSE',"): 1,
        ('tools/agent_mcp/tests/test_agent_stack_retired.py', f'"\'{RETIRED_OVERLAY}\',",'): 1,
        # The MCP's refusal tests: the retired project and overlay are inputs every verb must refuse.
        ('tools/agent_mcp/tests/test_commands.py', f"RETIRED_PROJECT = '{RETIRED_PROJECT}'"): 1,
        ('tools/agent_mcp/tests/test_commands.py', f"RETIRED_OVERLAY = '{RETIRED_OVERLAY}'"): 1,
        ('tools/agent_mcp/tests/test_dev_exec.py', f"EXPECTED_AGENT_PROJECT = '{RETIRED_PROJECT}'"): 1,
        (
            'tools/agent_mcp/tests/test_dev_guards.py',
            f"'the agent stack project': [EXPECTED_DOCKER, 'compose', '-p', '{RETIRED_PROJECT}'],",
        ): 1,
        ('tools/agent_mcp/tests/test_dev_guards.py', f"RETIRED_OVERLAY = '{RETIRED_OVERLAY}'"): 1,
        ('tools/agent_mcp/tests/test_dev_migrate.py', f"(*_TEST_GOOD[:3], '{RETIRED_PROJECT}', *_TEST_GOOD[4:]),"): 1,
        ('tools/agent_mcp/tests/test_dev_project.py', f"EXPECTED_AGENT_PROJECT = '{RETIRED_PROJECT}'"): 1,
        (
            'tools/agent_mcp/tests/test_dev_tools.py',
            f"'another project name': (_with(_GOOD, _HEAD + 3, '{RETIRED_PROJECT}'), 'a dev lifecycle verb runs only'),",
        ): 1,
        (
            'tools/agent_mcp/tests/test_dev_up.py',
            f"'another project name': (_with(_GOOD, _HEAD + 3, '{RETIRED_PROJECT}'), 'a dev lifecycle verb runs only'),",
        ): 1,
        (
            'tools/agent_mcp/tests/test_system_suite.py',
            f"'the retired agent-stack overlay': _prefix_with((*CLIENT_FILES[:2], '{RETIRED_OVERLAY}')),",
        ): 1,
        ('tools/agent_mcp/tests/test_system_suite.py', f"'{RETIRED_PROJECT}',"): 1,
        # The bind and peer-reach checks pass under any project prefix; the retired name is one no project uses.
        ('server/common/tests/test_grpc_bind_network.py', f"'{RETIRED_PROJECT}_ingest_store': _net(STORE_V4),"): 1,
        ('server/common/tests/test_grpc_bind_network.py', f"'{RETIRED_PROJECT}_ingest_egress': _net(EGRESS_V4),"): 1,
        ('server/common/tests/test_grpc_peer_reach.py', f"'{RETIRED_PROJECT}_ingest_store': _net(STORE_V4),"): 1,
        ('server/common/tests/test_grpc_peer_reach.py', f"'{RETIRED_PROJECT}_ingest_egress': _net(EGRESS_V4),"): 1,
    }
)
# The scribe's (tj-iee6sn.12), not this bead's.
RETIRED_SCAN_EXCLUDED = frozenset({'README.md'})


def _retired_lines() -> Counter:
    found: Counter = Counter()
    for path in _tracked_files():
        if path in RETIRED_SCAN_EXCLUDED:
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding='utf-8')
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        found.update((path, line.strip()) for line in text.splitlines() if RETIRED.search(line))
    return found


def test_no_tracked_file_names_the_retired_agent_stack_but_the_listed_lines():
    """The release note's grep acceptance, as a pin: the hits are EXACTLY the allowed lines, both ways."""
    found = _retired_lines()
    unexpected = sorted((found - ALLOWED_RETIRED_LINES).elements())
    stale = sorted((ALLOWED_RETIRED_LINES - found).elements())
    assert not unexpected, f'tracked files name the retired agent stack: {unexpected}'
    assert not stale, f'allowed lines that no longer exist (delete them from ALLOWED_RETIRED_LINES): {stale}'


def test_the_retired_file_is_gone_and_the_mcp_server_key_is_not_retired():
    """The overlay file is deleted; the MCP's agent-stack key (and its mcp__agent-stack__ prefix) stays and passes."""
    assert not (REPO_ROOT / RETIRED_OVERLAY).exists(), f'{RETIRED_OVERLAY} is back'
    servers = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))['mcpServers']
    assert 'agent-stack' in servers, sorted(servers)
    for kept in ('agent-stack', 'mcp__agent-stack__ps', 'docker-compose.agent-mcp.yaml', 'trader_joe_agent_mcp'):
        assert RETIRED.search(kept) is None, f'the retired-name pattern matches {kept!r}, which is not retired'
    for retired in (RETIRED_PROJECT, RETIRED_OVERLAY, f'{RETIRED_PREFIX}COMPOSE', f'-p {RETIRED_PROJECT} up'):
        assert RETIRED.search(retired), f'the retired-name pattern misses {retired!r}'


# --- 4. CI's render check ----------------------------------------------------------------------------------

# The sets Check Compose Renders renders, as (files, project): prod, dev, tools, the client, the MCP's own
# project and the fake-mode system set. Spelled out; the retired agent-stack set is not among them.
EXPECTED_RENDERS = Counter(
    {
        (('docker-compose.agent-mcp.yaml',), 'trader_joe_agent_mcp'): 1,
        ((BASE, 'docker-compose.web.yaml'), None): 1,
        ((BASE, 'docker-compose.override.yaml', 'docker-compose.web.yaml', 'docker-compose.web.dev.yaml'), None): 1,
        (
            (
                BASE,
                'docker-compose.override.yaml',
                'docker-compose.web.yaml',
                'docker-compose.web.dev.yaml',
                'docker-compose.tools.yaml',
            ),
            None,
        ): 1,
        ((BASE, CLIENT), None): 1,
        (TEST_PAIR, None): 1,
    }
)


def _render_step() -> dict:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    steps = [
        step for job in jobs.values() for step in (job or {}).get('steps') or [] if step.get('name') == RENDER_STEP
    ]
    assert len(steps) == 1, f'expected one {RENDER_STEP!r} step, found {len(steps)}'
    return steps[0]


def test_the_render_check_renders_exactly_the_surviving_sets():
    """Addendum 26 (8): the agent-stack render went with the overlay; every other render stays, each once."""
    lines = _run_lines(_render_step().get('run') or '')
    rendered = Counter(
        (_set_names(files), project)
        for line in lines
        for (files, rest), project in zip(_compose_calls(line), _compose_projects(line), strict=True)
        if rest[:1] == ['config']
    )
    assert rendered == EXPECTED_RENDERS, f'{RENDER_STEP} renders {sorted(rendered.elements(), key=str)}'
    echoes = [line for line in lines if line.startswith('echo ')]
    assert len(echoes) == 1 and 'agent-stack' not in echoes[0], echoes


def test_no_workflow_names_the_retired_set():
    """No workflow line renders, starts or names the retired project, overlay or its variables."""
    offenders = [
        f'{workflow.name}: {line.strip()}'
        for workflow in _workflow_files()
        for line in workflow.read_text(encoding='utf-8').splitlines()
        if RETIRED.search(line)
    ]
    assert not offenders, offenders
