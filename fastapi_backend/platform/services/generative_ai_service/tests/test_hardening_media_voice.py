"""
Hardening tests for the P6/P7 surfaces — adversarial edge cases the phase
suites do not cover: the D42 id-validation matrix corners, adapter tolerance
of malformed provider payloads, pcm16_to_wav byte-level correctness, D44
pricing at degenerate quantities, pipeline turns dying mid-leg (STT/TTS/
budget races), WS auth/ownership, sandbox dormancy corners, and the D45
override in its remaining directions. All offline — httpx, adapters, engine
model, and speech legs are faked; no network, no docker, no keys.
"""

import base64
import io
import wave
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.billing import (
    BudgetDecision,
    PessimisticMediaRates,
    RunCaps,
    price_media,
)
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    MediaModels,
    RunDeps,
    UserInput,
)
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.engine.tool_table import ToolContext
from platform.services.generative_ai_service.engine.types import ToolExecutionError
from platform.services.generative_ai_service.media import (
    MediaCallFailedError,
    MediaModelInvalidError,
    SpeechResult,
    price_speech,
    validate,
)
from platform.services.generative_ai_service.media.adapters import fal as fal_mod
from platform.services.generative_ai_service.media.adapters import (
    google_media,
    openai_media,
    openrouter_audio,
)
from platform.services.generative_ai_service.models import AISpendEvent
from platform.services.generative_ai_service.registry.schema import Capabilities, ModelInfo
from platform.services.generative_ai_service.sandbox import code_tool as code_tool_mod
from platform.services.generative_ai_service.sandbox import port as sandbox_port_mod
from platform.services.generative_ai_service.sandbox.code_tool import code_execution
from platform.services.generative_ai_service.sandbox.port import get_backend
from platform.services.generative_ai_service.transcript import (
    append_item,
    create_conversation,
    full_history,
)
from platform.services.generative_ai_service.transcript.types import (
    DataPart,
    Item,
    ItemKind,
    MediaPart,
    Role,
    TextPart,
    Usage,
)
from platform.services.generative_ai_service.voice import (
    AudioDelta,
    VoiceConfig,
    VoiceFailed,
    VoiceRuntime,
    VoiceSessionEnded,
    build_voice_session,
)
from platform.services.generative_ai_service.voice import port as port_mod
from platform.services.generative_ai_service.voice import transcriptor as transcriptor_mod
from platform.services.generative_ai_service.voice.adapters.pipeline import (
    PipelineVoiceSession,
    STT_MODEL_ID,
    TTS_MODEL_ID,
)


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope:"""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


def _deps(db, conversation_id):
    """Building RunDeps the way the WS router does:"""
    return RunDeps(db=db, owner_id="u1", conversation_id=conversation_id)


def _client_serving(*responses):
    """A fake httpx.AsyncClient class serving scripted responses (offline):"""
    queue = list(responses)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return queue.pop(0)

        async def get(self, *args, **kwargs):
            return queue.pop(0)

    return _Client


async def _drain(session):
    """Collecting the session's full event stream after it terminates:"""
    return [event async for event in session.events()]


def _pipeline_session(db, conv, stt, tts):
    """Building a pipeline session with injected speech legs (no key needed):"""
    config = VoiceConfig(spec=AgentSpec(name="a", model="test:test"))
    runtime = VoiceRuntime(db=db, owner_id="u1", conversation_id=conv.id,
                           model_id=config.spec.model, agent_name=config.spec.name)
    return PipelineVoiceSession(config, runtime, _deps(db, conv.id), stt=stt, tts=tts)


async def _ok_stt(audio: bytes):
    """A deterministic STT leg (fixed text, visible audio tally):"""
    return "what is the plan", Usage(input_audio_tokens=1_000)


async def _ok_tts(text: str):
    """A deterministic TTS leg (fixed bytes, visible audio tally):"""
    return b"WAVDATA", Usage(input_tokens=10, output_audio_tokens=1_000)


