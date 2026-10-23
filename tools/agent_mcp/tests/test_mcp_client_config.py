"""The checked-in client config, .mcp.json: the agent-stack entry's per-server tool timeout.

tj-iee6sn.4 (slice 0 of epic tj-iee6sn, plan revision 2, LONG CALLS / D8). Without a per-server
`timeout`, Claude Code's per-request timer for an HTTP server is 60 s, and this server answers with
json_response=True, so its first response byte IS the whole result: any verb over 60 s returned
nothing (tj-luej31). The Claude Code MCP docs (code.claude.com/docs/en/mcp, read 2026-10-17):

    "Set a per-server tool execution timeout by adding a `timeout` field in milliseconds to that
    server's `.mcp.json` entry"
    "Values below 1000 are ignored and fall through to `MCP_TOOL_TIMEOUT`"
    "Claude Code sets that timer to the greatest of three values: 60 seconds, the tool timeout that
    applies to the server, and `MCP_TIMEOUT`."

The docs state no maximum. The timeout must sit ABOVE the longest verb budget
(runner.VERB_TIMEOUT_SECONDS), so the server's own structured 'timeout' result reaches the agent
before the client gives up. Raising a verb budget without raising this value turns these tests red.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner
from tools.agent_mcp.tests.harness import REPO_ROOT


pytestmark = pytest.mark.build_infra

MCP_CONFIG = REPO_ROOT / '.mcp.json'
SERVER_NAME = 'agent-stack'

# Time between the server's own verb timeout firing and the client's: killing the process, building
# the 'timeout' result and sending it over HTTP. 30 s is generous for that and leaves the committed
# 60 s (1,860,000 - 1,800,000 ms) a clear pass.
MARGIN_MS = 30_000

# Below this the client ignores the field (docs, quoted above), and the 60 s timer comes back.
CLIENT_IGNORES_BELOW_MS = 1000

# Not in the docs: Node's setTimeout treats a delay above 2**31 - 1 ms as 1 ms, so an oversized value
# would time every call out at once rather than never. A guard against an extra digit, not a design.
NODE_TIMER_MAX_MS = 2**31 - 1


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _timeout() -> Any:
    return _load(MCP_CONFIG)['mcpServers'][SERVER_NAME]['timeout']


def test_mcp_config_is_valid_json_with_the_agent_stack_entry() -> None:
    config = _load(MCP_CONFIG)
    assert isinstance(config, dict), type(config)
    assert SERVER_NAME in config.get('mcpServers', {}), sorted(config.get('mcpServers', {}))


def test_agent_stack_timeout_is_present() -> None:
    entry = _load(MCP_CONFIG)['mcpServers'][SERVER_NAME]
    assert 'timeout' in entry, f'{SERVER_NAME} has no per-server timeout: calls over 60 s return nothing'


def test_agent_stack_timeout_is_an_integer_of_milliseconds() -> None:
    timeout = _timeout()
    # bool is a subclass of int, so isinstance would let `true` through.
    assert type(timeout) is int, f'timeout must be a JSON integer (milliseconds), got {type(timeout).__name__}'
    assert timeout >= CLIENT_IGNORES_BELOW_MS, f'{timeout} < {CLIENT_IGNORES_BELOW_MS}: the client ignores it'


def test_agent_stack_timeout_outlasts_the_longest_verb_budget() -> None:
    longest_verb, longest_s = max(runner.VERB_TIMEOUT_SECONDS.items(), key=lambda item: item[1])
    required = longest_s * 1000 + MARGIN_MS
    timeout = _timeout()
    assert timeout >= required, (
        f".mcp.json {SERVER_NAME} timeout {timeout} ms is below {longest_verb}'s {longest_s} s budget plus "
        f'a {MARGIN_MS} ms margin ({required} ms): the client would give up before the server reports timeout'
    )


def test_agent_stack_timeout_fits_a_node_timer() -> None:
    timeout = _timeout()
    assert timeout <= NODE_TIMER_MAX_MS, f'{timeout} ms overflows a Node timer, which then fires at once'
