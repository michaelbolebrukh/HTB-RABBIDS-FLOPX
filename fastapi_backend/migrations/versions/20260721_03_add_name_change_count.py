"""Add name_change_count to users.

Number of display-name changes made within the current allowance window
(paired with name_changed_at, the window start). Drives the "N changes per
period" limit. Idempotent, so re-running over a DB that already has the column
is a no-op. Portable (Alembic ops + generic types), per the database rule.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_03"
down_revision = "20260721_02"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "name_change_count"):
        op.add_column(
            "users",
            sa.Column("name_change_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _has_column("users", "name_change_count"):
        op.drop_column("users", "name_change_count")
