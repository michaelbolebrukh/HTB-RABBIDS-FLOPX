"""
Sandbox tests (P6, D38): workspace materialization (requirement 11), backend
selection + the dormant default, the code_execution fill against a fake
backend, and an env-gated Docker integration test (never in CI). All offline
except the explicitly opted-in Docker case.
"""

import json
import os

import pytest

from platform.services.generative_ai_service.engine.tool_table import ToolContext, registered_tool_names
from platform.services.generative_ai_service.engine.types import AgentFeatures, ToolExecutionError
from platform.services.generative_ai_service.sandbox import (
    ExecResult,
    SandboxError,
    SandboxLimits,
    SkillRef,
    WorkspaceSpec,
    get_backend,
    materialize_files,
)
from platform.services.generative_ai_service.sandbox import code_tool as code_tool_mod
from platform.services.generative_ai_service.sandbox import port as sandbox_port_mod
from platform.services.generative_ai_service.sandbox.code_tool import code_execution
from platform.services.generative_ai_service.sandbox.port import SandboxSession


def _ctx():
    """A minimal ToolContext (the sandbox tools never touch the DB):"""
    from uuid import uuid4
    return ToolContext(db=None, owner_id="u1", conversation_id=uuid4(), features=AgentFeatures())


# ============================================================================
# Workspace materialization (requirement 11 / D14)
# ============================================================================

def test_materialize_writes_the_open_spec_artifacts():
    workspace = WorkspaceSpec(
        files={"data/input.csv": b"a,b\n1,2"},
        agents_md="# Rules\nNever delete files.",
        skills=[SkillRef(name="report", files={"SKILL.md": b"# Report skill", "run.py": b"print(1)"})],
        mcp_servers={"search": "https://mcp.example/sse"},
    )
    files = materialize_files(workspace)
    assert files["AGENTS.md"] == b"# Rules\nNever delete files."
    assert files["skills/report/SKILL.md"] == b"# Report skill"
    assert files["skills/report/run.py"] == b"print(1)"
    assert files["data/input.csv"] == b"a,b\n1,2"
    config = json.loads(files[".mcp.json"])
    assert config == {"mcpServers": {"search": {"url": "https://mcp.example/sse"}}}


def test_materialize_rejects_escaping_paths_and_skill_without_manifest():
    # A hostile relative path must never leave the workspace root:
    with pytest.raises(SandboxError, match="escapes"):
        materialize_files(WorkspaceSpec(files={"../evil.sh": b"x"}))
    # A skill without SKILL.md is not a skill per agentskills.io:
    with pytest.raises(SandboxError, match="SKILL.md"):
        materialize_files(WorkspaceSpec(skills=[SkillRef(name="broken", files={"run.py": b"x"})]))


# ============================================================================
# Backend selection (D38 — dormant by default, loud on typos)
# ============================================================================

def test_backend_selection_states(monkeypatch):
    # Default: unset -> dormant (None), nothing constructed:
    monkeypatch.setattr(sandbox_port_mod.settings, "AI_SANDBOX_BACKEND", None)
    assert get_backend() is None

    # A typo must fail loudly, never silently disable sandboxing:
    monkeypatch.setattr(sandbox_port_mod.settings, "AI_SANDBOX_BACKEND", "dokcer")
    with pytest.raises(SandboxError, match="dokcer"):
        get_backend()

    # The supported value resolves the Docker backend:
    monkeypatch.setattr(sandbox_port_mod.settings, "AI_SANDBOX_BACKEND", "docker")
    from platform.services.generative_ai_service.sandbox.docker_backend import DockerBackend
    assert isinstance(get_backend(), DockerBackend)


# ============================================================================
# The code_execution fill (closing the P5 gap)
# ============================================================================