# ============================================================================
# D42 — the id-validation matrix corners
# ============================================================================

def test_media_id_validation_matrix(monkeypatch):
    monkeypatch.setattr(openai_media, "api_key", lambda: "test-key")
    monkeypatch.setattr(fal_mod, "api_key", lambda: "test-key")

    # Degenerate id shapes: empty, colonless pieces on either side — every one
    # must be the format error, never a lane lookup on a garbage prefix:
    for bad_id in ("", "openai:", ":tts-1", ":"):
        with pytest.raises(MediaModelInvalidError, match="expected 'provider:model'"):
            validate(MediaModels(tts_model=bad_id), "tts")

    # Provider prefixes are exact and case-sensitive — "OPENAI" is unknown:
    with pytest.raises(MediaModelInvalidError, match="no tts lane"):
        validate(MediaModels(tts_model="OPENAI:tts-1"), "tts")

    # An unknown provider names the lanes that DO exist (actionable error):
    with pytest.raises(MediaModelInvalidError, match="elevenlabs"):
        validate(MediaModels(tts_model="acme:reader-1"), "tts")

    # fal ids embed the endpoint path — nested slashes AND a second colon must
    # survive the provider:model split (partition on the FIRST colon only):
    validate(MediaModels(image_model="fal:fal-ai/flux/dev/image-to-image"), "image")
    validate(MediaModels(tts_model="fal:fal-ai/kokoro:latest"), "tts")

    # A lane a provider does not serve stays closed even with its key present:
    with pytest.raises(MediaModelInvalidError, match="no stt lane"):
        validate(MediaModels(stt_model="fal:fal-ai/whisper"), "stt")


# ============================================================================
# Adapter translation under malformed provider payloads (offline httpx)
# ============================================================================

@pytest.mark.asyncio
async def test_openai_adapter_tolerates_missing_provider_fields(monkeypatch):
    # STT 200 with an empty JSON object — no text, no usage block. The parse
    # must degrade to empty text and zeroed tokens, never a KeyError:
    monkeypatch.setattr(openai_media.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json={})))
    pcm = b"\x00\x01" * 16_000              # One second at 16kHz PCM16 mono
    result = await openai_media.transcribe(pcm, 16_000, "gpt-4o-mini-transcribe")
    assert result.text == ""
    assert result.usage.input_tokens == 0 and result.usage.input_audio_tokens == 0
    # The unit evidence is still computed from OUR bytes, not the response:
    assert result.audio_minutes == pytest.approx(1 / 60)

    # Image 200 whose data row carries neither b64 nor url — typed, loud:
    monkeypatch.setattr(openai_media.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json={"data": [{}]})))
    with pytest.raises(MediaCallFailedError, match="no image"):
        await openai_media.generate_image("a kite", "gpt-image-1.5", "1024x1024")

    # TTS 200 with an EMPTY body: accepted as zero-length audio today (no
    # emptiness check), with usage estimated at 0 audio-out / >=1 text-in —
    # pinning the floor so estimates never bill negative or zero input:
    monkeypatch.setattr(openai_media.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, content=b"")))
    result = await openai_media.synthesize("hi", "gpt-4o-mini-tts", "default")
    assert result.audio == b""
    assert result.usage.output_audio_tokens == 0 and result.usage.input_tokens == 1


