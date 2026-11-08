"""
MCP catalog + bridge (Phase 9).

The dev-owned registry of MCP servers (the credential-safe side of the trust
boundary, D50): servers are registered in code by ID; agent specs reference the
ID, never inline config or secrets. Mirrors the tool table (D33).

`build_mcp_tools` is the BRIDGE (D52): it lists a server's tools and turns each
into a `RegisteredTool` (a per-run fill) whose schema is the MCP JSON schema and
whose body calls the server through the `mcp_client` seam. Because MCP tools
become ordinary tool-table fills, they inherit P8 approval, billing, and the
native-or-fill machinery with no special-casing downstream.

No `pydantic_ai` import here (D3): the bridge produces plain `RegisteredTool`
entries; the adapter binds them to the SDK (via `Tool.from_schema`, since MCP
tools have a JSON schema rather than a Python signature).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import mcp_client
from ..engine.tool_table import RegisteredTool
from ..engine.types import EngineError, ToolExecutionError


class McpError(EngineError):
    """
    An MCP server problem (unreachable, unknown id, tool-level error). Tool-level
    errors are translated to the tool-retry path so the model sees them; a
    genuinely broken server surfaces as a failed run (loud, like any tool fault):
    """


@dataclass(frozen=True)
class MCPServerConfig:
    """
    One MCP server the agent may use. `url` (remote Streamable HTTP) + optional
    auth `headers` is the shape offered to user-added servers; `server` (an
    in-process `mcp` server object) is dev/local/test only. `allowed_tools`
    limits the exposed tools to a least-privilege subset:
    """
    id: str
    url: str | None = None
    headers: dict[str, str] | None = None
    allowed_tools: list[str] | None = None
    server: Any = None                     # In-process mcp server (dev/test) — never serialized
    # Provenance: dev-registered (trusted) vs user-added (untrusted origin). The
    # bridge labels user tools so they are never mistaken for platform tools (D51):
    origin: str = "dev"


# The dev catalog — module-level, populated in code at import/startup (like the
# tool table). User-added servers do NOT live here; they are resolved per-owner
# from the store (Slice 3) and merged at run time:
_MCP_CATALOG: dict[str, MCPServerConfig] = {}


def register_mcp_server(
    id: str,
    *,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    allowed_tools: list[str] | None = None,
    server: Any = None,
) -> None:
    """Registering a dev MCP server by id (code-only, D50); duplicate ids raise:"""
    if id in _MCP_CATALOG:
        raise McpError(f"MCP server {id!r} is already registered")
    _MCP_CATALOG[id] = MCPServerConfig(
        id=id, url=url, headers=headers, allowed_tools=allowed_tools, server=server, origin="dev",
    )


def get_mcp_server(id: str) -> MCPServerConfig | None:
    """Looking up a dev catalog entry by id (None when absent):"""
    return _MCP_CATALOG.get(id)


def resolve_mcp_configs(refs: list[str]) -> list[MCPServerConfig]:
    """
    Resolving an agent spec's `mcp_servers` references into configs. A reference
    is a catalog ID; for back-compat a raw URL (contains "://") is treated as an
    anonymous no-auth remote server, preserving the pre-P9 behavior. An unknown
    ID fails loudly (a spec must not silently run without a server it declared):
    """
    configs: list[MCPServerConfig] = []
    for ref in refs:
        if "://" in ref:
            configs.append(MCPServerConfig(id=ref, url=ref, origin="dev"))
            continue
        entry = get_mcp_server(ref)
        if entry is None:
            raise McpError(f"MCP server {ref!r} is not registered (registered: {sorted(_MCP_CATALOG)})")
        configs.append(entry)
    return configs


# Bridged MCP tool names are namespaced by server so two servers exposing the
# same tool name never collide, and analytics can attribute a call to its server:
def _bridged_name(server_id: str, tool_name: str) -> str:
    """Composing the namespaced tool name the model sees for a bridged MCP tool:"""
    return f"mcp__{server_id}__{tool_name}"


async def build_mcp_tools(configs: list[MCPServerConfig]) -> list[RegisteredTool]:
    """
    Bridging every tool of every server into a per-run `RegisteredTool` (D52):
    the model-facing schema is the MCP JSON schema, and the body calls the
    server through the seam. User-added servers (origin="user") get a provenance
    tag in the description so their tool text is never trusted as a platform
    tool. Listing failures surface as McpError (loud):
    """
    tools: list[RegisteredTool] = []
    for config in configs:
        try:
            specs = await mcp_client.list_tools(config)
        except ToolExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead/broken server is a loud, typed fault:
            raise McpError(f"MCP server {config.id!r} failed to list tools: {exc}") from exc

        for tool_name, description, schema in specs:
            desc = description
            if config.origin == "user":
                desc = f"[user-added MCP server {config.id!r}] {description}"
            tools.append(RegisteredTool(
                name=_bridged_name(config.id, tool_name),
                fn=_make_caller(config, tool_name),
                description=desc,
                parameters_schema=schema,
            ))
    return tools


def _make_caller(config: MCPServerConfig, tool_name: str):
    """
    Building the fill body for one bridged MCP tool: it ignores ctx (the call
    needs only the closed-over server config), calls the tool through the seam,
    and translates a tool-level MCP error into ToolExecutionError so the adapter
    turns it into a model-visible retry rather than failing the run:
    """
    async def _call(ctx, **kwargs) -> str:
        """Calling the MCP tool with the model-supplied arguments (ctx unused):"""
        try:
            return await mcp_client.call_tool(config, tool_name, kwargs)
        except McpError as exc:
            raise ToolExecutionError(str(exc)) from exc
    return _call
