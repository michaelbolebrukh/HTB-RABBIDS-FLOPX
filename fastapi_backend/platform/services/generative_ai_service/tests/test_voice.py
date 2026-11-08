"""
Voice port tests (P6, D36/D37): D37 selection goldens, the pipeline fallback
end-to-end (fake STT/TTS + TestModel — every model voice-capable offline),
the transcriptor's item shapes + kind="voice" ledger rows, the cross-modality
acceptance test (a TEXT run sees what was SAID), the mid-session budget stop,
and the pure translation goldens for both native adapters. Fully offline.
"""

import base64
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.billing import BudgetDecision, PessimisticRates, RunCaps, price_usage
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import AgentSpec, RunDeps, UserInput
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.models import AISpendEvent
from platform.services.generative_ai_service.registry import get_model_info
from platform.services.generative_ai_service.registry.schema import Capabilities, ModelInfo
from platform.services.generative_ai_service.transcript import create_conversation, full_history
from platform.services.generative_ai_service.transcript.types import ItemKind, Role, Usage
from platform.services.generative_ai_service.voice import (
    AudioDelta,
    InputTranscript,
    OutputTranscript,
    QueueVoiceSession,
    VoiceConfig,
    VoiceRuntime,
    VoiceSessionEnded,
    VoiceSessionStarted,
    VoiceUsage,
    build_voice_session,
)
from platform.services.generative_ai_service.voice import port as port_mod
from platform.services.generative_ai_service.voice import transcriptor as transcriptor_mod
from platform.services.generative_ai_service.voice.adapters import pipeline as pipeline_mod
from platform.services.generative_ai_service.voice.adapters.gemini_live import _usage_from_metadata
from platform.services.generative_ai_service.voice.adapters.openai_realtime import (
    _translate_server_event,
    _usage_from_done,
)
from platform.services.generative_ai_service.voice.adapters.pipeline import (
    PipelineVoiceSession,
    STT_MODEL_ID,
    TTS_MODEL_ID,
)


def _deps(db, conversation_id, db_factory=None):
    """Building RunDeps the way the WS router does:"""
    return RunDeps(db=db, owner_id="u1", conversation_id=conversation_id,
                   session_factory=db_factory)


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope:"""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


async def _fake_stt(audio: bytes):
    """A deterministic STT leg: fixed text + a visible audio-token tally:"""
    return "what is the plan", Usage(input_audio_tokens=50_000)


async def _fake_tts(text: str):
    """A deterministic TTS leg: fixed bytes + a visible audio-token tally:"""
    return b"WAVDATA", Usage(input_tokens=10, output_audio_tokens=50_000)


def _pipeline_session(db, conv, config=None, db_factory=None):
    """Building a pipeline session with fake speech legs (no key needed):"""
    config = config or VoiceConfig(spec=AgentSpec(name="a", model="test:test"))
    runtime = VoiceRuntime(db=db, owner_id="u1", conversation_id=conv.id,
                           model_id=config.spec.model, agent_name=config.spec.name)
    return PipelineVoiceSession(
        config, runtime, _deps(db, conv.id, db_factory), stt=_fake_stt, tts=_fake_tts,
    )


async def _drain(session):
    """Collecting the session's full event stream after close:"""
    return [event async for event in session.events()]