# A 200 whose body is not JSON (an edge proxy's HTML notice, a truncated
# stream) must surface as MediaCallFailedError, not a raw json.JSONDecodeError:
# types.py documents MediaCallFailedError as covering "malformed response", and
# the generate_image fill translates only MediaError into a reactable tool
# result (image_tool.py), so anything else kills the whole run untyped. Every
# adapter decodes through media/adapters/_json.py — covered here on all five
# JSON-reading legs, plus the non-object-JSON corner that breaks `.get`:
@pytest.mark.asyncio
async def test_media_adapters_type_non_json_200_bodies(monkeypatch):
    html = httpx.Response(200, content=b"<html>gateway</html>")

    monkeypatch.setattr(openai_media.httpx, "AsyncClient", _client_serving(html))
    with pytest.raises(MediaCallFailedError, match="non-JSON"):
        await openai_media.generate_image("a kite", "gpt-image-1.5", "1024x1024")

    monkeypatch.setattr(openai_media.httpx, "AsyncClient", _client_serving(html))
    with pytest.raises(MediaCallFailedError, match="non-JSON"):
        await openai_media.transcribe(b"\x00\x01" * 100, 16_000, "gpt-4o-mini-transcribe")

    monkeypatch.setattr(openrouter_audio.httpx, "AsyncClient", _client_serving(html))
    with pytest.raises(MediaCallFailedError, match="non-JSON"):
        await openrouter_audio.transcribe(b"\x00\x01" * 100, 16_000, "whisper-v3")

    monkeypatch.setattr(fal_mod.httpx, "AsyncClient", _client_serving(html))
    with pytest.raises(MediaCallFailedError, match="non-JSON"):
        await fal_mod.generate_image("a kite", "fal-ai/flux/dev", "1024x1024")

    monkeypatch.setattr(google_media.httpx, "AsyncClient", _client_serving(html))
    with pytest.raises(MediaCallFailedError, match="non-JSON"):
        await google_media.generate_image("a kite", "gemini-3.1-flash-image", "1024x1024")

    # Valid JSON that is not an object is malformed for the same reason — the
    # adapters index it with `.get`:
    monkeypatch.setattr(fal_mod.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json=["nope"])))
    with pytest.raises(MediaCallFailedError, match="non-object JSON"):
        await fal_mod.generate_image("a kite", "fal-ai/flux/dev", "1024x1024")


@pytest.mark.asyncio
async def test_fal_adapter_translation_and_empty_payloads(monkeypatch):
    # An empty images list is a typed failure naming the endpoint:
    monkeypatch.setattr(fal_mod.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json={"images": []})))
    with pytest.raises(MediaCallFailedError, match="no images"):
        await fal_mod.generate_image("a kite", "fal-ai/flux/dev", "1024x1024")

    # Bare-string image entries (some endpoints return URLs, not objects)
    # are wrapped into the url shape with the default mime:
    monkeypatch.setattr(fal_mod.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json={"images": ["https://cdn.fal/img"]})))
    result = await fal_mod.generate_image("a kite", "fal-ai/flux/dev", "1024x1024")
    assert result.url == "https://cdn.fal/img" and result.mime_type == "image/png"
    assert result.model_id == "fal:fal-ai/flux/dev"

    # TTS with no audio block is typed, never a fetch of a None url:
    monkeypatch.setattr(fal_mod.httpx, "AsyncClient",
                        _client_serving(httpx.Response(200, json={})))
    with pytest.raises(MediaCallFailedError, match="no audio"):
        await fal_mod.synthesize("hello", "fal-ai/kokoro", "default")

    # The happy TTS path fetches the hosted URL and keeps the declared mime
    # plus the D44 character evidence (fal reports no tokens):
    monkeypatch.setattr(fal_mod.httpx, "AsyncClient", _client_serving(
        httpx.Response(200, json={"audio": {"url": "https://cdn.fal/a.ogg", "content_type": "audio/ogg"}}),
        httpx.Response(200, content=b"OGGBYTES"),
    ))
    result = await fal_mod.synthesize("hello", "fal-ai/kokoro", "default")
    assert result.audio == b"OGGBYTES" and result.mime_type == "audio/ogg"
    assert result.usage is None and result.characters == 5


def test_pcm16_to_wav_header_and_odd_input():
    pcm = b"\x01\x02\x03\x04" * 100
    blob = openai_media.pcm16_to_wav(pcm, 16_000)

    # Container magic at the byte level (what audio endpoints sniff):
    assert blob[0:4] == b"RIFF" and blob[8:12] == b"WAVE" and blob[12:16] == b"fmt "

    # The declared format round-trips exactly: mono PCM16 at the given rate,
    # with every input frame intact:
    reader = wave.open(io.BytesIO(blob), "rb")
    assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 16_000)
    assert reader.getnframes() == 200
    assert reader.readframes(200) == pcm

    # Odd-length input (a torn PCM16 stream) must never crash the wrapper —
    # the wave module floors the frame count and keeps the bytes:
    torn = openai_media.pcm16_to_wav(b"\x01\x02\x03", 16_000)
    reader = wave.open(io.BytesIO(torn), "rb")
    assert reader.getnframes() == 1


