"""
Voice port — the VoiceSession interface and D37 selection (Phase 6).

`build_voice_session` is the ONE entry point: it runs the budget pre-flight,
renders recent conversation context in (so a voice session continues the
conversation rather than starting cold), and picks the implementation the
same way D34 picks tools — the registry decides:

- `capabilities.realtime` on the spec's model -> the provider's NATIVE
  realtime adapter (speech-to-speech, barge-in, low latency);
- anything else -> the STT -> `stream_agent` -> TTS PIPELINE under the same
  interface — every model is voice-capable from day one, degraded gracefully
  to turn-based latency.

This module never imports a provider SDK (D39); adapters are looked up from a
small registry so tests can swap in a scripted fake without patching imports.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from ..engine.model_info import get_model_info
from ..engine.types import BudgetCheckUnavailableError, BudgetExceededError, RunDeps
from ..billing import check_budget
from ..transcript import get_current_path
from ..transcript.types import ItemKind, Usage
from .transcriptor import VoiceRuntime
from .types import (
    VoiceConfig,
    VoiceEvent,
    VoiceFailed,
    VoiceNotAvailableError,
    VoiceSessionEnded,
    VoiceUsage,
)
from infrastructure.settings import settings


logger = logging.getLogger(__name__)

# How much recent conversation text is rendered into a native session's
# instructions at connect (chars, roughly 2k tokens) — enough continuity for a
# call without re-sending the whole transcript to a per-minute-billed session:
_CONTEXT_CHAR_BUDGET = 8_000


# ============================================================================
# THE INTERFACE
# ============================================================================

class VoiceSession(ABC):
    """
    One live voice session. The caller pushes mic audio in and consumes OUR
    VoiceEvents out; everything else (provider relay, pipeline turns,
    persistence, billing) happens behind this interface:
    """

    @abstractmethod
    async def start(self) -> None:
        """Bringing the session up (provider leg / pipeline readiness) and
        emitting VoiceSessionStarted. Called once, before any audio."""

    @abstractmethod
    async def send_audio(self, audio_b64: str) -> None:
        """Feeding one base64 chunk of caller microphone audio into the session."""

    @abstractmethod
    async def end_turn(self) -> None:
        """Committing the buffered input as a completed user turn (pipeline mode;
        native adapters with server-side voice-activity detection may no-op)."""

    @abstractmethod
    def events(self) -> AsyncIterator[VoiceEvent]:
        """THE output stream: audio deltas, transcripts, usage, terminal events."""

    @abstractmethod
    async def close(self) -> None:
        """Ending the session (idempotent): flush transcripts, bill, emit the terminal event."""


class QueueVoiceSession(VoiceSession):
    """
    Shared adapter machinery: an internal queue feeding `events()`, terminal
    handling, and the usage->billing->budget-recheck step every implementation
    must perform identically. Adapters emit via `_emit` and report usage via
    `_report_usage`; the base guarantees the stream ends exactly once:
    """

    def __init__(self, config: VoiceConfig, runtime: VoiceRuntime):
        self.config = config
        self.runtime = runtime
        self._queue: asyncio.Queue[VoiceEvent | None] = asyncio.Queue()
        self._ended = False

    async def _emit(self, event: VoiceEvent) -> None:
        """Queuing one event for the consumer (None sentinel is internal only):"""
        await self._queue.put(event)

    async def _report_usage(self, usage: Usage, model_id: str | None = None, price=None) -> bool:
        """
        The one usage path (all adapters): surface the event, record the spend
        row (token-priced, or at a caller-precomputed unit price — D44), then
        re-check the budget — False means the session must end now (the caller
        emits the terminal event with reason="budget_exhausted"):
        """
        await self._emit(VoiceUsage(session_id=self.runtime.session_id, usage=usage))
        await self.runtime.on_usage(usage, model_id=model_id, price=price)
        return await self.runtime.budget_allows()

    async def _end(self, reason: str) -> None:
        """Ending the stream exactly once: persist the end marker, emit, close the queue:"""
        if self._ended:
            return
        self._ended = True
        try:
            await self.runtime.on_session_ended(reason)
        except Exception:  # noqa: BLE001 — the terminal event must still reach the client:
            logger.exception("voice session end persistence failed")
        await self._queue.put(VoiceSessionEnded(
            session_id=self.runtime.session_id, reason=reason, item_ids=list(self.runtime.item_ids),
        ))
        await self._queue.put(None)

    async def _fail(self, error: str) -> None:
        """Failing the stream exactly once (typed terminal event, never a silent drop):"""
        if self._ended:
            return
        self._ended = True
        await self._queue.put(VoiceFailed(session_id=self.runtime.session_id, error=error))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[VoiceEvent]:
        """Draining the queue until the terminal sentinel:"""
        while True:
            event = await self._queue.get()
            if event is None:
                # Re-arming the sentinel so a SECOND iteration terminates at
                # once instead of blocking forever on an empty queue: the
                # stream is exhausted exactly once, but consuming it again
                # (a retried WS handler, a post-terminal drain) must never
                # deadlock — it yields nothing and returns:
                await self._queue.put(None)
                return
            yield event


# ============================================================================
# D37 SELECTION
# ============================================================================

# Native adapter factories by provider — populated lazily (the SDK-importing
# modules load only when a native session is actually requested) and swappable
# in tests. Each factory: (config, runtime, deps, context_text) -> VoiceSession:
_NATIVE_ADAPTERS: dict[str, Callable[..., VoiceSession]] = {}


def _native_factory(provider: str) -> Callable[..., VoiceSession] | None:
    """Resolving the native adapter for a provider (lazy import keeps D39 seams cold):"""
    if provider in _NATIVE_ADAPTERS:
        return _NATIVE_ADAPTERS[provider]
    if provider == "openai":
        from .adapters.openai_realtime import OpenAIRealtimeSession
        _NATIVE_ADAPTERS[provider] = OpenAIRealtimeSession
    elif provider == "google":
        from .adapters.gemini_live import GeminiLiveSession
        _NATIVE_ADAPTERS[provider] = GeminiLiveSession
    return _NATIVE_ADAPTERS.get(provider)


async def _recent_context_text(deps: RunDeps) -> str:
    """
    Rendering a bounded recent-conversation digest for the session's opening
    instructions (D36: the port renders context IN). Newest-last, trimmed to
    the char budget from the front so the most recent turns always survive:
    """
    path = await asyncio.to_thread(
        get_current_path, deps.db, deps.conversation_id, owner_id=deps.owner_id,
    )
    # Releasing the read transaction immediately — voice sessions are long-lived
    # and must not pin a pooled connection idle-in-transaction (the P2 rule):
    await asyncio.to_thread(deps.db.rollback)

    lines: list[str] = []
    for item in path:
        if item.kind != ItemKind.message or item.role is None:
            continue
        text = item.extract_text()
        if text:
            lines.append(f"{item.role.value}: {text}")
    joined = "\n".join(lines)
    return joined[-_CONTEXT_CHAR_BUDGET:] if len(joined) > _CONTEXT_CHAR_BUDGET else joined


async def build_voice_session(config: VoiceConfig, deps: RunDeps) -> VoiceSession:
    """
    Building the session for a conversation: budget pre-flight (same posture
    as the engine's, D32/PHASE4), context render, then D37 selection. The
    returned session has NOT emitted VoiceSessionStarted yet — the adapter
    does that once its provider leg (or pipeline) is actually ready:
    """
    spec = config.spec

    # Pre-flight budget check BEFORE any provider connection (money before sockets):
    if settings.AI_BUDGET_ENFORCEMENT:
        try:
            decision = await asyncio.to_thread(
                check_budget, deps.db,
                owner_id=deps.owner_id, conversation_id=deps.conversation_id, agent_name=spec.name,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed is the deliberate policy:
            logger.exception("voice budget pre-flight failed; blocking session (fail-closed)")
            raise BudgetCheckUnavailableError(str(exc)) from exc
        if not decision.allowed:
            raise BudgetExceededError(decision.binding_budget)

    info = get_model_info(spec.model)
    runtime = VoiceRuntime(
        db=deps.db, owner_id=deps.owner_id, conversation_id=deps.conversation_id,
        model_id=spec.model, agent_name=spec.name,
    )
    context_text = await _recent_context_text(deps)

    # The registry decides (D37) — realtime-capable model + known provider gets
    # the native adapter; everything else speaks through the pipeline. D45: an
    # EXPLICIT pipeline-leg pick (tts_model or stt_model named on the config)
    # beats the native adapter — picking a speciality model means "use it",
    # and only the pipeline can honor it:
    media_forced = config.media.tts_model is not None or config.media.stt_model is not None
    if info.capabilities.realtime and not media_forced:
        factory = _native_factory(info.provider)
        if factory is not None:
            return factory(config, runtime, deps, context_text)
        logger.warning(
            "model %s is realtime-capable but provider %s has no native adapter; using pipeline",
            spec.model, info.provider,
        )

    from .adapters.pipeline import PipelineVoiceSession
    return PipelineVoiceSession(config, runtime, deps, context_text)
