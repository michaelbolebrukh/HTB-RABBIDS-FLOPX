"""
OpenAI media adapter — TTS / STT / image over plain REST (Phase 7, D43).

The default lane (D42): every platform default routes here or to Google, on
keys the factory already provisions. Deliberately httpx-only — no SDK — so
the D3/D39 facade greps stay exactly as they are.

Billing evidence: OpenAI's speech models report token usage (parsed
defensively, provider-owned shape); images are priced per unit (the registry
carries per_image). TTS responses carry no usage block, so output audio
tokens are ESTIMATED from duration — the same documented estimate the P6
pipeline used (O13 reconciliation pending).
"""

from __future__ import annotations

import io
import wave

import httpx

from infrastructure.settings import settings

from ...transcript.types import Usage
from ..types import ImageResult, MediaCallFailedError, SpeechResult, TranscriptResult
from ._json import json_payload


_TIMEOUT_S = 120.0
# TTS duration -> estimated audio tokens (documented estimate, O13):
_AUDIO_TOKENS_PER_SECOND = 10
# The voice OpenAI uses when the spec asks for "default":
_DEFAULT_VOICE = "alloy"


def api_key() -> str | None:
    """Reading the OpenAI key (blank normalized to None, as everywhere):"""
    value = settings.OPENAI_API_KEY
    return value if value and value.strip() else None


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrapping raw PCM16 mono in a WAV container (what audio endpoints accept):"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


async def synthesize(text: str, model_name: str, voice: str) -> SpeechResult:
    """POST /v1/audio/speech -> WAV bytes + estimated usage:"""
    voice_name = _DEFAULT_VOICE if voice == "default" else voice
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key()}"},
            json={"model": model_name, "voice": voice_name, "input": text, "response_format": "wav"},
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"openai tts failed ({response.status_code}): {response.text[:200]}")
    audio = response.content
    # No usage block on speech responses — estimating (text at ~4 chars/token,
    # audio out from the 24kHz PCM16 WAV duration):
    seconds = max(len(audio) - 44, 0) / (24_000 * 2)
    usage = Usage(
        input_tokens=max(len(text) // 4, 1),
        output_audio_tokens=int(seconds * _AUDIO_TOKENS_PER_SECOND),
    )
    return SpeechResult(
        audio=audio, mime_type="audio/wav", model_id=f"openai:{model_name}",
        usage=usage, characters=len(text),
    )


async def transcribe(pcm: bytes, sample_rate: int, model_name: str) -> TranscriptResult:
    """POST /v1/audio/transcriptions (WAV multipart) -> text + parsed usage:"""
    wav = pcm16_to_wav(pcm, sample_rate)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key()}"},
            files={"file": ("turn.wav", wav, "audio/wav")},
            data={"model": model_name, "response_format": "json"},
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"openai stt failed ({response.status_code}): {response.text[:200]}")
    payload = json_payload(response, "openai stt")
    usage_raw = payload.get("usage") or {}
    details = usage_raw.get("input_token_details") or {}
    usage = Usage(
        input_tokens=details.get("text_tokens", 0) or 0,
        output_tokens=usage_raw.get("output_tokens", 0) or 0,
        input_audio_tokens=details.get("audio_tokens") or (usage_raw.get("input_tokens", 0) or 0),
    )
    minutes = (len(pcm) / (sample_rate * 2)) / 60
    return TranscriptResult(
        text=payload.get("text", ""), model_id=f"openai:{model_name}",
        usage=usage, audio_minutes=minutes,
    )


async def generate_image(prompt: str, model_name: str, size: str) -> ImageResult:
    """POST /v1/images/generations -> one base64 image:"""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key()}"},
            json={"model": model_name, "prompt": prompt, "size": size, "n": 1},
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"openai image failed ({response.status_code}): {response.text[:200]}")
    data = (json_payload(response, "openai image").get("data") or [{}])[0]
    if not data.get("b64_json") and not data.get("url"):
        raise MediaCallFailedError("openai image response carried no image")
    return ImageResult(
        model_id=f"openai:{model_name}", mime_type="image/png",
        data_b64=data.get("b64_json"), url=data.get("url"),
    )
