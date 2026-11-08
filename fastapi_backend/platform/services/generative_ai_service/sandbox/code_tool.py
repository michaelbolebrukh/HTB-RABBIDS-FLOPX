"""
code_execution fill — closing the P5 gap (Phase 6, D38/D34).

The client-side implementation behind the dual name "code_execution": when
the run model's registry entry lacks the native flag, the D34 split routes
the name here, and model-generated code runs in the configured sandbox
backend instead of the provider's interpreter.

Dormant state (D38): with no `AI_SANDBOX_BACKEND` configured the tool raises
ToolExecutionError — the adapter translates that into a tool result the model
can read and react to ("this deployment cannot execute code"), while the
deployment itself stays fully off, never half-working.

Registered at import (D33: registration is code); the package __init__
imports this module for the side effect, exactly like platform_tools.
"""

from __future__ import annotations

import logging

from ..engine.tool_table import ToolContext, register_tool
from ..engine.types import ToolExecutionError
from .port import get_backend
from .types import SandboxLimits, WorkspaceSpec


logger = logging.getLogger(__name__)

# language -> (workspace filename, exec argv). Small on purpose — a language
# is added when a real agent needs it, with its interpreter in the image:
_LANGUAGES: dict[str, tuple[str, list[str]]] = {
    "python": ("main.py", ["python", "main.py"]),
    "bash": ("main.sh", ["bash", "main.sh"]),
}


async def code_execution(ctx: ToolContext, code: str, language: str = "python") -> dict:
    """
    Run code in an isolated sandbox. The code executes with no network access
    and strict resource limits; returns exit_code, stdout, stderr and timing.
    """
    runner = _LANGUAGES.get(language)
    if runner is None:
        raise ToolExecutionError(
            f"unsupported language {language!r}; supported: {', '.join(sorted(_LANGUAGES))}"
        )

    backend = get_backend()
    if backend is None:
        # The D38 dormant state — loud, typed, and reactable by the model:
        raise ToolExecutionError("code execution is not configured on this deployment")

    filename, argv = runner
    limits = SandboxLimits()
    # Mounting any attached skills' bundles at /skills/<id>/ (P9, D53) so a
    # skill's scripts are runnable from the code the model writes; network stays
    # off (the sandbox default) — skills are containerized but not networked in v1:
    session = await backend.start(
        WorkspaceSpec(files={filename: code.encode()}, skills=list(ctx.skills)), limits,
    )
    try:
        result = await session.exec(argv)
    finally:
        await session.close()              # Nothing persists, success or not

    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
    }


# Registering at import (D33) — the docstring's first paragraph is what the
# model reads, so it describes the TOOL, not the plumbing:
register_tool(
    "code_execution",
    code_execution,
    description=(
        "Execute code in an isolated sandbox (no network, strict CPU/memory/time limits). "
        "Returns exit_code, stdout, stderr, and duration_ms. Supported languages: python, bash."
    ),
)
