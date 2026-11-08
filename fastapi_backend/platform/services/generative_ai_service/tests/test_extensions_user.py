"""
User-added extension tests (P9 Slice 3, D51/D54). BYO MCP/skills gated by the
agent's per-toggle, scoped per owner_id, remote-only + SSRF-guarded for MCP,
and loaded at run time only when enabled. No allowlist — the toggle is the gate.
"""

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import AgentFeatures, AgentSpec, RunDeps, UserInput
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.extensions import (
    add_user_extension,
    list_user_extensions,
    delete_user_extension,
    resolve_user_skill_configs,
)
from platform.services.generative_ai_service.extensions.mcp_catalog import MCPServerConfig, McpError
from platform.services.generative_ai_service.extensions.skill_catalog import SkillError
from platform.services.generative_ai_service.extensions import store as store_mod
from platform.services.generative_ai_service.transcript import create_conversation


_SKILL_MD = "---\nname: Mine\ndescription: My own skill.\n---\n# My skill\nDo the thing.\n"


def _use(model, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=model, requested_id=model_id, served_by="test"),
    )


def _spec(**kw):
    kw.setdefault("features", AgentFeatures(rag_search=False, summarization=False))
    return AgentSpec(name="a", model="test:test", **kw)


async def _drain(stream):
    events = [e async for e in stream]
    return events, {e.type: e for e in events}


# ============================================================================
# Validation — remote-only + SSRF (D52)
# ============================================================================

def test_user_mcp_must_be_remote_public_http():
    # Non-http scheme rejected:
    with pytest.raises(McpError, match="public http"):
        store_mod.validate_user_mcp({"url": "stdio:///bin/server"})
    # Loopback / private / metadata addresses rejected (SSRF guard reuse):
    for bad in ("http://localhost:9000", "http://127.0.0.1", "http://169.254.169.254", "http://10.0.0.5"):
        with pytest.raises(McpError):
            store_mod.validate_user_mcp({"url": bad})
    # A public literal passes and is normalized:
    clean = store_mod.validate_user_mcp({"url": "https://93.184.216.34/mcp", "headers": {"x": "y"}})
    assert clean == {"url": "https://93.184.216.34/mcp", "headers": {"x": "y"}}


def test_user_skill_requires_skill_md():
    with pytest.raises(SkillError, match="SKILL.md"):
        store_mod.validate_user_skill({"files": {"a.py": "x"}})
    clean = store_mod.validate_user_skill({"skill_md": _SKILL_MD, "files": {"run.py": "print(1)"}})
    assert clean["skill_md"] == _SKILL_MD and clean["files"] == {"run.py": "print(1)"}


# ============================================================================
# CRUD + per-owner isolation (D54)
# ============================================================================

def test_extensions_are_scoped_to_the_owner(db):
    add_user_extension(db, "owner_a", "a", "skill", {"skill_md": _SKILL_MD, "files": {}})
    add_user_extension(db, "owner_b", "a", "skill", {"skill_md": _SKILL_MD, "files": {}})

    assert len(list_user_extensions(db, "owner_a", "a")) == 1
    assert len(list_user_extensions(db, "owner_b", "a")) == 1
    # owner_a can never delete owner_b's extension:
    b_ext = list_user_extensions(db, "owner_b", "a")[0]
    assert delete_user_extension(db, "owner_a", b_ext.id) is False
    assert delete_user_extension(db, "owner_b", b_ext.id) is True
    assert list_user_extensions(db, "owner_b", "a") == []


# ============================================================================
# Engine merge — loaded only when the toggle is on
# ============================================================================

@pytest.mark.asyncio
async def test_user_skill_loads_only_when_toggle_on(db, monkeypatch):
    add_user_extension(db, "u1", "a", "skill", {"skill_md": _SKILL_MD, "files": {}})
    captured = {}

    async def stream_function(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "ok"

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    # Toggle OFF: the user's skill is invisible to the run:
    await _drain(engine_mod.stream_agent(_spec(allow_user_skills=False), deps, UserInput(text="hi")))
    assert "My own skill." not in captured["instructions"]

    # Toggle ON: the user's skill's L1 metadata reaches the model:
    conv2 = create_conversation(db, owner_id="u1")
    deps2 = RunDeps(db=db, owner_id="u1", conversation_id=conv2.id)
    await _drain(engine_mod.stream_agent(_spec(allow_user_skills=True), deps2, UserInput(text="hi")))
    assert "My own skill." in captured["instructions"]


@pytest.mark.asyncio
async def test_user_skill_not_leaked_across_owners(db, monkeypatch):
    add_user_extension(db, "owner_a", "a", "skill", {"skill_md": _SKILL_MD, "files": {}})
    captured = {}

    async def stream_function(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "ok"

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    # owner_b runs the SAME agent with the toggle on — owner_a's skill must not appear:
    conv = create_conversation(db, owner_id="owner_b")
    deps = RunDeps(db=db, owner_id="owner_b", conversation_id=conv.id)
    await _drain(engine_mod.stream_agent(_spec(allow_user_skills=True), deps, UserInput(text="hi")))
    assert "My own skill." not in captured["instructions"]


@pytest.mark.asyncio
async def test_user_mcp_merges_and_is_provenance_labeled(db, monkeypatch):
    # User MCP is remote-only in production; for an offline end-to-end we inject
    # an in-process server config the way the store's resolver would (origin=user):
    from mcp.server.fastmcp import FastMCP
    srv = FastMCP("mine")

    @srv.tool()
    def echo(text: str) -> str:
        """Echo text."""
        return f"echo: {text}"

    def fake_resolve(db_, owner, agent):
        return [MCPServerConfig(id="user_abc", server=srv._mcp_server, origin="user")]

    monkeypatch.setattr(engine_mod, "resolve_user_mcp_configs", fake_resolve)

    async def stream_function(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "mcp__user_abc__echo" and part.__class__.__name__ == "ToolReturnPart":
                    yield "done"
                    return
        # The user tool is provenance-labeled so it is never taken for a platform tool:
        tool = next(t for t in info.function_tools if t.name == "mcp__user_abc__echo")
        assert "user-added" in (tool.description or "")
        yield {1: DeltaToolCall(name="mcp__user_abc__echo", json_args='{"text": "hi"}', tool_call_id="u1")}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, by_type = await _drain(engine_mod.stream_agent(_spec(allow_user_mcp=True), deps, UserInput(text="go")))
    assert events[-1].type == "run_completed"
    assert by_type["tool_call_started"].tool_name == "mcp__user_abc__echo"


@pytest.mark.asyncio
async def test_user_mcp_ignored_when_toggle_off(db, monkeypatch):
    called = {"resolve": False}

    def fake_resolve(db_, owner, agent):
        called["resolve"] = True
        return []

    monkeypatch.setattr(engine_mod, "resolve_user_mcp_configs", fake_resolve)
    _use(TestModel(custom_output_text="x"), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    await _drain(engine_mod.stream_agent(_spec(allow_user_mcp=False), deps, UserInput(text="hi")))
    assert called["resolve"] is False, "user MCP was loaded despite the toggle being off"