# ============================================================================
# Pipeline fallback — the universal-coverage path, end to end
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_turn_end_to_end(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="the plan is 4242")),
    )
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv)

    await session.start()
    await session.send_audio(base64.b64encode(b"pcm-audio").decode())
    await session.end_turn()
    await session.close()
    events = await _drain(session)
    by_type = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event)

    # The full event contract, in one pass: started -> transcripts -> audio ->
    # usage -> ended (with the run's transcript items attached):
    assert isinstance(events[0], VoiceSessionStarted) and events[0].implementation == "pipeline"
    (input_final,) = [e for e in by_type["input_transcript"] if e.final]
    assert input_final.text == "what is the plan"
    output_finals = [e for e in by_type["output_transcript"] if e.final]
    assert output_finals and "4242" in output_finals[-1].text
    (audio,) = by_type["audio_delta"]
    assert base64.b64decode(audio.audio_b64) == b"WAVDATA"
    assert len(by_type["voice_usage"]) == 2      # One per speech leg (STT + TTS)
    (ended,) = by_type["voice_session_ended"]
    assert ended.reason == "client_closed" and ended.item_ids

    # The transcript holds the voice-tagged user turn AND the assistant reply
    # (persisted by the ordinary engine run — one shared conversation):
    hist = full_history(db, conv.id, owner_id="u1")
    user_item = next(i for i in hist if i.kind == ItemKind.message and i.role == Role.user)
    assert user_item.meta.get("modality") == "voice"
    assert any(i.role == Role.assistant and "4242" in (i.extract_text() or "") for i in hist)
    # The session lifecycle brackets the span as event items:
    assert sum(1 for i in hist if i.kind == ItemKind.event) == 2

    # Billing split (PHASE6): the speech legs landed as kind="voice" rows for
    # the STT/TTS models; the agent run billed itself as kind="run":
    voice_rows = db.query(AISpendEvent).filter_by(owner_id="u1", kind="voice").all()
    assert {r.model_id for r in voice_rows} == {STT_MODEL_ID, TTS_MODEL_ID}
    assert all(r.cost_microusd > 0 for r in voice_rows)


@pytest.mark.asyncio
async def test_cross_modality_a_text_run_sees_what_was_said(db, monkeypatch):
    """
    THE P6 acceptance test: turns persisted by a voice session are ordinary
    transcript items, so the NEXT text run renders them as history:
    """
    conv = create_conversation(db, owner_id="u1")
    runtime = VoiceRuntime(db=db, owner_id="u1", conversation_id=conv.id,
                           model_id="test:test", agent_name="a")
    # A native-adapter-shaped session: transcripts persist via the transcriptor:
    await runtime.on_session_started("native")
    await runtime.on_user_transcript("my locker code is 8181")
    await runtime.on_assistant_transcript("Noted — 8181.")
    await runtime.on_session_ended("client_closed")

    items = full_history(db, conv.id, owner_id="u1")
    spoken = [i for i in items if i.kind == ItemKind.message]
    assert all(i.meta.get("modality") == "voice" for i in spoken)

    async def scripted_stream(messages, info):
        """Asserting the rendered history contains the SPOKEN turn, then answering:"""
        history_text = str([p for m in messages for p in m.parts])
        assert "8181" in history_text            # The voice turn made it into the text run
        yield "Your locker code is 8181."

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=scripted_stream)),
    )
    spec = AgentSpec(name="a", model="test:test")
    events = [e async for e in engine_mod.stream_agent(spec, _deps(db, conv.id), UserInput(text="what is my locker code?"))]
    assert "8181" in str(events[-1].output)


@pytest.mark.asyncio
async def test_budget_exhaustion_ends_the_session_mid_call(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )
    # Enforcement on + a budget that is already gone by the first usage event:
    monkeypatch.setattr(transcriptor_mod.settings, "AI_BUDGET_ENFORCEMENT", True)
    monkeypatch.setattr(
        transcriptor_mod, "check_budget",
        lambda *a, **k: BudgetDecision(allowed=False, remaining_microusd=0,
                                       binding_budget="owner/daily", run_caps=RunCaps()),
    )
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv)
    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    events = await _drain(session)

    (ended,) = [e for e in events if isinstance(e, VoiceSessionEnded)]
    assert ended.reason == "budget_exhausted"
    # The stop happened at the FIRST usage report — no TTS leg ran after it:
    assert not any(isinstance(e, AudioDelta) for e in events)


# ============================================================================
# D37 selection goldens
# ============================================================================

class _FakeNativeSession(QueueVoiceSession):
    """A scripted native adapter standing in for a provider relay:"""

    def __init__(self, config, runtime, deps, context_text=""):
        super().__init__(config, runtime)
        self.context_text = context_text

    async def start(self):
        await self.runtime.on_session_started("native")
        await self._emit(VoiceSessionStarted(
            session_id=self.runtime.session_id, conversation_id=self.runtime.conversation_id,
            implementation="native", model=self.config.spec.model,
        ))

    async def send_audio(self, audio_b64):
        pass

    async def end_turn(self):
        pass

    async def close(self):
        await self._end("client_closed")


def _info(realtime: bool) -> ModelInfo:
    return ModelInfo(id="test:test", provider="test",
                     capabilities=Capabilities(realtime=realtime))


