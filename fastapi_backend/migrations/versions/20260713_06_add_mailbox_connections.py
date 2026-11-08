"""
Add `mailbox_connections`: audit registry of mail-client credential grants.

Every time a staff member takes the mailbox credential (reveals the settings
or downloads the Apple configuration profile) from the admin connector flow,
one row records who, which connector, and when. Audit knowledge, not
enforcement: the mailbox has one shared credential, so revocation is a
password rotation that disconnects every device at once.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()`
before Alembic and the model already declares the table, so it usually exists
by the time this runs; creation is guarded with an inspector check.

Revision ID: 20260713_06
Revises: 20260713_05
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op


revision = "20260713_06"
down_revision = "20260713_05"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    """Returning whether a table already exists (dialect-agnostic):"""
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table("mailbox_connections"):
        op.create_table(
            "mailbox_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("connector", sa.String(length=40), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("mailbox_connections"):
        op.drop_table("mailbox_connections")
