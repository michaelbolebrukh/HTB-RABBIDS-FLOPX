"""
HITL tool-approval hardening (P8, D46-D48). Adversarial coverage of the
pause/resume tool loop: a gated tool must pause WITHOUT executing, approve must
run it (optionally with edited args) and continue, deny must become the tool
result the model reads, and every failure mode (wrong decision set, nothing
pending, foreign owner, abandonment by a new turn, re-pause, multi-call turns,
native-tool inertness, voice auto-deny) must behave exactly as designed.

All offline — FunctionModel/TestModel drive the runs; a side-effect flag proves
whether the real tool body ran.
"""

import base64
from uuid import uuid4

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import adapter as adapter_mod
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    ApprovalDecision,
    RunDeps,
    ToolContext,
    UserInput,
)
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.engine.tool_table import (
    _TOOL_TABLE,
    RegisteredTool,
    register_tool,
)
from platform.services.generative_ai_service.engine.types import ApprovalResolutionError
from platform.services.generative_ai_service.exceptions import ConversationNotFoundError
from platform.services.generative_ai_service.transcript import (
    create_conversation,
    full_history,
    get_current_path,
)
from platform.services.generative_ai_service.transcript.types import ItemKind


# ============================================================================
# Harness
# ============================================================================

@pytest.fixture()
def clean_table():
    """Snapshotting the global tool table so per-test registrations don't leak:"""
    snapshot = dict(_TOOL_TABLE)
    yield
    _TOOL_TABLE.clear()
    _TOOL_TABLE.update(snapshot)


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope."""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


def _use(model, monkeypatch):
    """Pointing the engine's model builder at a scripted test model."""
    monkeypatch.setattr(engine_mod, "build_model", lambda model_id, fallbacks=None: _fake_built(model))


def _no_tools_spec(**kw):
    """A spec with the implicit RAG/summarizer tools off, so only our tool is bound."""
    kw.setdefault("features", AgentFeatures(rag_search=False, summarization=False))
    return AgentSpec(name="a", model="test:test", **kw)


def _call_then_answer(tool_name: str, json_args: str, answer: str):
    """
    A FunctionModel that calls `tool_name` on the FIRST turn and, once a result
    for it appears in history, answers with `answer` — the canonical two-turn
    tool conversation used to exercise pause/resume:
    """
    async def stream_function(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == tool_name and part.__class__.__name__ in (
                    "ToolReturnPart", "RetryPromptPart",
                ):
                    yield answer
                    return
        yield {1: DeltaToolCall(name=tool_name, json_args=json_args, tool_call_id="tc1")}

    return FunctionModel(stream_function=stream_function)


async def _drain(stream):
    """Collecting a run stream into (events, by_type)."""
    events = [e async for e in stream]
    return events, {e.type: e for e in events}


# ============================================================================
# The core pause / approve / deny contract
# ============================================================================

@pytest.mark.asyncio
async def test_gated_tool_pauses_without_executing(db, monkeypatch, clean_table):
    ran = []

    async def do_thing(ctx: ToolContext, target: str) -> str:
        """A consequential tool — records that it actually executed."""
        ran.append(target)
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    _use(_call_then_answer("do_thing", '{"target": "prod"}', "done"), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, by_type = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))

    # The run PAUSED, not completed, and the tool body never ran:
    assert events[-1].type == "run_paused"
    assert ran == [], "gated tool executed before approval"
    (pending,) = events[-1].approvals
    assert pending.tool_name == "do_thing" and pending.args == {"target": "prod"}

    # The pending call is checkpointed in the transcript, marked pending, with
    # NO tool_result beside it (that is the whole point of the checkpoint):
    hist = full_history(db, conv.id, owner_id="u1")
    call = next(i for i in hist if i.kind == ItemKind.tool_call)
    assert call.meta.get("approval") == "pending"
    assert not any(i.kind == ItemKind.tool_result for i in hist)


