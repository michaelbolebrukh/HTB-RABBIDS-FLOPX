"""
OpenRouter audio adapter — long-tail batch STT (Phase 7, D43).

OpenRouter shipped OpenAI-compatible audio endpoints in May 2026; its
`/api/v1/audio/transcriptions` rides the SAME key as our D28 chat fallback
lane, exposing whisper-v3 / Chirp 3 / Voxtral / MAI-Transcribe and friends
behind `openrouter:<model>` ids. Batch only — OpenRouter has no realtime
socket, so streaming voice always goes direct (MEDIA_PROVIDERS.md).
"""

from __future__ import annotations

import httpx

from infrastructure.settings import settings

from ..types import MediaCallFailedError, TranscriptResult
from ._json import json_payload
from .openai_media import pcm16_to_wav


_TIMEOUT_S = 120.0


def api_key() -> str | None:
    """Reading the OpenRouter key (blank normalized to None):"""
    value = settings.OPENROUTER_API_KEY
    return value if value and value.strip() else None


async def transcribe(pcm: bytes, sample_rate: int, model_name: str) -> TranscriptResult:
    """POST /api/v1/audio/transcriptions (OpenAI-compatible multipart):"""
    wav = pcm16_to_wav(pcm, sample_rate)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key()}"},
            files={"file": ("turn.wav", wav, "audio/wav")},
            data={"model": model_name},
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"openrouter stt failed ({response.status_code}): {response.text[:200]}")
    minutes = (len(pcm) / (sample_rate * 2)) / 60
    return TranscriptResult(
        text=json_payload(response, "openrouter stt").get("text", ""),
        model_id=f"openrouter:{model_name}",
        usage=None, audio_minutes=minutes,
    )
