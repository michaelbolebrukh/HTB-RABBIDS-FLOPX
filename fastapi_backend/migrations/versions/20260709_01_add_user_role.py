"""
Replace users.is_admin with a single `role` tier (user / staff / admin).

Introduces one ordered access tier as the source of truth so an invalid
"admin but not staff" state is unrepresentable; is_staff / is_admin become
derived on the model. Existing admins backfill to role='admin'. See the
access-control rule.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()` before
Alembic. On a fresh DB the model already declares `role` (and no longer declares
is_admin/is_staff), so the column exists and the old ones do not; every step is
guarded so this is a no-op there. On an existing DB it adds `role`, backfills
from the old flags, and drops them (batch mode so SQLite can drop columns too).

Revision ID: 20260709_01
Revises: 20260708_01
Create Date: 2026-07-09
"""

import sqlalchemy as sa
from alembic import op


revision = "20260709_01"
down_revision = "20260708_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    """Returning whether a column already exists on a table (dialect-agnostic):"""
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "role"):
        op.add_column(
            "users",
            sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
        )

    users = sa.table(
        "users",
        sa.column("role", sa.String),
        sa.column("is_admin", sa.Boolean),
        sa.column("is_staff", sa.Boolean),
    )

    # Backfill from whichever legacy flags exist (admin wins over staff):
    if _has_column("users", "is_admin"):
        op.execute(users.update().where(users.c.is_admin == sa.true()).values(role="admin"))
    if _has_column("users", "is_staff"):
        op.execute(
            users.update()
            .where(sa.and_(users.c.is_staff == sa.true(), users.c.role != "admin"))
            .values(role="staff")
        )

    # Drop the legacy flags. Compute existence before the batch (SQLite recreates
    # the table inside it); batch is a no-op wrapper on PostgreSQL:
    has_is_admin = _has_column("users", "is_admin")
    has_is_staff = _has_column("users", "is_staff")
    if has_is_admin or has_is_staff:
        with op.batch_alter_table("users") as batch_op:
            if has_is_admin:
                batch_op.drop_column("is_admin")
            if has_is_staff:
                batch_op.drop_column("is_staff")


def downgrade() -> None:
    if not _has_column("users", "is_admin"):
        op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("users", "is_staff"):
        op.add_column("users", sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.false()))

    users = sa.table(
        "users",
        sa.column("role", sa.String),
        sa.column("is_admin", sa.Boolean),
        sa.column("is_staff", sa.Boolean),
    )
    if _has_column("users", "role"):
        op.execute(users.update().where(users.c.role == "admin").values(is_admin=sa.true(), is_staff=sa.true()))
        op.execute(users.update().where(users.c.role == "staff").values(is_staff=sa.true()))
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("role")
