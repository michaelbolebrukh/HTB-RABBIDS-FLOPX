"""
Question flows: completed answer snapshots + reminder sent-records.

Two tables, both PURELY ADDITIVE — nothing existing changes shape or meaning, so
there is no backfill and a downgrade loses only what these tables hold.

`question_flow_responses` is APPEND-ONLY by contract: application code never
updates or deletes a row, so the table is the history of what each account
answered and when. `flow_key` is a plain VARCHAR rather than an FK or an enum
because flow definitions live in code (platform/question_flows.py) — a retired
flow's snapshots must outlive its registration. The single composite index
matches the only read shape (latest snapshot per account+flow), descending on
completed_at so latest-wins is an index step rather than a sort.

`question_flow_reminders` is the at-most-once mechanism for the reminder sweep,
not an audit log: the sweep inserts and commits the row BEFORE it sends, and the
unique constraint on (user_id, flow_key, stage) is what makes a concurrent
worker or a retried tick unable to produce a second email.

IDEMPOTENCY (migrations rule): every step is inspector-guarded, so replaying
over a partially- or fully-migrated database is a no-op. Portable (Alembic ops +
the JSONColumn variant from infrastructure/db_types, no PostgreSQL-only SQL).

Revision ID: 20260801_01
Revises: 20260725_04
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from alembic import op

from infrastructure.db_types import JSONColumn


revision = "20260801_01"
down_revision = "20260725_04"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _has_table("question_flow_responses"):
        op.create_table(
            "question_flow_responses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("flow_key", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("answers", JSONColumn, nullable=False),
            # Server-stamped at submit; the staleness fact every consumer reads:
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"],
                name="fk_question_flow_responses_user_id", ondelete="CASCADE",
            ),
        )

    if not _has_index("question_flow_responses", "ix_question_flow_responses_user_flow_completed"):
        op.create_index(
            "ix_question_flow_responses_user_flow_completed",
            "question_flow_responses",
            ["user_id", "flow_key", sa.text("completed_at DESC")],
        )

    if not _has_table("question_flow_reminders"):
        op.create_table(
            "question_flow_reminders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("flow_key", sa.String(length=64), nullable=False),
            # Ordinal into the flow's registered reminder_stages tuple:
            sa.Column("stage", sa.Integer(), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"],
                name="fk_question_flow_reminders_user_id", ondelete="CASCADE",
            ),
            # The hard at-most-once backstop the claim-first sweep relies on:
            sa.UniqueConstraint(
                "user_id", "flow_key", "stage",
                name="uq_question_flow_reminders_user_flow_stage",
            ),
        )

    if not _has_index("question_flow_reminders", "ix_question_flow_reminders_user_id"):
        op.create_index("ix_question_flow_reminders_user_id", "question_flow_reminders", ["user_id"])


def downgrade() -> None:
    if _has_table("question_flow_reminders"):
        if _has_index("question_flow_reminders", "ix_question_flow_reminders_user_id"):
            op.drop_index("ix_question_flow_reminders_user_id", table_name="question_flow_reminders")
        op.drop_table("question_flow_reminders")

    if _has_table("question_flow_responses"):
        if _has_index("question_flow_responses", "ix_question_flow_responses_user_flow_completed"):
            op.drop_index(
                "ix_question_flow_responses_user_flow_completed",
                table_name="question_flow_responses",
            )
        op.drop_table("question_flow_responses")
