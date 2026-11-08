"""
Voice package — realtime voice port public surface (Phase 6, D36/D37).

Callers (the router, product code, tests) import ONLY from here: the config,
the event union, the session interface, and the builder. Adapters are an
implementation detail behind `build_voice_session` and are never imported
directly (D39 — they are the only sanctioned provider-SDK seams).
"""

from .port import QueueVoiceSession, VoiceSession, build_voice_session
from .transcriptor import VoiceRuntime
from .types import (
    AudioDelta,
    InputTranscript,
    OutputTranscript,
    VoiceConfig,
    VoiceError,
    VoiceEvent,
    VoiceFailed,
    VoiceNotAvailableError,
    VoiceSessionEnded,
    VoiceSessionStarted,
    VoiceUsage,
)


__all__ = [
    "AudioDelta", "InputTranscript", "OutputTranscript",
    "QueueVoiceSession", "VoiceConfig", "VoiceError", "VoiceEvent", "VoiceFailed",
    "VoiceNotAvailableError", "VoiceRuntime", "VoiceSession", "VoiceSessionEnded",
    "VoiceSessionStarted", "VoiceUsage", "build_voice_session",
]
