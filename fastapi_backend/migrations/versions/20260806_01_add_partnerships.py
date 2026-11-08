"""
Partnerships: per-organisation, per-season records of an agreement's STATE.

One new table and nothing else. `organisations` and `users` are untouched: a
partnership confers nothing (conferral remains the organisation's
`granted_capabilities`, computed at check time), so no existing column changes
meaning and there is no backfill.

The status column is a plain VARCHAR with a 'draft' server default rather than a
database enum — valid states live in platform/partnerships.PARTNERSHIP_STATUSES
and are validated in Python, so the workflow can gain or lose a step without a
migration, and historical rows keep whatever state they were written with.

No unique constraint on (organisation_id, season): renewals are new rows and one
season may legitimately hold more than one agreement. The seeder's
one-row-per-season rule is a seeding decision it enforces itself.

IDEMPOTENCY (migrations rule): every step is inspector-guarded in both
directions, so replaying over a partially- or fully-migrated database is a
no-op. Plain Alembic ops with portable types, so the chain still replays on the
SQLite databases the tests build.

Revision ID: 20260806_01
Revises: 20260804_01
Create Date: 2026-08-06

Renumbered off the colliding `20260729_01` id (main's interview runway
migration already owned that revision) and reparented onto the single tip so
the chain stays linear.
"""

import sqlalchemy as sa
from alembic import op


revision = "20260806_01"
down_revision = "20260804_01"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return name in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if not _has_table("partnerships"):
        op.create_table(
            "partnerships",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organisation_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="draft"),
            sa.Column("season", sa.String(), nullable=False),
            sa.Column("offering_label", sa.String(), nullable=True),
            sa.Column("snapshot_key", sa.String(), nullable=True),
            # Contractual days, not instants — expiry is computed from ends_on
            # at read time and never stored as a flag:
            sa.Column("starts_on", sa.Date(), nullable=True),
            sa.Column("ends_on", sa.Date(), nullable=True),
            sa.Column("signed_on", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["organisation_id"], ["organisations.id"],
                name="fk_partnerships_organisation_id", ondelete="CASCADE",
            ),
        )

    # Indexes created separately from the table so a database that already has
    # the table (create_all ran first at boot) still gains anything missing:
    if not _has_index("partnerships", "ix_partnerships_organisation_id"):
        op.create_index("ix_partnerships_organisation_id", "partnerships", ["organisation_id"])
    if not _has_index("partnerships", "ix_partnerships_status"):
        op.create_index("ix_partnerships_status", "partnerships", ["status"])
    if not _has_index("partnerships", "ix_partnerships_season"):
        op.create_index("ix_partnerships_season", "partnerships", ["season"])


def downgrade() -> None:
    # Dropping the table removes the partnership history and nothing else: no
    # account and no organisation loses a grant, because a partnership never
    # conferred one:
    if _has_index("partnerships", "ix_partnerships_season"):
        op.drop_index("ix_partnerships_season", table_name="partnerships")
    if _has_index("partnerships", "ix_partnerships_status"):
        op.drop_index("ix_partnerships_status", table_name="partnerships")
    if _has_index("partnerships", "ix_partnerships_organisation_id"):
        op.drop_index("ix_partnerships_organisation_id", table_name="partnerships")

    if _has_table("partnerships"):
        op.drop_table("partnerships")