@pytest.mark.asyncio
async def test_approve_executes_and_continues(db, monkeypatch, clean_table):
    ran = []

    async def do_thing(ctx: ToolContext, target: str) -> str:
        ran.append(target)
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    _use(_call_then_answer("do_thing", '{"target": "prod"}', "all done"), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    paused, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))
    call_id = paused[-1].approvals[0].tool_call_id

    # Approving resumes the run: the tool runs now, the model answers, the run completes:
    decisions = [ApprovalDecision(tool_call_id=call_id, approved=True)]
    events, by_type = await _drain(engine_mod.resume_agent(spec, deps, decisions))

    assert ran == ["prod"], "approved tool did not execute"
    assert events[-1].type == "run_completed"
    assert "all done" in str(by_type["run_completed"].output)

    # The old pending marker is now "resolved" and a real result sits beside it:
    hist = full_history(db, conv.id, owner_id="u1")
    call = next(i for i in hist if i.kind == ItemKind.tool_call)
    assert call.meta.get("approval") == "resolved"
    result = next(i for i in hist if i.kind == ItemKind.tool_result)
    assert "did prod" in result.extract_text()


@pytest.mark.asyncio
async def test_approve_with_edited_args_runs_the_edited_call(db, monkeypatch, clean_table):
    ran = []

    async def do_thing(ctx: ToolContext, target: str) -> str:
        ran.append(target)
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    _use(_call_then_answer("do_thing", '{"target": "prod"}', "ok"), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    paused, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))
    call_id = paused[-1].approvals[0].tool_call_id

    # Approving with edited args must run the EDITED call ("staging", not "prod"):
    decisions = [ApprovalDecision(tool_call_id=call_id, approved=True, override_args={"target": "staging"})]
    events, _ = await _drain(engine_mod.resume_agent(spec, deps, decisions))
    assert events[-1].type == "run_completed"
    assert ran == ["staging"], f"edited args ignored: {ran}"


@pytest.mark.asyncio
async def test_deny_becomes_tool_result_and_model_adapts(db, monkeypatch, clean_table):
    ran = []

    async def do_thing(ctx: ToolContext, target: str) -> str:
        ran.append(target)
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")

    async def stream_function(messages, info):
        """First turn calls the tool; after the denial result, adapt and finish."""
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "do_thing" and part.__class__.__name__ == "ToolReturnPart":
                    assert "denied" in str(part.content).lower()
                    yield "Understood — I will not do that."
                    return
        yield {1: DeltaToolCall(name="do_thing", json_args='{"target": "prod"}', tool_call_id="tc1")}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    paused, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))
    call_id = paused[-1].approvals[0].tool_call_id

    decisions = [ApprovalDecision(
        tool_call_id=call_id, approved=False, reason="denied by policy",
    )]
    events, by_type = await _drain(engine_mod.resume_agent(spec, deps, decisions))

    assert ran == [], "denied tool executed anyway"
    assert events[-1].type == "run_completed"
    assert "will not" in str(by_type["run_completed"].output).lower()


# ============================================================================
# D46 — only OUR tools are gateable; native tools are structurally untouched
# ============================================================================

def test_native_tools_cannot_be_gated(db, clean_table):
    ran = []

    async def fill_tool(ctx: ToolContext, x: str) -> str:
        ran.append(x)
        return x

    register_tool("fill_tool", fill_tool, description="a fill")
    ctx = ToolContext(db=db, owner_id="u1", conversation_id=create_conversation(db, owner_id="u1").id,
                      features=AgentFeatures())
    entry = _TOOL_TABLE["fill_tool"]

    # A fill tool NAMED in the scope is gated; the SAME machinery cannot reach a
    # native tool because native names never resolve into `bind_tools` — so a
    # scope naming only-native tools produces zero gated tools:
    (gated,) = adapter_mod.bind_tools([entry], ctx, requires_approval=frozenset({"fill_tool"}))
    assert gated.requires_approval is True

    (ungated,) = adapter_mod.bind_tools([entry], ctx, requires_approval=frozenset({"web_search"}))
    assert ungated.requires_approval is False, "a native name gated one of our tools"


@pytest.mark.asyncio
async def test_approval_scope_naming_native_is_inert_not_a_pause(db, monkeypatch, clean_table):
    """A run whose only gated name is native completes normally (never pauses)."""
    async def fill_tool(ctx: ToolContext, x: str) -> str:
        return x

    register_tool("fill_tool", fill_tool, description="a fill")
    _use(TestModel(custom_output_text="done"), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    # requires_approval names a tool NOT bound (web_search here resolves to nothing
    # bindable in this offline spec) — the engine logs it inert and never pauses:
    spec = _no_tools_spec(tools=["fill_tool"], requires_approval=["web_search"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="hi")))
    assert events[-1].type == "run_completed"


