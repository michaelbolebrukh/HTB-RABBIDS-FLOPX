"""
Media port types — results and errors (Phase 7, D42/D44).

`MediaModels` itself lives in engine/types.py (it is spec data, and the spec
must not import this package) and is re-exported here for callers. Results
carry both the payload and the billing evidence it was made with: token usage
when the provider reported it, unit quantities (characters / images / audio
minutes) otherwise — the D44 rule that media never bills blind.

No SDK imports; adapters live behind the port (media/adapters/*).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.types import (  # noqa: F401 — re-exported spec surface (D42)
    DEFAULT_IMAGE_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    EngineError,
    MediaModels,
)
from ..transcript.types import Usage


# ============================================================================
# RESULTS — payload + billing evidence (D44)
# ============================================================================

@dataclass
class SpeechResult:
    """Synthesized speech: audio payload plus what the call cost to make."""
    audio: bytes
    mime_type: str
    model_id: str
    usage: Usage | None = None            # Token-billed providers (OpenAI) report this
    characters: int = 0                   # Unit-billed quantity (D44)


@dataclass
class TranscriptResult:
    """A transcription: text plus billing evidence (tokens or audio minutes)."""
    text: str
    model_id: str
    usage: Usage | None = None
    audio_minutes: float = 0.0


@dataclass
class ImageResult:
    """
    A generated image: exactly one of `data_b64` / `url` is set (providers
    return either inline bytes or a hosted URL — both map onto MediaPart).
    """
    model_id: str
    mime_type: str = "image/png"
    data_b64: str | None = None
    url: str | None = None
    usage: Usage | None = None
    images: int = 1                       # Unit quantity for D44 pricing


# ============================================================================
# ERRORS
# ============================================================================

class MediaError(EngineError):
    """Base for media-port errors (engine family, so routers map them uniformly)."""


class MediaModelInvalidError(MediaError):
    """
    Raised at validation (D42) when a picked model id cannot serve the
    modality — no adapter for its provider prefix, the registry says it
    cannot output the modality, or its lane's key is missing. Loud and
    immediate, never a degraded call:
    """


class MediaCallFailedError(MediaError):
    """The provider call itself failed (HTTP error, malformed response)."""
