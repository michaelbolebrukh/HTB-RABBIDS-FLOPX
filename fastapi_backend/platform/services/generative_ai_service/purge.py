"""
Owner-level purge — the host's account-deletion hook.

The AI tables are a deliberate FK-less island (`owner_id` is a plain string,
no foreign key into the host's `users` table — see models.py), so the host's
`ON DELETE CASCADE` never reaches them. Any host that deletes a user account
MUST call `purge_owner` in the same transaction, or conversations, memories,
and stored third-party extension credentials would outlive the account and
break the erasure promise the host's privacy policy makes.

Deliberately retained: `ai_spend_events`. Spend rows are billing/financial
records kept under the legal-records carve-out the privacy policy already
discloses (transaction records retained for accounting/compliance); they hold
token counts and costs, not conversation content.

Children are deleted explicitly rather than via FK cascade so behavior is
identical on SQLite (FK enforcement off by default) and PostgreSQL — the same
rule `transcript.delete_conversation` follows.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    AIBudget,
    AIConversation,
    AIMemory,
    AITranscriptItem,
    AITranscriptSummary,
    AIUserExtension,
)


def purge_owner(db: Session, owner_id: str, *, commit: bool = True) -> None:
    """
    Hard-deleting every AI row belonging to one owner: conversations (with
    their transcript items and summary cache), memories, user extensions
    (including the third-party credentials they store), and owner- or
    conversation-scoped budget rows. Pass `commit=False` to run inside the
    host's own transaction (the account-deletion path does, so the user row
    and the AI rows disappear atomically).
    """
    conv_ids = select(AIConversation.id).where(AIConversation.owner_id == owner_id)

    try:
        # Conversation-scoped budget rows key on the conversation uuid as a string:
        conv_id_strings = [str(cid) for cid in db.execute(conv_ids).scalars().all()]
        if conv_id_strings:
            db.execute(
                delete(AIBudget).where(
                    AIBudget.scope_type == "conversation",
                    AIBudget.scope_id.in_(conv_id_strings),
                )
            )
        db.execute(delete(AIBudget).where(AIBudget.scope_type == "owner", AIBudget.scope_id == owner_id))

        db.execute(delete(AITranscriptSummary).where(AITranscriptSummary.conversation_id.in_(conv_ids)))
        db.execute(delete(AITranscriptItem).where(AITranscriptItem.conversation_id.in_(conv_ids)))
        db.execute(delete(AIConversation).where(AIConversation.owner_id == owner_id))

        db.execute(delete(AIMemory).where(AIMemory.owner_id == owner_id))
        db.execute(delete(AIUserExtension).where(AIUserExtension.owner_id == owner_id))

        if commit:
            db.commit()
    except Exception:
        # Rolling back the partial delete before re-raising (repo transaction rule):
        db.rollback()
        raise
