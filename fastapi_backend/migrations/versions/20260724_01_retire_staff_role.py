"""
Retire the staff role: fold role='staff' into role='user'.

The staff tier is gone — authorization is now per-resource capability grants
(registry: platform/capabilities.py) checked by the single require_capability
primitive, with admin as the catch-all identity. Any row still carrying the
retired 'staff' value becomes a plain user; grants already stored on
users.capabilities are untouched, so a former staff member keeps exactly what
was explicitly granted. Admins are unaffected.

IDEMPOTENCY (migrations rule): the UPDATE matches only rows with role='staff',
so re-running over an already-converted (or fresh) DB is a no-op. Portable
(Alembic ops + generic types), per the database rule.

Revision ID: 20260724_01
Revises: 20260721_03
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op


revision = "20260724_01"
down_revision = "20260721_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    users = sa.table("users", sa.column("role", sa.String))
    # Folding the retired middle tier into plain users (admins are unaffected):
    op.execute(users.update().where(users.c.role == "staff").values(role="user"))


def downgrade() -> None:
    # No-op: which users were staff is lost by the upgrade, and that is
    # acceptable — no staff accounts exist in production, and capability
    # grants (the actual authorization) survive on users.capabilities.
    pass
