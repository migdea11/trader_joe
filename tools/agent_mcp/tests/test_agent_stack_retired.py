"""The agent stack is retired from the MCP code, and the tools carry their final names.

B12a (tj-iee6sn.21; ADR tj-4rr0la addenda 26 (5) and 27 (4)) retired the stack; tj-iee6sn.26 dropped the dev_
prefix (the architect's 12:55 UTC release note of 2026-10-19).

Every literal here is spelled out, never read from the module under test: a pin that took its expectation
from runner.py would agree with any change to it.

  * THE VERB SET is exactly the fifteen final names -- dev launchers up, build, tools; test launchers test_up,
    test_build; either mode down, restart, ps, logs, db_tables, http_get, migrate, migrate_status,
    run_system_tests, seed_dump -- in the handler table, the input schemas, the timeouts, and so the MCP tool
    list (server.py lists AgentStack.verbs with VERB_SCHEMAS, pinned in test_dev_migrate.py). No retired verb
    and no old dev_ name is in any of them: there are no aliases.
  * NO OLD TOOL NAME IN ANY TEXT. No description, refusal, hint, fix or docs string in the MCP's modules (nor
    the Makefile or the MCP Dockerfile) spells one of the nine old dev_<verb> tool names as a word. The only
    spellings allowed to contain one are the internal identifiers in ALLOWED_OLD_NAME_IDENTIFIERS, which stay.
  * THE SERVER LABEL, server.py's Server(...), equals the one .mcp.json key, agent-stack (tj-iee6sn.23 kept
    the name), so the protocol-level name and the client's tool prefix mcp__agent-stack__ agree.
  * NO MCP SOURCE NAMES THE AGENT STACK: its project, its overlay, the Makefile's AGENT_STACK_* variables,
    or its generated env files. No exception remains: the server label was the last (tj-iee6sn.26).
  * THE REMOVED SYMBOLS stay removed from stack.py and runner.py, ensure_snapshot (tj-iee6sn.26) included.

The image half -- the Dockerfile COPY and Dockerfile.dockerignore no longer carry the overlay, and S6's list
equals the bake list -- is test_commands.py's and test_dev_guards.py's.
"""

import ast
import json
import re

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import REPO_ROOT, VERB_SAMPLES, make_rig


pytestmark = pytest.mark.build_infra

MCP_DIR = REPO_ROOT / 'tools' / 'agent_mcp'
MCP_CONFIG = REPO_ROOT / '.mcp.json'

EXPECTED_VERBS = frozenset(
    {
        'up',
        'build',
        'tools',
        'test_up',
        'test_build',
        'down',
        'restart',
        'ps',
        'logs',
        'db_tables',
        'http_get',
        'migrate',
        'migrate_status',
        'run_system_tests',
        'seed_dump',
    }
)
# The nine tool names tj-iee6sn.26 replaced, old -> new (ADR tj-4rr0la addendum 27 (4)).
OLD_TO_NEW = {
    'dev_up': 'up',
    'dev_build': 'build',
    'dev_tools': 'tools',
    'dev_down': 'down',
    'dev_restart': 'restart',
    'dev_ps': 'ps',
    'dev_logs': 'logs',
    'dev_db_tables': 'db_tables',
    'dev_http_get': 'http_get',
}
# The agent stack's verbs (tj-iee6sn.21), the folded dev migrate trio, and the nine old names: none may answer.
RETIRED_VERBS = frozenset(
    {
        'stack_up',
        'stack_down',
        'stack_wipe',
        'migrate_check',
        'dev_migrate',
        'dev_migrate_check',
        'dev_migrate_status',
        *OLD_TO_NEW,
    }
)

# An old tool name as a word: not inside an identifier (_dev_up, dev_up_step) and not an attribute.
OLD_NAME_WORD = re.compile(r'(?<![\w.])(?:' + '|'.join(sorted(OLD_TO_NEW, key=len, reverse=True)) + r')(?!\w)')
# Every identifier that contains an old name and stays (the release note: internal helper names keep their
# prefix). Spelled out, so a new spelling that embeds an old name is seen by a human before it lands.
ALLOWED_OLD_NAME_IDENTIFIERS = frozenset(
    {
        '_dev_build',
        '_dev_db_tables',
        '_dev_down',
        '_dev_http_get',
        '_dev_http_get_script',
        '_dev_logs',
        '_dev_ps',
        '_dev_restart',
        '_dev_tools',
        '_dev_up',
        'check_dev_tools_argv',
        'check_dev_up_argv',
        'dev_build_step',
        'dev_db_tables_steps',
        'dev_down_steps',
        'dev_http_get_steps',
        'dev_logs_steps',
        'dev_ps_steps',
        'dev_restart_steps',
        'dev_tools_prefix',
        'dev_tools_step',
        'dev_up_step',
        'dev_up_wrapper_head',
        'validate_dev_http_get_name',
        'validate_dev_restart_service',
    }
)
CONTAINS_OLD_NAME = re.compile(r'\w*(?:' + '|'.join(OLD_TO_NEW) + r')\w*')
# The texts scanned: every MCP module (descriptions are the handlers' docstrings; refusals, hints and fix
# texts are their string literals) plus the two non-Python files tj-iee6sn.26 re-worded.
TEXT_FILES = (*sorted(MCP_DIR.glob('*.py')), REPO_ROOT / 'Makefile', MCP_DIR / 'Dockerfile')

