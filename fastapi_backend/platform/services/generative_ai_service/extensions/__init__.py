"""
Extensions — MCP servers and Skills attached to agents (Phase 9).

Two extensibility surfaces, both riding the existing spine (tool table, P8
approval, sandbox, budget ledger, access-control tiers) rather than a new
subsystem:

- MCP servers (`mcp_catalog` + `mcp_client`): external tool/data providers. We
  host the client (the official `mcp` SDK) and BRIDGE each server tool into our
  own tool table as a fill — so MCP tools get P8 approval, billing, and
  native-or-fill treatment uniformly, and are offline-testable.
- Skills (Phase 9 Slice 2): procedural-knowledge bundles executed in the
  sandbox.

Dependency note (why the `mcp` SDK, not pydantic-ai's MCP capability): the
capability pulls fastmcp-slim, which requires starlette>=1.0.1 and conflicts
with FastAPI 0.115 (starlette<0.46). The lower-level `mcp` SDK needs only
starlette>=0.27, coexists with our web stack, and bridging into the tool table
is the cleaner fit anyway. This package is the sole seam that imports `mcp`
(the facade rule D3 constrains pydantic_ai; this is an analogous isolated seam,
like the voice/media adapters).
"""

from .mcp_catalog import (
    MCPServerConfig,
    McpError,
    build_mcp_tools,
    get_mcp_server,
    register_mcp_server,
    resolve_mcp_configs,
)
from .skill_catalog import (
    SkillConfig,
    SkillError,
    build_skill_preamble,
    get_skill,
    register_skill,
    resolve_skills,
)
from .store import (
    add_user_extension,
    delete_user_extension,
    list_user_extensions,
    resolve_user_mcp_configs,
    resolve_user_skill_configs,
)


__all__ = [
    # MCP (Slice 1):
    "MCPServerConfig",
    "McpError",
    "build_mcp_tools",
    "get_mcp_server",
    "register_mcp_server",
    "resolve_mcp_configs",
    # Skills (Slice 2):
    "SkillConfig",
    "SkillError",
    "build_skill_preamble",
    "get_skill",
    "register_skill",
    "resolve_skills",
    # User-added extensions (Slice 3):
    "add_user_extension",
    "delete_user_extension",
    "list_user_extensions",
    "resolve_user_mcp_configs",
    "resolve_user_skill_configs",
]
