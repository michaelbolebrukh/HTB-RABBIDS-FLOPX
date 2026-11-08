"""Add name_changed_at to users.

Nullable timestamp of the last display-name change (null = never changed since
signup); drives the once-per-period name-change limit. Idempotent, so re-running
over a DB that already has the column is a no-op. Portable (Alembic ops + generic
types), per the database rule.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_01"
down_revision = "20260713_13"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "name_changed_at"):
        op.add_column("users", sa.Column("name_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column("users", "name_changed_at"):
        op.drop_column("users", "name_changed_at")
