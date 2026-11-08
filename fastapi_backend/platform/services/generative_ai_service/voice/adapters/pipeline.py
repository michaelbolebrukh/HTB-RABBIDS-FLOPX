"""
Pipeline voice adapter — STT -> stream_agent -> TTS (Phase 6/7, D37 fallback).

The universal-coverage implementation: any model the engine can run becomes
voice-capable through this session, at turn-based latency. Each committed turn
is transcribed, run through the ORDINARY engine path — so ALL P2–P5 machinery
applies (budgets, tools, RAG, summarization, fallbacks, ledger) — and the
final text is spoken back.

P7 (D42): the speech legs route through the MEDIA PORT, so the STT/TTS models
and the voice are the spec's pickable choices (`VoiceConfig.media`) with the
platform defaults applied when unset — ElevenLabs voices or any registry
speech model become session choices without touching this file. The injected
`stt`/`tts` callables remain as offline test seams.

Persistence split (deliberate): the engine already appends the user turn and
the assistant output as transcript items during `stream_agent` — the pipeline
tags the user turn `meta.modality="voice"` via UserInput.meta and folds the
run's item ids into the session's, rather than double-writing through the
transcriptor (which native adapters use, since no engine run happens there).

Billing split (PHASE6/7): the agent run bills itself (`kind="run"`); this
adapter bills only the speech legs (`kind="voice"`) — token evidence when the
provider reported it, unit pricing otherwise (D44).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable

from ... import media as _media
from ...engine import engine as _engine_mod
from ...engine.types import (
    ApprovalDecision,
    RunCompleted,
    RunDeps,
    RunFailed,
    RunPaused,
    TextDelta,
    UserInput,
)
from ...transcript.types import Usage
from ..port import QueueVoiceSession
from ..transcriptor import VoiceRuntime
from ..types import (
    AudioDelta,
    InputTranscript,
    OutputTranscript,
    VoiceConfig,
    VoiceNotAvailableError,
    VoiceSessionStarted,
)


logger = logging.getLogger(__name__)

# The default speech legs, re-exported under the P6 names (now D42 defaults,
# not pins — VoiceConfig.media overrides them per session):
STT_MODEL_ID = _media.DEFAULT_STT_MODEL
TTS_MODEL_ID = _media.DEFAULT_TTS_MODEL

# Test-seam types (offline fakes): audio bytes -> (text, Usage) and
# text -> (audio bytes, Usage):
SttFn = Callable[[bytes], Awaitable[tuple[str, Usage]]]
TtsFn = Callable[[str], Awaitable[tuple[bytes, Usage]]]


class PipelineVoiceSession(QueueVoiceSession):
    """
    The D37 fallback session. Turn lifecycle: `send_audio` buffers chunks;
    `end_turn` commits them — STT, one ordinary engine run, TTS — emitting
    the same VoiceEvents a native adapter would:
    """

    def __init__(
        self,
        config: VoiceConfig,
        runtime: VoiceRuntime,
        deps: RunDeps,
        context_text: str = "",
        stt: SttFn | None = None,
        tts: TtsFn | None = None,
    ):
        super().__init__(config, runtime)
        self.deps = deps
        # The rendered context is unused here by design: stream_agent renders
        # the (shared!) transcript itself on every turn, voice items included.
        del context_text
        # Resolving the session's speech choices (D42): the VoiceConfig-level
        # `voice` wins when media.tts_voice was left at its default:
        media = config.media
        if config.voice != "default" and media.tts_voice == "default":
            media = media.model_copy(update={"tts_voice": config.voice})
        self._media = media
        # Test seams; production (None) routes through the media port, whose
        # lanes are validated NOW so a broken pick fails at connect (D42):
        self._stt = stt
        self._tts = tts
        if stt is None or tts is None:
            needed = [m for m, seam in (("stt", stt), ("tts", tts)) if seam is None]
            try:
                _media.validate(self._media, *needed)
            except _media.MediaError as exc:
                raise VoiceNotAvailableError(str(exc)) from exc
        self._buffer: list[bytes] = []
        self._turn_lock = asyncio.Lock()   # One committed turn at a time (turn-based by design)

    async def start(self) -> None:
        """Announcing readiness — the pipeline has no provider leg to bring up:"""
        await self.runtime.on_session_started("pipeline")
        await self._emit(VoiceSessionStarted(
            session_id=self.runtime.session_id, conversation_id=self.runtime.conversation_id,
            implementation="pipeline", model=self.config.spec.model,
        ))

    async def send_audio(self, audio_b64: str) -> None:
        """Buffering one mic chunk until the client commits the turn:"""
        self._buffer.append(base64.b64decode(audio_b64))

    async def end_turn(self) -> None:
        """Committing the buffered audio as one full STT -> agent -> TTS turn:"""
        async with self._turn_lock:
            audio = b"".join(self._buffer)
            self._buffer.clear()
            if not audio or self._ended:
                return
            try:
                await self._run_turn(audio)
            except Exception as exc:  # noqa: BLE001 — typed terminal event, never a silent drop:
                logger.exception("pipeline voice turn failed")
                await self._fail(f"{type(exc).__name__}: {exc}")

    async def _transcribe(self, audio: bytes) -> tuple[str, Usage | None, object | None, str]:
        """The STT leg: (text, usage, precomputed price, model id) — seam or port:"""
        if self._stt is not None:
            text, usage = await self._stt(audio)
            return text, usage, None, STT_MODEL_ID
        result = await _media.transcribe(audio, self.config.input_sample_rate, self._media)
        return result.text, result.usage, _media.price_transcript(result), result.model_id

    async def _synthesize(self, text: str) -> tuple[bytes, Usage | None, object | None, str]:
        """The TTS leg: (audio, usage, precomputed price, model id) — seam or port:"""
        if self._tts is not None:
            audio, usage = await self._tts(text)
            return audio, usage, None, TTS_MODEL_ID
        result = await _media.synthesize_speech(text, self._media)
        return result.audio, result.usage, _media.price_speech(result), result.model_id

    async def _drive_run(self, stream, session_id) -> tuple[str, list]:
        """
        Draining one engine run's events, surfacing text deltas as partial
        output transcripts and folding persisted items into the session tally.
        Returns (final_text, pending_approvals) — a non-empty pending list means
        the run PAUSED (P8), which the caller resolves by auto-denying:
        """
        final_text = ""
        pending: list = []
        async for event in stream:
            if isinstance(event, TextDelta):
                await self._emit(OutputTranscript(session_id=session_id, text=event.delta, final=False))
            elif isinstance(event, RunCompleted):
                final_text = "" if event.output is None else str(event.output)
                self.runtime.item_ids.extend(event.item_ids)
            elif isinstance(event, RunPaused):
                # The paused run's checkpoint items still count toward the tally:
                self.runtime.item_ids.extend(event.item_ids)
                pending = event.approvals
            elif isinstance(event, RunFailed):
                raise RuntimeError(event.error)
        return final_text, pending

    async def _run_turn(self, audio: bytes) -> None:
        """One turn: transcribe, run the agent through the ordinary engine path, speak:"""
        session_id = self.runtime.session_id

        # 1. STT — the user's words become text, surfaced and billed:
        text, stt_usage, stt_price, stt_model = await self._transcribe(audio)
        await self._emit(InputTranscript(session_id=session_id, text=text, final=True))
        if not await self._report_usage(stt_usage or Usage(), model_id=stt_model, price=stt_price):
            await self._end("budget_exhausted")
            return
        if not text.strip():
            return  # Silence/noise turn — nothing to run

        # 2. The ordinary engine run (ALL P2–P5 machinery applies). The user
        #    item is tagged as a voice turn via UserInput.meta; streaming text
        #    deltas surface as partial output transcripts:
        user_input = UserInput(text=text, meta={"modality": "voice", "session_id": str(session_id)})
        final_text, pending = await self._drive_run(
            _engine_mod.stream_agent(self.config.spec, self.deps, user_input), session_id,
        )

        # A live voice turn cannot pause for a UI (P8/D46, W5): any
        # approval-gated tool is DENIED with a spoken-friendly reason and the
        # run resumes until it settles. The guard bounds a pathological
        # deny-then-re-call loop rather than hanging the session:
        guard = 0
        while pending and guard < 5:
            guard += 1
            decisions = [
                ApprovalDecision(
                    tool_call_id=a.tool_call_id, approved=False,
                    reason="Approval-gated tools are unavailable in a live voice session.",
                )
                for a in pending
            ]
            final_text, pending = await self._drive_run(
                _engine_mod.resume_agent(self.config.spec, self.deps, decisions), session_id,
            )
        await self._emit(OutputTranscript(session_id=session_id, text=final_text, final=True))

        # 3. TTS — the reply becomes audio, surfaced and billed:
        if final_text.strip():
            audio_out, tts_usage, tts_price, tts_model = await self._synthesize(final_text)
            await self._emit(AudioDelta(
                session_id=session_id, audio_b64=base64.b64encode(audio_out).decode(),
            ))
            if not await self._report_usage(tts_usage or Usage(), model_id=tts_model, price=tts_price):
                await self._end("budget_exhausted")

    async def close(self) -> None:
        """Ending the session normally (idempotent via the base's once-guard):"""
        await self._end("client_closed")
