"""Merge onboarding question-flow heads with managed-links tip.

Revision ID: 20260807_01
Revises: 20260801_05, 20260806_02
Create Date: 2026-08-07

The course-skip/onboarding branch forked from 20260725_04 (20260801_01..05)
while main continued through interview awards and then partnerships + managed
links (20260804_01 → 20260806_01 → 20260806_02). Merging main into the
onboarding branch left two alembic heads; this empty merge restores a single
linear tip so fresh preview DBs can `upgrade head`.
"""

from typing import Sequence, Union

from alembic import op  # noqa: F401


revision: str = "20260807_01"
down_revision: Union[str, tuple[str, ...], None] = ("20260801_05", "20260806_02")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
