"""
Adapter + engine tool tests (P5): ctx injection through bind_tools (D11),
expected-failure translation, D34 capability selection, D20 output-mode
golden branches, the requirement-7 acceptance run (answering from
summarized-away content via search_conversation), and the D35 background
embedding backfill. All offline — FunctionModel/TestModel drive the runs.
"""

import asyncio

import pytest
from pydantic_ai import ModelRetry, NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.capabilities import NativeTool, Thinking, WebFetch, WebSearch
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import adapter as adapter_mod
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    MediaModels,
    RunDeps,
    ToolContext,
    UserInput,
)
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.engine.tool_table import RegisteredTool
from platform.services.generative_ai_service.engine.types import ToolExecutionError
from platform.services.generative_ai_service.models import AISpendEvent, AITranscriptItem
from platform.services.generative_ai_service.registry.schema import Capabilities, ModelInfo
from platform.services.generative_ai_service.transcript import append_item, create_conversation, full_history
from platform.services.generative_ai_service.transcript.types import (
    Item,
    ItemKind,
    Role,
    TextPart as OurTextPart,
    Usage,
)


def _ctx(db, conversation_id, **features):
    """Building a ToolContext as the engine would (D11 injection)."""
    return ToolContext(
        db=db, owner_id="u1", conversation_id=conversation_id,
        features=AgentFeatures(**features),
    )


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope."""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


# ============================================================================
# bind_tools — ctx injection (D11)
# ============================================================================

@pytest.mark.asyncio
async def test_bound_tool_schema_hides_ctx_and_injects_it(db):
    conv = create_conversation(db, owner_id="u1")
    seen = {}

    async def spy_tool(ctx: ToolContext, query: str, k: int = 3) -> str:
        """Recording what arrived so injection can be asserted."""
        seen["ctx"] = ctx
        seen["query"] = query
        seen["k"] = k
        return "done"

    entry = RegisteredTool(name="spy_tool", fn=spy_tool, description="spy")
    ctx = _ctx(db, conv.id)
    (tool,) = adapter_mod.bind_tools([entry], ctx)

    model = TestModel()
    agent = adapter_mod.Agent(model, name="t", tools=[tool])
    await agent.run("go")

    # The model saw ONLY the real parameters — ctx is absent from the schema:
    schema = model.last_model_request_parameters.function_tools[0].parameters_json_schema
    assert set(schema["properties"]) == {"query", "k"}
    # ...and the injected ctx is OUR object; model args cannot name it, so a
    # hostile {"ctx": ...} argument would be rejected by schema validation:
    assert seen["ctx"] is ctx and isinstance(seen["query"], str)


@pytest.mark.asyncio
async def test_expected_tool_failure_becomes_model_retry(db):
    conv = create_conversation(db, owner_id="u1")

    async def failing_tool(ctx: ToolContext, url: str) -> str:
        """Failing the EXPECTED way (e.g. the SSRF guard fired)."""
        raise ToolExecutionError("fetch blocked")

    entry = RegisteredTool(name="failing_tool", fn=failing_tool, description="boom")
    (tool,) = adapter_mod.bind_tools([entry], _ctx(db, conv.id))

    # The wrapper re-raises as ModelRetry so the MESSAGE reaches the model:
    with pytest.raises(ModelRetry, match="fetch blocked"):
        await tool.function(url="http://10.0.0.1/")


# ============================================================================
# D34 capability selection + D20 output-mode goldens
# ============================================================================

def _info(**caps) -> ModelInfo:
    return ModelInfo(id="test:test", provider="test", capabilities=Capabilities(**caps))


def test_select_native_capabilities_follows_registry():
    spec = AgentSpec(
        name="a", model="test:test", tools=["web_fetch", "code_execution"],
        features=AgentFeatures(rag_search=False, web_search=True, thinking=True),
    )

    # All flags on: every dual name goes native, and Thinking rides along
    # (supports_thinking derives from capabilities.reasoning). Order follows
    # the requested-names order: explicit spec.tools first, implicit toggles after:
    info = _info(web_search=True, web_fetch=True, code_execution=True, reasoning=True)
    caps = adapter_mod.select_native_capabilities(spec, info)
    kinds = [type(c) for c in caps]
    assert kinds == [Thinking, WebFetch, NativeTool, WebSearch]

    # No flags: nothing native — the same names resolve as our fills instead:
    caps = adapter_mod.select_native_capabilities(spec, _info())
    assert caps == []


def test_choose_output_mode_three_branches():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    assert isinstance(adapter_mod.choose_output_mode(schema, _info(structured_output=True)), NativeOutput)
    assert isinstance(
        adapter_mod.choose_output_mode(schema, _info(structured_output=False, tool_call=True)), ToolOutput,
    )
    assert isinstance(
        adapter_mod.choose_output_mode(schema, _info(structured_output=False, tool_call=False)), PromptedOutput,
    )


# ============================================================================
# The requirement-7 acceptance run
# ============================================================================

@pytest.mark.asyncio
async def test_agent_answers_from_summarized_away_content_via_rag_tool(db, monkeypatch):
    """
    THE P5 acceptance test: content that would no longer be in the rendered
    context is recovered through the search_conversation tool and used in the
    answer. FunctionModel plays a model that searches first, then answers
    from the tool result:
    """
    conv = create_conversation(db, owner_id="u1")
    # An "old" turn holding the fact (in a long conversation this span would
    # be summarized out of the rendered context — the transcript keeps it):
    append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message, role=Role.user,
                         parts=[OurTextPart(text="For reference: the launch code is 4242.")]), owner_id="u1")

    async def scripted_stream(messages, info):
        """Turn 1: call the RAG tool. Turn 2: answer from its result."""
        for part in messages[-1].parts:
            if getattr(part, "tool_name", None) == "search_conversation":
                found = str(part.content)
                assert "4242" in found  # The tool surfaced the old turn
                yield "The launch code is 4242."
                return
        yield {1: DeltaToolCall(name="search_conversation",
                                json_args='{"query": "launch code"}', tool_call_id="tc1")}

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=scripted_stream)),
    )

    spec = AgentSpec(name="a", model="test:test")  # rag_search default True => tool injected
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="What is the launch code?"))]
    by_type = {e.type: e for e in events}

    # The run surfaced the tool lifecycle and answered from recovered content:
    assert "tool_call_started" in by_type and "tool_call_finished" in by_type
    assert by_type["tool_call_started"].tool_name == "search_conversation"
    assert "4242" in str(by_type["run_completed"].output)

    # The transcript recorded the call/result pair, tagged as OUR implementation (D34):
    hist = full_history(db, conv.id, owner_id="u1")
    call = next(i for i in hist if i.kind == ItemKind.tool_call)
    result = next(i for i in hist if i.kind == ItemKind.tool_result)
    assert call.meta.get("implementation") == "ours"
    assert result.meta.get("implementation") == "ours"
    assert call.tool_name == result.tool_name == "search_conversation"


@pytest.mark.asyncio
async def test_unknown_spec_tool_fails_run_loudly(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel()),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test", tools=["not_a_real_tool"],
                     features=AgentFeatures(rag_search=False))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="hi"))]
    assert events[-1].type == "run_failed"
    assert "not_a_real_tool" in events[-1].error


# ============================================================================
# D35 — background embedding backfill
# ============================================================================

class _FakeEmbedder:
    """Deterministic vectors + usage tallying (mirrors the SDK wrapper's contract)."""

    def __init__(self):
        self.total_usage = Usage()

    def embed(self, texts):
        # A deliberately LARGE tally: real per-run embedding costs round to 0
        # micro-USD (fractions of a cent per Mtok) — the test needs a visible
        # non-zero cost to prove the pricing path ran end to end:
        self.total_usage.input_tokens += 50_000 * len(texts)
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_background_backfill_embeds_and_records_spend(db, db_factory, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )
    # One embedder instance per make_embedder call, as in production:
    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: _FakeEmbedder())

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id, session_factory=db_factory)
    spec = AgentSpec(name="a", model="test:test")

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="remember the plan"))]
    assert events[-1].type == "run_completed"

    # Draining the detached backfill task (production just lets it finish):
    await asyncio.gather(*engine_mod._BACKFILL_TASKS)

    # Every text-bearing item got a vector, via a FRESH session:
    rows = db.query(AITranscriptItem).filter_by(conversation_id=conv.id).all()
    embedded = [r for r in rows if r.embedding is not None]
    assert embedded, "backfill embedded nothing"

    # ...and the embedding tokens were priced into the ledger (P4):
    spends = db.query(AISpendEvent).filter_by(owner_id="u1", kind="embedding").all()
    assert len(spends) == 1 and spends[0].cost_microusd > 0


