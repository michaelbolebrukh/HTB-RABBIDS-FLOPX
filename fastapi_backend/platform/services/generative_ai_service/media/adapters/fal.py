"""
fal.ai adapter — the media long-tail lane (Phase 7, D43).

One key exposes hundreds of image models (FLUX, Ideogram, Recraft, Grok
Imagine, Nano Banana copies…) and the TTS long tail (ElevenLabs, MiniMax,
Qwen3-TTS, Kokoro…) at direct-price parity (MEDIA_PROVIDERS.md). A fal model
id is `fal:<endpoint-path>` — the name part IS the fal endpoint (e.g.
`fal:fal-ai/nano-banana`), so any catalog model works day one; a wrong path
fails loudly at call time.

Dormant without `FAL_API_KEY` (port validation rejects fal:* models loudly).
Uses fal's synchronous `fal.run` surface; generated media comes back as
hosted URLs (stored on MediaPart.url — the object-storage follow-up O14
covers copying these into our own storage later).
"""

from __future__ import annotations

import httpx

from infrastructure.settings import settings

from ..types import ImageResult, MediaCallFailedError, SpeechResult
from ._json import json_payload


_TIMEOUT_S = 300.0                        # Image endpoints can queue under load


def api_key() -> str | None:
    """Reading the fal key (blank normalized to None):"""
    value = settings.FAL_API_KEY
    return value if value and value.strip() else None


async def _run(endpoint: str, body: dict) -> dict:
    """One synchronous fal.run call; non-200s become typed media errors:"""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            f"https://fal.run/{endpoint}",
            headers={"Authorization": f"Key {api_key()}"},
            json=body,
        )
    if response.status_code != 200:
        raise MediaCallFailedError(f"fal call failed ({response.status_code}): {response.text[:200]}")
    return json_payload(response, "fal call")


async def generate_image(prompt: str, endpoint: str, size: str) -> ImageResult:
    """Running a fal image endpoint; the common response shape is images[{url}]:"""
    body: dict = {"prompt": prompt, "num_images": 1}
    if size and "x" in size:
        width, height = size.split("x", 1)
        body["image_size"] = {"width": int(width), "height": int(height)}
    payload = await _run(endpoint, body)

    images = payload.get("images") or ([payload["image"]] if payload.get("image") else [])
    if not images:
        raise MediaCallFailedError(f"fal endpoint {endpoint!r} returned no images")
    first = images[0] if isinstance(images[0], dict) else {"url": images[0]}
    return ImageResult(
        model_id=f"fal:{endpoint}",
        mime_type=first.get("content_type") or "image/png",
        url=first.get("url"),
    )


async def synthesize(text: str, endpoint: str, voice: str) -> SpeechResult:
    """Running a fal TTS endpoint; audio comes back as a hosted URL we fetch:"""
    body: dict = {"text": text}
    if voice != "default":
        body["voice"] = voice             # Endpoint-specific voice ids pass through
    payload = await _run(endpoint, body)

    audio = payload.get("audio") or {}
    url = audio.get("url") if isinstance(audio, dict) else audio
    if not url:
        raise MediaCallFailedError(f"fal endpoint {endpoint!r} returned no audio")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        fetched = await client.get(url)
    if fetched.status_code != 200:
        raise MediaCallFailedError(f"fal audio fetch failed ({fetched.status_code})")
    mime = (audio.get("content_type") if isinstance(audio, dict) else None) or "audio/mpeg"
    return SpeechResult(
        audio=fetched.content, mime_type=mime,
        model_id=f"fal:{endpoint}", usage=None, characters=len(text),
    )
