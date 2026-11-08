"""Add accessibility_preferences for per-user platform accessibility settings.

One row per user holding the master toggle (nullable — NULL means no explicit
choice, so the client auto-detects from browser signals) plus text scale,
contrast, readability, and reduce-effects preferences. The frontend caches
these in localStorage for instant/offline application; this table is the
online cross-device source of truth (same pattern as game_settings).

Merged with main: renamed twice after main-side id collisions (20260705_01
transcript item seq, then the 20260709/20260710 role-tier chain). Idempotent, so
DBs that applied an old revision id re-run it as a no-op after the orphaned
version row is cleared.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_04"
down_revision = "20260713_03"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("accessibility_preferences"):
        op.create_table(
            "accessibility_preferences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=True),
            sa.Column("text_scale", sa.String(), nullable=False, server_default="md"),
            sa.Column(
                "high_contrast",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "enhanced_readability",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("reduce_effects", sa.String(), nullable=False, server_default="system"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("user_id", name="uq_accessibility_preferences_user"),
        )
        op.create_index(
            "ix_accessibility_preferences_user_id",
            "accessibility_preferences",
            ["user_id"],
        )


def downgrade() -> None:
    if _has_table("accessibility_preferences"):
        op.drop_index(
            "ix_accessibility_preferences_user_id",
            table_name="accessibility_preferences",
        )
        op.drop_table("accessibility_preferences")
