"""
Memory store — bi-temporal fact CRUD + hybrid recall (Phase 10).

Pure DB + ranking, no LLM and no `pydantic_ai`. Embeddings are portable JSON
ranked with Python cosine (the transcript's pattern — no pgvector dependency, so
this runs on SQLite in tests and Postgres in prod). The temporal-query builders
express Graphiti's bi-temporal reads as plain SQL filters:

- live-now:      superseded_at IS NULL AND valid window contains now
- as-of valid T: valid_at <= T AND (invalid_at IS NULL OR invalid_at > T)
- as-of tx TT:   recorded_at <= TT AND (superseded_at IS NULL OR superseded_at > TT)

Recall fuses semantic + lexical rankings with Reciprocal Rank Fusion (ported
from graphiti_core/search/search_utils.py:1780).
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from ..models import AIMemory


# ============================================================================
# Normalization + hashing (mem0 md5 dedup + cognee normalization)
# ============================================================================

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalizing a fact for exact-restatement dedup: lowered, whitespace-collapsed:"""
    return _WS.sub(" ", (text or "").strip().lower())


def content_hash(text: str) -> str:
    """The exact-dedup key (mem0's md5 pre-LLM filter):"""
    return hashlib.md5(normalize_text(text).encode()).hexdigest()


def _now() -> datetime:
    """Returning an aware UTC now (Date.now is unavailable in some contexts; DB uses func.now)."""
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Forcing a datetime to tz-aware UTC (SQLite round-trips naive) — Graphiti ensure_utc:"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ============================================================================
# Similarity + fusion
# ============================================================================

def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity (0.0 when either vector is degenerate):"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _tokens(text: str) -> set[str]:
    """Lexical tokens for the keyword ranking (the BM25 stand-in, portable):"""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _lexical_score(query: str, text: str) -> float:
    """Token-overlap score (Jaccard) — the lexical leg fused with semantic:"""
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q | t)


def rrf_fuse(ranked_lists: list[list[UUID]], rank_const: int = 1) -> dict[UUID, float]:
    """
    Reciprocal Rank Fusion (Graphiti verbatim): score[id] += 1/(rank + const).
    Needs no score normalization across the semantic + lexical lists:
    """
    scores: dict[UUID, float] = {}
    for ranked in ranked_lists:
        for rank, mid in enumerate(ranked):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (rank + rank_const)
    return scores


# ============================================================================
# Temporal slice predicates (applied in Python over loaded rows — portable)
# ============================================================================

def _live_at(mem: AIMemory, as_of: datetime | None) -> bool:
    """
    Whether a fact is believed-true at `as_of`. Two deliberately different modes:

    - `as_of is None` — "what do we currently believe": transaction-time ONLY
      (the row is not superseded). Valid-time bounds are NOT applied, because a
      fact stated for a near-future effective date ("we relocate to Denver on
      Aug 15") is still our current belief the moment it is recorded — applying
      valid_at > now would make freshly-recorded facts vanish until their date.
    - `as_of = T` — "what was true at date T": the full bi-temporal predicate,
      valid-time window `[valid_at, invalid_at)` must contain T (this is the
      time-travel path — a superseded fact is correctly live again at a past T).
    """
    superseded = _aware(mem.superseded_at)
    if as_of is None:
        return superseded is None

    t = _aware(as_of)
    # valid-time: true in the world at T (nullable bounds = open interval):
    valid_at = _aware(mem.valid_at)
    invalid_at = _aware(mem.invalid_at)
    if valid_at is not None and valid_at > t:
        return False
    if invalid_at is not None and invalid_at <= t:
        return False
    return True


# ============================================================================
# CRUD
# ============================================================================

def add_memory(
    db: Session, *, owner_id: str, scope: str, text: str,
    embedding: list[float] | None = None, valid_at: datetime | None = None,
    invalid_at: datetime | None = None, claim_key: str | None = None,
    source_turn_ids: list | None = None, meta: dict | None = None,
) -> AIMemory:
    """Inserting one fact (recorded_at defaults to now via the server default):"""
    row = AIMemory(
        owner_id=owner_id, scope=scope, text=text,
        content_hash=content_hash(text), claim_key=claim_key, embedding=embedding,
        valid_at=valid_at, invalid_at=invalid_at,
        source_turn_ids=list(source_turn_ids or []), meta=dict(meta or {}),
    )
    try:
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    _sync_vector_if_supported(db, row.id, embedding)
    return row


def _sync_vector_if_supported(db: Session, mem_id, vector: list[float] | None) -> None:
    """Mirroring a JSON embedding into the pgvector index column when available (D62):"""
    from . import pg_recall
    if vector and pg_recall.supports_pgvector(db):
        pg_recall.sync_vector(db, mem_id, vector)


def find_live_by_hash(db: Session, owner_id: str, scope: str, text: str) -> AIMemory | None:
    """The exact-restatement fast path: a still-live row with the same normalized text:"""
    return (
        db.query(AIMemory)
        .filter(AIMemory.owner_id == owner_id, AIMemory.scope == scope,
                AIMemory.content_hash == content_hash(text), AIMemory.superseded_at.is_(None))
        .first()
    )


def current_claim_holder(db: Session, owner_id: str, scope: str, claim_key: str) -> AIMemory | None:
    """The live holder of a single-valued claim slot (cognee O(1) supersession lookup):"""
    if not claim_key:
        return None
    return (
        db.query(AIMemory)
        .filter(AIMemory.owner_id == owner_id, AIMemory.scope == scope,
                AIMemory.claim_key == claim_key, AIMemory.superseded_at.is_(None))
        .order_by(AIMemory.recorded_at.desc())
        .first()
    )


