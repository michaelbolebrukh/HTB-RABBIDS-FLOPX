"""
Media package — speech + image speciality models public surface (Phase 7).

Callers (engine, voice pipeline, tools, routers) import ONLY from here: the
spec block, the three port functions, the pricing helpers, and the result/
error types. Adapters are an implementation detail behind the port (D43).
"""

from .port import (
    generate_image,
    price_image,
    price_speech,
    price_transcript,
    synthesize_speech,
    transcribe,
    validate,
)
from .types import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    ImageResult,
    MediaCallFailedError,
    MediaError,
    MediaModelInvalidError,
    MediaModels,
    SpeechResult,
    TranscriptResult,
)


__all__ = [
    "DEFAULT_IMAGE_MODEL", "DEFAULT_STT_MODEL", "DEFAULT_TTS_MODEL",
    "ImageResult", "MediaCallFailedError", "MediaError", "MediaModelInvalidError",
    "MediaModels", "SpeechResult", "TranscriptResult",
    "generate_image", "price_image", "price_speech", "price_transcript",
    "synthesize_speech", "transcribe", "validate",
]
