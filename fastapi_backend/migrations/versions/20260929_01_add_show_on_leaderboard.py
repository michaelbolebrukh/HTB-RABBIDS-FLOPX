"""
Per-member leaderboard visibility: `users.show_on_leaderboard`, default true.

Idempotent: the column is added only when absent, so the boot-time migration
is safe to re-run and safe against a database whose models already created it.

Revision ID: 20260929_01
Revises: 20260928_01
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20260929_01"
down_revision = "20260928_01"
branch_labels = None
depends_on = None

TABLE = "users"
COLUMN = "show_on_leaderboard"
INDEX = "ix_users_show_on_leaderboard"


def _columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def _indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(TABLE):
        return
    if COLUMN not in _columns():
        op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.true()))
    if INDEX not in _indexes():
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    if INDEX in _indexes():
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns():
        op.drop_column(TABLE, COLUMN)
