"""Add avatar_url to users.

Adds a nullable avatar URL captured from OAuth or set via upload.

Merged with main: originally chained off 20260610_01, but main grew its own
chain (ai transcripts, transcript seq, stripe) from that revision. Re-pointed
to sit after main's head so history stays linear with a single head.
Idempotent, so re-running over a DB that already has the column is a no-op.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_01"
down_revision = "20260711_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "avatar_url"):
        op.add_column("users", sa.Column("avatar_url", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("users", "avatar_url"):
        op.drop_column("users", "avatar_url")
