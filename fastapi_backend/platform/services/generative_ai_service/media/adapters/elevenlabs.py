"""
ElevenLabs adapter — premium voices + cloning lane (Phase 7, D43).

Dormant without `ELEVENLABS_API_KEY` (the port's validation rejects
elevenlabs:* models loudly when the key is absent — never a degraded call).
Voices are ElevenLabs voice ids passed through verbatim, which is exactly
what makes the 10k+ library and user-cloned voices reachable without the
platform ever holding a voice list (D42).

Unit billing: ElevenLabs bills per character; responses carry no token usage,
so SpeechResult carries the character quantity for the D44 unit path.
"""

from __future__ import annotations

import httpx

from infrastructure.settings import settings

from ..types import MediaCallFailedError, SpeechResult


_TIMEOUT_S = 120.0
# "Rachel" — ElevenLabs' canonical premade voice, used for "default":
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


def api_key() -> str | None:
    """Reading the ElevenLabs key (blank normalized to None):"""
    value = settings.ELEVENLABS_API_KEY
    return value if value and value.strip() else None


async def synthesize(text: str, model_name: str, voice: str) -> SpeechResult:
    """POST /v1/text-to-speech/{voice_id} -> MP3 bytes:"""
    voice_id = _DEFAULT_VOICE_ID if voice == "default" else voice
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key() or ""},
            json={"text": text, "model_id": model_name},
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"elevenlabs tts failed ({response.status_code}): {response.text[:200]}")
    return SpeechResult(
        audio=response.content, mime_type="audio/mpeg",
        model_id=f"elevenlabs:{model_name}", usage=None, characters=len(text),
    )