@pytest.mark.asyncio
async def test_selection_follows_registry_realtime_flag(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    config = VoiceConfig(spec=AgentSpec(name="a", model="test:test"))

    # Realtime-capable + a registered adapter for the provider -> native:
    monkeypatch.setattr(port_mod, "get_model_info", lambda model_id: _info(realtime=True))
    monkeypatch.setitem(port_mod._NATIVE_ADAPTERS, "test", _FakeNativeSession)
    session = await build_voice_session(config, _deps(db, conv.id))
    assert isinstance(session, _FakeNativeSession)
    # The port rendered recent context in (D36) — empty here, but a str:
    assert isinstance(session.context_text, str)

    # No realtime flag -> the pipeline serves (every model is voice-capable);
    # the D42 connect-time lane validation is satisfied by a patched key:
    monkeypatch.setattr(port_mod, "get_model_info", lambda model_id: _info(realtime=False))
    from platform.services.generative_ai_service.media.adapters import openai_media
    monkeypatch.setattr(openai_media, "api_key", lambda: "test-key")
    session = await build_voice_session(config, _deps(db, conv.id))
    assert isinstance(session, PipelineVoiceSession)

    # D45: an explicit pipeline-leg pick forces the pipeline even on a
    # realtime-capable model — the picked TTS/STT model must actually serve:
    monkeypatch.setattr(port_mod, "get_model_info", lambda model_id: _info(realtime=True))
    from platform.services.generative_ai_service.engine import MediaModels
    picked = VoiceConfig(spec=AgentSpec(name="a", model="test:test"),
                         media=MediaModels(tts_model="openai:tts-1"))
    session = await build_voice_session(picked, _deps(db, conv.id))
    assert isinstance(session, PipelineVoiceSession)


def test_registry_carries_the_p6_flags():
    """The override-set P6 capability flags actually load (D37/D40 data path):"""
    assert get_model_info("openai:gpt-realtime").capabilities.realtime is True
    assert get_model_info("openai:gpt-realtime").cost.input_audio == 32.0
    assert get_model_info("openai:gpt-5.2").capabilities.image_generation is True
    assert get_model_info("openai:gpt-4o-mini-tts").cost.is_priced()


# ============================================================================
# Audio pricing (P4 extension)
# ============================================================================

def test_price_usage_covers_audio_dimensions():
    info = ModelInfo(id="openai:gpt-realtime", provider="openai")
    info.cost.input, info.cost.output = 4.0, 16.0
    info.cost.input_audio, info.cost.output_audio = 32.0, 64.0
    usage = Usage(input_tokens=1000, output_tokens=100,
                  input_audio_tokens=2000, output_audio_tokens=500)
    price = price_usage(usage, info, pessimistic=PessimisticRates())
    # tokens x USD-per-Mtok == micro-USD per dimension (golden values):
    assert price.breakdown["input"] == 4_000
    assert price.breakdown["output"] == 1_600
    assert price.breakdown["input_audio"] == 64_000
    assert price.breakdown["output_audio"] == 32_000
    assert price.cost_microusd == 101_600 and price.priced


# ============================================================================
# Native adapter translation goldens (pure functions — no sockets)
# ============================================================================

def test_openai_realtime_translation_table():
    sid = VoiceRuntime.__new__(VoiceRuntime)  # Only a session_id is needed:
    from uuid import uuid4
    sid = uuid4()

    (audio,) = _translate_server_event({"type": "response.output_audio.delta", "delta": "QUJD"}, sid)
    assert isinstance(audio, AudioDelta) and audio.audio_b64 == "QUJD"

    (heard,) = _translate_server_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hello"}, sid,
    )
    assert isinstance(heard, InputTranscript) and heard.final and heard.text == "hello"

    (said,) = _translate_server_event(
        {"type": "response.output_audio_transcript.done", "transcript": "hi there"}, sid,
    )
    assert isinstance(said, OutputTranscript) and said.final and said.text == "hi there"

    # Unknown provider events translate to nothing (they add types freely):
    assert _translate_server_event({"type": "rate_limits.updated"}, sid) == []


