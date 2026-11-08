"""
OpenAI Realtime adapter — native speech-to-speech relay (Phase 6, D37).

A sanctioned D39 provider seam: this module (alone, with its sibling
adapters) may speak OpenAI's realtime wire format. The backend relays ONE
WebSocket per session to the provider — the key never reaches the client,
usage is metered server-side into the ledger, and transcripts persist even if
the client dies (the D37 rationale).

Wire protocol notes (GA realtime API, 2025): client events are JSON frames
(`session.update`, `input_audio_buffer.append`, ...); server events carry a
`type` string. `_translate_server_event` is a PURE function from one server
event to our VoiceEvents, so the whole translation table is offline-testable
without a socket; `_usage_from_done` likewise extracts our Usage from a
`response.done` frame. Both accept the GA event names and their older beta
aliases so a provider-side rename cannot silently mute the relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

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
    VoiceEvent,
    VoiceNotAvailableError,
    VoiceSessionStarted,
)


logger = logging.getLogger(__name__)

_REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"

# The voice OpenAI uses when the config asks for "default":
_DEFAULT_VOICE = "marin"


def _session_update(config: VoiceConfig, context_text: str) -> dict:
    """
    Building the opening `session.update`: agent instructions plus the rendered
    recent-conversation block (D36 — the port renders context IN), our audio
    format, input transcription on (we persist what was said), and server-side
    voice-activity detection matching the interruptions toggle:
    """
    instructions = config.spec.instructions or ""
    if context_text:
        instructions += (
            "\n\n[Recent conversation context — continue naturally from here]\n"
            f"{context_text}\n[End context]"
        )
    voice = _DEFAULT_VOICE if config.voice == "default" else config.voice
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": config.input_sample_rate},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "server_vad"} if config.interruptions else None,
                },
                "output": {"voice": voice},
            },
        },
    }


def _translate_server_event(event: dict, session_id) -> list[VoiceEvent]:
    """
    The pure translation table: one provider server event -> our VoiceEvents.
    Unknown event types translate to nothing (the provider adds types freely);
    terminal/usage/error frames are handled by the reader, not here:
    """
    kind = event.get("type", "")

    # Outbound audio to play (GA name, then the beta alias):
    if kind in ("response.output_audio.delta", "response.audio.delta"):
        audio = event.get("delta") or ""
        return [AudioDelta(session_id=session_id, audio_b64=audio)] if audio else []

    # The user's words, transcribed server-side (final per utterance):
    if kind == "conversation.item.input_audio_transcription.completed":
        return [InputTranscript(session_id=session_id, text=event.get("transcript") or "", final=True)]

    # The agent's words, streamed then finalized:
    if kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
        delta = event.get("delta") or ""
        return [OutputTranscript(session_id=session_id, text=delta, final=False)] if delta else []
    if kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
        return [OutputTranscript(session_id=session_id, text=event.get("transcript") or "", final=True)]

    return []


def _usage_from_done(event: dict) -> Usage | None:
    """Extracting our Usage from a `response.done` frame's usage block (None when absent):"""
    usage_raw = (event.get("response") or {}).get("usage")
    if not usage_raw:
        return None
    input_details = usage_raw.get("input_token_details") or {}
    output_details = usage_raw.get("output_token_details") or {}
    return Usage(
        # Text tokens ride the ordinary dimensions; audio tokens their own —
        # the provider reports them disjointly, so nothing double-counts:
        input_tokens=input_details.get("text_tokens", 0) or 0,
        output_tokens=output_details.get("text_tokens", 0) or 0,
        cache_read_tokens=(input_details.get("cached_tokens") or None),
        input_audio_tokens=input_details.get("audio_tokens") or None,
        output_audio_tokens=output_details.get("audio_tokens") or None,
    )


class OpenAIRealtimeSession(QueueVoiceSession):
    """
    One relayed native session: client audio up via `send_audio`, our
    VoiceEvents down via `events()`, persistence + billing through the shared
    VoiceRuntime at the semantic moments the wire protocol marks:
    """

    def __init__(self, config: VoiceConfig, runtime: VoiceRuntime, deps: RunDeps, context_text: str = ""):
        super().__init__(config, runtime)
        del deps  # Native sessions hold state provider-side; no engine run occurs
        self._context_text = context_text
        self._key = _api_key("openai")
        if self._key is None:
            raise VoiceNotAvailableError("OpenAI realtime voice needs an OpenAI key (none configured)")
        self._ws: Any = None
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        """Connecting the provider leg, configuring the session, starting the reader:"""
        model_name = self.config.spec.model.split(":", 1)[1]
        self._ws = await websockets.connect(
            _REALTIME_URL.format(model=model_name),
            additional_headers={"Authorization": f"Bearer {self._key}"},
        )
        await self._ws.send(json.dumps(_session_update(self.config, self._context_text)))
        await self.runtime.on_session_started("native")
        await self._emit(VoiceSessionStarted(
            session_id=self.runtime.session_id, conversation_id=self.runtime.conversation_id,
            implementation="native", model=self.config.spec.model,
        ))
        self._reader = asyncio.create_task(self._read_loop())

    async def send_audio(self, audio_b64: str) -> None:
        """Relaying one mic chunk into the provider's input buffer:"""
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64}))

    async def end_turn(self) -> None:
        """Committing the input buffer explicitly (server VAD usually does this itself):"""
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await self._ws.send(json.dumps({"type": "response.create"}))

    async def _read_loop(self) -> None:
        """Relaying server events down: translate, persist transcripts, meter usage:"""
        try:
            async for frame in self._ws:
                event = json.loads(frame)
                kind = event.get("type", "")

                for translated in _translate_server_event(event, self.runtime.session_id):
                    await self._emit(translated)
                    # Final transcripts are the persistence moments (D36):
                    if isinstance(translated, InputTranscript) and translated.final:
                        await self.runtime.on_user_transcript(translated.text)
                    elif isinstance(translated, OutputTranscript) and translated.final:
                        await self.runtime.on_assistant_transcript(translated.text)

                # Usage lands per response — bill it and re-check the budget so
                # a long call cannot outrun an enforced budget (PHASE6):
                if kind == "response.done":
                    usage = _usage_from_done(event)
                    if usage is not None and not await self._report_usage(usage):
                        await self._end("budget_exhausted")
                        await self._close_ws()
                        return

                if kind == "error":
                    message = (event.get("error") or {}).get("message", "provider error")
                    logger.warning("openai realtime error frame: %s", message)
                    await self._fail(message)
                    await self._close_ws()
                    return
            # The provider closed the socket — a normal end from our side:
            await self._end("provider_closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — typed terminal event, never a silent drop:
            logger.exception("openai realtime relay failed")
            await self._fail(f"{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        """Ending the session: stop the reader, close the provider leg, emit the terminal:"""
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown is best-effort:
                pass
            self._reader = None
        await self._close_ws()
        await self._end("client_closed")

    async def _close_ws(self) -> None:
        """Closing the provider socket defensively (idempotent):"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — teardown is best-effort:
                pass
            self._ws = None
