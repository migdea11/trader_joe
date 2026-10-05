"""The MCP endpoint: the verbs as MCP tools over streamable HTTP, behind the bearer-token gate.

The only module that imports the MCP SDK (the agent-mcp uv group, installed in the MCP image alone).
It uses the SDK's low-level server rather than FastMCP on purpose: FastMCP silently DROPS an unknown
keyword argument (its argument model ignores extras) and runs a synchronous tool on the event loop.
Here every tool publishes an explicit, closed input schema, and AgentStack.call enforces it.
"""

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from tools.agent_mcp.auth import BearerTokenMiddleware, TokenFileError, load_or_create_token
from tools.agent_mcp.runner import VERB_SCHEMAS, AgentStack
from tools.agent_mcp.settings import Settings, SettingsError, load_settings


MCP_PATH = '/mcp'
# Statuses that are not the verb doing its job.
ERROR_STATUSES = frozenset({'refused', 'busy', 'failed', 'timeout', 'error'})

log = logging.getLogger('agent_mcp')


def build_server(agent_stack: AgentStack) -> Server:
    """The MCP server: list_tools from VERB_SCHEMAS, call_tool into AgentStack.call."""
    server: Server = Server('trader_joe_agent_stack')

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=verb, description=agent_stack.describe(verb), inputSchema=VERB_SCHEMAS[verb])
            for verb in agent_stack.verbs
        ]

    # validate_input=False: the SDK's schema check would refuse a bad call BEFORE AgentStack.call, so
    # the refusal would leave no audit line. AgentStack.call does the whole check itself (exact key
    # set, then each value's validator) and audits every call, refused ones included.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await agent_stack.call(name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(result, indent=2))],
            structuredContent=result,
            isError=result['status'] in ERROR_STATUSES,
        )

    return server


def build_app(settings: Settings, agent_stack: AgentStack, token: str) -> BearerTokenMiddleware:
    """The ASGI app: the token gate outermost, then the streamable-HTTP MCP endpoint at MCP_PATH."""
    manager = StreamableHTTPSessionManager(
        app=build_server(agent_stack),
        json_response=True,
        # Stateless: nothing a client does lives beyond its request, so there is no session to
        # hijack or leak between the agents that share this server.
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.hostname, f'{settings.hostname}:{settings.port}'],
            allowed_origins=[],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Route(MCP_PATH, endpoint=StreamableHTTPASGIApp(manager))], lifespan=lifespan)
    return BearerTokenMiddleware(app, token)


def main() -> None:
    """Start the server: settings, token (generated at first start), audit-log check, then serve."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        settings = load_settings(os.environ)
        token = load_or_create_token(settings.token_file)
    except (SettingsError, TokenFileError, OSError) as error:
        log.error('refusing to start: %s', error)
        sys.exit(2)
    agent_stack = AgentStack(settings)
    # Nothing about the token is logged, not even its file (make agent-mcp-up prints that path).
    log.info(
        'agent-stack MCP on http://%s:%d%s; audit log %s',
        settings.hostname,
        settings.port,
        MCP_PATH,
        agent_stack.audit_log,
    )
    uvicorn.run(
        build_app(settings, agent_stack, token),
        host=settings.bind,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        log_level='info',
    )
