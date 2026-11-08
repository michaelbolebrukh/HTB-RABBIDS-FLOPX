"""
Migration-chain guard: the Alembic revision graph must stay LINEAR.

Two feature branches that each add a migration from the same parent merge into
a two-headed graph; `alembic upgrade head` then refuses with "Multiple head
revisions" — a failure invisible to the test suite (tests use create_all, not
Alembic) that only appears at deploy time (it broke the 2026-07-11 deploy).
This test makes the merge that creates a second head fail CI instead.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_single_alembic_head():
    """Asserting exactly one head so 'upgrade head' can never be ambiguous:"""
    base_dir = Path(__file__).resolve().parents[2]
    cfg = Config(str(base_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(base_dir / "migrations"))

    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, (
        f"Alembic has {len(heads)} heads {heads}; re-parent the newest migration "
        f"onto the other chain's tip to restore a single linear head"
    )
