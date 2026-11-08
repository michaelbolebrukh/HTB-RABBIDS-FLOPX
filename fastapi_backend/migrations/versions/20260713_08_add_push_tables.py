"""
Add `platform_keys` + `push_subscriptions`: web-push notifications.

platform_keys stores instance-generated key material (first consumer: the
VAPID keypair, generated once at first boot) so no per-instance env var is
needed. push_subscriptions holds one row per browser/device that opted into
new-mail notifications; dead endpoints are pruned on delivery failure.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()`
before Alembic and the models already declare both tables, so they usually
exist by the time this runs; creation is guarded with inspector checks.

Revision ID: 20260713_08
Revises: 20260713_07
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op


revision = "20260713_08"
down_revision = "20260713_07"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    """Returning whether a table already exists (dialect-agnostic):"""
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table("platform_keys"):
        op.create_table(
            "platform_keys",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
    if not _has_table("push_subscriptions"):
        op.create_table(
            "push_subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
            sa.Column("p256dh", sa.Text(), nullable=False),
            sa.Column("auth", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("push_subscriptions"):
        op.drop_table("push_subscriptions")
    if _has_table("platform_keys"):
        op.drop_table("platform_keys")
