"""Add the FLOPX product tables.

Archived observations (live adapters append here), the paper-trading ledger
(accounts, orders, positions), saved procurement workloads and strategy
decisions. Every table is guarded by an existence check because
`Base.metadata.create_all()` runs before Alembic on boot and will usually have
created them already; this migration makes a database migrated by Alembic
alone complete, and gives the tables a reversible history.

Revision ID: 20261107_01
Revises: 20261010_01
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261107_01"
down_revision = "20261010_01"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID = sa.Uuid(as_uuid=True)

TABLES = (
    "flopx_strategy_decisions",
    "flopx_workloads",
    "flopx_paper_positions",
    "flopx_paper_orders",
    "flopx_paper_accounts",
    "flopx_observations",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("flopx_observations"):
        op.create_table(
            "flopx_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("series_id", sa.String(128), nullable=False, index=True),
            sa.Column("t", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("unit", sa.String(32), nullable=False),
            sa.Column("provenance", JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("series_id", "t", name="uq_flopx_observations_series_t"),
        )
        op.create_index("ix_flopx_observations_series_t", "flopx_observations", ["series_id", "t"])

    if not _has_table("flopx_paper_accounts"):
        op.create_table(
            "flopx_paper_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
            sa.Column("cash_minor", sa.BigInteger(), nullable=False),
            sa.Column("realised_pnl_minor", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("peak_equity_minor", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if not _has_table("flopx_paper_orders"):
        op.create_table(
            "flopx_paper_orders",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("ticker", sa.String(16), nullable=False, index=True),
            sa.Column("side", sa.String(4), nullable=False),
            sa.Column("order_type", sa.String(6), nullable=False),
            sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
            sa.Column("notional_minor", sa.BigInteger(), nullable=True),
            sa.Column("limit_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("status", sa.String(9), nullable=False, index=True),
            sa.Column("origin", sa.String(64), nullable=False, server_default="manual"),
            sa.Column("reason", sa.String(64), nullable=True),
            sa.Column("fill_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("fill_provenance", JSONB, nullable=True),
            sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_flopx_paper_orders_account_created", "flopx_paper_orders", ["account_id", "created_at"])

    if not _has_table("flopx_paper_positions"):
        op.create_table(
            "flopx_paper_positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("ticker", sa.String(16), nullable=False, index=True),
            sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
            sa.Column("avg_cost", sa.Numeric(18, 6), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("account_id", "ticker", name="uq_flopx_paper_positions_account_ticker"),
        )

    if not _has_table("flopx_workloads"):
        op.create_table(
            "flopx_workloads",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("spec", JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if not _has_table("flopx_strategy_decisions"):
        op.create_table(
            "flopx_strategy_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("strategy_id", sa.String(64), nullable=False, index=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("t", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("action", sa.String(4), nullable=False),
            sa.Column("ticker", sa.String(16), nullable=True),
            sa.Column("rationale", JSONB, nullable=False),
            sa.Column("known_data_snapshot", JSONB, nullable=False),
            sa.Column("mode", sa.String(8), nullable=False, index=True),
            sa.Column("acted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_flopx_strategy_decisions_strategy_t", "flopx_strategy_decisions", ["strategy_id", "t"])


def downgrade() -> None:
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