# ============================================================================
# D44 — pricing at degenerate quantities
# ============================================================================

def test_price_media_quantity_corner_cases():
    info = ModelInfo(id="elevenlabs:eleven_v3", provider="elevenlabs")
    info.cost.per_million_characters = 100.0

    # Zero quantity is a free, PRICED call (empty text is not "unpriceable"):
    price = price_media(0, "characters", info)
    assert price.cost_microusd == 0 and price.priced and price.breakdown == {"characters": 0}

    # One character at $100/1M chars = exactly 100 micro-USD (no float dust):
    assert price_media(1, "characters", info).cost_microusd == 100

    # A rate cheap enough that one unit lands on .5 micro-USD rounds to ZERO
    # (banker's rounding) — pinning that sub-half-micro synthesis bills free:
    cheap = ModelInfo(id="x:tts", provider="x")
    cheap.cost.per_million_characters = 0.5
    assert price_media(1, "characters", cheap).cost_microusd == 0

    # Fractional audio minutes round on the total, not per minute:
    stt = ModelInfo(id="deepgram:nova-3", provider="deepgram")
    stt.cost.per_audio_minute = 0.0043
    assert price_media(0.001, "audio_minutes", stt).cost_microusd == 4   # round(4.3)

    # Negative quantities pass straight through as negative cost — there is
    # no clamp or raise today. Every real caller derives quantity from
    # len(text)/len(pcm)/image count (always >= 0), so this is unreachable in
    # production; pinned so a future refund-abuse path cannot appear silently:
    img = ModelInfo(id="openai:gpt-image-1.5", provider="openai")
    img.cost.per_image = 0.05
    assert price_media(-5, "images", img).cost_microusd == -250_000

    # Token evidence does NOT win pricing when the registry cannot price the
    # tokens: an unpriced unit-billed model with a usage block still routes to
    # the per-character path (pessimistic here, flagged unpriced):
    result = SpeechResult(audio=b"x", mime_type="audio/mpeg", model_id="fal:fal-ai/kokoro",
                          usage=Usage(input_tokens=10), characters=100)
    price = price_speech(result)
    assert not price.priced
    assert price.cost_microusd == round(100 * PessimisticMediaRates().per_million_characters)


# ============================================================================
# D41 — the speech-stage audio detector
# ============================================================================

def test_has_audio_part_detects_only_audio_media():
    conv_id = uuid4()

    def _msg(*parts):
        return Item(conversation_id=conv_id, kind=ItemKind.message, role=Role.assistant, parts=list(parts))

    # Native audio-out anywhere in the run's items suppresses the TTS stage:
    audio = _msg(MediaPart(media="audio", mime_type="audio/wav", data_b64="QUJD"))
    assert engine_mod._has_audio_part([audio])

    # Images, text, and structured payloads must NOT suppress it — a run that
    # drew a picture still gets its reply spoken:
    image = _msg(MediaPart(media="image", mime_type="image/png", data_b64="QUJD"))
    text = _msg(TextPart(text="spoken reply"))
    event = Item(conversation_id=conv_id, kind=ItemKind.event, parts=[DataPart(data={"event": "x"})])
    assert not engine_mod._has_audio_part([image, text, event])
    assert not engine_mod._has_audio_part([])


# ============================================================================
# The read-aloud endpoint — hostile inputs and failure paths
# ============================================================================

