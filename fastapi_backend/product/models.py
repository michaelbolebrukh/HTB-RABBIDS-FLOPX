"""FLOPX product tables.

Only what the architecture needs now (§6 paper broker, §7 archived
observations, strategies, saved workloads). Reference data (GPU models,
providers, regions, offers, facilities, companies, markets, feed) is file-backed
sample data in product/flopx/data/sample and is not persisted; live adapters
append observations to `flopx_observations` so history accumulates across
deploys.

Money is integer minor units (USD cents); quantities and prices are Numeric so
the ledger arithmetic is exact. Enums are stored as their `.value` strings.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import expression, func

from infrastructure.database import Base
from infrastructure.db_types import JSONColumn, Uuid
from product.enums import DecisionAction, OrderSide, OrderStatus, OrderType, StrategyMode


def _enum(kind):
    return Enum(kind, values_callable=lambda e: [i.value for i in e], native_enum=False)


class Observation(Base):
    """One archived observation of a series (§7): live adapters append here."""

    __tablename__ = "flopx_observations"
    __table_args__ = (
        UniqueConstraint("series_id", "t", name="uq_flopx_observations_series_t"),
        Index("ix_flopx_observations_series_t", "series_id", "t"),
    )

    id = Column(Integer, primary_key=True)
    series_id = Column(String(128), nullable=False, index=True)
    t = Column(DateTime(timezone=True), nullable=False, index=True)
    value = Column(Float, nullable=False)
    unit = Column(String(32), nullable=False)
    provenance = Column(JSONColumn, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaperAccount(Base):
    """One simulated account per user (§6): cash in USD cents, no margin."""

    __tablename__ = "flopx_paper_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    cash_minor = Column(BigInteger, nullable=False)
    # Running realised P&L and the equity high-water mark, so P&L and max
    # drawdown are ledger facts rather than recomputed from every fill:
    realised_pnl_minor = Column(BigInteger, nullable=False, default=0, server_default="0")
    peak_equity_minor = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaperOrder(Base):
    __tablename__ = "flopx_paper_orders"
    __table_args__ = (
        Index("ix_flopx_paper_orders_account_created", "account_id", "created_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(Integer, ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String(16), nullable=False, index=True)
    side = Column(_enum(OrderSide), nullable=False)
    order_type = Column(_enum(OrderType), nullable=False)
    quantity = Column(Numeric(18, 6), nullable=False)
    notional_minor = Column(BigInteger, nullable=True)
    limit_price = Column(Numeric(18, 6), nullable=True)
    status = Column(_enum(OrderStatus), nullable=False, index=True)
    # "manual" | "strategy:<id>":
    origin = Column(String(64), nullable=False, default="manual", server_default="manual")
    # Rejection reason token (insufficient-cash, unknown-instrument, ...):
    reason = Column(String(64), nullable=True)
    fill_price = Column(Numeric(18, 6), nullable=True)
    fill_provenance = Column(JSONColumn, nullable=True)
    filled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaperPosition(Base):
    __tablename__ = "flopx_paper_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "ticker", name="uq_flopx_paper_positions_account_ticker"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String(16), nullable=False, index=True)
    quantity = Column(Numeric(18, 6), nullable=False)
    avg_cost = Column(Numeric(18, 6), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SavedWorkload(Base):
    """A procurement job a signed-in user saved (§3 Workload as JSON)."""

    __tablename__ = "flopx_workloads"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    spec = Column(JSONColumn, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StrategyDecision(Base):
    """A strategy's decision with the data it knew at decision time (§6)."""

    __tablename__ = "flopx_strategy_decisions"
    __table_args__ = (
        Index("ix_flopx_strategy_decisions_strategy_t", "strategy_id", "t"),
    )

    id = Column(Integer, primary_key=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    # The account the forward step acted for (null for a backtest row):
    account_id = Column(Integer, ForeignKey("flopx_paper_accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    t = Column(DateTime(timezone=True), nullable=False, index=True)
    action = Column(_enum(DecisionAction), nullable=False)
    ticker = Column(String(16), nullable=True)
    rationale = Column(JSONColumn, nullable=False)
    known_data_snapshot = Column(JSONColumn, nullable=False)
    mode = Column(_enum(StrategyMode), nullable=False, index=True)
    acted = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # The decision ledger (architecture §10.7): a step PROPOSES, an explicit accept SUBMITS, the broker
    # answers filled or rejected; a decision with nothing to do is skipped with a reason. Nothing is
    # shown as executed before a fill. Status values: proposed | submitted | filled | rejected | skipped.
    status = Column(String(16), nullable=False, default="proposed", server_default="proposed", index=True)
    # The signal the decision acted on (`signal:<strategy>:<ticker>:<date>`), its kind, the orders it placed:
    signal_id = Column(String(128), nullable=True, index=True)
    signal_kind = Column(String(32), nullable=True)
    order_ids = Column(JSONColumn, nullable=True)
    skipped_reason = Column(String(64), nullable=True)
    horizon_days = Column(Integer, nullable=True)
    proposed_weight = Column(Float, nullable=True)
