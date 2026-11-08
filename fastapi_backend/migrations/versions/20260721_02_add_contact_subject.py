"""Add subject to contact_submissions.

Optional subject line for contact form submissions (e.g. the prefilled
"Name change" support flow); carried into the admin notification email
subject. Idempotent, so re-running over a DB that already has the column is
a no-op. Portable (Alembic ops + generic types), per the database rule.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_02"
down_revision = "20260721_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("contact_submissions", "subject"):
        op.add_column("contact_submissions", sa.Column("subject", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("contact_submissions", "subject"):
        op.drop_column("contact_submissions", "subject")