def _speech_app(db, user_id=7):
    """Building the router app the way the P7 endpoint test does:"""
    from fastapi import FastAPI

    from infrastructure.dependencies import get_current_verified_user, get_db
    from platform.services.generative_ai_service import routers as routers_mod

    app = FastAPI()
    app.include_router(routers_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_verified_user] = lambda: SimpleNamespace(id=user_id)
    return app


@pytest.mark.asyncio
async def test_read_aloud_input_edge_cases(db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from platform.services.generative_ai_service import routers as routers_mod

    spoken: list[str] = []

    async def fake_synthesize(text, models):
        spoken.append(text)
        return SpeechResult(audio=b"AUDIO", mime_type="audio/wav", model_id=models.resolved_tts(),
                            usage=Usage(input_tokens=5, output_audio_tokens=10), characters=len(text))

    monkeypatch.setattr(routers_mod, "synthesize_speech", fake_synthesize)
    app = _speech_app(db)

    conv = create_conversation(db, owner_id="7")
    text_item = append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message,
                                     role=Role.assistant, parts=[TextPart(text="item words")]), owner_id="7")
    # An owned item with NO text parts (image-only message — its text_content
    # column is NULL):
    media_item = append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message,
                                      role=Role.assistant,
                                      parts=[MediaPart(media="image", url="https://x/img.png")]), owner_id="7")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Whitespace-only text is "nothing to speak", exactly like empty:
        response = await client.post("/ai/speech", json={"text": "   \n\t"})
        assert response.status_code == 422

        # An owned item with no speakable text behaves as not-found — never a
        # synthesis of empty text, never a 500:
        response = await client.post("/ai/speech", json={"item_id": str(media_item.id)})
        assert response.status_code == 404

        # When BOTH text and item_id arrive, the item wins (the id is the
        # stronger claim — pinning the precedence so clients can rely on it):
        response = await client.post("/ai/speech",
                                     json={"text": "raw words", "item_id": str(text_item.id)})
        assert response.status_code == 200 and spoken[-1] == "item words"

        # Caller-supplied text is CAPPED: TTS is a paid per-character lane, so
        # an unbounded body is a billing-amplification vector on an
        # authenticated endpoint. Over the ceiling is a 422 that never reaches
        # the provider; a body just under it still synthesizes:
        before = len(spoken)
        response = await client.post("/ai/speech", json={"text": "a" * 200_000})
        assert response.status_code == 422 and len(spoken) == before

        ok = "a" * (routers_mod._MAX_SPEECH_CHARS - 1)
        response = await client.post("/ai/speech", json={"text": ok})
        assert response.status_code == 200 and len(spoken[-1]) == len(ok)


@pytest.mark.asyncio
async def test_read_aloud_failure_and_ledger_paths(db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from platform.services.generative_ai_service import routers as routers_mod

    app = _speech_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A malformed per-request model pick is the caller's 422, raised by
        # the real D42 validation before any network call:
        response = await client.post("/ai/speech", json={"text": "hi", "tts_model": "no-prefix"})
        assert response.status_code == 422 and "provider:model" in response.json()["detail"]

        # A provider failure maps to 502, never a naked 500:
        async def broken_synthesize(text, models):
            raise MediaCallFailedError("openai tts failed (503): overloaded")

        monkeypatch.setattr(routers_mod, "synthesize_speech", broken_synthesize)
        response = await client.post("/ai/speech", json={"text": "hi"})
        assert response.status_code == 502

        # A ledger write failure never fails delivered audio (P4 posture):
        async def fake_synthesize(text, models):
            return SpeechResult(audio=b"AUDIO", mime_type="audio/wav", model_id=models.resolved_tts(),
                                usage=Usage(input_tokens=5, output_audio_tokens=10), characters=len(text))

        def broken_record_spend(*args, **kwargs):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(routers_mod, "synthesize_speech", fake_synthesize)
        monkeypatch.setattr(routers_mod, "record_spend", broken_record_spend)
        response = await client.post("/ai/speech", json={"text": "hi"})
        assert response.status_code == 200 and response.content == b"AUDIO"

    # The broken ledger really wrote nothing (the 200 was audio-first):
    assert db.query(AISpendEvent).count() == 0


# ============================================================================
# Pipeline turns dying mid-leg — the session's failure contract
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_stt_failure_fails_typed_without_a_run(db, monkeypatch):
    async def broken_stt(audio):
        raise RuntimeError("stt exploded")

    async def never_tts(text):
        raise AssertionError("TTS must not run after a dead STT leg")

    # The engine must never be reached either — a failed transcription has no
    # text to run:
    monkeypatch.setattr(engine_mod, "build_model",
                        lambda model_id, fallbacks=None: pytest.fail("engine ran without a transcript"))
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv, stt=broken_stt, tts=never_tts)

    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    events = await _drain(session)

    # Terminal contract: the session dies TYPED (voice_failed carrying the
    # exception), exactly once — a later close() adds nothing:
    (failed,) = [e for e in events if isinstance(e, VoiceFailed)]
    assert "RuntimeError: stt exploded" in failed.error
    assert not any(isinstance(e, VoiceSessionEnded) for e in events)
    await session.close()
    assert await _drain(session) == []

    # Nothing half-persisted: the transcript holds only the session-start
    # bracket, no message turns:
    hist = full_history(db, conv.id, owner_id="u1")
    assert sum(1 for i in hist if i.kind == ItemKind.message) == 0


