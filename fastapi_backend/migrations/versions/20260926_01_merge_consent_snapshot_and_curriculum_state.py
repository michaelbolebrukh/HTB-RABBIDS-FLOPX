"""
Merge migration heads after main's CV consent snapshot landed beside the
curriculum activation bookkeeping.

Heads being merged:
  - 20260919_01 (cv_share_consents.statement_snapshot, from main)
  - 20260923_01 (learn_curriculum_state / archives, from the Learn branch)

Both revise 20260809_03. No schema changes — linearises the graph so
`alembic upgrade head` works again.

Revision ID: 20260926_01
Revises: 20260919_01, 20260923_01
Create Date: 2026-09-26
"""

revision = "20260926_01"
down_revision = ("20260919_01", "20260923_01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
