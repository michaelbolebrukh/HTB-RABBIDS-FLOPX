"""
Write-time reconciliation — the part mem0/cognee/Letta all dropped (Phase 10).

`memory_write(fact)` does NOT just append. It reconciles the new fact against
what is already believed, so the store never rots into a pile of contradictory
restatements. The pipeline (ported mechanism-by-mechanism from the frontier):

1. Exact-dedup fast path — a still-live row with the same normalized md5 just
   accrues the new source turn id, no embed, no LLM (mem0's md5 pre-filter +
   Graphiti's verbatim short-circuit).
2. Candidate recall — hybrid semantic + lexical over the scope's live rows
   (store.recall), plus the current holder of the fact's `claim_key` slot
   (cognee deterministic-slot lookup). Empty → straight insert.
3. LLM resolve — an INJECTED `resolve_fn` classifies each candidate as a
   duplicate (same claim, merge provenance) or a contradiction (supersede),
   returning integer indices over an anti-hallucination-remapped list (mem0
   `main.py:889`). Injected so offline tests run with a deterministic fake
   resolver at ZERO live cost; the engine wires the real gpt-4o-mini one.
4. Temporal-overlap arithmetic — `temporal_resolve_contradictions`, ported
   near-verbatim from Graphiti's `resolve_edge_contradictions`
   (`edge_operations.py:538-573`). A contradiction supersedes ONLY when the
   validity windows overlap; sequential truths ("Austin until Aug", "Denver
   since Aug") stay both-live with their windows closed at the boundary. This
   is the supersede-vs-accrue decision, done on timestamps — exactly where mem0
   (non-deterministic prose-merge) and cognee (first-write-wins) fail.

No `pydantic_ai` import (facade rule D3): the LLM and the embedder both arrive
as injected callables, so this whole module is pure and offline-testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ..models import AIMemory
from . import store


# ============================================================================
# Injected-callable contracts (kept SDK-free — the engine supplies real ones)
# ============================================================================

# Embedding one or more texts to portable vectors (sync; the orchestrator hops
# it to a thread). None disables semantic recall — reconciliation degrades to
# lexical + claim_key, never fails:
EmbedFn = Callable[[list[str]], list[list[float]]]

# Classifying the new fact against indexed candidates (async; the engine wraps
# a cheap LLM). Injected so tests pass a deterministic fake and spend nothing:
ResolveFn = Callable[[str, list[str]], Awaitable["ResolveResult"]]


@dataclass
class ResolveResult:
    """
    The resolver's verdict over the candidate list (indices into the SAME
    order the orchestrator passed). `duplicate_idxs` = the new fact restates an
    existing one (merge provenance, no new row); `contradicted_idxs` = the new
    fact conflicts with an existing one (supersede per the temporal rules):
    """
    duplicate_idxs: list[int] = field(default_factory=list)
    contradicted_idxs: list[int] = field(default_factory=list)


# ============================================================================
# Temporal-overlap arithmetic — Graphiti resolve_edge_contradictions, ported
# ============================================================================

@dataclass
class Invalidation:
    """One existing fact to retire, with the valid-time end to stamp (None = only tx-time)."""
    mem: AIMemory
    invalid_at: datetime | None


@dataclass
class ContradictionOutcome:
    """
    The temporal decision for a batch of contradictions: which existing facts
    to invalidate (and when their world-validity ends), the valid-time end to
    stamp on the NEW fact when it turns out to describe the past, and the facts
    judged sequentially-true (kept live — traced for the benchmark):
    """
    invalidate: list[Invalidation] = field(default_factory=list)
    new_invalid_at: datetime | None = None
    keep_both: list[AIMemory] = field(default_factory=list)


def temporal_resolve_contradictions(
    *,
    new_valid_at: datetime | None,
    new_invalid_at: datetime | None,
    contradicted: list[AIMemory],
    now: datetime,
) -> ContradictionOutcome:
    """
    Deciding supersede-vs-accrue on TIMESTAMPS (Graphiti's crown jewel, ported).
    For the incoming fact and each existing contradiction, both carrying a
    valid-time window `[valid_at, invalid_at)` (nullable = open):

    - Old ended before New began → sequential truths, keep both (NO supersede).
    - Both dated, Old started first, windows overlap → Old's window ends where
      New begins; Old is superseded.
    - Both dated, New started first → New describes an EARLIER state; New's
      window ends where Old begins, Old stays live (learning about the past).
    - Same start, or either date missing → fall back to transaction-time: the
      newly recorded belief wins, Old is superseded (closed at New's start or now).

    Pure over its datetime inputs — the offline unit tests exercise every branch
    with no DB and no LLM.
    """
    out = ContradictionOutcome(new_invalid_at=new_invalid_at)
    nva = store._aware(new_valid_at)
    for old in contradicted:
        ova = store._aware(old.valid_at)
        ovi = store._aware(old.invalid_at)

        # Both windows dated — decide by overlap + order:
        if nva is not None and ova is not None:
            if ovi is not None and ovi <= nva:
                out.keep_both.append(old)              # old already closed before new — sequential
                continue
            if ova < nva:
                out.invalidate.append(Invalidation(old, nva))   # old first, overlap → old ends at new's start
                continue
            if ova > nva:
                # new is the older/historical statement → close ITS window at old's start
                out.new_invalid_at = ova if out.new_invalid_at is None else min(out.new_invalid_at, ova)
                out.keep_both.append(old)
                continue
            # identical valid_at → genuine replacement, retire old:
            out.invalidate.append(Invalidation(old, nva))
            continue

        # Missing valid-time on either side → transaction-time wins (newer belief):
        out.invalidate.append(Invalidation(old, nva or now))
    return out


# ============================================================================
# Resolver prompt scaffolding (pure strings — the engine's LLM builder uses these)
# ============================================================================

RESOLVE_INSTRUCTIONS = (
    "You maintain a durable memory of facts about a user. Given a NEW fact and a "
    "numbered list of EXISTING facts, classify each existing fact's relationship "
    "to the new one. Return STRICT JSON, no prose:\n"
    '{"duplicate_idxs": [ints], "contradicted_idxs": [ints]}\n\n'
    "- duplicate_idxs: existing facts that state the SAME thing as the new fact "
    "(a restatement or paraphrase). The new fact adds no new information.\n"
    "- contradicted_idxs: existing facts that CANNOT both be true with the new "
    "fact for the same slot (the value changed): a new email replacing an old "
    "email, a new city replacing an old city, a new job title, a new phone number.\n\n"
    "Do NOT mark as contradicted facts that can coexist (accrue): 'speaks English' "
    "and 'speaks German'; 'owns a Tesla' and 'owns a Ford'; a child's name and "
    "another child's name; a fact about a different subject entirely. An index may "
    "appear in at most one list. If none apply, return empty lists."
)


def build_resolve_prompt(new_text: str, candidate_texts: list[str]) -> str:
    """
    Rendering the resolver's user turn: the new fact plus the candidates shown
    with INTEGER ids (mem0's anti-hallucination remap — the model never sees the
    real uuids, so it cannot invent one; the orchestrator holds the id map):
    """
    lines = [f"NEW fact: {new_text}", "", "EXISTING facts:"]
    for i, text in enumerate(candidate_texts):
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def parse_resolve_response(raw: str | dict, candidate_count: int) -> ResolveResult:
    """
    Parsing the resolver's JSON into a validated ResolveResult, defensively:
    indices out of range are dropped, an index claimed as both duplicate and
    contradiction resolves to duplicate (the safer no-supersede reading), and
    any unparseable output degrades to "no change" rather than corrupting the
    store. The model's output is untrusted data (P1), handled as such:
    """
    if isinstance(raw, str):
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
        except (json.JSONDecodeError, ValueError):
            data = {}
    else:
        data = raw or {}

    def _valid(key: str) -> list[int]:
        vals = data.get(key, []) if isinstance(data, dict) else []
        out: list[int] = []
        for v in vals if isinstance(vals, list) else []:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < candidate_count:
                out.append(i)
        return out

    dupes = _valid("duplicate_idxs")
    contras = [i for i in _valid("contradicted_idxs") if i not in dupes]  # duplicate wins the tie
    return ResolveResult(duplicate_idxs=sorted(set(dupes)), contradicted_idxs=sorted(set(contras)))


# ============================================================================
# The orchestrator
# ============================================================================

@dataclass
class ReconcileResult:
    """
    The traceable outcome of one write (the benchmark inspects every field):
    `action` is deduped | added | updated; `memory` is the live row the write
    resolved to; `invalidated` are the facts this write superseded; `candidates`
    are the ids the resolver considered (for step-by-step tracing):
    """
    action: str
    memory: AIMemory | None
    invalidated: list[AIMemory] = field(default_factory=list)
    candidates: list[UUID] = field(default_factory=list)


async def reconcile_write(
    db: Session,
    *,
    owner_id: str,
    scope: str,
    text: str,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
    source_turn_ids: list | None = None,
    claim_key: str | None = None,
    meta: dict | None = None,
    embed_fn: EmbedFn | None = None,
    resolve_fn: ResolveFn | None = None,
    recall_k: int = 8,
    now: datetime | None = None,
    db_lock: "object | None" = None,
) -> ReconcileResult:
    """
    Reconciling one new fact into the bi-temporal store (the P10 write path).
    `valid_at` is when the fact became true in the world — normally the turn's
    timestamp, supplied by the caller (no fragile LLM date-extraction needed for
    the common case). `embed_fn`/`resolve_fn` are injected; with `resolve_fn`
    None the write still dedups exactly and (when a `claim_key` names a live
    holder) supersedes deterministically, so a no-LLM path stays useful.

    `db_lock` (an `asyncio.Lock`, injected by the engine) serializes access to
    the shared run session so concurrent tool calls in one model step cannot use
    it at the same instant (the SQLAlchemy "concurrent operations" race). It is
    held ONLY around each DB hop — never across the embed (network) or resolve
    (LLM) calls — so genuine parallelism is preserved.
    """
    import asyncio
    from contextlib import nullcontext

    now = now or store._now()
    turn_ids = list(source_turn_ids or [])

    async def _db(fn, *args, **kwargs):
        """Running one DB op in a worker thread, guarded by the run's session lock:"""
        async with (db_lock or nullcontext()):
            return await asyncio.to_thread(fn, *args, **kwargs)

    # (1) Exact-restatement fast path — merge provenance, no embed, no LLM:
    exact = await _db(store.find_live_by_hash, db, owner_id, scope, text)
    if exact is not None:
        if turn_ids:
            await _db(store.append_source, db, exact, turn_ids)
        return ReconcileResult(action="deduped", memory=exact, candidates=[exact.id])

    # (2) Embed (best-effort) + candidate recall over the live slice:
    vector: list[float] | None = None
    if embed_fn is not None:
        try:
            vectors = await asyncio.to_thread(embed_fn, [text])
            vector = list(vectors[0]) if vectors else None
        except Exception:  # noqa: BLE001 — embedding is best-effort; lexical recall still works:
            vector = None

    def _candidates() -> list[AIMemory]:
        """Recalling hybrid candidates + the claim_key holder, deduped, in one thread hop."""
        hits = store.recall(
            db, owner_id=owner_id, scope=scope, query=text,
            query_embedding=vector, k=recall_k,
        )
        found = [m for m, _ in hits]
        if claim_key:
            holder = store.current_claim_holder(db, owner_id, scope, claim_key)
            if holder is not None and holder.id not in {m.id for m in found}:
                found.append(holder)
        return found

    candidates = await _db(_candidates)
    candidate_ids = [m.id for m in candidates]

    # No neighbours → nothing to reconcile against, straight insert:
    if not candidates:
        row = await _db(
            store.add_memory, db, owner_id=owner_id, scope=scope, text=text,
            embedding=vector, valid_at=valid_at, invalid_at=invalid_at,
            claim_key=claim_key, source_turn_ids=turn_ids, meta=meta,
        )
        return ReconcileResult(action="added", memory=row, candidates=candidate_ids)

    # (3) Resolve duplicates vs contradictions:
    if resolve_fn is not None:
        verdict = await resolve_fn(text, [m.text for m in candidates])
    else:
        # No-LLM path: a live claim_key holder is a deterministic contradiction
        # (cognee slot), otherwise assume the fact is new (never blind-merge):
        verdict = ResolveResult()
        if claim_key:
            for i, m in enumerate(candidates):
                if m.claim_key == claim_key:
                    verdict.contradicted_idxs.append(i)

    # A duplicate means "no new information" — accrue provenance onto the
    # existing row(s) and DON'T insert (Graphiti's short-circuit):
    if verdict.duplicate_idxs:
        primary = candidates[verdict.duplicate_idxs[0]]
        for i in verdict.duplicate_idxs:
            if turn_ids:
                await _db(store.append_source, db, candidates[i], turn_ids)
        return ReconcileResult(action="deduped", memory=primary, candidates=candidate_ids)

    # (4) Insert the new fact, then apply temporal arithmetic to contradictions:
    contradicted = [candidates[i] for i in verdict.contradicted_idxs]
    outcome = temporal_resolve_contradictions(
        new_valid_at=valid_at, new_invalid_at=invalid_at, contradicted=contradicted, now=now,
    )

    row = await _db(
        store.add_memory, db, owner_id=owner_id, scope=scope, text=text,
        embedding=vector, valid_at=valid_at, invalid_at=outcome.new_invalid_at,
        claim_key=claim_key, source_turn_ids=turn_ids, meta=meta,
    )

    def _apply() -> None:
        """Retiring each superseded fact in ONE session (invalidate, don't delete)."""
        for inv in outcome.invalidate:
            store.invalidate(db, inv.mem, invalid_at=inv.invalid_at, superseded_by=row.id)

    if outcome.invalidate:
        await _db(_apply)

    action = "updated" if outcome.invalidate else "added"
    return ReconcileResult(
        action=action, memory=row,
        invalidated=[inv.mem for inv in outcome.invalidate], candidates=candidate_ids,
    )