@pytest.mark.asyncio
async def test_pipeline_tts_failure_preserves_the_persisted_turn(db, monkeypatch):
    async def broken_tts(text):
        raise RuntimeError("speaker melted")

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="the plan is 4242")),
    )
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv, stt=_ok_stt, tts=broken_tts)

    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    events = await _drain(session)

    # The session dies typed and no audio ever flowed:
    (failed,) = [e for e in events if isinstance(e, VoiceFailed)]
    assert "speaker melted" in failed.error
    assert not any(isinstance(e, AudioDelta) for e in events)

    # ...but the ENGINE RUN already committed: the spoken user turn and the
    # assistant reply survive the dead speaker — the conversation is intact
    # for the next (text or voice) turn:
    hist = full_history(db, conv.id, owner_id="u1")
    user_item = next(i for i in hist if i.kind == ItemKind.message and i.role == Role.user)
    assert user_item.meta.get("modality") == "voice"
    assert any(i.role == Role.assistant and "4242" in (i.extract_text() or "") for i in hist)


@pytest.mark.asyncio
async def test_pipeline_silent_and_empty_turns_run_nothing(db, monkeypatch):
    async def silence_stt(audio):
        return "   \n", Usage(input_audio_tokens=100)

    async def never_tts(text):
        raise AssertionError("TTS must not run for a silent turn")

    monkeypatch.setattr(engine_mod, "build_model",
                        lambda model_id, fallbacks=None: pytest.fail("engine ran on silence"))
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv, stt=silence_stt, tts=never_tts)

    await session.start()
    # Committing with an EMPTY buffer is a pure no-op (no STT, no billing):
    await session.end_turn()
    # A turn whose transcript is whitespace bills its STT leg (audio was
    # processed) but never reaches the engine or the TTS leg:
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    await session.close()
    events = await _drain(session)

    types = [e.type for e in events]
    assert types == ["voice_session_started", "input_transcript", "voice_usage", "voice_session_ended"]
    assert events[-1].reason == "client_closed"
    # No message items — only the two session-bracket event items:
    hist = full_history(db, conv.id, owner_id="u1")
    assert sum(1 for i in hist if i.kind == ItemKind.message) == 0


# ============================================================================
# Budget exhaustion racing a turn
# ============================================================================