# What no MCP source may spell: the retired project, overlay, Makefile variables and generated env files.
RETIRED_WORDS = re.compile(
    r'trader_joe_agent_stack|AGENT_STACK|docker-compose\.agent-stack\.yaml|agent_stack[a-z_]*\.env'
)
# No exception: server.py's label was the last one, and tj-iee6sn.26 renamed it.
ALLOWED_MENTIONS: set[tuple[str, str]] = set()

REMOVED_STACK_NAMES = (
    'ensure_snapshot',
    'PROJECT',
    'COMPOSE_FILES',
    'SERVICES',
    'SNAPSHOT_BOUND_SERVICES',
    'WAIT_TIMEOUT_SECONDS',
    'compose_prefix',
    '_steps',
    'stack_up_steps',
    'stack_down_steps',
    'wipe_clear_steps',
    'logs_steps',
    'ps_steps',
    'postgres_running_steps',
    'alembic_steps',
    'alembic_check_steps',
    'validate_service',
    'ensure_env_files',
    'check_env_file_paths',
    'read_committed_defaults',
    'build_env_values',
    'all_worktree_paths',
    'check_data_dir',
    'remove_data_dir',
    'is_plain_dir',
    'DATA_MOUNTS',
    'DATA_DIR_NAME',
    'STATE_FILE_NAME',
    'ENV_DEFAULT_SOURCES',
    'ENV_FILE_NAMES',
    'ROOT_ENV_NAME',
    'STORE_ENV_NAME',
    'INGEST_ENV_NAME',
    'AGENT_DATABASE_NAME',
    'AGENT_STORE_API_NETWORK',
    'BROKER_CREDENTIALS',
)
REMOVED_RUNNER_NAMES = (
    '_stack_up',
    '_stack_down',
    '_stack_wipe',
    '_logs',
    '_ps',
    '_guarded_env',
    '_guarded_env_blocking',
    '_existing_snapshot',
    '_state_file',
    '_recorded_worktree',
    '_alembic',
    '_alembic_check',
)


def test_the_verb_set_is_exactly_the_fifteen_in_every_table():
    """Addenda 26 (5) / 27 (4): the handlers (so the tool list), the schemas and the timeouts agree."""
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert set(agent.verbs) == EXPECTED_VERBS, set(agent.verbs) ^ EXPECTED_VERBS
    assert len(agent.verbs) == len(EXPECTED_VERBS) == 15
    assert set(runner.VERB_SCHEMAS) == EXPECTED_VERBS, set(runner.VERB_SCHEMAS) ^ EXPECTED_VERBS
    assert set(runner.VERB_TIMEOUT_SECONDS) == EXPECTED_VERBS, set(runner.VERB_TIMEOUT_SECONDS) ^ EXPECTED_VERBS
    assert set(VERB_SAMPLES) == EXPECTED_VERBS


def test_every_old_name_maps_to_a_final_name_and_none_is_one():
    assert set(OLD_TO_NEW.values()) <= EXPECTED_VERBS
    assert not set(OLD_TO_NEW) & EXPECTED_VERBS
    assert not RETIRED_VERBS & EXPECTED_VERBS


@pytest.mark.parametrize('verb', sorted(RETIRED_VERBS))
def test_no_retired_verb_is_registered_or_callable(verb: str):
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type]
    assert verb not in agent.verbs and verb not in runner.VERB_SCHEMAS and verb not in runner.VERB_TIMEOUT_SECONDS
    assert verb not in runner._OPTIONAL_DEFAULTS


@pytest.mark.parametrize('old', sorted(OLD_TO_NEW))
def test_an_old_name_is_refused_as_unknown_and_runs_nothing(old: str, tmp_path, monkeypatch):
    """No alias: a call by an old name is the unknown-verb refusal, which lists only the final names and runs nothing."""
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call(old, {})
    assert result['status'] == 'refused' and rig.docker.steps == [], result
    listed = result['message'].removeprefix('unknown verb; the verbs are: ')
    assert listed != result['message'] and set(listed.split(', ')) == EXPECTED_VERBS, result


def test_no_tool_description_names_an_old_or_retired_verb():
    """The text an MCP client shows an agent for each tool offers only the final names."""
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type]
    word = re.compile(r'(?<![\w.])(?:' + '|'.join(sorted(RETIRED_VERBS, key=len, reverse=True)) + r')(?!\w)')
    stale = {verb: word.findall(agent.describe(verb)) for verb in agent.verbs if word.search(agent.describe(verb))}
    assert not stale, stale