# ============================================================================
# D40/D41 — image generation (native per registry, our fill otherwise)
# ============================================================================

def test_image_generation_capability_follows_registry():
    from pydantic_ai.capabilities import ImageGeneration

    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, image_generation=True))

    # Registry-flagged model -> the native capability rides along:
    caps = adapter_mod.select_native_capabilities(spec, _info(image_generation=True))
    assert any(isinstance(c, ImageGeneration) for c in caps)

    # Non-capable model -> NO capability here; the ENGINE injects the
    # generate_image fill instead (D41 superseded D40's loud failure):
    caps = adapter_mod.select_native_capabilities(spec, _info())
    assert not any(isinstance(c, ImageGeneration) for c in caps)

    # D45: an explicit image_model pick beats the native flag — the fill
    # (with the picked model) serves even on a natively capable chat model:
    picked = AgentSpec(name="a", model="test:test",
                       features=AgentFeatures(rag_search=False, image_generation=True),
                       media=MediaModels(image_model="openai:gpt-image-1.5"))
    caps = adapter_mod.select_native_capabilities(picked, _info(image_generation=True))
    assert not any(isinstance(c, ImageGeneration) for c in caps)


def test_generated_file_part_becomes_media_item_and_event():
    from uuid import uuid4
    from pydantic_ai import BinaryImage
    from pydantic_ai.messages import FilePart, PartStartEvent
    from platform.services.generative_ai_service.engine.types import MediaGenerated

    part = FilePart(content=BinaryImage(data=b"\x89PNG-bytes", media_type="image/png"))

    # Persistence: a MediaPart message item — the universal form IS the image:
    item = adapter_mod._response_part_to_item(part, uuid4(), "openai:gpt-5.2")
    assert item.kind.value == "message" and item.role.value == "assistant"
    (media,) = item.parts
    assert media.media == "image" and media.mime_type == "image/png"
    assert media.data_b64 and item.meta == {"generated": True}

    # Streaming: the part start surfaces as MediaGenerated immediately:
    event = adapter_mod.translate_event(PartStartEvent(index=0, part=part), uuid4())
    assert isinstance(event, MediaGenerated)
    assert event.media == "image" and event.mime_type == "image/png"

    # Rendering: a media-only assistant item is omitted rather than replayed
    # as an empty text part (the adjacent text already references the image):
    assert adapter_mod._part_from_item(item, provider="anthropic") is None


@pytest.mark.asyncio
async def test_backfill_failure_never_affects_the_run(db, db_factory, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )

    class _BrokenEmbedder(_FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("embeddings API down")

    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: _BrokenEmbedder())

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id, session_factory=db_factory)
    spec = AgentSpec(name="a", model="test:test")

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="hello"))]
    # The run completed normally despite the broken embedder (D35 best-effort):
    assert events[-1].type == "run_completed"
    await asyncio.gather(*engine_mod._BACKFILL_TASKS, return_exceptions=True)
