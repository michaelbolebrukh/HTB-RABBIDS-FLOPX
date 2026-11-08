"""
Add ai_user_extensions for user-added MCP servers and Skills (Phase 9, D51).

IDEMPOTENCY (repo migrations rule): startup runs `Base.metadata.create_all()`
BEFORE Alembic, so the table usually already exists by migration time. The DDL
is guarded with a dialect-agnostic inspector check (present -> skip), same
pattern as the earlier ai_ migrations.

Revision ID: 20260713_09
Revises: 20260713_08
Create Date: 2026-07-13
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn, Uuid


revision = "20260713_09"
down_revision = "20260713_08"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    """Returning the set of tables that already exist (dialect-agnostic):"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "ai_user_extensions" not in _existing_tables():
        op.create_table(
            "ai_user_extensions",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("agent_name", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("config", JSONColumn, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_ai_user_ext_owner_agent", "ai_user_extensions", ["owner_id", "agent_name"])


def downgrade() -> None:
    if "ai_user_extensions" in _existing_tables():
        op.drop_table("ai_user_extensions")
