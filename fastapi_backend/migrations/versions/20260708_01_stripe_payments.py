"""
Stripe-native payments: purchases mirror, webhook idempotency, user billing
identity + entitlement cache. Retires the pre-Stripe product_pricing catalog
(catalog now lives in code and reconciles into Stripe).

Revision ID: 20260708_01
Revises: 20260705_01
Create Date: 2026-07-08

Rebased onto main: originally chained off 20260610_01 as 20260705_01, but main
added its own 20260705_01 (transcript_item_seq). Re-pointed to sit after main's
head so history stays linear with a single head. Idempotent, so re-running over a
DB that already has these tables is a no-op.
"""

from alembic import op
import sqlalchemy as sa

from infrastructure.db_types import JSONColumn


revision = "20260708_01"
down_revision = "20260705_01"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(col["name"] == column for col in _inspector().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in _inspector().get_indexes(table))


def upgrade() -> None:
    # Users: Stripe customer link + entitlement cache:
    if not _has_column("users", "stripe_customer_id"):
        op.add_column("users", sa.Column("stripe_customer_id", sa.String(), nullable=True))
    if not _has_index("users", "ix_users_stripe_customer_id"):
        op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"], unique=True)
    if not _has_column("users", "entitlements"):
        op.add_column("users", sa.Column("entitlements", JSONColumn, nullable=True))

    # Purchases mirror (written by webhooks; Stripe remains source of truth):
    if not _has_table("purchases"):
        op.create_table(
            "purchases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("item_key", sa.String(), nullable=False),
            sa.Column("item_type", sa.String(), nullable=False),
            sa.Column("stripe_checkout_session_id", sa.String(), nullable=False),
            sa.Column("stripe_payment_intent_id", sa.String(), nullable=True),
            sa.Column("stripe_subscription_id", sa.String(), nullable=True),
            sa.Column("amount_total_minor", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("shipping", JSONColumn, nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_purchases_user_id", "purchases", ["user_id"])
        op.create_index("ix_purchases_item_key", "purchases", ["item_key"])
        op.create_index("ix_purchases_stripe_checkout_session_id", "purchases", ["stripe_checkout_session_id"], unique=True)
        op.create_index("ix_purchases_stripe_payment_intent_id", "purchases", ["stripe_payment_intent_id"])
        op.create_index("ix_purchases_stripe_subscription_id", "purchases", ["stripe_subscription_id"])
        op.create_index("ix_purchases_status", "purchases", ["status"])

    # Processed webhook events for idempotent handling:
    if not _has_table("stripe_events"):
        op.create_table(
            "stripe_events",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # Retiring the pre-Stripe catalog table:
    if _has_table("product_pricing"):
        op.drop_table("product_pricing")


def downgrade() -> None:
    if _has_table("stripe_events"):
        op.drop_table("stripe_events")
    if _has_table("purchases"):
        op.drop_table("purchases")

    if _has_index("users", "ix_users_stripe_customer_id"):
        op.drop_index("ix_users_stripe_customer_id", table_name="users")
    if _has_column("users", "stripe_customer_id"):
        op.drop_column("users", "stripe_customer_id")
    if _has_column("users", "entitlements"):
        op.drop_column("users", "entitlements")

    # Restoring the retired pre-Stripe catalog table:
    if not _has_table("product_pricing"):
        op.create_table(
            "product_pricing",
            sa.Column("slug", sa.String(), primary_key=True),
            sa.Column("type", sa.String(), nullable=False, server_default="event"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="GBP"),
            sa.Column("original_price_minor", sa.Integer(), nullable=False),
            sa.Column("current_price_minor", sa.Integer(), nullable=False),
            sa.Column("per_user_limit", sa.Integer(), nullable=True),
            sa.Column("event_start_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("purchase_valid_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("purchase_valid_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
