"""
Sandbox port — SandboxSession + pluggable backends (Phase 6, D38).

The port is deliberately tiny: a backend starts a session from a workspace
and limits; a session executes commands and moves files; closing destroys
everything (nothing persists on the host). Backends are pluggable behind
`get_backend()`:

- `AI_SANDBOX_BACKEND` unset -> DORMANT: `get_backend()` returns None and the
  `code_execution` fill raises its typed error (config-minimalism fail-closed
  — Render's runtime has no Docker daemon, so prod ships dormant until a
  hosted backend is chosen; that is a later org-services decision).
- `AI_SANDBOX_BACKEND=docker` -> the Docker CLI backend (dev machines and any
  docker-capable host).
- An unknown value raises loudly at first use — a typo must never silently
  disable sandboxing while looking configured.

The port shape (start/exec/put/get/close) is what keeps O12 open: a hosted
provider (E2B/Modal) or a Claude-Agent-SDK/Cursor-SDK backend implements the
same protocol without a new abstraction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from .types import ExecResult, SandboxError, SandboxLimits, WorkspaceSpec
from infrastructure.settings import settings


class SandboxSession(ABC):
    """One live isolated execution environment (destroyed on close):"""

    @abstractmethod
    async def exec(self, command: list[str], timeout_s: int | None = None) -> ExecResult:
        """Running one command inside the sandbox under the session's limits."""

    @abstractmethod
    async def put_file(self, path: str, content: bytes) -> None:
        """Writing a file into the sandbox workspace (path relative to its root)."""

    @abstractmethod
    async def get_file(self, path: str) -> bytes:
        """Reading a file back out of the sandbox workspace."""

    @abstractmethod
    async def close(self) -> None:
        """Destroying the sandbox — nothing survives on the host (idempotent)."""


class SandboxBackend(Protocol):
    """The pluggable factory: workspace + limits -> a running session."""

    async def start(self, workspace: WorkspaceSpec, limits: SandboxLimits) -> SandboxSession: ...


def get_backend() -> SandboxBackend | None:
    """
    Resolving the configured backend (D38). None = dormant; unknown values
    fail loudly (never a typo that half-disables the sandbox silently):
    """
    name = getattr(settings, "AI_SANDBOX_BACKEND", None)
    if name is None or not str(name).strip():
        return None
    name = str(name).strip().lower()
    if name == "docker":
        from .docker_backend import DockerBackend
        return DockerBackend()
    raise SandboxError(f"unknown AI_SANDBOX_BACKEND {name!r} (supported: docker)")