def test_openai_realtime_usage_extraction():
    usage = _usage_from_done({"response": {"usage": {
        "input_tokens": 1200, "output_tokens": 500,
        "input_token_details": {"text_tokens": 200, "audio_tokens": 1000, "cached_tokens": 64},
        "output_token_details": {"text_tokens": 100, "audio_tokens": 400},
    }}})
    # Text and audio ride disjoint dimensions — nothing double-counts:
    assert usage.input_tokens == 200 and usage.input_audio_tokens == 1000
    assert usage.output_tokens == 100 and usage.output_audio_tokens == 400
    assert usage.cache_read_tokens == 64
    assert _usage_from_done({"response": {}}) is None


def test_gemini_live_usage_extraction():
    metadata = SimpleNamespace(
        prompt_token_count=300, response_token_count=200,
        prompt_tokens_details=[SimpleNamespace(modality="TEXT", token_count=100),
                               SimpleNamespace(modality="AUDIO", token_count=200)],
        response_tokens_details=[SimpleNamespace(modality="AUDIO", token_count=200)],
    )
    usage = _usage_from_metadata(metadata)
    assert usage.input_tokens == 100 and usage.input_audio_tokens == 200
    assert usage.output_tokens == 0 and usage.output_audio_tokens == 200

    # No modality detail -> the undivided totals land on the text dimensions:
    bare = SimpleNamespace(prompt_token_count=50, response_token_count=20,
                           prompt_tokens_details=None, response_tokens_details=None)
    usage = _usage_from_metadata(bare)
    assert usage.input_tokens == 50 and usage.output_tokens == 20
    assert _usage_from_metadata(None) is None


# ============================================================================
# The WebSocket surface (offline: auth, engine, and speech legs all faked)
# ============================================================================