# ============================================================================
# Per-invocation predicate (conditional approval)
# ============================================================================

@pytest.mark.asyncio
async def test_predicate_gates_only_the_consequential_call(db, monkeypatch, clean_table):
    ran = []

    async def transfer(ctx: ToolContext, amount: int) -> str:
        ran.append(amount)
        return f"transferred {amount}"

    # Only large transfers need approval (the predicate over model-facing args):
    register_tool("transfer", transfer, description="move money",
                  approval_predicate=lambda amount: amount > 100)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["transfer"])   # NOT whole-tool gated — predicate decides
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    # A small transfer runs straight through (predicate False):
    _use(_call_then_answer("transfer", '{"amount": 5}', "small done"), monkeypatch)
    events, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="send 5")))
    assert events[-1].type == "run_completed" and ran == [5]

    # A large transfer pauses (predicate True):
    ran.clear()
    conv2 = create_conversation(db, owner_id="u1")
    deps2 = RunDeps(db=db, owner_id="u1", conversation_id=conv2.id)
    _use(_call_then_answer("transfer", '{"amount": 500}', "big done"), monkeypatch)
    paused, _ = await _drain(engine_mod.stream_agent(spec, deps2, UserInput(text="send 500")))
    assert paused[-1].type == "run_paused" and ran == []

    # Approving runs the large transfer once, and only once:
    call_id = paused[-1].approvals[0].tool_call_id
    events, _ = await _drain(engine_mod.resume_agent(
        spec, deps2, [ApprovalDecision(tool_call_id=call_id, approved=True)]))
    assert events[-1].type == "run_completed" and ran == [500]


# ============================================================================
# Abandonment, resume validation, ownership
# ============================================================================

@pytest.mark.asyncio
async def test_new_turn_abandons_a_pending_call(db, monkeypatch, clean_table):
    async def do_thing(ctx: ToolContext, target: str) -> str:
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    _use(_call_then_answer("do_thing", '{"target": "prod"}', "x"), monkeypatch)
    paused, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))
    assert paused[-1].type == "run_paused"

    # The user sends a NEW message instead of deciding — the pending call is
    # abandoned and the fresh turn completes cleanly (no provider pairing break):
    _use(TestModel(custom_output_text="new answer"), monkeypatch)
    plain = _no_tools_spec()
    events, _ = await _drain(engine_mod.stream_agent(plain, deps, UserInput(text="never mind, hello")))
    assert events[-1].type == "run_completed"

    hist = full_history(db, conv.id, owner_id="u1")
    call = next(i for i in hist if i.kind == ItemKind.tool_call)
    assert call.meta.get("approval") == "abandoned"

    # And an abandoned call can no longer be resumed (nothing pending):
    with pytest.raises(ApprovalResolutionError):
        engine_mod.find_pending_approvals(deps)


@pytest.mark.asyncio
async def test_resume_rejects_mismatched_decision_set(db, monkeypatch, clean_table):
    async def do_thing(ctx: ToolContext, target: str) -> str:
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    _use(_call_then_answer("do_thing", '{"target": "prod"}', "x"), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))

    # A decision for an unknown call id does not cover the pending set → RunFailed
    # carrying the typed resolution error, and nothing executes:
    bad = [ApprovalDecision(tool_call_id="not-a-real-id", approved=True)]
    events, _ = await _drain(engine_mod.resume_agent(spec, deps, bad))
    assert events[-1].type == "run_failed"
    assert "ApprovalResolutionError" in events[-1].error

    # The pending call is still pending (a failed resume changed nothing):
    pending = engine_mod.find_pending_approvals(deps)
    assert len(pending) == 1


