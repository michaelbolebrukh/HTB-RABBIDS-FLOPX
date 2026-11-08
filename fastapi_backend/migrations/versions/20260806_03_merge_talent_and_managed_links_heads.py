"""
Merge migration heads after main's managed_links tip landed beside the
talent CV chain.

Heads being merged:
  - 20260801_02_talent (talent: cv_nudge_sent_at)
  - 20260806_02 (managed_links; revises partnerships 20260806_01)

No schema changes — linearises the graph so `alembic upgrade head` works.

Replaces the earlier empty merge that incorrectly reused revision id
20260806_02 (now owned by managed_links on main).

Revision ID: 20260806_03
Revises: 20260801_02_talent, 20260806_02
Create Date: 2026-08-06

Note: the talent-chain parent was renamed from "20260801_02" to
"20260801_02_talent" during the onboarding-branch merge (id collision with
main's question_flow_leads migration); updated here to match.
"""

revision = "20260806_03"
down_revision = ("20260801_02_talent", "20260806_02")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
