"""
Add ai_spend_events and ai_budgets for the generative AI service's cost meter
and budget enforcement (Phase 4, D7).

IDEMPOTENCY (repo migrations rule): startup runs `Base.metadata.create_all()`
BEFORE Alembic, so these tables usually already exist by migration time. Every
DDL op is guarded with a dialect-agnostic inspector check (present -> skip,
absent -> create), same pattern as 20260704_01/20260705_01. Re-parented from 20260705_01
onto 20260710_01 after the Stripe chain merged first (single linear head).

Revision ID: 20260711_01
Revises: 20260710_01
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn, Uuid


revision = "20260711_01"
down_revision = "20260710_01"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    """Returning the set of tables that already exist (dialect-agnostic):"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    # Creating the append-only spend ledger (integer micro-USD costs):
    if "ai_spend_events" not in existing:
        op.create_table(
            "ai_spend_events",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("conversation_id", Uuid(as_uuid=True), nullable=True),
            sa.Column("run_id", Uuid(as_uuid=True), nullable=True),
            sa.Column("agent_name", sa.String(), nullable=True),
            sa.Column("model_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False, server_default="run"),
            sa.Column("usage", JSONColumn, nullable=False),
            sa.Column("cost_microusd", sa.BigInteger(), nullable=True),
            sa.Column("priced", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_ai_spend_events_conversation_id", "ai_spend_events", ["conversation_id"])
        op.create_index("ix_ai_spend_owner_time", "ai_spend_events", ["owner_id", "created_at"])

    # Creating scoped budget limits (one row per scope x window):
    if "ai_budgets" not in existing:
        op.create_table(
            "ai_budgets",
            sa.Column("id", Uuid(as_uuid=True), primary_key=True),
            sa.Column("scope_type", sa.String(), nullable=False),
            sa.Column("scope_id", sa.String(), nullable=False),
            sa.Column("window", sa.String(), nullable=False),
            sa.Column("limit_microusd", sa.BigInteger(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("meta", JSONColumn, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("scope_type", "scope_id", "window"),
        )
        op.create_index("ix_ai_budgets_scope_id", "ai_budgets", ["scope_id"])


def downgrade() -> None:
    # Dropping guarded so a downgrade is safe regardless of actual state:
    existing = _existing_tables()
    if "ai_budgets" in existing:
        op.drop_table("ai_budgets")
    if "ai_spend_events" in existing:
        op.drop_table("ai_spend_events")
