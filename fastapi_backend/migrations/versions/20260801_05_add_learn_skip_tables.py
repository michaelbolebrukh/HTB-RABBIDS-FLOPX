"""Retired: add learn skip tables

This revision created or altered tables that belonged to the predecessor
instance's product layer. That product layer was replaced wholesale on
2026-11-07 (FLOPX), so the revision is kept ONLY so the Alembic chain keeps its
ids and its single head: both directions are deliberate no-ops. Existing
databases keep whatever these tables hold; a separate, explicit cleanup
migration drops them when the operator decides to.

Revision ID: 20260801_05
Revises: 20260801_04
"""

from alembic import op  # noqa: F401 - kept so the file stays a recognisable migration


revision = '20260801_05'
down_revision = "20260801_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
