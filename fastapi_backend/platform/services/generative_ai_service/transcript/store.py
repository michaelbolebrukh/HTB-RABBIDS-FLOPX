"""
Transcript store — append-only persistence for conversation items (Phase 1).

Design contract (PHASE1.md):
- Append-only: items are never mutated after insert. "Editing" a user message
  means appending a SIBLING item (same parent) and moving the conversation's
  current-leaf pointer — the old branch remains intact as source of truth.
- The only delete is conversation-level hard delete (privacy); item-level
  deletion does not exist.
- Service-function style per repo rules: plain functions, database session as
  the first argument, explicit commits. Async callers wrap these with
  `asyncio.to_thread` (agreed session style, O-b) until async sessions are
  revisited at extraction.

No `pydantic_ai` or provider SDK imports are allowed anywhere in this module.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..exceptions import ConversationNotFoundError, InvalidItemError, ItemNotFoundError
from ..models import AIConversation, AITranscriptItem, AITranscriptSummary
from .types import Item, ItemKind, Role, Usage, content_parts_adapter


# ============================================================================
# ORM <-> PYDANTIC CONVERSION (dumb field copies by design)
# ============================================================================

def _row_to_item(row: AITranscriptItem) -> Item:
    """Hydrating a pydantic Item from an ORM row (parts via the discriminated-union adapter):"""
    return Item(
        id=row.id,
        conversation_id=row.conversation_id,
        parent_id=row.parent_id,
        kind=ItemKind(row.kind),
        role=Role(row.role) if row.role else None,
        parts=content_parts_adapter.validate_python(row.parts or []),
        tool_name=row.tool_name,
        tool_call_id=row.tool_call_id,
        model_used=row.model_used,
        usage=Usage.model_validate(row.usage) if row.usage else None,
        token_estimates=row.token_estimates or {},
        raw=row.raw,
        meta=row.meta or {},
        created_at=row.created_at,
    )


def _next_seq(db: Session, conversation_id: UUID) -> int:
    """
    Computing the next monotonic sequence for a conversation (max+1). Appends to
    a single conversation are serialized by the app's turn-by-turn flow, so a
    read-then-write here is safe in practice; the seq column keeps full_history
    ordering deterministic where timestamps tie:
    """
    current_max = db.execute(
        select(func.max(AITranscriptItem.seq)).where(AITranscriptItem.conversation_id == conversation_id)
    ).scalar_one_or_none()
    return (current_max or 0) + 1


def _item_to_row(item: Item, seq: int) -> AITranscriptItem:
    """
    Building an ORM row from a pydantic Item. Enum values are stored as their
    `.value` strings (repo enum rule); `text_content` is extracted here so
    search never has to parse `parts` at query time; `seq` is the monotonic
    insertion order assigned by the caller:
    """
    return AITranscriptItem(
        seq=seq,
        id=item.id or uuid4(),
        conversation_id=item.conversation_id,
        parent_id=item.parent_id,
        kind=item.kind.value,
        role=item.role.value if item.role else None,
        parts=content_parts_adapter.dump_python(item.parts, mode="json"),
        tool_name=item.tool_name,
        tool_call_id=item.tool_call_id,
        model_used=item.model_used,
        usage=item.usage.model_dump(mode="json") if item.usage else None,
        token_estimates=item.token_estimates,
        raw=item.raw,
        meta=item.meta,
        text_content=item.extract_text(),
    )


def _owned_items(db: Session, item_ids: list[UUID], owner_id: str) -> list[AITranscriptItem]:
    """
    Loading transcript items by id, restricted to conversations the owner owns.
    The one place item-addressed access is scoped, so id-addressed writes
    (meta merges) cannot reach across owners:
    """
    if not item_ids:
        return []
    return list(
        db.execute(
            select(AITranscriptItem)
            .join(AIConversation, AITranscriptItem.conversation_id == AIConversation.id)
            .where(AITranscriptItem.id.in_(item_ids), AIConversation.owner_id == owner_id)
        ).scalars().all()
    )


# ============================================================================
# CONVERSATIONS
# ============================================================================

def create_conversation(
    db: Session,
    owner_id: str,
    title: str | None = None,
    meta: dict | None = None,
) -> AIConversation:
    """Creating a new empty conversation for an owner (host-agnostic string id, D26):"""
    conversation = AIConversation(owner_id=owner_id, title=title, meta=meta or {})
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_conversation(db: Session, conversation_id: UUID, *, owner_id: str) -> AIConversation:
    """
    Fetching a conversation THAT THE OWNER OWNS, or raising the package-level
    not-found error.

    `owner_id` is required and keyword-only on purpose: an unscoped lookup is
    not expressible through this module, so no entry point can reach another
    owner's transcript by forgetting a check. Every other store function routes
    its conversation resolution through here.

    A conversation that does not exist and one owned by somebody else raise the
    IDENTICAL error (same type, same message) — the store is never an existence
    oracle for ids the caller does not own:
    """
    conversation = db.execute(
        select(AIConversation).where(
            AIConversation.id == conversation_id,
            AIConversation.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFoundError(conversation_id)
    return conversation


def delete_conversation(db: Session, conversation_id: UUID, *, owner_id: str) -> None:
    """
    Hard-deleting a conversation and everything under it (agreed deletion
    policy: conversation-level only, privacy-shaped). Deleting children
    explicitly rather than relying on FK cascade so behavior is identical on
    SQLite (where foreign-key enforcement is off by default) and PostgreSQL:
    """
    conversation = get_conversation(db, conversation_id, owner_id=owner_id)
    try:
        db.execute(delete(AITranscriptSummary).where(AITranscriptSummary.conversation_id == conversation_id))
        db.execute(delete(AITranscriptItem).where(AITranscriptItem.conversation_id == conversation_id))
        db.delete(conversation)
        db.commit()
    except Exception:
        # Rolling back the partial delete before re-raising (repo transaction rule):
        db.rollback()
        raise


# ============================================================================
# ITEMS — APPEND & BRANCH
# ============================================================================

def append_item(db: Session, item: Item, *, owner_id: str) -> Item:
    """
    Appending an item to a conversation and advancing the current-leaf pointer.

    Parent resolution: when `item.parent_id` is None, the item is chained onto
    the conversation's current leaf (the normal "next turn" case). An explicit
    `parent_id` targets any existing item instead — that is branching, and
    `branch_from` below is the documented wrapper for it.

    `owner_id` scopes the conversation lookup, so an append into somebody
    else's transcript is not expressible:
    """
    conversation = get_conversation(db, item.conversation_id, owner_id=owner_id)

    # Chaining onto the current leaf when no explicit parent was given:
    if item.parent_id is None:
        item.parent_id = conversation.current_item_id

    # Validating an explicit/inherited parent actually belongs to this conversation:
    if item.parent_id is not None:
        parent = db.get(AITranscriptItem, item.parent_id)
        if parent is None:
            raise ItemNotFoundError(item.parent_id)
        if parent.conversation_id != item.conversation_id:
            raise InvalidItemError(
                f"Parent {item.parent_id} belongs to conversation {parent.conversation_id}, "
                f"not {item.conversation_id}"
            )

    row = _item_to_row(item, _next_seq(db, item.conversation_id))
    try:
        db.add(row)
        # Advancing the active branch to the new leaf:
        conversation.current_item_id = row.id
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(row)
    return _row_to_item(row)


def append_items(
    db: Session,
    items: list[Item],
    meta_updates: dict[UUID, dict] | None = None,
    *,
    owner_id: str,
) -> list[Item]:
    """
    Appending several items to a conversation ATOMICALLY — one transaction, one
    commit. This is how a run's output (e.g. tool_call -> tool_result ->
    assistant text) is persisted: a client disconnect or DB error must never
    leave a tool_call committed without its tool_result (which would make every
    later turn fail a provider tool-pairing check). Items chain onto the current
    leaf in order; the leaf advances once, at the end.

    `meta_updates` (P8) merges keys into EXISTING items in the SAME commit —
    the approval resume uses it to flip its pending tool_calls to "resolved" at
    the instant their results land, so a crash can never leave a pending marker
    beside a real result (a duplicate) or a resolved marker with no result (a
    bare call). Empty/None keeps the pre-P8 behavior exactly.
    """
    if not items and not meta_updates:
        return []

    # A meta-only call (no new items) still needs a conversation to anchor on;
    # derive it from the updated rows below:
    conversation_id = items[0].conversation_id if items else None

    try:
        rows: list[AITranscriptItem] = []
        if items:
            conversation = get_conversation(db, conversation_id, owner_id=owner_id)
            seq = _next_seq(db, conversation_id)
            parent_id = conversation.current_item_id
            for item in items:
                if item.conversation_id != conversation_id:
                    raise InvalidItemError("append_items requires all items in one conversation")
                # Chaining each item onto the previous (or the current leaf for the first):
                item.parent_id = parent_id
                row = _item_to_row(item, seq)
                db.add(row)
                db.flush()                   # Assigning row.id without committing:
                rows.append(row)
                parent_id = row.id
                seq += 1
            # Advancing the leaf to the last appended item:
            conversation.current_item_id = rows[-1].id

        # Merging meta into existing items in the SAME transaction (P8):
        if meta_updates:
            existing = _owned_items(db, list(meta_updates), owner_id)
            for row in existing:
                merged = dict(row.meta or {})
                merged.update(meta_updates[row.id])
                row.meta = merged

        db.commit()
    except Exception:
        db.rollback()
        raise

    for row in rows:
        db.refresh(row)
    return [_row_to_item(row) for row in rows]


def set_items_meta(db: Session, updates: dict[UUID, dict], *, owner_id: str) -> None:
    """
    Merging keys into the meta of existing items, one commit (P8): the approval
    lifecycle flips a pending tool_call's marker to approved/denied/abandoned
    without touching anything else on the row. Meta is reassigned as a whole
    dict so the JSON column change is always detected.

    Items are addressed by id here rather than through a conversation, so the
    owner scope is applied to the row selection itself — an id belonging to
    another owner simply does not load, and nothing is written:
    """
    if not updates:
        return
    rows = _owned_items(db, list(updates), owner_id)
    try:
        for row in rows:
            merged = dict(row.meta or {})
            merged.update(updates[row.id])
            row.meta = merged
        db.commit()
    except Exception:
        db.rollback()
        raise


def branch_from(db: Session, parent_id: UUID | None, item: Item, *, owner_id: str) -> Item:
    """
    Creating a new branch: appending `item` as a child of `parent_id` (a
    sibling of whatever was there before). This IS the message-edit mechanism —
    nothing is mutated; the old branch stays reachable via `full_history` and
    `set_current`. `parent_id=None` starts a new root branch (the "edit the
    very first message" case, replacing the legacy dummy-root trick).
    """
    # Setting the explicit branch point, then reusing the append path. The
    # `parent_id=None` case must NOT fall through to current-leaf chaining, so
    # a sentinel in meta is avoided by handling it directly here:
    if parent_id is None:
        conversation = get_conversation(db, item.conversation_id, owner_id=owner_id)
        item.parent_id = None
        row = _item_to_row(item, _next_seq(db, item.conversation_id))
        try:
            db.add(row)
            conversation.current_item_id = row.id
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(row)
        return _row_to_item(row)

    item.parent_id = parent_id
    return append_item(db, item, owner_id=owner_id)


def set_current(db: Session, conversation_id: UUID, item_id: UUID, *, owner_id: str) -> None:
    """Switching the active branch by pointing the conversation at another leaf:"""
    conversation = get_conversation(db, conversation_id, owner_id=owner_id)
    row = db.get(AITranscriptItem, item_id)
    if row is None or row.conversation_id != conversation_id:
        raise ItemNotFoundError(item_id)
    conversation.current_item_id = item_id
    db.commit()


# ============================================================================
# ITEMS — READS
# ============================================================================

def get_path(db: Session, item_id: UUID, *, owner_id: str) -> list[Item]:
    """
    Returning the active-branch path root -> item (the exact sequence a
    renderer turns into model context). Loads the item's conversation in one
    query and walks parent pointers in Python — a conversation's items number
    in the thousands at most, and pointer-walking is portable and unambiguous
    where a recursive CTE's row order is not:
    """
    leaf = db.get(AITranscriptItem, item_id)
    if leaf is None:
        raise ItemNotFoundError(item_id)
    # The item is only reachable through a conversation the owner owns — a
    # foreign leaf refuses exactly like an unknown conversation:
    get_conversation(db, leaf.conversation_id, owner_id=owner_id)

    # Fetching all items of the conversation once, then walking up by id:
    rows = db.execute(
        select(AITranscriptItem).where(AITranscriptItem.conversation_id == leaf.conversation_id)
    ).scalars().all()
    by_id = {row.id: row for row in rows}

    path: list[AITranscriptItem] = []
    node: AITranscriptItem | None = leaf
    while node is not None:
        path.append(node)
        node = by_id.get(node.parent_id) if node.parent_id else None

    # Walked leaf -> root, so reversing into chronological order:
    path.reverse()
    return [_row_to_item(row) for row in path]


def get_current_path(db: Session, conversation_id: UUID, *, owner_id: str) -> list[Item]:
    """Returning the path of the conversation's active branch (empty when no items yet):"""
    conversation = get_conversation(db, conversation_id, owner_id=owner_id)
    if conversation.current_item_id is None:
        return []
    return get_path(db, conversation.current_item_id, owner_id=owner_id)


def full_history(db: Session, conversation_id: UUID, *, owner_id: str) -> list[Item]:
    """
    Returning EVERY item across ALL branches, chronological. This is the
    source-of-truth view (feeds RAG indexing and exports); renderers use
    `get_path` instead:
    """
    get_conversation(db, conversation_id, owner_id=owner_id)  # Raising early on unknown/foreign ids:
    rows = db.execute(
        select(AITranscriptItem)
        .where(AITranscriptItem.conversation_id == conversation_id)
        # Ordering by seq, with created_at/id as stable secondary keys so that
        # even a duplicate seq (only possible under a rare concurrent append to
        # the SAME conversation) still yields a deterministic order:
        .order_by(AITranscriptItem.seq, AITranscriptItem.created_at, AITranscriptItem.id)
    ).scalars().all()
    return [_row_to_item(row) for row in rows]


# ============================================================================
# SUMMARY CACHE (entries, never replacements — D4/D24)
# ============================================================================

def get_summary(
    db: Session,
    conversation_id: UUID,
    span_end_item_id: UUID,
    token_budget: int,
    tokenizer_family: str,
) -> AITranscriptSummary | None:
    """Looking up a cached summary by its exact renderer key (None on miss):"""
    return db.execute(
        select(AITranscriptSummary).where(
            AITranscriptSummary.conversation_id == conversation_id,
            AITranscriptSummary.span_end_item_id == span_end_item_id,
            AITranscriptSummary.token_budget == token_budget,
            AITranscriptSummary.tokenizer_family == tokenizer_family,
        )
    ).scalar_one_or_none()


def put_summary(
    db: Session,
    conversation_id: UUID,
    span_end_item_id: UUID,
    token_budget: int,
    tokenizer_family: str,
    summary_text: str,
    usage: dict | None = None,
) -> AITranscriptSummary:
    """
    Inserting a summary cache entry (idempotent on the unique key: an existing
    entry for the same span/budget/family is returned untouched — summaries of
    an append-only span never need regenerating):
    """
    existing = get_summary(db, conversation_id, span_end_item_id, token_budget, tokenizer_family)
    if existing is not None:
        return existing

    summary = AITranscriptSummary(
        conversation_id=conversation_id,
        span_end_item_id=span_end_item_id,
        token_budget=token_budget,
        tokenizer_family=tokenizer_family,
        summary_text=summary_text,
        usage=usage,
    )
    try:
        db.add(summary)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(summary)
    return summary
