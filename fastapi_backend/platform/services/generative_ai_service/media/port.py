"""
Media port — speech + image speciality calls behind one surface (Phase 7).

The three port functions (`synthesize_speech`, `transcribe`, `generate_image`)
are the ONLY way the engine, voice pipeline, tools, and routers reach media
providers. Each call:

1. resolves the model (spec choice or platform default, D42);
2. validates it — provider prefix has an adapter for the modality, the lane's
   key exists, and the registry (when it knows the model) says the model can
   serve the modality — loudly, before any network call;
3. routes to the provider adapter (D43 lanes);
4. returns a result carrying billing evidence for the D44 pricing helpers.

Pricing rule (D44): token-billed evidence wins when the registry prices those
tokens; otherwise the unit path (characters / images / audio minutes) prices
the call, pessimistically expensive when the model is unpriced.
"""

from __future__ import annotations

from ..billing import PessimisticMediaRates, PessimisticRates, PriceResult, price_media, price_usage
from ..registry import get_model_info
from .adapters import elevenlabs as _elevenlabs
from .adapters import fal as _fal
from .adapters import google_media as _google
from .adapters import openai_media as _openai
from .adapters import openrouter_audio as _openrouter
from .types import (
    ImageResult,
    MediaModelInvalidError,
    MediaModels,
    SpeechResult,
    TranscriptResult,
)


# Provider prefix -> (adapter module, modalities it serves). The key check
# calls `adapter.api_key()` at resolve time (dynamic attribute lookup, so
# tests can patch a lane's key reader). Adding a lane = one adapter module +
# one row here (D43):
_LANES: dict[str, tuple] = {
    "openai": (_openai, {"tts", "stt", "image"}),
    "google": (_google, {"tts", "stt", "image"}),
    "elevenlabs": (_elevenlabs, {"tts"}),
    "fal": (_fal, {"tts", "image"}),
    "openrouter": (_openrouter, {"stt"}),
}

# Modality -> the registry output-modality string it requires:
_MODALITY_OUTPUT = {"tts": "audio", "image": "image"}


def _resolve(model_id: str, modality: str):
    """
    D42 validation: returning (adapter, model_name) for a picked id, or
    raising MediaModelInvalidError with the exact reason — never a degraded
    call, never a guess:
    """
    provider, sep, name = model_id.partition(":")
    if not sep or not provider or not name:
        raise MediaModelInvalidError(f"invalid media model id {model_id!r}; expected 'provider:model'")

    lane = _LANES.get(provider)
    if lane is None or modality not in lane[1]:
        raise MediaModelInvalidError(
            f"no {modality} lane for provider {provider!r} "
            f"(available: {sorted(p for p, l in _LANES.items() if modality in l[1])})"
        )
    adapter = lane[0]
    if adapter.api_key() is None:
        raise MediaModelInvalidError(
            f"{modality} model {model_id!r} needs the {provider} key, which is not configured"
        )

    # Registry modality check — only when the registry actually KNOWS the
    # model (unknown ids stay usable day one and fail at call time instead):
    info = get_model_info(model_id)
    required = _MODALITY_OUTPUT.get(modality)
    if required and info.source != "default" and required not in info.modalities.output:
        raise MediaModelInvalidError(
            f"registry says {model_id!r} cannot output {required} (modalities.output={info.modalities.output})"
        )
    return adapter, name


def validate(models: MediaModels, *modalities: str) -> None:
    """
    Eagerly validating a spec's choices for the given modalities ("tts",
    "stt", "image") — the connect-time check voice sessions run so a broken
    pick fails BEFORE audio starts flowing, not mid-conversation:
    """
    resolved = {"tts": models.resolved_tts(), "stt": models.resolved_stt(), "image": models.resolved_image()}
    for modality in modalities:
        _resolve(resolved[modality], modality)


# ============================================================================
# THE THREE PORT FUNCTIONS
# ============================================================================

async def synthesize_speech(text: str, models: MediaModels) -> SpeechResult:
    """Synthesizing speech for text with the spec's TTS model + voice (D42):"""
    model_id = models.resolved_tts()
    adapter, name = _resolve(model_id, "tts")
    return await adapter.synthesize(text, name, models.tts_voice)


async def transcribe(pcm: bytes, sample_rate: int, models: MediaModels) -> TranscriptResult:
    """Transcribing raw PCM16 mono audio with the spec's STT model (D42):"""
    model_id = models.resolved_stt()
    adapter, name = _resolve(model_id, "stt")
    return await adapter.transcribe(pcm, sample_rate, name)


async def generate_image(prompt: str, models: MediaModels, size: str = "1024x1024") -> ImageResult:
    """Generating one image with the spec's image model (D42):"""
    model_id = models.resolved_image()
    adapter, name = _resolve(model_id, "image")
    return await adapter.generate_image(prompt, name, size)


# ============================================================================
# D44 PRICING — one helper per result type (token evidence wins when priced)
# ============================================================================

def price_speech(result: SpeechResult) -> PriceResult:
    """Pricing a synthesis: token usage at registry rates, else per-character:"""
    info = get_model_info(result.model_id)
    if result.usage is not None and info.cost.is_priced():
        return price_usage(result.usage, info, pessimistic=PessimisticRates())
    return price_media(result.characters, "characters", info, pessimistic=PessimisticMediaRates())


def price_transcript(result: TranscriptResult) -> PriceResult:
    """Pricing a transcription: token usage at registry rates, else per-minute:"""
    info = get_model_info(result.model_id)
    if result.usage is not None and info.cost.is_priced():
        return price_usage(result.usage, info, pessimistic=PessimisticRates())
    return price_media(result.audio_minutes, "audio_minutes", info, pessimistic=PessimisticMediaRates())


def price_image(result: ImageResult) -> PriceResult:
    """Pricing image generation: per-image is the primary unit (D44):"""
    info = get_model_info(result.model_id)
    if info.cost.per_image is not None:
        return price_media(result.images, "images", info, pessimistic=PessimisticMediaRates())
    if result.usage is not None and info.cost.is_priced():
        return price_usage(result.usage, info, pessimistic=PessimisticRates())
    return price_media(result.images, "images", info, pessimistic=PessimisticMediaRates())