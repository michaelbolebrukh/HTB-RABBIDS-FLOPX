"""
Skill catalog + progressive disclosure (Phase 9 Slice 2, D50/D53).

A Skill is a folder bundle whose entry point is SKILL.md (the agentskills.io
open spec): YAML frontmatter (name + description) followed by procedural
instructions, plus optional bundled files/scripts. Skills are CONTAINERIZED
(D53): the model reads a skill's body and runs its scripts inside the sandbox.

Progressive disclosure keeps the context lean:
- L1 metadata (name + description) — injected as a one-line preamble into the
  agent's instructions so the model knows the skill EXISTS and when to use it;
- L2 body — returned by the `open_skill` tool only when the model invokes it;
- L3 files/scripts — mounted into the sandbox at /skills/<id>/ so `code_execution`
  runs them on demand (their bytes never enter the model's context).

Dev skills are registered in code by id (D50), like the tool table. No
`pydantic_ai` import here (D3): this is plain catalog data + a registered fill.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..engine.tool_table import ToolContext, register_tool
from ..engine.types import EngineError, ToolExecutionError
from ..sandbox.types import SkillRef


logger = logging.getLogger(__name__)


class SkillError(EngineError):
    """A skill problem (unknown id, malformed bundle) — loud, never a silent skip."""


@dataclass(frozen=True)
class SkillConfig:
    """
    One registered skill: its id (the folder/reference name), the L1 metadata
    parsed from SKILL.md frontmatter, and the full bundle (paths -> bytes,
    SKILL.md included) that materializes into the sandbox:
    """
    id: str
    description: str
    files: dict[str, bytes] = field(default_factory=dict)
    origin: str = "dev"                    # dev (trusted) vs user (untrusted origin) — Slice 3

    def to_ref(self) -> SkillRef:
        """Turning this into the sandbox's SkillRef (mounted at /skills/<id>/):"""
        return SkillRef(name=self.id, files=dict(self.files))


_SKILL_CATALOG: dict[str, SkillConfig] = {}


def _parse_frontmatter(skill_md: str) -> dict:
    """
    Reading the YAML frontmatter block (--- ... ---) at the top of SKILL.md.
    Missing/blank frontmatter is tolerated (description falls back to empty):
    """
    text = skill_md.lstrip()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def register_skill(
    id: str,
    *,
    path: str | None = None,
    skill_md: str | None = None,
    files: dict[str, bytes] | None = None,
) -> None:
    """
    Registering a dev skill by id (code-only, D50). Either point at a bundle
    `path` (a folder containing SKILL.md), or pass `skill_md` (+ optional extra
    `files`) inline. The description comes from SKILL.md frontmatter; duplicate
    ids raise:
    """
    if id in _SKILL_CATALOG:
        raise SkillError(f"skill {id!r} is already registered")

    bundle: dict[str, bytes] = {}
    if path is not None:
        root = Path(path)
        md = root / "SKILL.md"
        if not md.exists():
            raise SkillError(f"skill {id!r} bundle {path!r} has no SKILL.md")
        for f in root.rglob("*"):
            if f.is_file():
                bundle[str(f.relative_to(root))] = f.read_bytes()
        skill_md = bundle["SKILL.md"].decode("utf-8", errors="replace")
    else:
        if skill_md is None:
            raise SkillError(f"skill {id!r} needs a bundle path or skill_md text")
        bundle["SKILL.md"] = skill_md.encode()
        for name, content in (files or {}).items():
            bundle[name] = content

    meta = _parse_frontmatter(skill_md)
    _SKILL_CATALOG[id] = SkillConfig(
        id=id, description=str(meta.get("description", "")).strip(), files=bundle, origin="dev",
    )


def get_skill(id: str) -> SkillConfig | None:
    """Looking up a dev skill by id (None when absent):"""
    return _SKILL_CATALOG.get(id)


def resolve_skills(ids: list[str]) -> list[SkillConfig]:
    """Resolving an agent spec's skill ids; an unknown id fails loudly (D33/D50):"""
    resolved: list[SkillConfig] = []
    for id in ids:
        entry = get_skill(id)
        if entry is None:
            raise SkillError(f"skill {id!r} is not registered (registered: {sorted(_SKILL_CATALOG)})")
        resolved.append(entry)
    return resolved


def build_skill_preamble(configs: list[SkillConfig]) -> str:
    """
    Composing the always-on L1 metadata block (D53): the model learns each
    skill's id + description and how to load it, at ~one line each — the body
    and files stay out of context until `open_skill` / `code_execution`:
    """
    lines = [
        "Available skills — call open_skill(skill_id) to read a skill's instructions "
        "before using it; its bundled scripts are mounted at /skills/<skill_id>/ inside "
        "code_execution:",
    ]
    for c in configs:
        lines.append(f"- {c.id}: {c.description}" if c.description else f"- {c.id}")
    return "\n".join(lines)


# ============================================================================
# open_skill — the L2 disclosure tool (registered at import, D33)
# ============================================================================

async def open_skill(ctx: ToolContext, skill_id: str) -> dict:
    """
    Load a skill's full instructions before using it. Returns the skill's
    SKILL.md body and the list of its bundled files (mounted at /skills/<skill_id>/
    inside code_execution, which you call to run them).
    """
    for ref in ctx.skills:
        if ref.name == skill_id:
            body = ref.files.get("SKILL.md", b"").decode("utf-8", errors="replace")
            others = sorted(p for p in ref.files if p != "SKILL.md")
            return {
                "skill": skill_id,
                "instructions": body,
                "files": others,
                "mounted_at": f"/skills/{skill_id}/",
            }
    raise ToolExecutionError(f"skill {skill_id!r} is not attached to this agent")


register_tool(
    "open_skill",
    open_skill,
    description=(
        "Load a skill's full instructions (its SKILL.md) before using it, and list its "
        "bundled files. The files are mounted at /skills/<skill_id>/ inside code_execution."
    ),
)
