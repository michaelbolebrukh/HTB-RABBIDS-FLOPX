"""
Sandbox package — containerized agent execution public surface (Phase 6, D38).

Callers import the port (session/backend contracts), the types, and the
workspace materializer from here. `code_tool` self-registers the D34
`code_execution` fill when the service package imports it (see the service
__init__) — nothing here talks to Docker until a backend is configured.
"""

from .port import SandboxBackend, SandboxSession, get_backend
from .types import (
    ExecResult,
    SandboxError,
    SandboxExecutionError,
    SandboxLimits,
    SandboxNotConfiguredError,
    SkillRef,
    WorkspaceSpec,
)
from .workspace import materialize_files


__all__ = [
    "ExecResult", "SandboxBackend", "SandboxError", "SandboxExecutionError",
    "SandboxLimits", "SandboxNotConfiguredError", "SandboxSession", "SkillRef",
    "WorkspaceSpec", "get_backend", "materialize_files",
]