def test_voice_websocket_end_to_end(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="spoken reply")),
    )
    # Faking the WS handler's host couplings: its own session, token auth,
    # and the pipeline's speech legs (no key, no network). The media-port
    # functions are patched on the package the pipeline calls through:
    from platform.services.generative_ai_service import media as media_pkg
    from platform.services.generative_ai_service.media import SpeechResult, TranscriptResult

    async def fake_transcribe(pcm, sample_rate, models):
        return TranscriptResult(text="what is the plan", model_id=STT_MODEL_ID,
                                usage=Usage(input_audio_tokens=50_000), audio_minutes=0.1)

    async def fake_synthesize(text, models):
        return SpeechResult(audio=b"WAVDATA", mime_type="audio/wav", model_id=TTS_MODEL_ID,
                            usage=Usage(input_tokens=10, output_audio_tokens=50_000),
                            characters=len(text))

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    # The session gate is a host coupling like the two above - once at connect,
    # once per turn. That it is wired at all, and closes the socket when it
    # refuses, is pinned separately by test_voice_websocket_refuses_a_dead_session
    # and test_voice_websocket_ends_a_turn_after_the_session_is_revoked:
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    monkeypatch.setattr(routers_mod, "is_session_live", lambda db_, sid, user_id: True)
    monkeypatch.setattr(media_pkg, "validate", lambda models, *modalities: None)
    monkeypatch.setattr(media_pkg, "transcribe", fake_transcribe)
    monkeypatch.setattr(media_pkg, "synthesize_speech", fake_synthesize)
    # db.close() inside the handler would kill the test fixture's session:
    monkeypatch.setattr(db, "close", lambda: None)

    app = FastAPI()
    app.include_router(routers_mod.router)
    client = TestClient(app)
    conv = _create(db, owner_id="7")

    with client.websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t") as ws:
        ws.send_json({"type": "audio", "audio_b64": base64.b64encode(b"pcm").decode()})
        ws.send_json({"type": "end_turn"})
        ws.send_json({"type": "close"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] in ("voice_session_ended", "voice_failed"):
                break

    types = [f["type"] for f in frames]
    assert types[0] == "voice_session_started" and frames[0]["implementation"] == "pipeline"
    assert "input_transcript" in types and "audio_delta" in types
    assert types[-1] == "voice_session_ended"
    # The spoken exchange persisted into the shared transcript:
    hist = full_history(db, conv.id, owner_id="7")
    assert any("spoken reply" in (i.extract_text() or "") for i in hist)


def test_voice_websocket_refuses_a_dead_session(db, monkeypatch):
    """A signed access token is not enough to open a voice socket.

    This is the ONE authenticated path in the backend that does not resolve
    through `get_current_user`, so it has to make the session check itself. It
    is also the most expensive path to leave open: the socket is long-lived,
    spends AI budget, and nothing re-checks auth after accept - so a token
    lifted from a signed-out tab would keep spending for its whole TTL.

    The check's own behaviour (revoked / expired / wrong user / malformed) is
    covered exhaustively in infrastructure/tests/test_session_revocation.py.
    What must be pinned HERE is that the call site exists, is handed the
    token's own claims, and that a refusal closes the socket before accept.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.exceptions import JWTException
    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    seen = {}

    def refuse(db_, sid, user_id):
        seen["sid"], seen["user_id"] = sid, user_id
        raise JWTException

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "assert_session_live", refuse)
    # Proving the gate runs BEFORE the user is even loaded:
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: pytest.fail("gate was bypassed"))
    monkeypatch.setattr(db, "close", lambda: None)

    app = FastAPI()
    app.include_router(routers_mod.router)
    conv = _create(db, owner_id="7")

    with pytest.raises(WebSocketDisconnect) as caught:
        with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t"):
            pass

    assert caught.value.code == routers_mod._WS_UNAUTHORIZED
    # The claims came from the token, not from anywhere the caller controls:
    assert seen == {"sid": "3", "user_id": 7}


def test_voice_websocket_ends_a_turn_after_the_session_is_revoked(db, monkeypatch):
    """Connect-time auth is not enough: a socket must not outrun a sign-out.

    Nothing re-reads auth after accept, so without a re-check a socket opened
    one second before a sign-out kept transcribing and spending AI budget for
    its whole lifetime - the exact "outlives any revocation" property the
    connect-time gate was added to remove. The turn boundary is where the spend
    happens, and it is where the budget is already re-checked
    (`voice/port.py::_report_usage`), so it is where auth is re-checked too.

    The voice session is faked here: the subject is the router's uplink loop,
    not the pipeline (which test_voice_websocket_end_to_end covers). That also
    keeps the assertion deterministic - it is about what the server DOES, not
    about which frames happen to arrive first.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    turns: list[str] = []

    class _FakeSession:
        async def start(self): pass
        async def send_audio(self, audio_b64): turns.append("audio")
        async def end_turn(self): turns.append("end_turn")
        async def close(self): pass

        async def events(self):
            # Never yields: the downlink relay simply waits, exactly as it does
            # between a real session's frames.
            await asyncio.Event().wait()
            yield  # pragma: no cover

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    async def _build(config, deps):        # build_voice_session is awaited
        return _FakeSession()

    monkeypatch.setattr(routers_mod, "build_voice_session", _build)
    monkeypatch.setattr(db, "close", lambda: None)

    # Live at connect, dead by the time the user finishes speaking - a sign-out
    # in another tab, or reuse detection, mid-conversation:
    checks = []

    def liveness(db_, session_id, user_id):
        checks.append((session_id, user_id))
        return False

    monkeypatch.setattr(routers_mod, "is_session_live", liveness)

    app = FastAPI()
    app.include_router(routers_mod.router)
    conv = _create(db, owner_id="7")

    with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t") as ws:
        ws.send_json({"type": "audio", "audio_b64": base64.b64encode(b"pcm").decode()})
        ws.send_json({"type": "end_turn"})
        # WAIT FOR THE SERVER'S OWN ANSWER before letting the socket close.
        # Leaving the `with` block here closes the client end immediately, and
        # nothing guarantees the uplink loop has drained either frame by then -
        # under load it frequently has not, and `checks` reads empty. Measured
        # on a 4-core sandbox: roughly one run in two failed that way, always
        # with `assert [] == [(3, 7)]`, which reads as a broken re-check and is
        # a broken test. The revocation path's whole point is that the server
        # closes the socket itself, so waiting for that close is both the
        # synchronisation point and a stronger assertion than the original.
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == routers_mod._WS_UNAUTHORIZED
    # Re-checked with the token's own claims, exactly once - per turn, not per
    # audio frame (which would be one query per packet):
    assert checks == [(3, 7)]
    # And the turn never ran: no transcription, no model call, no spend.
    assert turns == ["audio"], f"the revoked session still took a turn: {turns}"


def test_voice_websocket_takes_the_turn_while_the_session_is_live(db, monkeypatch):
    """The control for the test above: a live session is not disturbed by the check."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    turns: list[str] = []

    class _FakeSession:
        async def start(self): pass
        async def send_audio(self, audio_b64): turns.append("audio")
        async def end_turn(self): turns.append("end_turn")
        async def close(self): pass

        async def events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    monkeypatch.setattr(routers_mod, "is_session_live", lambda db_, sid, user_id: True)
    async def _build(config, deps):        # build_voice_session is awaited
        return _FakeSession()

    monkeypatch.setattr(routers_mod, "build_voice_session", _build)
    monkeypatch.setattr(db, "close", lambda: None)

    app = FastAPI()
    app.include_router(routers_mod.router)
    conv = _create(db, owner_id="7")

    with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t") as ws:
        ws.send_json({"type": "audio", "audio_b64": base64.b64encode(b"pcm").decode()})
        ws.send_json({"type": "end_turn"})
        ws.send_json({"type": "close"})
        # Same race as the test above, same fix: `close` makes the uplink loop
        # break, and the handler's finally closes the socket. Waiting for that
        # close is what guarantees all three frames were processed before the
        # assertion reads `turns`.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()

    assert turns == ["audio", "end_turn"]


def test_voice_websocket_rechecks_a_silent_socket(db, monkeypatch):
    """The re-check must not run on the CLIENT's schedule.

    Two shapes of the same mistake were shipped before this. Gating on the
    `end_turn` frame let a caller stream audio forever by never sending one -
    and `end_turn` is a documented no-op for the native Gemini adapter anyway.
    Moving to a clock evaluated inside the uplink loop was no better: the loop
    only runs when a frame arrives, so a caller that sends one burst and goes
    quiet is never re-checked at all, while the provider keeps generating and a
    per-minute-billed session keeps spending from its own task.

    So: no frames after the first, and the socket must still close.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    class _FakeSession:
        async def start(self): pass
        async def send_audio(self, audio_b64): pass
        async def end_turn(self): pass
        async def close(self): pass

        async def events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async def _build(config, deps):
        return _FakeSession()

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    monkeypatch.setattr(routers_mod, "build_voice_session", _build)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(routers_mod, "_WS_AUTH_RECHECK_SECONDS", 0.1)

    checks = []

    def revoked(db_, session_id, user_id):
        checks.append((session_id, user_id))
        return False

    monkeypatch.setattr(routers_mod, "is_session_live", revoked)

    app = FastAPI()
    app.include_router(routers_mod.router)
    conv = _create(db, owner_id="7")

    import time

    closed = False
    try:
        with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t") as ws:
            ws.send_json({"type": "audio", "audio_b64": base64.b64encode(b"pcm").decode()})
            # Then silence. Waiting on a clock rather than on receive_json, so a
            # regression that drops the watchdog FAILS here instead of blocking
            # forever on a socket that stays open:
            deadline = time.monotonic() + 5
            while not checks and time.monotonic() < deadline:
                time.sleep(0.05)
            # Only now, once the check has demonstrably run: the server has
            # already closed, so this returns immediately instead of blocking.
            if checks:
                ws.receive_json()
    except WebSocketDisconnect as exc:
        closed = exc.code == routers_mod._WS_UNAUTHORIZED

    assert checks, "a silent socket was never re-checked"
    assert checks[0] == (3, 7)
    assert closed, "the revoked session's socket was left open"


def test_voice_websocket_does_not_recheck_on_every_frame(db, monkeypatch):
    """The clock is a ceiling, not a per-packet query - audio arrives ~50x/second."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    class _FakeSession:
        async def start(self): pass
        async def send_audio(self, audio_b64): pass
        async def end_turn(self): pass
        async def close(self): pass

        async def events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async def _build(config, deps):
        return _FakeSession()

    calls = []
    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    monkeypatch.setattr(routers_mod, "build_voice_session", _build)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(routers_mod, "is_session_live",
                        lambda db_, sid, user_id: (calls.append(1), True)[1])

    app = FastAPI()
    app.include_router(routers_mod.router)
    conv = _create(db, owner_id="7")

    with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t") as ws:
        for _ in range(50):
            ws.send_json({"type": "audio", "audio_b64": base64.b64encode(b"pcm").decode()})
        ws.send_json({"type": "close"})

    assert len(calls) <= 1, f"50 audio frames caused {len(calls)} liveness queries"
