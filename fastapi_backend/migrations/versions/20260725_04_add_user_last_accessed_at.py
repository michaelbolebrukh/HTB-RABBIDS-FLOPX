"""
Add users.last_accessed_at — when the account last made an authenticated request.

NULLABLE with no default and no backfill, deliberately: null means "this account
has not been seen since the column shipped". Defaulting it to now() would claim
every dormant account was just active, and backfilling it from created_at would
claim a signup date was an access — both invent activity that never happened.
Null is the honest state and is distinguishable from a genuinely old timestamp.

NO INDEX, also deliberately: nothing filters or sorts on this column today, and
it is the most write-hot column on `users` (every active session stamps it once
per throttle window). An index would be maintained on every stamp and would rule
out heap-only-tuple updates on PostgreSQL, paying real write cost for an ordering
no query asks for. When an admin "who has been active" query lands, it can add
the index in its own migration.

IDEMPOTENCY (migrations rule): both directions are guarded by an inspector check,
so replaying over a partially-migrated database is a no-op.

Revision ID: 20260725_04
Revises: 20260725_03
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op


revision = "20260725_04"
down_revision = "20260725_03"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("users")
    return any(column["name"] == "last_accessed_at" for column in columns)


def upgrade() -> None:
    if _has_column():
        return
    op.add_column("users", sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column("users", "last_accessed_at")
