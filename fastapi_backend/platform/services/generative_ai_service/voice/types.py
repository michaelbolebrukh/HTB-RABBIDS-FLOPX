"""
Voice port types — OUR realtime-voice interface (Phase 6, D36/D37).

The voice port bookends the engine's world: audio goes in, our VoiceEvents
come out, and everything said lands in the SAME conversation transcript as
text turns. These types are the whole contract clients and the router see —
no provider realtime SDK, no `pydantic_ai`, and no provider wire format ever
crosses this boundary (D39: adapters/*.py are the only sanctioned SDK seams,
exactly as adapter.py/providers.py are for pydantic_ai).

Event philosophy mirrors the P2 run events: conversation-level happenings in
our domain, discriminated on `type`, so a UI (or a relay) switches on the
string and never learns which provider — or whether the pipeline fallback —
served the session.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field

from ..engine.types import AgentSpec, EngineError, MediaModels
from ..transcript.types import Usage


# ============================================================================
# CONFIG
# ============================================================================

class VoiceConfig(BaseModel):
    """
    Everything a voice session needs beyond the RunDeps identity. The SAME
    AgentSpec that drives text runs drives voice runs — instructions, model,
    features, and limits carry over, so an agent is voice-capable by
    definition rather than by a parallel voice-agent configuration:
    """
    spec: AgentSpec
    voice: str = "default"              # Provider-mapped voice name ("default" = adapter's choice)
    input_sample_rate: int = 16_000     # PCM16 mono in; adapters transcode/declare as needed
    interruptions: bool = True          # Barge-in (native adapters only; pipeline is turn-based)
    # Speciality models for the PIPELINE legs (P7, D42): pickable STT/TTS
    # models like everything else; native sessions ignore this (the agent's
    # own realtime model speaks). `voice` above wins over media.tts_voice
    # when the latter is left "default":
    media: MediaModels = Field(default_factory=MediaModels)


# ============================================================================
# EVENTS (discriminated union on `type` — clients switch on the string)
# ============================================================================

class VoiceSessionStarted(BaseModel):
    """The session is live; `implementation` reports which D37 path serves it."""
    type: Literal["voice_session_started"] = "voice_session_started"
    session_id: UUID
    conversation_id: UUID
    implementation: Literal["native", "pipeline"]
    model: str                          # Canonical id of the model actually serving


class AudioDelta(BaseModel):
    """A chunk of outbound audio for the client to play (base64 PCM16/provider format)."""
    type: Literal["audio_delta"] = "audio_delta"
    session_id: UUID
    audio_b64: str


class InputTranscript(BaseModel):
    """What the USER said, as transcribed. `final=True` marks the committed turn text."""
    type: Literal["input_transcript"] = "input_transcript"
    session_id: UUID
    text: str
    final: bool = False


class OutputTranscript(BaseModel):
    """What the AGENT said, as transcribed. `final=True` marks the completed utterance."""
    type: Literal["output_transcript"] = "output_transcript"
    session_id: UUID
    text: str
    final: bool = False


class VoiceUsage(BaseModel):
    """
    Token usage reported during the session (periodic on native adapters,
    per-stage on the pipeline). The port re-checks the budget on each one so a
    long call cannot outrun an enforced budget (PHASE6 security note):
    """
    type: Literal["voice_usage"] = "voice_usage"
    session_id: UUID
    usage: Usage


class VoiceSessionEnded(BaseModel):
    """Terminal event: why the session ended plus the transcript items it appended."""
    type: Literal["voice_session_ended"] = "voice_session_ended"
    session_id: UUID
    reason: str                         # "client_closed" | "budget_exhausted" | "provider_closed" | ...
    item_ids: list[UUID] = Field(default_factory=list)


class VoiceFailed(BaseModel):
    """Terminal event for a session that died abnormally (typed, never a silent drop)."""
    type: Literal["voice_failed"] = "voice_failed"
    session_id: UUID
    error: str


VoiceEvent = Annotated[
    Union[
        VoiceSessionStarted, AudioDelta, InputTranscript, OutputTranscript,
        VoiceUsage, VoiceSessionEnded, VoiceFailed,
    ],
    Field(discriminator="type"),
]


# ============================================================================
# ERRORS
# ============================================================================

class VoiceError(EngineError):
    """Base for voice-port errors (kept in the engine's error family for router mapping)."""


class VoiceNotAvailableError(VoiceError):
    """
    Raised at connect when no implementation can serve the session (e.g. the
    pipeline fallback has no STT/TTS key) — loud and immediate, never a
    session that half-works (config-minimalism fail-closed):
    """
