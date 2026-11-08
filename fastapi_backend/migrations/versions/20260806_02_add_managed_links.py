"""
Add `managed_links`: stable public keys with mutable destinations for NFC/QR campaigns.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()` before
Alembic and the model already declares the table, so it usually exists by the
time this runs; creation is guarded with an inspector check.

Revision ID: 20260806_02
Revises: 20260806_01
Create Date: 2026-08-06

Renumbered to 20260806_02 after rebase onto main: main already owned
20260806_01 for partnerships, so this tip follows that revision.
"""

import sqlalchemy as sa
from alembic import op


revision = "20260806_02"
down_revision = "20260806_01"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    """Returning whether a table already exists (dialect-agnostic):"""
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table("managed_links"):
        op.create_table(
            "managed_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=200), nullable=True),
            sa.Column("destination", sa.String(length=2048), nullable=False),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("key"),
        )
        op.create_index("ix_managed_links_key", "managed_links", ["key"])
        op.create_index("ix_managed_links_is_active", "managed_links", ["is_active"])


def downgrade() -> None:
    if _has_table("managed_links"):
        op.drop_index("ix_managed_links_is_active", table_name="managed_links")
        op.drop_index("ix_managed_links_key", table_name="managed_links")
        op.drop_table("managed_links")
