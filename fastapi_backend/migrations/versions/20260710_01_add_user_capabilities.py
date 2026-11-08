"""
Add `capabilities` to users: granular grants on top of the role tier.

String grants (e.g. "inbox:read", "inbox:write") for staff surfaces too
sensitive for every staff member; admin implicitly holds all of them. First
consumer is the admin inbox session endpoint. See the access-control rule.

IDEMPOTENCY (migrations rule): startup runs `Base.metadata.create_all()` before
Alembic and the model already declares `capabilities`, so the column usually
exists by the time this runs; the add is guarded with an inspector check.

Revision ID: 20260710_01
Revises: 20260709_01
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn


revision = "20260710_01"
down_revision = "20260709_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    """Returning whether a column already exists on a table (dialect-agnostic):"""
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "capabilities"):
        op.add_column("users", sa.Column("capabilities", JSONColumn, nullable=True))


def downgrade() -> None:
    if _has_column("users", "capabilities"):
        op.drop_column("users", "capabilities")
