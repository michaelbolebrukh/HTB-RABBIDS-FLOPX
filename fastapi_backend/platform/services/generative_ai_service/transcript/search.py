"""
Conversation search — one public contract, two backends (Phase 1).

`search_conversation` is the API that P5 later exposes to agents as the
`search_conversation` tool, unchanged. Backends behind it:

- v1 (always available, keyless): case-insensitive text match over the
  `text_content` column that the store extracts on append.
- v2 (used automatically when an embedder is supplied AND items carry
  vectors): cosine ranking IN PYTHON over the conversation's stored vectors.
  A single conversation holds thousands of items at most, so a linear scan is
  plenty — this deliberately avoids a pgvector dependency and keeps the schema
  SQLite-portable (PHASE1.md decision).

The embedder is a tiny protocol so tests can pass a fake and P2 can wrap
Pydantic AI's `Embedder` behind it without this module ever importing an SDK.
"""

from __future__ import annotations

import math
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AITranscriptItem
from .store import _row_to_item, get_conversation, get_current_path
from .types import Item


# Retrieval scopes (D30): what portion of the transcript the model may see.
# "active_path" (default) = only ancestors of the current leaf — abandoned
# branches are treated as retracted content (an edit is usually a retraction,
# and dead branches hold regenerated-away answers that contradict the live
# thread). "all_branches" = everything; an explicit opt-in that P5 gates
# behind a per-agent toggle. Storage always keeps all branches regardless —
# this scopes what is SURFACED, never what is STORED:
SearchScope = Literal["active_path", "all_branches"]


class EmbedderProtocol(Protocol):
    """Anything that turns texts into equal-length float vectors (sync; async callers use to_thread):"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Returning one float vector per input text (all vectors equal length)."""
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Computing cosine similarity between two vectors (0.0 when either is degenerate):"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_pending_items(
    db: Session, conversation_id: UUID, embedder: EmbedderProtocol, *, owner_id: str,
) -> int:
    """
    Backfilling vectors for text-bearing items that do not have one yet.
    Designed to run as a background task after appends (P2 wires this up);
    returns how many items were embedded. This is the ONE sanctioned update to
    item rows — `embedding` is derived data, not content, so the append-only
    contract is not violated:
    """
    # Owner-scoped resolution first: the backfill never touches a conversation
    # the caller does not own (store.get_conversation is the one chokepoint):
    get_conversation(db, conversation_id, owner_id=owner_id)
    rows = db.execute(
        select(AITranscriptItem).where(
            AITranscriptItem.conversation_id == conversation_id,
            AITranscriptItem.text_content.is_not(None),
            AITranscriptItem.embedding.is_(None),
        )
    ).scalars().all()
    if not rows:
        return 0

    vectors = embedder.embed([row.text_content for row in rows])
    try:
        for row, vector in zip(rows, vectors):
            row.embedding = vector
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(rows)


def search_conversation(
    db: Session,
    conversation_id: UUID,
    query: str,
    k: int = 8,
    embedder: EmbedderProtocol | None = None,
    scope: SearchScope = "active_path",
    *,
    owner_id: str,
) -> list[Item]:
    """
    Returning the k most relevant items of a conversation for `query`.

    Backend selection: semantic ranking when an embedder is provided and any
    vectors exist in scope; plain text matching otherwise. The caller never
    has to know which ran — same signature, same return shape.

    Scope (D30): defaults to the active path only, so retrieval matches what
    the conversation IS from the model's point of view. Passing
    scope="all_branches" widens to edited-away branches — callers must opt in
    explicitly (P5 gates this behind a per-agent toggle):
    """
    # Resolving the allowed item ids up front when scoped to the active path
    # (a path is at most a few thousand items — an IN-clause is fine):
    # Owner-scoped resolution up front, so the all-branches scope is gated too:
    get_conversation(db, conversation_id, owner_id=owner_id)
    path_ids: set[UUID] | None = None
    if scope == "active_path":
        path_ids = {item.id for item in get_current_path(db, conversation_id, owner_id=owner_id)}
        if not path_ids:
            return []

    if embedder is not None:
        # Loading only vector-bearing rows for the in-Python cosine scan:
        vector_query = select(AITranscriptItem).where(
            AITranscriptItem.conversation_id == conversation_id,
            AITranscriptItem.embedding.is_not(None),
        )
        if path_ids is not None:
            vector_query = vector_query.where(AITranscriptItem.id.in_(path_ids))
        rows = db.execute(vector_query).scalars().all()

        if rows:
            query_vector = embedder.embed([query])[0]
            # Ranking by similarity and keeping the top k:
            scored = sorted(
                rows,
                key=lambda row: _cosine(query_vector, row.embedding),
                reverse=True,
            )[:k]
            return [_row_to_item(row) for row in scored]

    # Keyless fallback: case-insensitive substring match, newest first
    # (ilike compiles to LIKE-with-lower on SQLite and ILIKE on PostgreSQL):
    text_query = (
        select(AITranscriptItem)
        .where(
            AITranscriptItem.conversation_id == conversation_id,
            AITranscriptItem.text_content.ilike(f"%{query}%"),
        )
        .order_by(AITranscriptItem.created_at.desc())
        .limit(k)
    )
    if path_ids is not None:
        text_query = text_query.where(AITranscriptItem.id.in_(path_ids))
    rows = db.execute(text_query).scalars().all()
    return [_row_to_item(row) for row in rows]
