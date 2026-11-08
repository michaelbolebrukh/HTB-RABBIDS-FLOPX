"""
pgvector-backed recall — the production retrieval path (P10/D62).

On PostgreSQL with the `vector` extension, the semantic leg of recall runs as an
ANN query over an HNSW index and the lexical leg as a ranked full-text query
over a GIN index — both sub-linear, which is what every frontier memory system
does (mem0→Qdrant/HNSW, Graphiti→Neo4j vector index, LangMem→pgvector). The
exact Python-cosine scan in `store.py` remains the fallback for non-Postgres
engines (the SQLite unit suite) and for vectors that don't match the index
dimension — a degraded-but-correct path, not the production one.

Deliberately RAW SQL, no ORM column and no `pgvector` pip dependency: the
`embedding_vec` column + indexes are created ONLY by migration `20260713_13`
(guarded, PG-only), so `Base.metadata.create_all()` never has to emit a `vector`
type before the extension exists, and non-PG engines never see the column at
all. `supports_pgvector(db)` probes for the column once per engine and caches.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

# The index dimension — must match the engine's embedding model
# (adapter.EMBEDDING_MODEL_ID = openai:text-embedding-3-small → 1536). Changing
# the embedding model to a different width requires a new migration that
# re-declares the column + reindexes (documented in D62):
EMBEDDING_DIM = 1536

# Capability probe cache, keyed per engine (id -> bool). An engine's schema does
# not change mid-process; a fresh process re-probes:
_SUPPORT_CACHE: dict[int, bool] = {}


def supports_pgvector(db: Session) -> bool:
    """
    Whether this session's engine has the ANN path available: PostgreSQL AND the
    migration-created `embedding_vec` column present (which implies the `vector`
    extension installed — the migration skips the column when it cannot create
    the extension). Cached per engine:
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    key = id(bind.engine)
    cached = _SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import warnings
        with warnings.catch_warnings():
            # SQLAlchemy's inspector doesn't know the `vector` type name; we
            # only need column NAMES, so the reflection warning is noise:
            warnings.filterwarnings("ignore", message=".*Did not recognize type.*")
            cols = {c["name"] for c in inspect(bind).get_columns("ai_memories")}
        supported = "embedding_vec" in cols
    except Exception:  # noqa: BLE001 — a probe failure means "no ANN", never an error:
        logger.warning("pgvector support probe failed; falling back to Python recall", exc_info=True)
        supported = False
    _SUPPORT_CACHE[key] = supported
    return supported


def _vec_literal(vector: list[float]) -> str:
    """Serializing a vector to pgvector's input format (bound as one parameter):"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def sync_vector(db: Session, mem_id: UUID, vector: list[float]) -> None:
    """
    Mirroring a fact's JSON embedding into the indexed `embedding_vec` column
    (write path + backfill). Only called when `supports_pgvector` and the vector
    matches the index dimension; best-effort — a failure logs and leaves the row
    reachable via the fallback scan, never fails the write:
    """
    if len(vector) != EMBEDDING_DIM:
        return
    try:
        db.execute(
            text("UPDATE ai_memories SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            {"v": _vec_literal(vector), "id": mem_id},
        )
        db.commit()
    except Exception:  # noqa: BLE001 — the JSON embedding remains the source of truth:
        db.rollback()
        logger.warning("pgvector sync failed for memory %s (fallback scan still works)", mem_id, exc_info=True)


def _temporal_where(as_of: datetime | None, include_superseded: bool) -> str:
    """
    Rendering the SQL temporal slice matching store._live_at exactly:
    live-now = not superseded; as-of T = valid window contains T (time travel —
    superseded rows are correctly visible again); audit = no filter:
    """
    if include_superseded:
        return ""
    if as_of is None:
        return " AND superseded_at IS NULL"
    return (
        " AND (valid_at IS NULL OR valid_at <= :as_of)"
        " AND (invalid_at IS NULL OR invalid_at > :as_of)"
    )


def ann_semantic_ids(
    db: Session, *, owner_id: str, scope: str, query_embedding: list[float],
    n: int, as_of: datetime | None = None, include_superseded: bool = False,
) -> list[UUID]:
    """
    The semantic leg on the HNSW index: top-n ids by cosine distance over the
    requested temporal slice. Caller guarantees `supports_pgvector` and the
    query vector is EMBEDDING_DIM wide:
    """
    params: dict = {
        "q": _vec_literal(query_embedding), "owner": owner_id, "scope": scope, "n": n,
    }
    if as_of is not None and not include_superseded:
        params["as_of"] = as_of
    rows = db.execute(
        text(
            "SELECT id FROM ai_memories"
            " WHERE owner_id = :owner AND scope = :scope AND embedding_vec IS NOT NULL"
            + _temporal_where(as_of, include_superseded)
            + " ORDER BY embedding_vec <=> CAST(:q AS vector) LIMIT :n"
        ),
        params,
    ).fetchall()
    return [row[0] for row in rows]


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def fts_lexical_ids(
    db: Session, *, owner_id: str, scope: str, query: str,
    n: int, as_of: datetime | None = None, include_superseded: bool = False,
) -> list[UUID]:
    """
    The lexical leg on the GIN full-text index: top-n ids by ts_rank_cd. The
    query is tokenized with the same regex as the Python fallback and OR-joined,
    matching the token-overlap semantics (`plainto_tsquery` would AND every word
    and over-constrain question-shaped queries):
    """
    tokens = _TOKEN_RE.findall((query or "").lower())
    if not tokens:
        return []
    params: dict = {
        "q": " | ".join(dict.fromkeys(tokens)), "owner": owner_id, "scope": scope, "n": n,
    }
    if as_of is not None and not include_superseded:
        params["as_of"] = as_of
    rows = db.execute(
        text(
            "SELECT id FROM ai_memories"
            " WHERE owner_id = :owner AND scope = :scope"
            " AND to_tsvector('simple', text) @@ to_tsquery('simple', :q)"
            + _temporal_where(as_of, include_superseded)
            + " ORDER BY ts_rank_cd(to_tsvector('simple', text),"
            "                       to_tsquery('simple', :q)) DESC LIMIT :n"
        ),
        params,
    ).fetchall()
    return [row[0] for row in rows]
