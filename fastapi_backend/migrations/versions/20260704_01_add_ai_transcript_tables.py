"""
Add ai_conversations, ai_transcript_items, ai_transcript_summaries for the
generative AI service's provider-independent transcript store (Phase 1).

Tables are package-owned by platform/services/generative_ai_service (D26
extraction rules): `ai_` prefix, owner_id as a plain string with no FK into
users, portable column types throughout.

IDEMPOTENCY (repo migrations rule): application startup runs
`Base.metadata.create_all()` BEFORE Alembic (infrastructure/database.py), so
these tables usually already exist by the time this migration runs. Like the
neighbouring game_runs migration, every DDL op is therefore guarded so the
sequence "create_all then migrate" succeeds regardless of database state. The
guard is a dialect-agnostic inspector check (works on PostgreSQL and SQLite).

Revision ID: 20260704_01
Revises: 20260610_01
Create Date: 2026-07-04
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn, Uuid


revision = "20260704_01"
down_revision = "20260610_01"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    """Returning the set of tables that already exist (dialect-agnostic):"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    # Creating the conversation container (owner is host-agnostic, no FK — D26).
    # The models declare every index below, so create_all builds the complete
    # table; when it already exists we skip the whole table + its indexes:
    if "ai_conversations" not in existing:
        op.create_table(
            "ai_conversations",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("current_item_id", Uuid(as_uuid=True), nullable=True),
            sa.Column("meta", JSONColumn, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_ai_conversations_owner_id", "ai_conversations", ["owner_id"])

    # Creating the append-only item stream (see transcript/types.py for semantics):
    if "ai_transcript_items" not in existing:
        op.create_table(
            "ai_transcript_items",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "conversation_id",
                Uuid(as_uuid=True),
                sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("parent_id", Uuid(as_uuid=True), sa.ForeignKey("ai_transcript_items.id"), nullable=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=True),
            sa.Column("parts", JSONColumn, nullable=False),
            sa.Column("tool_name", sa.String(), nullable=True),
            sa.Column("tool_call_id", sa.String(), nullable=True),
            sa.Column("model_used", sa.String(), nullable=True),
            sa.Column("usage", JSONColumn, nullable=True),
            sa.Column("token_estimates", JSONColumn, nullable=False),
            sa.Column("raw", JSONColumn, nullable=True),
            sa.Column("meta", JSONColumn, nullable=False),
            sa.Column("text_content", sa.Text(), nullable=True),
            sa.Column("embedding", JSONColumn, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_ai_transcript_items_conversation_id", "ai_transcript_items", ["conversation_id"])
        op.create_index("ix_ai_transcript_items_parent_id", "ai_transcript_items", ["parent_id"])
        op.create_index("ix_ai_transcript_items_kind", "ai_transcript_items", ["kind"])
        op.create_index("ix_ai_transcript_items_created_at", "ai_transcript_items", ["created_at"])

    # Creating the summary cache (entries keyed by span/budget/tokenizer — D4/D24):
    if "ai_transcript_summaries" not in existing:
        op.create_table(
            "ai_transcript_summaries",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "conversation_id",
                Uuid(as_uuid=True),
                sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("span_end_item_id", Uuid(as_uuid=True), nullable=False),
            sa.Column("token_budget", sa.Integer(), nullable=False),
            sa.Column("tokenizer_family", sa.String(), nullable=False),
            sa.Column("summary_text", sa.Text(), nullable=False),
            sa.Column("usage", JSONColumn, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("conversation_id", "span_end_item_id", "token_budget", "tokenizer_family"),
        )
        op.create_index("ix_ai_transcript_summaries_conversation_id", "ai_transcript_summaries", ["conversation_id"])
        op.create_index("ix_ai_summaries_lookup", "ai_transcript_summaries", ["conversation_id", "span_end_item_id"])


def downgrade() -> None:
    # Dropping children before the parent table, each guarded so a downgrade is
    # safe regardless of which tables are actually present:
    existing = _existing_tables()
    if "ai_transcript_summaries" in existing:
        op.drop_table("ai_transcript_summaries")
    if "ai_transcript_items" in existing:
        op.drop_table("ai_transcript_items")
    if "ai_conversations" in existing:
        op.drop_table("ai_conversations")