def test_pending_lookup_requires_something_pending_and_ownership(db, clean_table):
    conv = create_conversation(db, owner_id="u1")
    # Nothing pending -> typed error (router maps to 409):
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)
    with pytest.raises(ApprovalResolutionError, match="no tool calls"):
        engine_mod.find_pending_approvals(deps)

    # A foreign owner and an unknown id are told the SAME "not found" — the
    # store's owner-scoped lookup makes the two indistinguishable:
    foreign = RunDeps(db=db, owner_id="someone-else", conversation_id=conv.id)
    with pytest.raises(ConversationNotFoundError) as foreign_exc:
        engine_mod.find_pending_approvals(foreign)
    unknown_id = uuid4()
    missing = RunDeps(db=db, owner_id="u1", conversation_id=unknown_id)
    with pytest.raises(ConversationNotFoundError) as missing_exc:
        engine_mod.find_pending_approvals(missing)
    assert str(foreign_exc.value).replace(str(conv.id), "") == \
        str(missing_exc.value).replace(str(unknown_id), "")


# ============================================================================
# Billing, multi-call turns, re-pause
# ============================================================================

@pytest.mark.asyncio
async def test_paused_run_bills_tokens_consumed(db, monkeypatch, clean_table):
    from platform.services.generative_ai_service.models import AISpendEvent

    async def do_thing(ctx: ToolContext, target: str) -> str:
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")
    _use(_call_then_answer("do_thing", '{"target": "prod"}', "x"), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do it")))
    assert events[-1].type == "run_paused"

    # A pause still consumed input + pre-pause output tokens — it bills like a run:
    spends = db.query(AISpendEvent).filter_by(owner_id="u1", kind="run").all()
    assert len(spends) == 1


@pytest.mark.asyncio
async def test_resume_can_pause_again(db, monkeypatch, clean_table):
    ran = []

    async def step(ctx: ToolContext, n: int) -> str:
        ran.append(n)
        return f"step {n}"

    register_tool("step", step, description="a step")

    async def stream_function(messages, info):
        """Call step(1); after its result, call step(2); after that, finish."""
        results = [p for m in messages for p in getattr(m, "parts", [])
                   if getattr(p, "tool_name", None) == "step" and p.__class__.__name__ == "ToolReturnPart"]
        if not results:
            yield {1: DeltaToolCall(name="step", json_args='{"n": 1}', tool_call_id="s1")}
        elif len(results) == 1:
            yield {1: DeltaToolCall(name="step", json_args='{"n": 2}', tool_call_id="s2")}
        else:
            yield "both steps done"

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["step"], requires_approval=["step"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    # First pause on step(1):
    p1, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="go")))
    assert p1[-1].type == "run_paused"
    id1 = p1[-1].approvals[0].tool_call_id

    # Approving runs step(1), the model calls step(2) → pauses AGAIN:
    p2, _ = await _drain(engine_mod.resume_agent(
        spec, deps, [ApprovalDecision(tool_call_id=id1, approved=True)]))
    assert p2[-1].type == "run_paused" and ran == [1]
    id2 = p2[-1].approvals[0].tool_call_id
    assert id2 != id1

    # Approving the second finishes the run:
    ev, by = await _drain(engine_mod.resume_agent(
        spec, deps, [ApprovalDecision(tool_call_id=id2, approved=True)]))
    assert ev[-1].type == "run_completed" and ran == [1, 2]
    assert "both steps done" in str(by["run_completed"].output)


@pytest.mark.asyncio
async def test_two_gated_calls_in_one_turn_need_both_decisions(db, monkeypatch, clean_table):
    ran = []

    async def act(ctx: ToolContext, which: str) -> str:
        ran.append(which)
        return f"acted {which}"

    register_tool("act", act, description="an act")

    async def stream_function(messages, info):
        """One turn emitting TWO gated calls; finish once both results are present."""
        results = [p for m in messages for p in getattr(m, "parts", [])
                   if getattr(p, "tool_name", None) == "act" and p.__class__.__name__ == "ToolReturnPart"]
        if len(results) >= 2:
            yield "both acted"
            return
        yield {
            1: DeltaToolCall(name="act", json_args='{"which": "a"}', tool_call_id="a1"),
            2: DeltaToolCall(name="act", json_args='{"which": "b"}', tool_call_id="a2"),
        }

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["act"], requires_approval=["act"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    paused, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="do both")))
    assert paused[-1].type == "run_paused"
    ids = sorted(a.tool_call_id for a in paused[-1].approvals)
    assert len(ids) == 2

    # Covering only ONE of the two pending calls is rejected (must be exact):
    partial = [ApprovalDecision(tool_call_id=ids[0], approved=True)]
    ev, _ = await _drain(engine_mod.resume_agent(spec, deps, partial))
    assert ev[-1].type == "run_failed" and "ApprovalResolutionError" in ev[-1].error
    assert ran == []

    # Approving one and denying the other resolves the turn, running only the approved one:
    decisions = [
        ApprovalDecision(tool_call_id=ids[0], approved=True),
        ApprovalDecision(tool_call_id=ids[1], approved=False, reason="no"),
    ]
    ev, by = await _drain(engine_mod.resume_agent(spec, deps, decisions))
    assert ev[-1].type == "run_completed"
    assert ran == ["a"] or ran == ["b"]   # exactly the approved one ran, once


