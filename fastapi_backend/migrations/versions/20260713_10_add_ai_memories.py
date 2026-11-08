"""
Add ai_memories — the bi-temporal agent-authored fact store (Phase 10).

IDEMPOTENCY (repo migrations rule): startup runs `Base.metadata.create_all()`
before Alembic, so the table usually already exists by migration time. The DDL
is guarded with a dialect-agnostic inspector check, same pattern as the earlier
ai_ migrations.

Revision ID: 20260713_10
Revises: 20260713_09
Create Date: 2026-07-13
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn, Uuid


revision = "20260713_10"
down_revision = "20260713_09"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    """Returning the set of tables that already exist (dialect-agnostic):"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "ai_memories" not in _existing_tables():
        op.create_table(
            "ai_memories",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("agent_name", sa.String(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("claim_key", sa.String(), nullable=True),
            sa.Column("embedding", JSONColumn, nullable=True),
            sa.Column("valid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_by", Uuid(as_uuid=True), nullable=True),
            sa.Column("source_turn_ids", JSONColumn, nullable=False),
            sa.Column("meta", JSONColumn, nullable=False),
        )
        op.create_index("ix_ai_memories_content_hash", "ai_memories", ["content_hash"])
        op.create_index("ix_ai_memories_scope_live", "ai_memories", ["owner_id", "agent_name", "superseded_at"])


def downgrade() -> None:
    if "ai_memories" in _existing_tables():
        op.drop_table("ai_memories")
