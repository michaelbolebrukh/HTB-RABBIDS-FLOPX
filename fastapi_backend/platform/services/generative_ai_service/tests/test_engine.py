"""
Engine tests: end-to-end run against TestModel (offline), transcript
persistence, multi-turn continuity, mid-conversation model switching, and the
run_agent convenience wrapper.
"""

import pytest
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    RunCompleted,
    RunDeps,
    RunStarted,
    TextDelta,
    UserInput,
    run_agent,
    stream_agent,
)
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.transcript import create_conversation, full_history


# These are the CHAT-PATH tests: tools would add noise (TestModel calls every
# tool it is given, and rag_search injects search_conversation by default
# since P5), so the specs here disable them. Tool behavior has its own tests:
_NO_TOOLS = AgentFeatures(rag_search=False)


@pytest.fixture()
def fake_model(monkeypatch):
    """Replacing build_model with a TestModel factory so runs never hit the network:"""
    def _install(output_text="ok"):
        def fake_build(model_id, fallbacks=None):
            return BuiltModel(
                model=TestModel(custom_output_text=output_text),
                requested_id=model_id, served_by=model_id.split(":")[0],
            )
        monkeypatch.setattr(engine_mod, "build_model", fake_build)
    return _install


@pytest.mark.asyncio
async def test_end_to_end_run_streams_and_persists(db, fake_model):
    fake_model("Hello from the engine!")
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="assistant", model="openai:gpt-5.2", instructions="Be helpful.", features=_NO_TOOLS)
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [ev async for ev in stream_agent(spec, deps, UserInput(text="Say hello"))]
    types = [e.type for e in events]
    assert types[0] == "run_started"
    assert "text_delta" in types
    assert types[-1] == "run_completed"

    # Transcript holds the user turn and the assistant turn:
    hist = full_history(db, conv.id, owner_id="u1")
    assert [i.role.value for i in hist] == ["user", "assistant"]
    assert hist[1].extract_text() == "Hello from the engine!"
    # The assistant item is tagged with the serving model and carries raw:
    assert hist[1].model_used == "openai:gpt-5.2" and hist[1].raw is not None


@pytest.mark.asyncio
async def test_multi_turn_continuity(db, fake_model):
    fake_model("reply")
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="assistant", model="openai:gpt-5.2", features=_NO_TOOLS)
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    await run_agent(spec, deps, UserInput(text="first"))
    await run_agent(spec, deps, UserInput(text="second"))

    hist = full_history(db, conv.id, owner_id="u1")
    # Two turns => 4 items, in order:
    assert [i.role.value for i in hist] == ["user", "assistant", "user", "assistant"]
    assert [i.extract_text() for i in hist if i.role.value == "user"] == ["first", "second"]


@pytest.mark.asyncio
async def test_mid_conversation_model_switch(db, monkeypatch):
    # Turn 1 on openai, turn 2 on anthropic — same conversation, one transcript:
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    def build_for(model_id, fallbacks=None):
        return BuiltModel(model=TestModel(custom_output_text=f"from {model_id}"),
                          requested_id=model_id, served_by=model_id.split(":")[0])
    monkeypatch.setattr(engine_mod, "build_model", build_for)

    await run_agent(AgentSpec(name="a", model="openai:gpt-5.2", features=_NO_TOOLS), deps, UserInput(text="q1"))
    await run_agent(AgentSpec(name="a", model="anthropic:claude-sonnet-4-6", features=_NO_TOOLS), deps, UserInput(text="q2"))

    hist = full_history(db, conv.id, owner_id="u1")
    assistants = [i for i in hist if i.role.value == "assistant"]
    # Each assistant turn records the model that produced it (requirement 1):
    assert assistants[0].model_used == "openai:gpt-5.2"
    assert assistants[1].model_used == "anthropic:claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_run_agent_returns_result(db, fake_model):
    fake_model("the answer")
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="assistant", model="openai:gpt-5.2", features=_NO_TOOLS)
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    result = await run_agent(spec, deps, UserInput(text="question"))
    assert result.output == "the answer"
    assert len(result.item_ids) >= 1
    assert result.model_served == "openai:gpt-5.2"
