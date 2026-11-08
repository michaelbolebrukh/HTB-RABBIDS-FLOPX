"""
Add the pgvector ANN index to ai_memories (P10/D62) — the production recall path.

PostgreSQL-only by design (the AI service's production dialect; D62 relaxes the
portability rule for this service): on other dialects this migration is a
complete no-op and recall keeps the exact Python-cosine fallback.

What it does on PostgreSQL, each step guarded + idempotent:
1. CREATE EXTENSION vector — if the extension is unavailable (image without
   pgvector, no privilege), log and SKIP everything below: the service keeps
   working on the fallback path rather than failing the boot.
2. ADD COLUMN embedding_vec vector(1536) — deliberately NOT on the ORM model,
   so `Base.metadata.create_all()` (which runs before Alembic on boot) never
   emits a `vector` type before the extension exists.
3. Backfill from the JSONB `embedding` column (1536-dim rows only).
4. HNSW index (cosine) for the semantic leg + GIN full-text index for the
   lexical leg.

Revision ID: 20260713_13
Revises: 20260713_12
Create Date: 2026-07-19
"""

import logging

import sqlalchemy as sa
from alembic import op


revision = "20260713_13"
down_revision = "20260713_12"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Must match memory/pg_recall.EMBEDDING_DIM (text-embedding-3-small):
_DIM = 1536


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_column(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if "ai_memories" not in insp.get_table_names():
        return False
    return name in {c["name"] for c in insp.get_columns("ai_memories")}


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    try:
        # SAVEPOINT-scoped probe: a failed CREATE EXTENSION otherwise aborts the
        # surrounding migration transaction, and Alembic's own version stamp then
        # dies with InFailedSqlTransaction — failing the very boot this guard
        # exists to protect:
        with bind.begin_nested():
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:  # noqa: BLE001 — no extension => stay on the fallback path, never fail boot:
        logger.warning(
            "pgvector extension unavailable; skipping ANN index (memory recall "
            "falls back to exact Python cosine)", exc_info=True,
        )
        return

    if not _has_column("embedding_vec"):
        bind.execute(sa.text(f"ALTER TABLE ai_memories ADD COLUMN embedding_vec vector({_DIM})"))

    # Backfilling rows whose JSON embedding matches the index width (JSONB array
    # text form '[1.0, 2.0]' is valid pgvector input):
    bind.execute(sa.text(
        "UPDATE ai_memories SET embedding_vec = embedding::text::vector "
        "WHERE embedding IS NOT NULL AND embedding_vec IS NULL "
        f"AND jsonb_array_length(embedding) = {_DIM}"
    ))

    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ai_memories_embedding_vec "
        "ON ai_memories USING hnsw (embedding_vec vector_cosine_ops)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ai_memories_text_fts "
        "ON ai_memories USING gin (to_tsvector('simple', text))"
    ))


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_ai_memories_text_fts"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_ai_memories_embedding_vec"))
    if _has_column("embedding_vec"):
        bind.execute(sa.text("ALTER TABLE ai_memories DROP COLUMN embedding_vec"))
    # The extension stays installed (other consumers may rely on it).
