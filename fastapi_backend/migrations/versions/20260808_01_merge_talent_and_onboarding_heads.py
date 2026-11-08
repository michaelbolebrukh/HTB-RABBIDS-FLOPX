"""Merge the talent CV-funnel heads with the onboarding/links heads.

Revision ID: 20260808_01
Revises: 20260806_03, 20260807_01
Create Date: 2026-08-08

Two independent merge migrations landed side by side once main (onboarding
question-flow + managed links, 20260807_01) was merged into the talent CV
branch (talent + managed links, 20260806_03) — both already-merged tips, still
two heads. No schema changes; this restores a single linear tip so
`alembic upgrade head` works again.
"""

from typing import Sequence, Union

from alembic import op  # noqa: F401


revision: str = "20260808_01"
down_revision: Union[str, tuple[str, ...], None] = ("20260806_03", "20260807_01")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
