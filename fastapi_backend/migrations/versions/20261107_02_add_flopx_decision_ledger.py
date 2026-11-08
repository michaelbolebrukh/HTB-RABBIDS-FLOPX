"""Add the decision-ledger columns to flopx_strategy_decisions (architecture §10.7).

A strategy step now PROPOSES a decision and an explicit accept SUBMITS it, so a
decision carries a status, the signal it acted on, the orders it placed and a
skipped reason. Add-only and idempotent: each column is added only when absent,
because `Base.metadata.create_all()` runs before Alembic on boot and may already
have created the table with these columns. Downgrade drops exactly the columns
this migration added.

Revision ID: 20261107_02
Revises: 20261107_01
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261107_02"
down_revision = "20261107_01"
branch_labels = None
depends_on = None

TABLE = "flopx_strategy_decisions"
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

COLUMNS = (
    sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
    sa.Column("signal_id", sa.String(128), nullable=True),
    sa.Column("signal_kind", sa.String(32), nullable=True),
    sa.Column("order_ids", JSONB, nullable=True),
    sa.Column("skipped_reason", sa.String(64), nullable=True),
    sa.Column("horizon_days", sa.Integer(), nullable=True),
    sa.Column("proposed_weight", sa.Float(), nullable=True),
)
INDEXES = (
    ("ix_flopx_strategy_decisions_status", "status"),
    ("ix_flopx_strategy_decisions_signal_id", "signal_id"),
)


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def _indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return set()
    return {i["name"] for i in inspector.get_indexes(TABLE)}


def upgrade() -> None:
    present = _columns()
    if not present:
        return  # the table is created by 20261107_01 (or create_all) with these columns already
    for column in COLUMNS:
        if column.name not in present:
            op.add_column(TABLE, column.copy())
    existing = _indexes()
    for name, column in INDEXES:
        if name not in existing:
            op.create_index(name, TABLE, [column])


def downgrade() -> None:
    existing = _indexes()
    for name, _ in INDEXES:
        if name in existing:
            op.drop_index(name, table_name=TABLE)
    present = _columns()
    for column in reversed(COLUMNS):
        if column.name in present:
            op.drop_column(TABLE, column.name)
