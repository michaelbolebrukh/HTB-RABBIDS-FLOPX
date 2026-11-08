"""
Google media adapter — Gemini TTS + Gemini image models over REST (Phase 7).

The second default lane (the platform-default image model is Gemini's
`gemini-3.1-flash-image`, D42). Speaks the `generateContent` REST surface
directly with httpx — no google-genai SDK — so the facade greps stay
untouched (the SDK remains confined to the Gemini Live voice adapter, D39).

Gemini bills media in tokens (usageMetadata is parsed when present), but the
registry also carries per-unit rates so the D44 unit path prices calls whose
metadata comes back empty.
"""

from __future__ import annotations

import httpx

from infrastructure.settings import settings

from ...transcript.types import Usage
from ..types import ImageResult, MediaCallFailedError, SpeechResult, TranscriptResult
from ._json import json_payload
from .openai_media import pcm16_to_wav


_TIMEOUT_S = 120.0
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini TTS returns raw PCM16 at this rate (wrapped to WAV for playability):
_TTS_SAMPLE_RATE = 24_000
# The prebuilt voice used when the spec asks for "default":
_DEFAULT_VOICE = "Kore"


def api_key() -> str | None:
    """Reading the Google key (blank normalized to None, as everywhere):"""
    value = settings.GOOGLE_API_KEY
    return value if value and value.strip() else None


def _usage_from_metadata(payload: dict) -> Usage | None:
    """Extracting our Usage from a generateContent usageMetadata block:"""
    metadata = payload.get("usageMetadata")
    if not metadata:
        return None
    return Usage(
        input_tokens=metadata.get("promptTokenCount", 0) or 0,
        output_tokens=metadata.get("candidatesTokenCount", 0) or 0,
    )


def _inline_parts(payload: dict) -> list[dict]:
    """Collecting the inlineData parts of the first candidate (defensive):"""
    candidates = payload.get("candidates") or []
    if not candidates:
        return []
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return [p["inlineData"] for p in parts if isinstance(p, dict) and p.get("inlineData")]


async def _generate_content(model_name: str, body: dict) -> dict:
    """One generateContent call; non-200s become typed media errors:"""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            f"{_BASE}/{model_name}:generateContent",
            params={"key": api_key()},
            json=body,
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"gemini call failed ({response.status_code}): {response.text[:200]}")
    return json_payload(response, "gemini call")


async def synthesize(text: str, model_name: str, voice: str) -> SpeechResult:
    """Gemini TTS: generateContent with AUDIO response modality -> WAV bytes:"""
    voice_name = _DEFAULT_VOICE if voice == "default" else voice
    payload = await _generate_content(model_name, {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}},
        },
    })
    import base64
    inline = _inline_parts(payload)
    if not inline:
        raise MediaCallFailedError("gemini tts response carried no audio")
    pcm = base64.b64decode(inline[0].get("data") or "")
    return SpeechResult(
        audio=pcm16_to_wav(pcm, _TTS_SAMPLE_RATE), mime_type="audio/wav",
        model_id=f"google:{model_name}", usage=_usage_from_metadata(payload),
        characters=len(text),
    )


async def generate_image(prompt: str, model_name: str, size: str) -> ImageResult:
    """Gemini image models: generateContent with IMAGE response modality:"""
    del size  # Gemini image models pick dimensions from the prompt/config, not a size param
    payload = await _generate_content(model_name, {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    })
    for inline in _inline_parts(payload):
        mime = inline.get("mimeType") or "image/png"
        if mime.startswith("image/") and inline.get("data"):
            return ImageResult(
                model_id=f"google:{model_name}", mime_type=mime,
                data_b64=inline["data"], usage=_usage_from_metadata(payload),
            )
    raise MediaCallFailedError("gemini image response carried no image")


async def transcribe(pcm: bytes, sample_rate: int, model_name: str) -> TranscriptResult:
    """
    Gemini audio-in transcription (the ride-along lane — cheaper than any
    dedicated ASR when the turn already targets Gemini, MEDIA_PROVIDERS.md):
    """
    import base64
    payload = await _generate_content(model_name, {
        "contents": [{"parts": [
            {"text": "Transcribe this audio verbatim. Output ONLY the transcript text."},
            {"inlineData": {
                "mimeType": "audio/wav",
                "data": base64.b64encode(pcm16_to_wav(pcm, sample_rate)).decode(),
            }},
        ]}],
    })
    candidates = payload.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") if candidates else []
    text = "".join(p.get("text", "") for p in (parts or []) if isinstance(p, dict))
    minutes = (len(pcm) / (sample_rate * 2)) / 60
    return TranscriptResult(
        text=text.strip(), model_id=f"google:{model_name}",
        usage=_usage_from_metadata(payload), audio_minutes=minutes,
    )
