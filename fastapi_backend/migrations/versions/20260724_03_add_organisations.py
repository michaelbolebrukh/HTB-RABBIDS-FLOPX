"""
Organisations: a first-class org<->account relation that CONFERS capabilities.

Companies are data, not authorization: gating stays the one primitive
(require_capability) and no gate ever asks about an organisation. What an
organisation does is confer — while it is active, its `granted_capabilities`
are unioned onto its members' effective grants at CHECK time
(platform/services/organisation_service.effective_capabilities).

PURELY ADDITIVE: conferral is computed, never materialised, so `users` is not
touched at all — `users.capabilities` keeps its exact meaning (grants held
DIRECTLY by the account) and no backfill is needed, because no existing row
changes meaning. Revocation is deleting a membership row, so there is likewise
no copy of a conferred grant that a downgrade could strand.

IDEMPOTENCY (migrations rule): every step is guarded by an inspector check, so
replaying over a partially- or fully-migrated database is a no-op. Portable
(Alembic ops + the JSONColumn variant from infrastructure/db_types, no
PostgreSQL-only SQL).

Revision ID: 20260724_03
Revises: 20260724_02
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn


revision = "20260724_03"
down_revision = "20260724_02"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("organisations"):
        op.create_table(
            "organisations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("website_url", sa.String(), nullable=True),
            sa.Column("logo_url", sa.String(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("granted_capabilities", JSONColumn, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        # Unique AND indexed in one object, matching what the model's
        # `unique=True, index=True` slug column emits:
        op.create_index("ix_organisations_slug", "organisations", ["slug"], unique=True)
        op.create_index("ix_organisations_is_active", "organisations", ["is_active"])

    if not _has_table("organisation_members"):
        op.create_table(
            "organisation_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organisation_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["organisation_id"], ["organisations.id"],
                name="fk_organisation_members_organisation_id", ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"],
                name="fk_organisation_members_user_id", ondelete="CASCADE",
            ),
            # One membership per (org, user) — what makes add_member idempotent:
            sa.UniqueConstraint("organisation_id", "user_id", name="uq_organisation_members_org_user"),
        )
        op.create_index("ix_organisation_members_organisation_id", "organisation_members", ["organisation_id"])
        op.create_index("ix_organisation_members_user_id", "organisation_members", ["user_id"])


def downgrade() -> None:
    # Dropping both tables removes every conferral. No account loses a grant it
    # holds directly, because conferred grants were never written onto the user:
    if _has_table("organisation_members"):
        op.drop_index("ix_organisation_members_user_id", table_name="organisation_members")
        op.drop_index("ix_organisation_members_organisation_id", table_name="organisation_members")
        op.drop_table("organisation_members")

    if _has_table("organisations"):
        op.drop_index("ix_organisations_is_active", table_name="organisations")
        op.drop_index("ix_organisations_slug", table_name="organisations")
        op.drop_table("organisations")
