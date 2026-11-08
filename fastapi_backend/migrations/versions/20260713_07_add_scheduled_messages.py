"""
Add `scheduled_messages`: the schedule-send outbox.

A composed message waits here (provider-neutral JSON payload) until send_at,
when the inbox dispatcher thread sends it through the normal send path and
removes the row; failed dispatches stay visible as status=failed.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()`
before Alembic and the model already declares the table, so it usually exists
by the time this runs; creation is guarded with an inspector check.

Revision ID: 20260713_07
Revises: 20260713_06
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn


revision = "20260713_07"
down_revision = "20260713_06"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    """Returning whether a table already exists (dialect-agnostic):"""
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table("scheduled_messages"):
        op.create_table(
            "scheduled_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("payload", JSONColumn, nullable=False),
            sa.Column("send_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
                index=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("scheduled_messages"):
        op.drop_table("scheduled_messages")
