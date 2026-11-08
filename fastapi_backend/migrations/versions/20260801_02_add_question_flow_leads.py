"""
Question flow leads: the DORMANT pre-account answer store.

Separate from 20260801_01 deliberately. The response/reminder tables are the
feature; this one is an optional module that stays empty unless a flow sets
lead_capture=True, and it carries a retention obligation the other two do not.
Keeping it in its own revision means an instance that never opts in can see
exactly what it is (and could drop it) without touching the feature's tables.

Address-keyed, not user-keyed, because the whole point is that no account exists
yet. Lawful basis Art 6(1)(b) — steps at the data subject's request prior to a
contract — NOT marketing consent; this table is never a marketing list.
`answers` is NULLABLE and deliberately unvalidated (a lead is mid-flow by
definition), so nothing may read it without re-validating. The unique
(email, flow_key) pair is the upsert key: one row per address per flow, which is
what makes both the retention purge and the conversion delete a single,
obviously-complete statement. `converted_user_id` is audit only and SET NULL on
user delete — the normal conversion path removes the row rather than marking it.

IDEMPOTENCY (migrations rule): inspector-guarded in both directions.

Revision ID: 20260801_02
Revises: 20260801_01
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn


revision = "20260801_02"
down_revision = "20260801_01"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _has_table("question_flow_leads"):
        op.create_table(
            "question_flow_leads",
            sa.Column("id", sa.Integer(), primary_key=True),
            # Stored already normalized (trimmed + lowercased), so lookups are exact:
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("flow_key", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("answers", JSONColumn, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("converted_user_id", sa.Integer(), nullable=True),
            sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["converted_user_id"], ["users.id"],
                name="fk_question_flow_leads_converted_user_id", ondelete="SET NULL",
            ),
            sa.UniqueConstraint("email", "flow_key", name="uq_question_flow_leads_email_flow"),
        )

    if not _has_index("question_flow_leads", "ix_question_flow_leads_email"):
        op.create_index("ix_question_flow_leads_email", "question_flow_leads", ["email"])

    # created_at is indexed for the retention purge, which is the only scan that
    # ever walks this table without an address:
    if not _has_index("question_flow_leads", "ix_question_flow_leads_created_at"):
        op.create_index("ix_question_flow_leads_created_at", "question_flow_leads", ["created_at"])


def downgrade() -> None:
    if _has_table("question_flow_leads"):
        if _has_index("question_flow_leads", "ix_question_flow_leads_created_at"):
            op.drop_index("ix_question_flow_leads_created_at", table_name="question_flow_leads")
        if _has_index("question_flow_leads", "ix_question_flow_leads_email"):
            op.drop_index("ix_question_flow_leads_email", table_name="question_flow_leads")
        op.drop_table("question_flow_leads")
