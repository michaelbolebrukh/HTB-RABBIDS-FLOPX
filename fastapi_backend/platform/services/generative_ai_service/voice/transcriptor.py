"""
Voice transcriptor — session -> transcript items + ledger rows (Phase 6, D36).

The single place voice sessions touch persistence, shared by every adapter
(native and pipeline), so all implementations write IDENTICAL shapes:

- Session lifecycle -> `event` items (kind=event, meta.modality="voice") that
  bracket the voice span inside the ordinary transcript.
- Transcribed turns -> ordinary `message` items with meta.modality="voice" —
  which is exactly what makes text and voice one conversation: the next TEXT
  run renders these like any other turn (RAG, summarization, budgets intact).
- Usage -> ledger rows `kind="voice"`, priced at the registry's audio/text
  rates, recorded PER USAGE EVENT (not once at close) so the mid-session
  budget re-check reads real spend and a long call cannot outrun a budget.

Sync DB work hops through asyncio.to_thread like the engine (D29). No SDK
imports (the transcriptor sits on OUR side of the D39 seam).
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID, uuid4

from ..billing import PessimisticRates, check_budget, price_usage, record_spend
from ..engine.model_info import get_model_info
from ..transcript import append_item
from ..transcript.types import DataPart, Item, ItemKind, Role, TextPart, Usage
from infrastructure.settings import settings


logger = logging.getLogger(__name__)


class VoiceRuntime:
    """
    Per-session persistence + billing runtime, injected into adapters by
    `build_voice_session`. Adapters call the `on_*` methods at the semantic
    moments; the runtime owns HOW those become items and spend rows:
    """

    def __init__(self, db, owner_id: str, conversation_id: UUID, model_id: str, agent_name: str):
        self.db = db
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.model_id = model_id
        self.agent_name = agent_name
        self.session_id: UUID = uuid4()
        self.item_ids: list[UUID] = []      # Everything this session appended, for VoiceSessionEnded
        self.total_usage = Usage()          # Session total, for reporting (rows are per event)

    # ------------------------------------------------------------------ items

    async def on_session_started(self, implementation: str) -> None:
        """Bracketing the voice span in the transcript with a start event item:"""
        await self._append(Item(
            conversation_id=self.conversation_id, kind=ItemKind.event,
            parts=[DataPart(data={
                "event": "voice_session_started",
                "session_id": str(self.session_id),
                "implementation": implementation,
                "model": self.model_id,
            })],
            meta={"modality": "voice"},
        ))

    async def on_user_transcript(self, text: str) -> None:
        """Persisting a final user utterance as an ordinary user message item:"""
        if not text.strip():
            return
        await self._append(Item(
            conversation_id=self.conversation_id, kind=ItemKind.message, role=Role.user,
            parts=[TextPart(text=text)],
            meta={"modality": "voice", "session_id": str(self.session_id)},
        ))

    async def on_assistant_transcript(self, text: str) -> None:
        """Persisting a final assistant utterance as an ordinary assistant message item:"""
        if not text.strip():
            return
        await self._append(Item(
            conversation_id=self.conversation_id, kind=ItemKind.message, role=Role.assistant,
            parts=[TextPart(text=text)], model_used=self.model_id,
            meta={"modality": "voice", "session_id": str(self.session_id)},
        ))

    async def on_session_ended(self, reason: str) -> None:
        """Closing the bracket with an end event item carrying the session totals:"""
        await self._append(Item(
            conversation_id=self.conversation_id, kind=ItemKind.event,
            parts=[DataPart(data={
                "event": "voice_session_ended",
                "session_id": str(self.session_id),
                "reason": reason,
                "usage": self.total_usage.model_dump(),
            })],
            meta={"modality": "voice"},
        ))

    async def _append(self, item: Item) -> None:
        """Appending one item off-loop and tracking its id for the terminal event:"""
        stored = await asyncio.to_thread(append_item, self.db, item, owner_id=self.owner_id)
        if stored.id is not None:
            self.item_ids.append(stored.id)

    # ---------------------------------------------------------------- billing

    async def on_usage(self, usage: Usage, model_id: str | None = None, price=None) -> None:
        """
        Recording one usage report as a `kind="voice"` ledger row immediately
        (P4 path: registry rates incl. audio dimensions, pessimistic fallback
        for unpriced models). A caller-precomputed `price` wins — that is how
        unit-billed speech legs (per character/minute, D44) enter the ledger.
        A ledger failure never kills the live call — same posture as run
        pricing (money data lost is Sentry-worthy, the user's session is not
        collateral):
        """
        billed_model = model_id or self.model_id
        self._accumulate(usage)
        try:
            if price is None:
                price = price_usage(usage, get_model_info(billed_model), pessimistic=PessimisticRates())
            if price.cost_microusd is not None:
                await asyncio.to_thread(
                    record_spend, self.db,
                    owner_id=self.owner_id, model_id=billed_model, usage=usage, price=price,
                    kind="voice", conversation_id=self.conversation_id,
                    run_id=self.session_id, agent_name=self.agent_name,
                )
        except Exception:  # noqa: BLE001 — deliberate: live calls are not failed over ledger writes:
            logger.exception("voice spend recording failed for session %s", self.session_id)

    def _accumulate(self, usage: Usage) -> None:
        """Summing a usage report into the session total (all dimensions):"""
        t = self.total_usage
        t.input_tokens += usage.input_tokens
        t.output_tokens += usage.output_tokens
        for field in ("reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
                      "input_audio_tokens", "output_audio_tokens"):
            value = getattr(usage, field)
            if value:
                setattr(t, field, (getattr(t, field) or 0) + value)

    async def budget_allows(self) -> bool:
        """
        Re-checking the budget mid-session (PHASE6 security note). Informative
        mode (enforcement off, D32) always allows; with enforcement on, an
        unverifiable check fails CLOSED like the engine's pre-flight:
        """
        if not settings.AI_BUDGET_ENFORCEMENT:
            return True
        try:
            decision = await asyncio.to_thread(
                check_budget, self.db,
                owner_id=self.owner_id, conversation_id=self.conversation_id,
                agent_name=self.agent_name,
            )
        except Exception:  # noqa: BLE001 — fail-closed is the deliberate policy (money):
            logger.exception("voice budget re-check failed; ending session (fail-closed)")
            return False
        return decision.allowed