# ============================================================================
# Rendering: a pending call never reaches a provider bare
# ============================================================================

def test_pending_call_renders_with_a_synthetic_return(db, clean_table):
    from platform.services.generative_ai_service.transcript.types import DataPart, Item, Role

    conv = create_conversation(db, owner_id="u1")
    call = Item(
        conversation_id=conv.id, kind=ItemKind.tool_call, role=Role.assistant,
        tool_name="do_thing", tool_call_id="tc9", parts=[DataPart(data={"target": "prod"})],
        model_used="test:test", meta={"implementation": "ours", "approval": "pending"},
    )

    # A NON-resume render must pair the pending call with a synthetic "not
    # executed" return so no provider ever sees a bare tool_call:
    msgs = adapter_mod.to_message_history([call], provider="test", resume=False)
    returns = [p for m in msgs for p in m.parts if p.__class__.__name__ == "ToolReturnPart"]
    assert returns and "not executed" in str(returns[0].content)

    # A RESUME render sends it bare (the deferred results will supply the return):
    msgs_resume = adapter_mod.to_message_history([call], provider="test", resume=True)
    returns_resume = [p for m in msgs_resume for p in m.parts if p.__class__.__name__ == "ToolReturnPart"]
    assert not returns_resume


# ============================================================================
# Voice: a live turn cannot pause — it auto-denies and continues (W5)
# ============================================================================

@pytest.mark.asyncio
async def test_voice_pipeline_autodenies_gated_tool(db, monkeypatch, clean_table):
    from platform.services.generative_ai_service.voice.adapters.pipeline import PipelineVoiceSession
    from platform.services.generative_ai_service.voice.transcriptor import VoiceRuntime
    from platform.services.generative_ai_service.voice.types import (
        OutputTranscript,
        VoiceConfig,
        VoiceSessionEnded,
    )
    from platform.services.generative_ai_service.transcript.types import Usage

    ran = []

    async def do_thing(ctx: ToolContext, target: str) -> str:
        ran.append(target)
        return f"did {target}"

    register_tool("do_thing", do_thing, description="does a thing")

    async def stream_function(messages, info):
        """Call the gated tool; once its (denial) result appears, answer."""
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "do_thing" and part.__class__.__name__ == "ToolReturnPart":
                    yield "I could not do that here."
                    return
        yield {1: DeltaToolCall(name="do_thing", json_args='{"target": "prod"}', tool_call_id="tc1")}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_tools_spec(tools=["do_thing"], requires_approval=["do_thing"])
    config = VoiceConfig(spec=spec)
    runtime = VoiceRuntime(db=db, owner_id="u1", conversation_id=conv.id,
                           model_id=spec.model, agent_name=spec.name)
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    async def ok_stt(audio):
        return "please do the thing", Usage(input_audio_tokens=10)

    async def ok_tts(text):
        return b"WAV", Usage(output_audio_tokens=10)

    session = PipelineVoiceSession(config, runtime, deps, stt=ok_stt, tts=ok_tts)
    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    await session.close()
    events = [e async for e in session.events()]

    # The gated tool was auto-DENIED (never executed) and the turn still finished
    # with a spoken answer — a live session never hangs waiting for a UI:
    assert ran == [], "voice turn executed a gated tool without approval"
    finals = [e for e in events if isinstance(e, OutputTranscript) and e.final]
    assert finals and "could not" in finals[-1].text.lower()
    assert any(isinstance(e, VoiceSessionEnded) for e in events)