@pytest.mark.asyncio
async def test_budget_denial_after_tts_still_delivers_the_audio(db, monkeypatch):
    """
    The race: the budget dies DURING a turn (fine at the STT check, gone by
    the post-TTS check). The already-synthesized audio is delivered, then the
    session ends budget_exhausted — spent money is never thrown away:
    """
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )
    monkeypatch.setattr(transcriptor_mod.settings, "AI_BUDGET_ENFORCEMENT", True)
    calls = {"n": 0}

    def racing_check(*args, **kwargs):
        calls["n"] += 1
        return BudgetDecision(allowed=calls["n"] == 1, remaining_microusd=0,
                              binding_budget="owner/daily", run_caps=RunCaps())

    monkeypatch.setattr(transcriptor_mod, "check_budget", racing_check)
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv, stt=_ok_stt, tts=_ok_tts)

    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    events = await _drain(session)

    assert any(isinstance(e, AudioDelta) for e in events)
    (ended,) = [e for e in events if isinstance(e, VoiceSessionEnded)]
    assert ended.reason == "budget_exhausted"
    assert calls["n"] == 2                    # STT check allowed, TTS check denied


@pytest.mark.asyncio
async def test_budget_recheck_error_fails_closed_mid_session(db, monkeypatch):
    """
    An UNVERIFIABLE budget mid-call (the checker itself dying) must end the
    session like exhaustion — fail-closed, money before uptime (PHASE6):
    """
    monkeypatch.setattr(transcriptor_mod.settings, "AI_BUDGET_ENFORCEMENT", True)

    def dying_check(*args, **kwargs):
        raise RuntimeError("budget table unreachable")

    monkeypatch.setattr(transcriptor_mod, "check_budget", dying_check)
    monkeypatch.setattr(engine_mod, "build_model",
                        lambda model_id, fallbacks=None: pytest.fail("engine ran on an unverifiable budget"))
    conv = create_conversation(db, owner_id="u1")
    session = _pipeline_session(db, conv, stt=_ok_stt, tts=_ok_tts)

    await session.start()
    await session.send_audio(base64.b64encode(b"pcm").decode())
    await session.end_turn()
    events = await _drain(session)

    (ended,) = [e for e in events if isinstance(e, VoiceSessionEnded)]
    assert ended.reason == "budget_exhausted"
    assert not any(isinstance(e, AudioDelta) for e in events)


# ============================================================================
# The WebSocket surface — auth and conversation ownership
# ============================================================================

def _ws_app(db, monkeypatch, sub="7"):
    """Building the WS app with the host couplings faked (same as the P6 test):"""
    from fastapi import FastAPI

    from platform.services.generative_ai_service import routers as routers_mod

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": sub})
    # Session liveness is gated before ownership and tested separately; stubbing it
    # keeps this test on the ownership refusal it is named for.
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda *args, **kwargs: None)
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(db, "close", lambda: None)
    app = FastAPI()
    app.include_router(routers_mod.router)
    return app


def test_voice_ws_rejects_bad_token_before_accept(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.services.generative_ai_service import routers as routers_mod

    def bad_token(token, expected_use):
        raise ValueError("signature mismatch")

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", bad_token)
    monkeypatch.setattr(db, "close", lambda: None)
    app = FastAPI()
    app.include_router(routers_mod.router)
    client = TestClient(app)
    conv = create_conversation(db, owner_id="7")

    # Rejected BEFORE accept with the application auth code — and a missing
    # token entirely takes the same door:
    for query in ("?token=forged", ""):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ai/conversations/{conv.id}/voice{query}"):
                pass
        assert exc_info.value.code == 4401
    # Nothing was ever written into the conversation:
    assert full_history(db, conv.id, owner_id="7") == []


# The voice WS authenticates the USER and then resolves the conversation
# through the owner-scoped store lookup, BEFORE accept: a foreign conversation
# closes with the same code as an unknown one, so the socket never renders
# another owner's turns into a session context (D36) or appends into it:
def test_voice_ws_refuses_foreign_conversations(db, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from platform.services.generative_ai_service import media as media_pkg
    from platform.services.generative_ai_service import routers as routers_mod

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="leaked")),
    )
    monkeypatch.setattr(media_pkg, "validate", lambda models, *modalities: None)
    # Authenticated as user 8; the conversation belongs to user 7:
    app = _ws_app(db, monkeypatch, sub="8")
    client = TestClient(app)
    foreign_conv = create_conversation(db, owner_id="7")

    # A foreign conversation and an unknown id take the SAME door, with the
    # same close code — the socket is not an existence oracle:
    for target in (foreign_conv.id, uuid4()):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ai/conversations/{target}/voice?token=t") as ws:
                ws.receive_json()
        assert exc_info.value.code == routers_mod._WS_NOT_FOUND

    # Nothing was written into the victim's conversation:
    assert full_history(db, foreign_conv.id, owner_id="7") == []


