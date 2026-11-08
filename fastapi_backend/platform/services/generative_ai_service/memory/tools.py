"""
Memory tools — the agent's self-editing interface to durable memory (Phase 10).

Four tools, registered at import (D33), docstrings adapted from Letta's
battle-tuned `archival_memory_insert/_search` + `core_memory_replace`
(`function_sets/base.py`) — the model reads the docstring as the tool's contract,
so the wording is load-bearing, not decoration:

- `memory_write`   — remember a durable fact (reconciled bi-temporally on write).
- `memory_search`  — recall facts, optionally as-of a past date (time travel).
- `memory_supersede` — retire a fact the agent knows is no longer true.
- `memory_delete`  — hard-delete a fact (privacy / a mistaken write).

The reconciliation, temporal arithmetic, and ranking live in `store`/`reconcile`;
these are the thin, model-facing shims. No `pydantic_ai` import (D3): the LLM
resolver and the embedder arrive on the ToolContext, injected by the engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from ..engine.tool_table import ToolContext, register_tool
from ..engine.types import ToolExecutionError
from . import store
from .reconcile import reconcile_write


def _parse_date(value: str | None) -> datetime | None:
    """
    Parsing a model-supplied ISO date/datetime to tz-aware UTC, tolerantly:
    a bare `YYYY-MM-DD` becomes midnight UTC. A malformed value is a typed tool
    error (reaches the model as a result, so it can retry) — never a run crash:
    """
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolExecutionError(f"could not parse date {value!r} (use ISO 8601, e.g. 2026-05-01)") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _digest(mem, score: float | None = None) -> dict:
    """Reducing a memory row to a compact, model-safe digest (never the embedding):"""
    out = {
        "id": str(mem.id),
        "fact": mem.text,
        "valid_from": mem.valid_at.isoformat() if mem.valid_at else None,
        "valid_until": mem.invalid_at.isoformat() if mem.invalid_at else None,
        "recorded_at": mem.recorded_at.isoformat() if mem.recorded_at else None,
        "superseded": mem.superseded_at is not None,
    }
    if score is not None:
        out["relevance"] = round(score, 4)
    return out


# ============================================================================
# memory_write
# ============================================================================

async def memory_write(ctx: ToolContext, fact: str, valid_from: str | None = None, claim_key: str | None = None) -> dict:
    """
    Remember a durable fact about the user for future conversations. Write ONE
    clear, self-contained fact (e.g. "The user's primary email is a@b.com"), not
    a whole conversation. Memory is reconciled automatically: if this restates
    something already known it is merged, and if it changes a known value (a new
    email, city, job title) the old value is retired but kept for history — you
    do not need to delete the old one yourself. Optionally pass `valid_from` (ISO
    date) when the fact became true if it is not now, and `claim_key` (a short
    slot name like "primary_email") when this fact replaces a single-valued
    setting. Returns what the write resolved to (added / merged / updated).
    """
    fact = (fact or "").strip()
    if not fact:
        raise ToolExecutionError("fact must be non-empty")
    # Storing the turn id as a string — source_turn_ids is a portable JSON column
    # (a raw UUID is not JSON-serializable), matching the transcript's string ids:
    turn_ids = [str(ctx.current_turn_id)] if ctx.current_turn_id is not None else []
    result = await reconcile_write(
        ctx.db, owner_id=ctx.owner_id, scope=ctx.memory_scope, text=fact,
        valid_at=_parse_date(valid_from), claim_key=claim_key, source_turn_ids=turn_ids,
        embed_fn=ctx.memory_embed_fn, resolve_fn=ctx.memory_resolve_fn, db_lock=ctx.db_lock,
    )
    return {
        "action": result.action,
        "memory": _digest(result.memory) if result.memory is not None else None,
        "retired": [_digest(m) for m in result.invalidated],
    }


# ============================================================================
# memory_search
# ============================================================================

async def memory_search(ctx: ToolContext, query: str, k: int = 8, as_of: str | None = None) -> list[dict]:
    """
    Search your durable memory for facts about the user and return the most
    relevant ones. Use this at the start of a task to recall what you already
    know. IMPORTANT: memory keeps history — for any question about a PAST point
    in time you MUST pass `as_of` (an ISO date): as_of="2026-05-01" returns the
    values that were true THEN (including since-replaced ones), not today's;
    without it you only see current values and cannot answer past-dated
    questions. Returns compact fact digests with their validity windows; an
    empty list means nothing relevant is remembered (do not invent an answer).
    """
    query = (query or "").strip()
    if not query:
        return []
    k = max(1, min(int(k), 25))
    as_of_dt = _parse_date(as_of)

    vector = None
    if ctx.memory_embed_fn is not None:
        try:
            vectors = await asyncio.to_thread(ctx.memory_embed_fn, [query])
            vector = list(vectors[0]) if vectors else None
        except Exception:  # noqa: BLE001 — semantic leg is best-effort; lexical still ranks:
            vector = None

    def _recall():
        """Recalling in a worker thread, releasing the connection after (P2 discipline)."""
        try:
            return store.recall(
                ctx.db, owner_id=ctx.owner_id, scope=ctx.memory_scope, query=query,
                query_embedding=vector, k=k, as_of=as_of_dt,
            )
        finally:
            ctx.db.rollback()

    async with ctx.db_lock:                       # guard the shared session (P10 concurrency)
        hits = await asyncio.to_thread(_recall)
    return [_digest(mem, score) for mem, score in hits]


# ============================================================================
# memory_supersede / memory_delete (uniqueness-guarded, Letta core_memory_replace)
# ============================================================================

def _load_owned(ctx: ToolContext, memory_id: str):
    """
    Loading a memory by id and asserting it belongs to this scope — the model
    supplies the id, so ownership is verified before any mutation (the trust
    boundary: a model must never retire another owner's or agent's fact):
    """
    try:
        mid = UUID(str(memory_id))
    except (ValueError, AttributeError) as exc:
        raise ToolExecutionError(f"invalid memory id {memory_id!r}") from exc
    from ..models import AIMemory
    mem = ctx.db.get(AIMemory, mid)
    if mem is None or mem.owner_id != ctx.owner_id or mem.scope != ctx.memory_scope:
        raise ToolExecutionError(f"memory {memory_id} not found")
    return mem


async def memory_supersede(ctx: ToolContext, memory_id: str, reason: str | None = None) -> dict:
    """
    Retire a fact you know is no longer true, when normal write-reconciliation
    did not catch it. The fact is invalidated (marked no longer current) but kept
    for history — search as-of a past date will still find it. Pass the fact's id
    from memory_search. Prefer memory_write for a NEW value (it retires the old
    one for you); use this only to invalidate without a replacement.
    """
    def _do():
        """Invalidating in one session; the uniqueness guard rejects an already-retired fact."""
        mem = _load_owned(ctx, memory_id)
        if mem.superseded_at is not None:
            raise ToolExecutionError(f"memory {memory_id} is already retired")
        if reason:
            mem.meta = {**(mem.meta or {}), "retired_reason": reason}
        store.invalidate(ctx.db, mem, invalid_at=store._now(), superseded_by=None)
        return mem
    async with ctx.db_lock:                       # guard the shared session (P10 concurrency)
        mem = await asyncio.to_thread(_do)
    return {"action": "retired", "memory": _digest(mem)}


async def memory_delete(ctx: ToolContext, memory_id: str) -> dict:
    """
    Permanently delete a fact from memory (privacy, or a mistaken write). This is
    a HARD delete — unlike memory_supersede it removes all history and cannot be
    undone or found as-of a past date. Use it only when a fact must not persist
    at all; otherwise prefer memory_supersede. Pass the fact's id from memory_search.
    """
    def _do():
        """Deleting in one session (privacy hard-delete — the only DELETE memory exposes)."""
        mem = _load_owned(ctx, memory_id)
        deleted = _digest(mem)
        ctx.db.delete(mem)
        try:
            ctx.db.commit()
        except Exception:
            ctx.db.rollback()
            raise
        return deleted
    async with ctx.db_lock:                       # guard the shared session (P10 concurrency)
        deleted = await asyncio.to_thread(_do)
    return {"action": "deleted", "memory": deleted}


# ============================================================================
# The always-on usage preamble (injected as extra_system when memory is on)
# ============================================================================

MEMORY_PREAMBLE = (
    "You have a durable memory that persists across conversations. Use memory_search "
    "to recall what you already know about the user before answering, and memory_write "
    "to remember new durable facts (preferences, identities, ongoing situations). "
    "Memory reconciles itself: writing a changed value retires the old one automatically. "
    "Memory also keeps HISTORY: when a question asks about a PAST point in time "
    "('where did I live in May?', 'what was my title last year?'), call memory_search "
    "again with as_of set to that date — the current value alone is not the answer. "
    "When the user states WHEN a fact became true, pass valid_from to memory_write. "
    "Never state a remembered fact you did not retrieve, and if memory has nothing "
    "relevant, say so rather than guessing."
)


# ============================================================================
# Registration — import-time, code-only (D33)
# ============================================================================

register_tool(
    "memory_write", memory_write,
    description="Remember a durable fact about the user for future conversations (reconciled automatically).",
)
register_tool(
    "memory_search", memory_search,
    description="Search durable memory for facts about the user; pass as_of (ISO date) to recall a past value.",
)
register_tool(
    "memory_supersede", memory_supersede,
    description="Retire a remembered fact that is no longer true (kept for history).",
)
register_tool(
    "memory_delete", memory_delete,
    description="Permanently delete a remembered fact (privacy hard-delete).",
)


# The tool names memory adds when features.memory is on (order-preserving):
MEMORY_TOOL_NAMES = ("memory_search", "memory_write", "memory_supersede", "memory_delete")
