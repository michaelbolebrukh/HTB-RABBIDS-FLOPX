"""
Rename ai_memories.agent_name -> scope (P10/D59: configurable memory namespace).

Memory is no longer keyed by the agent's NAME but by a free `scope` string, so
agents sharing a scope share memory (default = one shared namespace per owner).
The column is renamed to match.

IDEMPOTENCY + PORTABILITY: startup runs `Base.metadata.create_all()` before
Alembic, so a FRESH database already has the `scope` column (from the model) by
migration time — the guard below then no-ops. Only a database that already ran
the original create (with `agent_name`) is actually rewritten. The rename goes
through `batch_alter_table` so it works on both PostgreSQL and SQLite; the scope
index is dropped first and recreated after (batch reflection would otherwise
recreate it against the old column name).

Revision ID: 20260713_12
Revises: 20260713_11
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op


revision = "20260713_12"
down_revision = "20260713_11"
branch_labels = None
depends_on = None

_TABLE = "ai_memories"
_INDEX = "ix_ai_memories_scope_live"


def _columns() -> set[str]:
    insp = sa.inspect(op.get_bind())
    if _TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(_TABLE)}


def _indexes() -> set[str]:
    insp = sa.inspect(op.get_bind())
    if _TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(_TABLE)}


def _rename(old: str, new: str) -> None:
    """Renaming a column of ai_memories, rebuilding the scope index around it:"""
    cols = _columns()
    if old not in cols or new in cols:
        return  # already at the target shape (fresh DB or re-run) — no-op
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(old, new_column_name=new)
    op.create_index(_INDEX, _TABLE, ["owner_id", new, "superseded_at"])


def upgrade() -> None:
    _rename("agent_name", "scope")


def downgrade() -> None:
    _rename("scope", "agent_name")