# ============================================================================
# D45 — the remaining override directions
# ============================================================================

@pytest.mark.asyncio
async def test_stt_pick_alone_forces_the_pipeline(db, monkeypatch):
    """
    D45 with only HALF the speech pair picked: an explicit stt_model (no TTS
    pick) must still force the pipeline on a realtime-capable model — a
    native session cannot honor ANY pipeline-leg pick:
    """
    monkeypatch.setattr(
        port_mod, "get_model_info",
        lambda model_id: ModelInfo(id="test:test", provider="test",
                                   capabilities=Capabilities(realtime=True)),
    )
    monkeypatch.setitem(port_mod._NATIVE_ADAPTERS, "test",
                        lambda *args, **kwargs: pytest.fail("native adapter chosen over an explicit STT pick"))
    monkeypatch.setattr(openai_media, "api_key", lambda: "test-key")

    conv = create_conversation(db, owner_id="u1")
    config = VoiceConfig(spec=AgentSpec(name="a", model="test:test"),
                         media=MediaModels(stt_model="openai:gpt-4o-mini-transcribe"))
    session = await build_voice_session(config, _deps(db, conv.id))
    assert isinstance(session, PipelineVoiceSession)


@pytest.mark.asyncio
async def test_image_pick_without_the_toggle_stays_dormant(db, monkeypatch):
    """
    D45's boundary: an explicit image_model with image_generation OFF changes
    nothing — the pick configures the feature, it never enables it. The model
    sees no generate_image tool and no image money moves:
    """
    captured: dict = {}

    async def scripted_stream(messages, info):
        """Recording the tools the model was offered, then answering:"""
        captured["tools"] = [t.name for t in info.function_tools]
        yield "No drawing today."

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=scripted_stream)),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, image_generation=False),
                     media=MediaModels(image_model="openai:gpt-image-1.5"))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="draw me a kite"))]
    assert events[-1].type == "run_completed"
    assert "generate_image" not in captured["tools"]
    assert not any(e.type == "media_generated" for e in events)
    assert db.query(AISpendEvent).filter_by(kind="image").count() == 0


# ============================================================================
# Sandbox dormancy corners (D38)
# ============================================================================

def test_sandbox_backend_blank_values_and_case(monkeypatch):
    # Blank and whitespace-only values are the SAME dormant state as unset —
    # an empty env var never becomes a loud "unknown backend" boot failure:
    for blank in ("", "   "):
        monkeypatch.setattr(sandbox_port_mod.settings, "AI_SANDBOX_BACKEND", blank)
        assert get_backend() is None

    # The value is normalized (trimmed, case-insensitive) before matching:
    monkeypatch.setattr(sandbox_port_mod.settings, "AI_SANDBOX_BACKEND", "  DoCkEr  ")
    from platform.services.generative_ai_service.sandbox.docker_backend import DockerBackend
    assert isinstance(get_backend(), DockerBackend)


@pytest.mark.asyncio
async def test_code_execution_language_check_precedes_dormancy(monkeypatch):
    """
    On a DORMANT deployment a bad language must still report the language
    error — the model can fix its language; it cannot fix the deployment:
    """
    monkeypatch.setattr(code_tool_mod, "get_backend", lambda: None)
    ctx = ToolContext(db=None, owner_id="u1", conversation_id=uuid4(), features=AgentFeatures())
    with pytest.raises(ToolExecutionError, match="unsupported language"):
        await code_execution(ctx, code="puts 1", language="ruby")
