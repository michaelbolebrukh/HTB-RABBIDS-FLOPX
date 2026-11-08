"""
Add a monotonic `seq` column to ai_transcript_items for deterministic ordering.

Server timestamps are unsafe for intra-run ordering (multiple items appended in
one run share a created_at on both SQLite and PostgreSQL), so full_history now
orders by an app-assigned per-conversation sequence instead.

IDEMPOTENCY (repo migrations rule): startup runs `Base.metadata.create_all()`
before Alembic, and the model already declares `seq` (index=True), so the
column and its index usually exist by the time this migration runs. Both DDL
ops are therefore guarded with a dialect-agnostic inspector check — without
this the migration raises "duplicate column name: seq" on the create_all path.

Revision ID: 20260705_01
Revises: 20260704_01
Create Date: 2026-07-05
"""

import sqlalchemy as sa
from alembic import op


revision = "20260705_01"
down_revision = "20260704_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    """Returning whether a column already exists on a table (dialect-agnostic):"""
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def _has_index(table: str, index: str) -> bool:
    """Returning whether a named index already exists on a table:"""
    return any(ix["name"] == index for ix in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    # Adding seq with a server default of 0 so existing rows backfill, then
    # keeping the app-side default for new inserts (store assigns max+1).
    # Guarded because create_all may already have added it from the model:
    if not _has_column("ai_transcript_items", "seq"):
        op.add_column(
            "ai_transcript_items",
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_index("ai_transcript_items", "ix_ai_transcript_items_seq"):
        op.create_index("ix_ai_transcript_items_seq", "ai_transcript_items", ["seq"])


def downgrade() -> None:
    # Dropping index then column, each guarded so downgrade is state-tolerant:
    if _has_index("ai_transcript_items", "ix_ai_transcript_items_seq"):
        op.drop_index("ix_ai_transcript_items_seq", table_name="ai_transcript_items")
    if _has_column("ai_transcript_items", "seq"):
        op.drop_column("ai_transcript_items", "seq")