class _FakeSession(SandboxSession):
    """A scripted sandbox session recording the lifecycle the fill drives:"""

    def __init__(self):
        self.execs: list[list[str]] = []
        self.files: dict[str, bytes] = {}
        self.closed = False

    async def exec(self, command, timeout_s=None):
        self.execs.append(list(command))
        return ExecResult(exit_code=0, stdout="6\n", stderr="", duration_ms=12)

    async def put_file(self, path, content):
        self.files[path] = content

    async def get_file(self, path):
        return self.files[path]

    async def close(self):
        self.closed = True


class _FakeBackend:
    """A SandboxBackend that captures what start() received:"""

    def __init__(self):
        self.sessions: list[_FakeSession] = []
        self.workspaces: list[WorkspaceSpec] = []
        self.limits: list[SandboxLimits] = []

    async def start(self, workspace, limits):
        self.workspaces.append(workspace)
        self.limits.append(limits)
        session = _FakeSession()
        self.sessions.append(session)
        return session


def test_code_execution_is_registered_in_the_tool_table():
    """The D33 wiring: the fill exists on every deployment (dormant or not):"""
    assert "code_execution" in registered_tool_names()


@pytest.mark.asyncio
async def test_code_execution_dormant_without_backend(monkeypatch):
    monkeypatch.setattr(code_tool_mod, "get_backend", lambda: None)
    # The D38 dormant state: typed, reactable by the model, nothing half-works:
    with pytest.raises(ToolExecutionError, match="not configured"):
        await code_execution(_ctx(), code="print(1+2)")


@pytest.mark.asyncio
async def test_code_execution_runs_and_always_closes(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(code_tool_mod, "get_backend", lambda: backend)

    result = await code_execution(_ctx(), code="print(2*3)")
    assert result == {"exit_code": 0, "stdout": "6\n", "stderr": "", "duration_ms": 12, "truncated": False}

    # The code entered as the workspace seed file and ran with the right argv:
    (workspace,) = backend.workspaces
    assert workspace.files == {"main.py": b"print(2*3)"}
    (session,) = backend.sessions
    assert session.execs == [["python", "main.py"]]
    assert session.closed                       # Destroyed after the run
    # The default limits are the hard-boundary posture (no network):
    assert backend.limits[0].network == "none"

    # An unsupported language is a typed, reactable failure:
    with pytest.raises(ToolExecutionError, match="unsupported language"):
        await code_execution(_ctx(), code="puts 1", language="ruby")


@pytest.mark.asyncio
async def test_code_execution_closes_even_when_exec_dies(monkeypatch):
    class _DyingSession(_FakeSession):
        async def exec(self, command, timeout_s=None):
            raise RuntimeError("container vanished")

    class _DyingBackend(_FakeBackend):
        async def start(self, workspace, limits):
            session = _DyingSession()
            self.sessions.append(session)
            return session

    backend = _DyingBackend()
    monkeypatch.setattr(code_tool_mod, "get_backend", lambda: backend)
    with pytest.raises(RuntimeError, match="vanished"):
        await code_execution(_ctx(), code="print(1)")
    assert backend.sessions[0].closed           # Never leaks a container


# ============================================================================
# Docker integration (opt-in only — needs a daemon; never runs in CI)
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("AI_SANDBOX_IT") != "1",
                    reason="set AI_SANDBOX_IT=1 on a docker-capable host to run")
async def test_docker_backend_end_to_end():
    from platform.services.generative_ai_service.sandbox.docker_backend import DockerBackend

    backend = DockerBackend()
    session = await backend.start(
        WorkspaceSpec(files={"main.py": b"print(open('AGENTS.md').read())"},
                      agents_md="rule: be kind"),
        SandboxLimits(max_seconds=30),
    )
    try:
        result = await session.exec(["python", "main.py"])
        assert result.exit_code == 0 and "be kind" in result.stdout
        # No network — the boundary actually holds:
        offline = await session.exec(["python", "-c", "import urllib.request as u; u.urlopen('https://example.com', timeout=3)"])
        assert offline.exit_code != 0
    finally:
        await session.close()