@pytest.mark.parametrize('path', TEXT_FILES, ids=[p.name for p in TEXT_FILES])
def test_no_mcp_text_spells_an_old_tool_name(path):
    """Descriptions, refusals, hints, fix texts and docs: an old name never appears as a word (tj-iee6sn.26)."""
    hits = [
        (number, line.strip())
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1)
        if OLD_NAME_WORD.search(line)
    ]
    assert not hits, f'{path.name} still names an old tool: {hits}'


def test_only_the_listed_identifiers_embed_an_old_name():
    """Internal identifiers keep their dev_ prefix; anything else that embeds an old name is new and reds."""
    seen = set()
    for path in TEXT_FILES:
        seen |= set(CONTAINS_OLD_NAME.findall(path.read_text(encoding='utf-8')))
    assert seen <= ALLOWED_OLD_NAME_IDENTIFIERS, sorted(seen - ALLOWED_OLD_NAME_IDENTIFIERS)
    assert not ALLOWED_OLD_NAME_IDENTIFIERS & set(OLD_TO_NEW), 'a bare old name is never an allowed identifier'
    # Non-vacuity: the scan reads the modules (the handlers and step builders are there today).
    assert {'_dev_up', 'dev_up_step', 'dev_tools_prefix'} <= seen


def test_the_old_name_scan_sees_each_spelling_it_exists_to_catch():
    for text in (
        "raise Refused('the image is not on the daemon: call dev_build first')",
        "hint = 'start the project first: dev_up (your dev data) or test_up (test mode)'",
        '"""... use dev_db_tables, which works while data_store is down."""',
        "'dev_http_get': self._dev_http_get,",
        "# dev_tools' set",
        'see the steps and dev_logs',
    ):
        assert OLD_NAME_WORD.search(text), text
    for text in ('self._dev_up', 'stack.dev_up_step(', 'dev_tools_prefix', 'check_dev_up_argv', 'test_dev_up_x'):
        assert not OLD_NAME_WORD.search(text), text


def _server_labels() -> list[str]:
    """Every Server(<label>) call in server.py, read by parsing: server.py imports the MCP SDK, which this venv omits."""
    tree = ast.parse((MCP_DIR / 'server.py').read_text(encoding='utf-8'))
    labels = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Server':
            assert len(node.args) == 1 and isinstance(node.args[0], ast.Constant), ast.dump(node)
            labels.append(node.args[0].value)
    return labels


def test_the_server_label_is_the_mcp_json_key():
    """tj-iee6sn.23 kept the name agent-stack: the protocol-level label and the client's key agree."""
    keys = list(json.loads(MCP_CONFIG.read_text(encoding='utf-8'))['mcpServers'])
    assert keys == ['agent-stack'], keys
    assert _server_labels() == keys


def test_no_tool_description_describes_the_agent_stack():
    """The descriptions an MCP client shows an agent are the handlers' docstrings: none may offer the agent stack."""
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type]
    stale = {verb: agent.describe(verb) for verb in agent.verbs if 'agent stack' in agent.describe(verb).lower()}
    assert not stale, stale


def _mentions() -> set[tuple[str, str]]:
    found = set()
    for path in sorted(MCP_DIR.glob('*.py')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if RETIRED_WORDS.search(line):
                found.add((path.name, line.strip()))
    return found


def test_no_mcp_source_names_the_agent_stack():
    """No module spells a retired word; server.py's label, the last exception, was renamed (tj-iee6sn.26)."""
    mentions = _mentions()
    assert mentions == ALLOWED_MENTIONS == set(), (
        f'retired agent-stack words in tools/agent_mcp/*.py: {sorted(mentions - ALLOWED_MENTIONS)}'
    )


def test_the_grep_sees_each_retired_word():
    """Non-vacuity for the pin above: the pattern matches every spelling it exists to catch."""
    for word in (
        "PROJECT = 'trader_joe_agent_stack'",
        'AGENT_STACK_COMPOSE',
        "'docker-compose.agent-stack.yaml',",
        "ROOT_ENV_NAME = 'agent_stack.env'",
        "STORE_ENV_NAME = 'agent_stack_store.env'",
    ):
        assert RETIRED_WORDS.search(word), word
    assert not RETIRED_WORDS.search('docker-compose.agent-dev.yaml AgentStack agent_stack.verbs')


@pytest.mark.parametrize('name', REMOVED_STACK_NAMES)
def test_a_removed_stack_name_stays_removed(name: str):
    assert not hasattr(stack, name), f'stack.{name} is back: the agent stack was retired (tj-iee6sn.21)'


@pytest.mark.parametrize('name', REMOVED_RUNNER_NAMES)
def test_a_removed_runner_name_stays_removed(name: str):
    assert not hasattr(runner.AgentStack, name), f'AgentStack.{name} is back (tj-iee6sn.21)'
