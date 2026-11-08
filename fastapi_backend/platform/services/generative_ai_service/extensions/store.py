"""
User-added extension store (Phase 9 Slice 3, D51).

Per-`owner_id` persistence for the MCP servers and Skills a user brings to an
agent whose developer enabled BYO extensions. No allowlist: the enable-toggle
is the gate; safety comes from the runtime posture (user MCP is remote-only +
SSRF-guarded, user skills run in the sandbox, results are untrusted data) and
per-owner isolation. Resolution turns rows into the SAME config types the dev
catalog produces, tagged `origin="user"` so their provenance is never lost.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from ..models import AIUserExtension
from .mcp_catalog import MCPServerConfig, McpError
from .skill_catalog import SkillConfig, SkillError, _parse_frontmatter


# ============================================================================
# Validation (at add time — a broken pick is rejected before it is stored)
# ============================================================================

def validate_user_mcp(config: dict) -> dict:
    """
    Validating a user MCP server config: remote-only (http/https), reachable,
    and NOT resolving to a private/loopback/reserved address (the same SSRF
    guard `web_fetch` uses). Returns the cleaned {url, headers}. User-added
    servers never run code on our infra — stdio/local is dev-only (D52):
    """
    url = (config or {}).get("url")
    if not url or not isinstance(url, str):
        raise McpError("Add a URL for the MCP server.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise McpError("MCP servers must use a public http:// or https:// URL.")
    if not parts.hostname:
        raise McpError("That MCP URL isn't valid. Include a full address, like https://example.com.")
    # Lazy import breaks a module-load cycle (platform_tools -> engine ->
    # extensions -> store); the SSRF guard is only needed at add time:
    from ..engine.types import ToolExecutionError
    from ..platform_tools import _assert_public_url, _resolve_host

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = _resolve_host(parts.hostname, port)
        _assert_public_url(url, addresses)
    except (ToolExecutionError, ipaddress.AddressValueError) as exc:
        # The shared SSRF guard raises ToolExecutionError; surface it as an
        # MCP config rejection so the endpoint maps it to a clean 422:
        raise McpError("That MCP server's address can't be used. Use a public URL, not a local or private address.") from exc
    headers = config.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise McpError("Headers must be a set of key-value pairs.")
    return {"url": url, "headers": headers or None}


def validate_user_skill(config: dict) -> dict:
    """
    Validating a user Skill config: a SKILL.md body (frontmatter optional) and
    optional extra text files. Returns the cleaned {skill_md, files}:
    """
    skill_md = (config or {}).get("skill_md")
    if not skill_md or not isinstance(skill_md, str):
        raise SkillError("Add the skill's SKILL.md content.")
    files = config.get("files") or {}
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        raise SkillError("Each extra file must be plain text, named by its filename.")
    return {"skill_md": skill_md, "files": files}


# ============================================================================
# CRUD
# ============================================================================

def add_user_extension(db: Session, owner_id: str, agent_name: str, kind: str, config: dict) -> AIUserExtension:
    """Validating + persisting one user extension (kind = 'mcp' | 'skill'):"""
    if kind == "mcp":
        clean = validate_user_mcp(config)
    elif kind == "skill":
        clean = validate_user_skill(config)
    else:
        raise McpError(f"'{kind}' isn't a supported extension type. Use 'mcp' or 'skill'.")

    row = AIUserExtension(owner_id=owner_id, agent_name=agent_name, kind=kind, config=clean)
    try:
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row


def list_user_extensions(db: Session, owner_id: str, agent_name: str) -> list[AIUserExtension]:
    """Listing an owner's extensions for one agent (owner-scoped, D54):"""
    return (
        db.query(AIUserExtension)
        .filter(AIUserExtension.owner_id == owner_id, AIUserExtension.agent_name == agent_name)
        .order_by(AIUserExtension.created_at)
        .all()
    )


def delete_user_extension(db: Session, owner_id: str, ext_id) -> bool:
    """Deleting one of the OWNER'S extensions (never another user's); True if removed:"""
    row = db.query(AIUserExtension).filter(
        AIUserExtension.id == ext_id, AIUserExtension.owner_id == owner_id,
    ).first()
    if row is None:
        return False
    try:
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


# ============================================================================
# Resolution (run time — into the same config types the dev catalog produces)
# ============================================================================

def _safe_id(row: AIUserExtension) -> str:
    """A filesystem/tool-name-safe id for a user extension (folder + tool prefix):"""
    return f"user_{row.id.hex}"


def resolve_user_mcp_configs(db: Session, owner_id: str, agent_name: str) -> list[MCPServerConfig]:
    """Turning an owner's user MCP rows into remote configs tagged origin='user':"""
    configs: list[MCPServerConfig] = []
    for row in list_user_extensions(db, owner_id, agent_name):
        if row.kind != "mcp":
            continue
        configs.append(MCPServerConfig(
            id=_safe_id(row), url=row.config.get("url"),
            headers=row.config.get("headers"), origin="user",
        ))
    return configs


def resolve_user_skill_configs(db: Session, owner_id: str, agent_name: str) -> list[SkillConfig]:
    """Turning an owner's user skill rows into SkillConfigs tagged origin='user':"""
    configs: list[SkillConfig] = []
    for row in list_user_extensions(db, owner_id, agent_name):
        if row.kind != "skill":
            continue
        skill_md = row.config.get("skill_md", "")
        files = {"SKILL.md": skill_md.encode()}
        for name, content in (row.config.get("files") or {}).items():
            files[name] = content.encode()
        meta = _parse_frontmatter(skill_md)
        configs.append(SkillConfig(
            id=_safe_id(row), description=str(meta.get("description", "")).strip(),
            files=files, origin="user",
        ))
    return configs