def live_memories(db: Session, owner_id: str, scope: str) -> list[AIMemory]:
    """Every currently-believed fact for the scope (superseded rows excluded):"""
    return (
        db.query(AIMemory)
        .filter(AIMemory.owner_id == owner_id, AIMemory.scope == scope,
                AIMemory.superseded_at.is_(None))
        .all()
    )


def all_memories(db: Session, owner_id: str, scope: str) -> list[AIMemory]:
    """Every fact including superseded ones (for as-of / audit queries):"""
    return (
        db.query(AIMemory)
        .filter(AIMemory.owner_id == owner_id, AIMemory.scope == scope)
        .all()
    )


def append_source(db: Session, mem: AIMemory, turn_ids: list) -> None:
    """Appending provenance turn ids to a fact on dedup (Graphiti episodes[] accumulate):"""
    merged = list(dict.fromkeys([*(mem.source_turn_ids or []), *turn_ids]))
    mem.source_turn_ids = merged
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def invalidate(db: Session, mem: AIMemory, *, invalid_at: datetime | None, superseded_by: UUID | None) -> None:
    """
    Invalidate-don't-delete: retire a fact in transaction-time (superseded_at)
    and, when a valid-time end is known, in valid-time (invalid_at). The row and
    its embedding stay for as-of/audit queries:
    """
    mem.superseded_at = _now()
    mem.superseded_by = superseded_by
    if invalid_at is not None and mem.invalid_at is None:
        mem.invalid_at = invalid_at
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def set_embedding(db: Session, mem: AIMemory, vector: list[float]) -> None:
    """Filling a fact's embedding (write path embeds inline; this backfills any gaps):"""
    mem.embedding = vector
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    _sync_vector_if_supported(db, mem.id, vector)


# ============================================================================
# Hybrid recall
# ============================================================================

def recall(
    db: Session, *, owner_id: str, scope: str, query: str,
    query_embedding: list[float] | None = None, k: int = 8,
    as_of: datetime | None = None, include_superseded: bool = False,
) -> list[tuple[AIMemory, float]]:
    """
    Hybrid recall over the requested temporal slice: semantic + lexical fused by
    RRF. `as_of` gives valid-time travel; `include_superseded` opens the audit
    view. Two engines, one contract (D62): on PostgreSQL with pgvector the legs
    run as indexed SQL (HNSW ANN + GIN full-text — sub-linear, the production
    path); elsewhere, or when the query vector doesn't match the index width,
    the exact Python scan below (the portable test/fallback path):
    """
    from . import pg_recall

    if (
        pg_recall.supports_pgvector(db)
        and query_embedding
        and len(query_embedding) == pg_recall.EMBEDDING_DIM
    ):
        return _recall_pg(
            db, owner_id=owner_id, scope=scope, query=query,
            query_embedding=query_embedding, k=k,
            as_of=as_of, include_superseded=include_superseded,
        )

    rows = all_memories(db, owner_id, scope) if (include_superseded or as_of is not None) \
        else live_memories(db, owner_id, scope)
    candidates = [m for m in rows if include_superseded or _live_at(m, as_of)]
    if not candidates:
        return []

    # Semantic ranking (only over vector-bearing rows):
    sem_ranked: list[UUID] = []
    if query_embedding:
        scored = [
            (m.id, cosine(query_embedding, m.embedding))
            for m in candidates if m.embedding
        ]
        sem_ranked = [mid for mid, _ in sorted(scored, key=lambda p: p[1], reverse=True)]

    # Lexical ranking (always available):
    lex_scored = [(m.id, _lexical_score(query, m.text)) for m in candidates]
    lex_ranked = [mid for mid, s in sorted(lex_scored, key=lambda p: p[1], reverse=True) if s > 0]

    fused = rrf_fuse([lst for lst in (sem_ranked, lex_ranked) if lst])
    if not fused:
        return []
    by_id = {m.id: m for m in candidates}
    # Corroboration tiebreak (Graphiti episode_mentions): more source turns wins:
    ordered = sorted(
        fused.items(),
        key=lambda kv: (kv[1], len(by_id[kv[0]].source_turn_ids or [])),
        reverse=True,
    )
    return [(by_id[mid], score) for mid, score in ordered[:k]]


def _recall_pg(
    db: Session, *, owner_id: str, scope: str, query: str,
    query_embedding: list[float], k: int,
    as_of: datetime | None, include_superseded: bool,
) -> list[tuple[AIMemory, float]]:
    """
    The indexed recall (D62): HNSW ANN semantic leg + GIN full-text lexical leg,
    each top-(2k) in SQL, fused by the SAME RRF + corroboration tiebreak as the
    Python path — two engines, one ranking contract. Only the fused union rows
    are loaded, so cost tracks k, not the scope's total memory count:
    """
    from . import pg_recall

    fetch_n = max(k * 2, 10)   # Overfetching per leg so fusion has real overlap to rank
    sem_ranked = pg_recall.ann_semantic_ids(
        db, owner_id=owner_id, scope=scope, query_embedding=query_embedding,
        n=fetch_n, as_of=as_of, include_superseded=include_superseded,
    )
    lex_ranked = pg_recall.fts_lexical_ids(
        db, owner_id=owner_id, scope=scope, query=query,
        n=fetch_n, as_of=as_of, include_superseded=include_superseded,
    )
    fused = rrf_fuse([lst for lst in (sem_ranked, lex_ranked) if lst])
    if not fused:
        return []
    rows = db.query(AIMemory).filter(AIMemory.id.in_(list(fused))).all()
    by_id = {m.id: m for m in rows}
    ordered = sorted(
        fused.items(),
        key=lambda kv: (kv[1], len(by_id[kv[0]].source_turn_ids or []) if kv[0] in by_id else 0),
        reverse=True,
    )
    return [(by_id[mid], score) for mid, score in ordered[:k] if mid in by_id]
