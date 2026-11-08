"""
Sandbox types — workspace, limits, results (Phase 6, D38).

Pure data for the sandbox port: what to materialize into an isolated
workspace (requirement 11 — AGENTS.md rules, agentskills.io skills, MCP
config), what resource ceilings apply, and what an execution returned. No
Docker, no SDK, no I/O in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..engine.types import EngineError


# ============================================================================
# LIMITS + RESULTS
# ============================================================================

class SandboxLimits(BaseModel):
    """
    Resource ceilings for one sandbox session. Defaults are the hard-boundary
    posture from PHASE6's security notes: short, small, and OFFLINE — network
    access is an explicit allowlist opt-in, never a default:
    """
    max_seconds: int = 60                 # Wall-clock ceiling per exec
    max_memory_mb: int = 512
    network: Literal["none", "allowlist"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list)
    # stdout/stderr byte caps — a bomb of output must not flood the model's
    # context (it returns as a tool result); excess is truncated and flagged:
    max_output_bytes: int = 64_000


@dataclass
class ExecResult:
    """The outcome of one command execution inside the sandbox."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False               # True when an output cap clipped stdout/stderr


# ============================================================================
# WORKSPACE (requirement 11 / D14 — the open-spec artifacts)
# ============================================================================

class SkillRef(BaseModel):
    """
    One agentskills.io skill to materialize: a named folder of files whose
    entry point is SKILL.md (the open spec's contract). `files` maps paths
    RELATIVE to the skill folder -> content:
    """
    name: str
    files: dict[str, bytes] = Field(default_factory=dict)


class WorkspaceSpec(BaseModel):
    """
    Everything a sandbox workspace starts with. The materializer (workspace.py)
    turns this into concrete files so an agent running INSIDE the sandbox
    finds the same open-spec artifacts it would in any repo:
    """
    files: dict[str, bytes] = Field(default_factory=dict)   # Seed files (uploads to process, etc.)
    agents_md: str | None = None          # Rules the in-sandbox agent must adhere to (AGENTS.md)
    skills: list[SkillRef] = Field(default_factory=list)    # agentskills.io folders
    mcp_servers: dict[str, str] = Field(default_factory=dict)  # name -> URL, written as MCP config


# ============================================================================
# ERRORS
# ============================================================================

class SandboxError(EngineError):
    """Base for sandbox-port errors (engine error family for uniform handling)."""


class SandboxNotConfiguredError(SandboxError):
    """
    Raised when execution is requested but no backend is configured
    (`AI_SANDBOX_BACKEND` unset) — the D38 dormant state: fully off, loudly
    typed, never half-working (config-minimalism fail-closed):
    """


class SandboxExecutionError(SandboxError):
    """The backend itself failed (daemon missing, container died) — not the code's exit status."""
