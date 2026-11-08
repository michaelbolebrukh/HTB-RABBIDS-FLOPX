"""
MCP client seam — the ONLY module that imports the `mcp` SDK (Phase 9).

We host the MCP client ourselves (client-side MCP) so a server's tools run under
our control and their results flow through our transcript as ordinary tool
results (untrusted data, D49). Connect-per-call keeps this stateless and matches
how our fills work — a session is opened, used, and closed for each `list`/`call`
(a per-run session cache is a later optimization, not needed for correctness).

Two connection shapes:
- remote: Streamable HTTP to a `url` (optional auth headers) — the only shape
  offered to user-added servers (no code runs on our infra);
- in-process: a pre-built `mcp` server object, used by dev/local and by tests as
  an offline seam (no network).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session

if TYPE_CHECKING:
    from .mcp_catalog import MCPServerConfig


# One tool as the model needs to see it: name, description, and the JSON schema
# for its arguments (MCP tools carry a schema, not a Python signature):
ToolSpec = tuple[str, str, dict]


@asynccontextmanager
async def _open_session(config: "MCPServerConfig"):
    """
    Opening an initialized MCP client session for a server config, remote or
    in-process. Raising if the config names neither (a broken pick fails loudly):
    """
    if config.server is not None:
        # In-process (dev/local/test) — the helper wires a client to the server
        # over memory streams; it initializes the session for us:
        async with create_connected_server_and_client_session(config.server) as session:
            yield session
    elif config.url:
        async with streamablehttp_client(config.url, headers=config.headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        from .mcp_catalog import McpError
        raise McpError(f"MCP server {config.id!r} has neither a url nor an in-process server")


async def list_tools(config: "MCPServerConfig") -> list[ToolSpec]:
    """
    Listing a server's tools (filtered to `allowed_tools` when the dev set a
    least-privilege subset), as (name, description, args_schema) triples:
    """
    async with _open_session(config) as session:
        result = await session.list_tools()
    specs: list[ToolSpec] = []
    for tool in result.tools:
        if config.allowed_tools is not None and tool.name not in config.allowed_tools:
            continue
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        specs.append((tool.name, tool.description or "", schema))
    return specs


async def call_tool(config: "MCPServerConfig", tool_name: str, args: dict[str, Any]) -> str:
    """
    Calling one tool and reducing its result to text (the tool-result content
    our transcript stores). A tool-level error is surfaced as text via McpError
    so the bridge can turn it into a model-visible retry, not a dead run:
    """
    from .mcp_catalog import McpError

    async with _open_session(config) as session:
        result = await session.call_tool(tool_name, args)

    # Reducing content blocks to text; non-text blocks (images, etc.) become a
    # short placeholder — the model asked for a tool result, not raw bytes:
    chunks: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        chunks.append(text if text is not None else f"[{getattr(block, 'type', 'content')}]")
    text = "\n".join(chunks)
    if getattr(result, "isError", False):
        raise McpError(f"MCP tool {tool_name!r} error: {text}")
    return text
