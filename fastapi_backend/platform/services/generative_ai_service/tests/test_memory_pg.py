"""
Memory pgvector integration tests (P10/D62) — the REAL production recall path,
on real PostgreSQL with the vector extension.

Skipped unless TEST_DATABASE_URL points at Postgres (CI's pgvector/pgvector:pg16
service sets it; local runs need a local PG with the extension). Exercises the
actual migration (20260713_13), the write-path vector sync, ANN + full-text
recall, ranking parity with the Python fallback, the as-of SQL filter, and the
graceful fallbacks.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
from platform.services.generative_ai_service.memory import pg_recall, store
from platform.services.generative_ai_service.models import AIMemory


_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="TEST_DATABASE_URL is not Postgres; pgvector integration runs only against real PG",
)

_MIGRATION = Path(__file__).resolve().parents[4] / "migrations" / "versions" / "20260713_13_add_pgvector_memory_index.py"


def _vec(seed: str, dim: int = pg_recall.EMBEDDING_DIM) -> list[float]:
    """A deterministic unit-ish vector from a seed (hash-expanded, so distinct seeds separate):"""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        out.extend(b / 255.0 for b in h)
        counter += 1
    v = out[:dim]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _blend(a: list[float], b: list[float], t: float) -> list[float]:
    """Interpolating two vectors — controlled similarity for ranking assertions:"""
    return [x * (1 - t) + y * t for x, y in zip(a, b)]


@pytest.fixture(scope="module")
def pg_engine():
    """A module-scoped engine with ai_memories created and migration _13 applied:"""
    eng = create_engine(_URL)
    Base.metadata.create_all(eng, tables=[AIMemory.__table__])
    spec = importlib.util.spec_from_file_location("m13", _MIGRATION)
    m13 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m13)
    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            m13.upgrade()
            m13.upgrade()          # idempotent re-run must be safe
        conn.commit()
    try:
        yield eng
    finally:
        with eng.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS ai_memories CASCADE"))
            conn.commit()
        eng.dispose()
        pg_recall._SUPPORT_CACHE.clear()


@pytest.fixture()
def db(pg_engine):
    """A clean session per test (table truncated, support cache reset):"""
    with pg_engine.connect() as conn:
        conn.execute(text("TRUNCATE ai_memories"))
        conn.commit()
    session = sessionmaker(bind=pg_engine)()
    try:
        yield session
    finally:
        session.close()


def test_migration_created_vector_column_and_indexes(pg_engine):
    cols = {c["name"] for c in inspect(pg_engine).get_columns("ai_memories")}
    assert "embedding_vec" in cols
    idx = {i["name"] for i in inspect(pg_engine).get_indexes("ai_memories")}
    # HNSW + GIN indexes exist (inspector sees them as plain indexes):
    assert "ix_ai_memories_embedding_vec" in idx
    assert "ix_ai_memories_text_fts" in idx


def test_write_path_syncs_vector(db):
    row = store.add_memory(db, owner_id="u", scope="s", text="likes espresso", embedding=_vec("espresso"))
    got = db.execute(text("SELECT embedding_vec IS NOT NULL FROM ai_memories WHERE id = :id"),
                     {"id": row.id}).scalar()
    assert got is True
    assert pg_recall.supports_pgvector(db) is True


def test_ann_recall_matches_python_ranking(db):
    """
    Parity: with distinct-similarity vectors, the ANN path must return the same
    top result the exact Python cosine scan would — two engines, one contract:
    """
    q = _vec("query-topic")
    # Three memories at controlled similarity to the query (0.9/0.5/0.1 blends):
    rows = []
    for name, t in [("closest", 0.1), ("middle", 0.5), ("far", 0.9)]:
        rows.append(store.add_memory(
            db, owner_id="u", scope="s", text=f"fact about {name}",
            embedding=_blend(q, _vec(f"noise-{name}"), t),
        ))
    hits = store.recall(db, owner_id="u", scope="s", query="unrelated words", query_embedding=q, k=3)
    assert hits and "closest" in hits[0][0].text
    # And the Python fallback agrees on the winner (rank parity on the top hit):
    ranked_py = sorted(rows, key=lambda m: store.cosine(q, m.embedding), reverse=True)
    assert ranked_py[0].text == hits[0][0].text


def test_fts_lexical_leg_finds_keyword(db):
    store.add_memory(db, owner_id="u", scope="s", text="the user's daughter is allergic to penicillin",
                     embedding=_vec("a"))
    store.add_memory(db, owner_id="u", scope="s", text="the user works in finance", embedding=_vec("b"))
    # Query vector points nowhere near either; the lexical leg must still surface it:
    hits = store.recall(db, owner_id="u", scope="s", query="penicillin allergy",
                        query_embedding=_vec("unrelated"), k=2)
    assert any("penicillin" in m.text for m, _ in hits)


def test_as_of_time_travel_via_sql(db):
    """The SQL temporal filter matches _live_at: a superseded fact is visible again at a past date:"""
    D = lambda y, m, d: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731
    old = store.add_memory(db, owner_id="u", scope="s", text="company HQ is in Austin",
                           embedding=_vec("hq-austin"), valid_at=D(2026, 1, 1))
    new = store.add_memory(db, owner_id="u", scope="s", text="company HQ is in Denver",
                           embedding=_vec("hq-denver"), valid_at=D(2026, 8, 1))
    store.invalidate(db, old, invalid_at=D(2026, 8, 1), superseded_by=new.id)

    q = _vec("hq-austin")
    now_hits = store.recall(db, owner_id="u", scope="s", query="where is HQ", query_embedding=q, k=5)
    assert not any("Austin" in m.text for m, _ in now_hits)          # retired now
    past_hits = store.recall(db, owner_id="u", scope="s", query="where is HQ",
                             query_embedding=q, k=5, as_of=D(2026, 5, 1))
    texts = [m.text for m, _ in past_hits]
    assert any("Austin" in t for t in texts) and not any("Denver" in t for t in texts)


def test_scope_isolation_in_sql(db):
    store.add_memory(db, owner_id="alice", scope="s", text="alice fact", embedding=_vec("x"))
    store.add_memory(db, owner_id="bob", scope="s", text="bob fact", embedding=_vec("x"))
    hits = store.recall(db, owner_id="alice", scope="s", query="fact", query_embedding=_vec("x"), k=5)
    assert hits and all(m.owner_id == "alice" for m, _ in hits)


def test_dim_mismatch_falls_back_to_python(db, monkeypatch):
    """A query vector not matching the index width must never hit the ANN path:"""
    store.add_memory(db, owner_id="u", scope="s", text="toy fact", embedding=[1.0, 2.0])
    def _boom(*a, **k):
        raise AssertionError("ANN path must not run for non-index-width vectors")
    monkeypatch.setattr(pg_recall, "ann_semantic_ids", _boom)
    hits = store.recall(db, owner_id="u", scope="s", query="toy fact", query_embedding=[1.0, 2.0], k=3)
    assert hits and "toy" in hits[0][0].text


def test_backfill_fills_missing_vectors(db, pg_engine):
    """Rows written before the vector column existed are indexed by the migration backfill:"""
    row = store.add_memory(db, owner_id="u", scope="s", text="pre-migration fact", embedding=_vec("old"))
    db.execute(text("UPDATE ai_memories SET embedding_vec = NULL WHERE id = :id"), {"id": row.id})
    db.commit()
    spec = importlib.util.spec_from_file_location("m13b", _MIGRATION)
    m13 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m13)
    with pg_engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            m13.upgrade()
        conn.commit()
    got = db.execute(text("SELECT embedding_vec IS NOT NULL FROM ai_memories WHERE id = :id"),
                     {"id": row.id}).scalar()
    assert got is True
