"""
Add `marketing_suppressions`: email-keyed do-not-send list for bulk/marketing mail.

Keyed by the raw (normalized) email rather than a user id so it covers
recipients who are not registered users — a bulk announcement can go to any
address. `users.marketing_consent` stays the registered user's stored
preference; marketing_service keeps the two in lockstep, with this table as the
address-level source of truth any sender (app or the mailbox-ops skill) consults.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()` before
Alembic and the model already declares the table, so it usually exists by the
time this runs; creation is guarded with an inspector check.

Revision ID: 20260713_11
Revises: 20260713_10
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op


revision = "20260713_11"
down_revision = "20260713_10"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    """Returning whether a table already exists (dialect-agnostic):"""
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table("marketing_suppressions"):
        op.create_table(
            "marketing_suppressions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("email", sa.String(), nullable=False, unique=True, index=True),
            sa.Column("reason", sa.String(), nullable=False, server_default="unsubscribe"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("marketing_suppressions"):
        op.drop_table("marketing_suppressions")
