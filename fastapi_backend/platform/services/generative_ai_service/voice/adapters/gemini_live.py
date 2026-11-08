"""
Gemini Live adapter — second native speech-to-speech relay (Phase 6, D37).

The sibling sanctioned D39 seam to openai_realtime.py, proving the port is
real: two providers with entirely different wire shapes behind ONE
VoiceSession contract. Gemini Live speaks through the `google-genai` SDK's
async live API rather than a raw socket.

Protocol notes: `client.aio.live.connect(...)` yields a session;
`send_realtime_input` streams mic audio; `receive()` yields server messages
carrying audio bytes, INCREMENTAL input/output transcription fragments, a
`turn_complete` flag, and `usage_metadata` with per-modality token splits.
Fragments are accumulated and flushed as final transcripts on turn
completion. `_usage_from_metadata` is a pure extractor (offline-testable);
the message loop itself is exercised against a scripted fake session.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from ...engine.providers import _api_key
from ...engine.types import RunDeps
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


def _usage_from_metadata(metadata: Any) -> Usage | None:
    """
    Extracting our Usage from Gemini's usage_metadata: totals split by
    modality (TEXT vs AUDIO) in prompt/response token detail lists. Pure and
    defensive — the SDK object shape is provider-owned:
    """
    if metadata is None:
        return None

    def _split(details: Any) -> tuple[int, int]:
        """Summing (text_tokens, audio_tokens) out of a modality-detail list:"""
        text = audio = 0
        for entry in details or []:
            modality = str(getattr(entry, "modality", "")).upper()
            count = getattr(entry, "token_count", 0) or 0
            if "AUDIO" in modality:
                audio += count
            else:
                text += count
        return text, audio

    input_text, input_audio = _split(getattr(metadata, "prompt_tokens_details", None))
    output_text, output_audio = _split(getattr(metadata, "response_tokens_details", None))

    # Falling back to the undivided totals when no modality detail came back:
    if not any((input_text, input_audio)):
        input_text = getattr(metadata, "prompt_token_count", 0) or 0
    if not any((output_text, output_audio)):
        output_text = getattr(metadata, "response_token_count", 0) or 0

    return Usage(
        input_tokens=input_text, output_tokens=output_text,
        input_audio_tokens=input_audio or None, output_audio_tokens=output_audio or None,
    )


class GeminiLiveSession(QueueVoiceSession):
    """One relayed Gemini Live session behind the shared VoiceSession contract:"""

    def __init__(self, config: VoiceConfig, runtime: VoiceRuntime, deps: RunDeps, context_text: str = ""):
        super().__init__(config, runtime)
        del deps  # Native sessions hold state provider-side; no engine run occurs
        self._context_text = context_text
        self._key = _api_key("google")
        if self._key is None:
            raise VoiceNotAvailableError("Gemini Live voice needs a Google key (none configured)")
        self._ctx: Any = None              # The SDK's connect() context manager
        self._session: Any = None
        self._reader: asyncio.Task | None = None
        # Incremental transcription fragments, flushed on turn completion:
        self._input_fragments: list[str] = []
        self._output_fragments: list[str] = []

    def _connect(self) -> Any:
        """Building the SDK connect() context (isolated so tests can override it):"""
        from google import genai
        from google.genai import types as genai_types

        instructions = self.config.spec.instructions or ""
        if self._context_text:
            instructions += (
                "\n\n[Recent conversation context — continue naturally from here]\n"
                f"{self._context_text}\n[End context]"
            )
        client = genai.Client(api_key=self._key)
        live_config = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=instructions or None,
            # Transcription both ways — we persist what was actually said (D36):
            input_audio_transcription={},
            output_audio_transcription={},
        )
        return client.aio.live.connect(
            model=self.config.spec.model.split(":", 1)[1], config=live_config,
        )

    async def start(self) -> None:
        """Connecting the provider leg and starting the reader:"""
        self._ctx = self._connect()
        self._session = await self._ctx.__aenter__()
        await self.runtime.on_session_started("native")
        await self._emit(VoiceSessionStarted(
            session_id=self.runtime.session_id, conversation_id=self.runtime.conversation_id,
            implementation="native", model=self.config.spec.model,
        ))
        self._reader = asyncio.create_task(self._read_loop())

    async def send_audio(self, audio_b64: str) -> None:
        """Relaying one mic chunk (Gemini takes raw PCM bytes with a rate-tagged mime):"""
        if self._session is not None:
            from google.genai import types as genai_types
            await self._session.send_realtime_input(audio=genai_types.Blob(
                data=base64.b64decode(audio_b64),
                mime_type=f"audio/pcm;rate={self.config.input_sample_rate}",
            ))

    async def end_turn(self) -> None:
        """No-op: Gemini Live's server-side VAD owns turn boundaries."""

    async def _read_loop(self) -> None:
        """Relaying server messages down: audio out, transcripts, usage, turn flushes:"""
        session_id = self.runtime.session_id
        try:
            async for message in self._session.receive():
                # Outbound audio arrives as raw bytes on `data`:
                data = getattr(message, "data", None)
                if data:
                    await self._emit(AudioDelta(
                        session_id=session_id, audio_b64=base64.b64encode(data).decode(),
                    ))

                content = getattr(message, "server_content", None)
                if content is not None:
                    # Incremental transcription fragments (both directions):
                    fragment = getattr(getattr(content, "input_transcription", None), "text", None)
                    if fragment:
                        self._input_fragments.append(fragment)
                        await self._emit(InputTranscript(session_id=session_id, text=fragment, final=False))
                    fragment = getattr(getattr(content, "output_transcription", None), "text", None)
                    if fragment:
                        self._output_fragments.append(fragment)
                        await self._emit(OutputTranscript(session_id=session_id, text=fragment, final=False))

                    # Turn completion is the persistence moment (D36):
                    if getattr(content, "turn_complete", False):
                        await self._flush_turn()

                # Usage metering + the mid-session budget re-check (PHASE6):
                usage = _usage_from_metadata(getattr(message, "usage_metadata", None))
                if usage is not None and not await self._report_usage(usage):
                    await self._end("budget_exhausted")
                    await self._close_provider()
                    return
            await self._end("provider_closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — typed terminal event, never a silent drop:
            logger.exception("gemini live relay failed")
            await self._fail(f"{type(exc).__name__}: {exc}")

    async def _flush_turn(self) -> None:
        """Finalizing the accumulated fragments as the turn's transcripts:"""
        session_id = self.runtime.session_id
        if self._input_fragments:
            text = "".join(self._input_fragments)
            self._input_fragments.clear()
            await self._emit(InputTranscript(session_id=session_id, text=text, final=True))
            await self.runtime.on_user_transcript(text)
        if self._output_fragments:
            text = "".join(self._output_fragments)
            self._output_fragments.clear()
            await self._emit(OutputTranscript(session_id=session_id, text=text, final=True))
            await self.runtime.on_assistant_transcript(text)

    async def close(self) -> None:
        """Ending the session: stop the reader, close the provider leg, flush, emit:"""
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown is best-effort:
                pass
            self._reader = None
        await self._flush_turn()           # A close mid-turn must not drop said words
        await self._close_provider()
        await self._end("client_closed")

    async def _close_provider(self) -> None:
        """Closing the SDK context defensively (idempotent):"""
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — teardown is best-effort:
                pass
            self._ctx = None
            self._session = None
