"""
Media tests (P7, D41–D44): unit pricing goldens, D42 validation, the
generate_image fill end to end (offline FunctionModel run), the D41
speech_output stage, and the read-aloud endpoint. All offline — port
functions and adapters are faked; no network, no keys.
"""

from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service import image_tool as image_tool_mod
from platform.services.generative_ai_service.billing import PessimisticMediaRates, price_media
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import AgentFeatures, AgentSpec, MediaModels, RunDeps, UserInput
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.media import (
    ImageResult,
    MediaModelInvalidError,
    SpeechResult,
    price_speech,
    validate,
)
from platform.services.generative_ai_service.media.adapters import openai_media
from platform.services.generative_ai_service.models import AISpendEvent
from platform.services.generative_ai_service.registry import get_model_info
from platform.services.generative_ai_service.registry.schema import Capabilities, ModelInfo
from platform.services.generative_ai_service.transcript import create_conversation, full_history
from platform.services.generative_ai_service.transcript.types import ItemKind, Usage


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope:"""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


# ============================================================================
# D44 — unit pricing goldens
# ============================================================================

def test_price_media_units_and_pessimistic():
    info = ModelInfo(id="elevenlabs:eleven_v3", provider="elevenlabs")
    info.cost.per_million_characters = 100.0
    # 1,000 chars at $100/1M chars = $0.10 = 100,000 micro-USD:
    price = price_media(1_000, "characters", info)
    assert price.cost_microusd == 100_000 and price.priced

    info = ModelInfo(id="google:gemini-3.1-flash-image", provider="google")
    info.cost.per_image = 0.05
    price = price_media(1, "images", info)
    assert price.cost_microusd == 50_000 and price.breakdown == {"images": 50_000}

    info = ModelInfo(id="deepgram:nova-3", provider="deepgram")
    info.cost.per_audio_minute = 0.0043
    price = price_media(10, "audio_minutes", info)
    assert price.cost_microusd == 43_000

    # Unpriced model + pessimistic rates -> expensive and flagged unpriced:
    unknown = ModelInfo(id="x:y", provider="x")
    price = price_media(1, "images", unknown, pessimistic=PessimisticMediaRates())
    assert price.cost_microusd == 300_000 and not price.priced
    # ...and with no pessimistic rates, honestly unpriceable:
    assert price_media(1, "images", unknown).cost_microusd is None

    with pytest.raises(ValueError, match="unknown media unit"):
        price_media(1, "pixels", unknown)


def test_price_speech_prefers_token_evidence_then_units():
    # Token evidence + a priced model -> the token path (registry rates):
    result = SpeechResult(audio=b"x", mime_type="audio/wav",
                          model_id="openai:gpt-4o-mini-tts",
                          usage=Usage(input_tokens=100, output_audio_tokens=1000),
                          characters=400)
    price = price_speech(result)
    assert price.priced and "output_audio" in price.breakdown

    # No usage (unit-billed provider) -> the per-character path:
    result = SpeechResult(audio=b"x", mime_type="audio/mpeg",
                          model_id="elevenlabs:eleven_v3", usage=None, characters=1_000)
    price = price_speech(result)
    assert price.cost_microusd == 100_000 and price.breakdown == {"characters": 100_000}


def test_registry_carries_p7_entries():
    """The override-set P7 data actually loads (D42/D44 data path):"""
    assert get_model_info("openai:gpt-realtime-2.1").capabilities.realtime is True
    assert get_model_info("openai:gpt-image-1.5").cost.per_image == 0.034
    assert get_model_info("elevenlabs:eleven_v3").cost.per_million_characters == 100.0
    assert get_model_info("openai:tts-1").cost.per_million_characters == 15.0
    assert "audio" in get_model_info("openai:gpt-4o-mini-tts").modalities.output


# ============================================================================
# D42 — validation goldens
# ============================================================================

def test_validation_rejects_bad_picks_loudly(monkeypatch):
    monkeypatch.setattr(openai_media, "api_key", lambda: "test-key")

    # Defaults resolve (keys patched present):
    validate(MediaModels(), "tts", "stt")

    # A provider with no lane for the modality:
    with pytest.raises(MediaModelInvalidError, match="no image lane"):
        validate(MediaModels(image_model="deepgram:nova-3"), "image")

    # A lane whose key is missing (dormant, D43):
    from platform.services.generative_ai_service.media.adapters import elevenlabs
    monkeypatch.setattr(elevenlabs, "api_key", lambda: None)
    with pytest.raises(MediaModelInvalidError, match="needs the elevenlabs key"):
        validate(MediaModels(tts_model="elevenlabs:eleven_v3"), "tts")

    # A registry-known model that cannot output the modality:
    with pytest.raises(MediaModelInvalidError, match="cannot output audio"):
        validate(MediaModels(tts_model="openai:gpt-image-1.5"), "tts")

    # An id without a provider prefix:
    with pytest.raises(MediaModelInvalidError, match="expected 'provider:model'"):
        validate(MediaModels(tts_model="gpt-4o-mini-tts"), "tts")

    # An UNKNOWN model on a known lane stays usable (day-one policy —
    # the registry default entry must not block it):
    validate(MediaModels(tts_model="openai:tts-99-brand-new"), "tts")


# ============================================================================
# D41 — the generate_image fill, end to end (offline)
# ============================================================================

@pytest.mark.asyncio
async def test_image_fill_end_to_end(db, monkeypatch):
    """
    A non-image model + image_generation=True: the engine injects the fill,
    the model calls it, the image persists as a MediaPart item, MediaGenerated
    streams, and a kind="image" spend row lands (D41/D44):
    """
    async def fake_generate_image(prompt, models, size="1024x1024"):
        assert prompt == "a red kite over the sea"
        return ImageResult(model_id=models.resolved_image(), mime_type="image/png",
                           data_b64="aW1hZ2UtYnl0ZXM=")

    monkeypatch.setattr(image_tool_mod, "_port_generate_image", fake_generate_image)

    async def scripted_stream(messages, info):
        """Turn 1: call the fill. Turn 2: narrate the result:"""
        for part in messages[-1].parts:
            if getattr(part, "tool_name", None) == "generate_image":
                assert "stored in the conversation" in str(part.content)
                yield "Here is your kite."
                return
        yield {1: DeltaToolCall(name="generate_image",
                                json_args='{"prompt": "a red kite over the sea"}', tool_call_id="tc1")}

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=scripted_stream)),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, image_generation=True))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="draw me a kite"))]
    by_type = {e.type: e for e in events}
    assert by_type["run_completed"].output == "Here is your kite."

    # The image streamed as the SAME event the native path emits:
    media_event = by_type["media_generated"]
    assert media_event.media == "image" and media_event.data_b64 == "aW1hZ2UtYnl0ZXM="

    # ...persisted as a MediaPart item tagged like the native path (D41):
    hist = full_history(db, conv.id, owner_id="u1")
    image_item = next(i for i in hist if i.kind == ItemKind.message
                      and any(getattr(p, "type", "") == "media" for p in i.parts))
    assert image_item.meta == {"generated": True, "implementation": "ours"}
    assert image_item.model_used == "google:gemini-3.1-flash-image"
    assert image_item.id in by_type["run_completed"].item_ids

    # ...and billed per image at the registry rate ($0.05 -> 50,000 micro-USD):
    (spend,) = db.query(AISpendEvent).filter_by(owner_id="u1", kind="image").all()
    assert spend.cost_microusd == 50_000
    assert spend.model_id == "google:gemini-3.1-flash-image"


@pytest.mark.asyncio
async def test_explicit_image_pick_overrides_native(db, monkeypatch):
    """
    D45: a NATIVELY image-capable chat model + an explicit image_model pick ->
    the fill still runs, with the PICKED model (a weak-image chat model can be
    paired with the best image model). Unset pick + native flag stays native:
    """
    real_get_model_info = engine_mod.get_model_info

    def native_image_info(model_id):
        """Claiming native image output for the chat model only:"""
        if model_id == "test:test":
            return ModelInfo(id="test:test", provider="test",
                             capabilities=Capabilities(image_generation=True))
        return real_get_model_info(model_id)

    monkeypatch.setattr(engine_mod, "get_model_info", native_image_info)

    async def fake_generate_image(prompt, models, size="1024x1024"):
        return ImageResult(model_id=models.resolved_image(), mime_type="image/png",
                           data_b64="cGlja2Vk")

    monkeypatch.setattr(image_tool_mod, "_port_generate_image", fake_generate_image)

    async def scripted_stream(messages, info):
        """Turn 1: call the fill. Turn 2: narrate the result:"""
        for part in messages[-1].parts:
            if getattr(part, "tool_name", None) == "generate_image":
                yield "Done."
                return
        yield {1: DeltaToolCall(name="generate_image",
                                json_args='{"prompt": "x"}', tool_call_id="tc1")}

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=scripted_stream)),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, image_generation=True),
                     media=MediaModels(image_model="openai:gpt-image-1.5"))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="draw"))]
    by_type = {e.type: e for e in events}
    assert by_type["run_completed"].output == "Done."

    # The PICKED model drew it (the fill ran despite the native flag), billed
    # at ITS registry rate ($0.034/image -> 34,000 micro-USD):
    (spend,) = db.query(AISpendEvent).filter_by(owner_id="u1", kind="image").all()
    assert spend.model_id == "openai:gpt-image-1.5"
    assert spend.cost_microusd == 34_000


# ============================================================================
# D41 — the speech_output stage
# ============================================================================

@pytest.mark.asyncio
async def test_speech_output_stage_speaks_the_run(db, monkeypatch):
    async def fake_synthesize(text, models):
        assert text == "the plan is 4242"
        return SpeechResult(audio=b"WAV-BYTES", mime_type="audio/wav",
                            model_id=models.resolved_tts(),
                            usage=Usage(input_tokens=5, output_audio_tokens=1000),
                            characters=len(text))

    import platform.services.generative_ai_service.media as media_pkg
    monkeypatch.setattr(media_pkg, "synthesize_speech", fake_synthesize)
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="the plan is 4242")),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, speech_output=True))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="plan?"))]
    by_type = {e.type: e for e in events}

    # The audio streamed and persisted, and the run reports its item:
    media_event = by_type["media_generated"]
    assert media_event.media == "audio" and media_event.mime_type == "audio/wav"
    hist = full_history(db, conv.id, owner_id="u1")
    audio_item = next(i for i in hist
                      if any(getattr(p, "type", "") == "media" and p.media == "audio" for p in i.parts))
    assert audio_item.meta.get("modality") == "voice"
    assert audio_item.id in by_type["run_completed"].item_ids

    # Billed kind="voice" through the P4 ledger:
    (spend,) = db.query(AISpendEvent).filter_by(owner_id="u1", kind="voice").all()
    assert spend.cost_microusd > 0


@pytest.mark.asyncio
async def test_speech_output_failure_never_kills_the_run(db, monkeypatch):
    async def broken_synthesize(text, models):
        raise RuntimeError("TTS provider down")

    import platform.services.generative_ai_service.media as media_pkg
    monkeypatch.setattr(media_pkg, "synthesize_speech", broken_synthesize)
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test",
                     features=AgentFeatures(rag_search=False, speech_output=True))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="hi"))]
    # Silence, never a dead run (D41 best-effort):
    assert events[-1].type == "run_completed"
    assert not any(e.type == "media_generated" for e in events)


# ============================================================================
# The read-aloud endpoint (the textual UI's button)
# ============================================================================

@pytest.mark.asyncio
async def test_read_aloud_endpoint(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from infrastructure.dependencies import get_db, get_current_verified_user
    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import append_item
    from platform.services.generative_ai_service.transcript.types import Item, Role, TextPart

    async def fake_synthesize(text, models):
        return SpeechResult(audio=b"SPOKEN:" + text.encode(), mime_type="audio/wav",
                            model_id=models.resolved_tts(),
                            usage=Usage(input_tokens=5, output_audio_tokens=100),
                            characters=len(text))

    monkeypatch.setattr(routers_mod, "synthesize_speech", fake_synthesize)

    app = FastAPI()
    app.include_router(routers_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_verified_user] = lambda: SimpleNamespace(id=7)

    conv = create_conversation(db, owner_id="7")
    item = append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message, role=Role.assistant,
                                parts=[TextPart(text="read me aloud")]), owner_id="7")
    other_conv = create_conversation(db, owner_id="someone-else")
    foreign = append_item(db, Item(conversation_id=other_conv.id, kind=ItemKind.message,
                                   role=Role.assistant, parts=[TextPart(text="secret")]), owner_id="someone-else")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Raw text:
        response = await client.post("/ai/speech", json={"text": "hello there"})
        assert response.status_code == 200
        assert response.content == b"SPOKEN:hello there"
        assert response.headers["content-type"].startswith("audio/wav")

        # An owned item (the UI button path):
        response = await client.post("/ai/speech", json={"item_id": str(item.id)})
        assert response.status_code == 200 and response.content == b"SPOKEN:read me aloud"

        # Someone else's item is a 404, never spoken:
        response = await client.post("/ai/speech", json={"item_id": str(foreign.id)})
        assert response.status_code == 404

        # Nothing to speak:
        response = await client.post("/ai/speech", json={})
        assert response.status_code == 422

    # Both syntheses billed kind="voice" to the caller:
    spends = db.query(AISpendEvent).filter_by(owner_id="7", kind="voice").all()
    assert len(spends) == 2 and all(s.cost_microusd > 0 for s in spends)