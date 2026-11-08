"""
Skill extensibility tests (P9 Slice 2, D53). Progressive disclosure: L1 metadata
reaches the model's instructions, the body loads only via open_skill, bundles
mount into code_execution, and unknown/unattached skills fail loudly.
"""

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    RunDeps,
    ToolContext,
    UserInput,
)
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.extensions import skill_catalog
from platform.services.generative_ai_service.extensions.skill_catalog import (
    build_skill_preamble,
    open_skill,
    register_skill,
    resolve_skills,
)
from platform.services.generative_ai_service.engine.types import ToolExecutionError
from platform.services.generative_ai_service.sandbox import code_tool as code_tool_mod
from platform.services.generative_ai_service.sandbox.types import SandboxLimits, SkillRef, WorkspaceSpec
from platform.services.generative_ai_service.transcript import create_conversation, full_history
from platform.services.generative_ai_service.transcript.types import ItemKind


_SKILL_MD = """---
name: Cooking
description: Plans meals and recipes.
---
# Cooking skill

Step 1: pick a cuisine. Run recipe.py to compute portions.
"""


@pytest.fixture()
def clean_skills():
    """Snapshotting the dev skill catalog so per-test registrations don't leak:"""
    snapshot = dict(skill_catalog._SKILL_CATALOG)
    yield
    skill_catalog._SKILL_CATALOG.clear()
    skill_catalog._SKILL_CATALOG.update(snapshot)


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
# Catalog + L1 metadata
# ============================================================================

def test_frontmatter_description_is_parsed(clean_skills):
    register_skill("cooking", skill_md=_SKILL_MD, files={"recipe.py": b"print('cook')"})
    (cfg,) = resolve_skills(["cooking"])
    assert cfg.description == "Plans meals and recipes."
    assert set(cfg.files) == {"SKILL.md", "recipe.py"}


def test_l1_preamble_lists_id_and_description(clean_skills):
    register_skill("cooking", skill_md=_SKILL_MD)
    preamble = build_skill_preamble(resolve_skills(["cooking"]))
    assert "open_skill" in preamble
    assert "cooking: Plans meals and recipes." in preamble


@pytest.mark.asyncio
async def test_l1_metadata_reaches_the_model_instructions(db, monkeypatch, clean_skills):
    register_skill("cooking", skill_md=_SKILL_MD)
    captured = {}

    async def stream_function(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "done"

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, _ = await _drain(engine_mod.stream_agent(_spec(skills=["cooking"]), deps, UserInput(text="hi")))
    assert events[-1].type == "run_completed"
    # The skill exists in the model's context (L1) without its body:
    assert "cooking" in captured["instructions"]
    assert "Plans meals and recipes." in captured["instructions"]
    assert "pick a cuisine" not in captured["instructions"]   # L2 body stays out


# ============================================================================
# L2 disclosure via open_skill
# ============================================================================

@pytest.mark.asyncio
async def test_open_skill_returns_body_and_file_list(db, monkeypatch, clean_skills):
    register_skill("cooking", skill_md=_SKILL_MD, files={"recipe.py": b"print('cook')"})

    async def stream_function(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "open_skill" and part.__class__.__name__ == "ToolReturnPart":
                    assert "pick a cuisine" in str(part.content)   # L2 body disclosed
                    assert "recipe.py" in str(part.content)        # L3 files listed
                    yield "loaded"
                    return
        yield {1: DeltaToolCall(name="open_skill", json_args='{"skill_id": "cooking"}', tool_call_id="s1")}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, by_type = await _drain(engine_mod.stream_agent(_spec(skills=["cooking"]), deps, UserInput(text="cook")))
    assert events[-1].type == "run_completed"
    assert by_type["tool_call_started"].tool_name == "open_skill"

    hist = full_history(db, conv.id, owner_id="u1")
    result = next(i for i in hist if i.kind == ItemKind.tool_result)
    # The open_skill result is a structured dict (DataPart), not free text:
    data = next(p.data for p in result.parts if getattr(p, "type", None) == "data")
    assert "Cooking skill" in data["instructions"]


@pytest.mark.asyncio
async def test_open_skill_for_unattached_skill_errors():
    # The tool only discloses skills actually attached to THIS agent's run:
    ctx = ToolContext(
        db=None, owner_id="u1", conversation_id=None, features=AgentFeatures(),
        skills=[SkillRef(name="cooking", files={"SKILL.md": b"x"})],
    )
    with pytest.raises(ToolExecutionError, match="not attached"):
        await open_skill(ctx, skill_id="gardening")


# ============================================================================
# L3 — bundles mount into code_execution
# ============================================================================

class _FakeSession:
    def __init__(self):
        self.closed = False

    async def exec(self, command, timeout_s=None):
        from platform.services.generative_ai_service.sandbox.types import ExecResult
        return ExecResult(exit_code=0, stdout="ran", stderr="", duration_ms=1)

    async def put_file(self, path, content): ...
    async def get_file(self, path): return b""
    async def close(self): self.closed = True


class _FakeBackend:
    def __init__(self):
        self.workspaces = []

    async def start(self, workspace, limits):
        self.workspaces.append(workspace)
        return _FakeSession()


@pytest.mark.asyncio
async def test_skill_bundle_mounts_into_code_execution(db, monkeypatch, clean_skills):
    register_skill("cooking", skill_md=_SKILL_MD, files={"recipe.py": b"print('portions')"})
    backend = _FakeBackend()
    monkeypatch.setattr(code_tool_mod, "get_backend", lambda: backend)

    async def stream_function(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "code_execution" and part.__class__.__name__ == "ToolReturnPart":
                    yield "done"
                    return
        yield {1: DeltaToolCall(
            name="code_execution",
            json_args='{"code": "import subprocess; subprocess.run([\'python\',\'/skills/cooking/recipe.py\'])"}',
            tool_call_id="c1",
        )}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    spec = _spec(skills=["cooking"], tools=["code_execution"])
    events, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="run it")))
    assert events[-1].type == "run_completed"

    # The skill bundle rode into the sandbox session, mounted at /skills/cooking/:
    (workspace,) = backend.workspaces
    mounted = {ref.name: ref for ref in workspace.skills}
    assert "cooking" in mounted
    assert "recipe.py" in mounted["cooking"].files


# ============================================================================
# Loud failure
# ============================================================================

@pytest.mark.asyncio
async def test_unknown_skill_fails_loudly(db, monkeypatch, clean_skills):
    _use(TestModel(custom_output_text="x"), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, _ = await _drain(engine_mod.stream_agent(_spec(skills=["nope"]), deps, UserInput(text="hi")))
    assert events[-1].type == "run_failed"
    assert "nope" in events[-1].error and "SkillError" in events[-1].error
