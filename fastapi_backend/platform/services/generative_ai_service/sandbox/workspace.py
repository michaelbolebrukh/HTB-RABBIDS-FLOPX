"""
Workspace materialization — WorkspaceSpec -> concrete files (Phase 6, D38/D14).

The single place the open-spec artifacts take their on-disk shape, shared by
every backend so a Docker sandbox and a future hosted sandbox lay out
IDENTICAL workspaces (requirement 11):

- `AGENTS.md` at the workspace root — the rules file the open agent spec says
  an agent must read and adhere to.
- `skills/<name>/...` — agentskills.io skill folders (entry point SKILL.md).
- `.mcp.json` — MCP server config in the conventional `mcpServers` shape.
- Seed files verbatim at their given relative paths.

Pure function of the spec — no I/O here; backends take the returned map and
write it however their isolation mechanism requires.
"""

from __future__ import annotations

import json
import posixpath

from .types import SandboxError, WorkspaceSpec


def materialize_files(workspace: WorkspaceSpec) -> dict[str, bytes]:
    """
    Flattening a WorkspaceSpec into {relative_path: content}. Paths are
    validated to stay INSIDE the workspace (a spec is data and could carry a
    hostile "../" path — the sandbox boundary must hold from the first byte):
    """
    files: dict[str, bytes] = {}

    for path, content in workspace.files.items():
        files[_safe_path(path)] = content

    if workspace.agents_md is not None:
        files["AGENTS.md"] = workspace.agents_md.encode()

    for skill in workspace.skills:
        for rel, content in skill.files.items():
            files[_safe_path(posixpath.join("skills", skill.name, rel))] = content
        # A skill without SKILL.md is not a skill per the open spec — refuse it
        # loudly rather than materializing something agents cannot discover:
        if not any(p == posixpath.join("skills", skill.name, "SKILL.md") for p in files):
            raise SandboxError(f"skill {skill.name!r} has no SKILL.md (agentskills.io requires one)")

    if workspace.mcp_servers:
        config = {"mcpServers": {name: {"url": url} for name, url in workspace.mcp_servers.items()}}
        files[".mcp.json"] = json.dumps(config, indent=2).encode()

    return files


def _safe_path(path: str) -> str:
    """Normalizing one relative path and rejecting absolute/escaping ones:"""
    normalized = posixpath.normpath(path)
    if normalized.startswith(("/", "..")) or normalized == ".":
        raise SandboxError(f"workspace path {path!r} escapes the workspace root")
    return normalized
