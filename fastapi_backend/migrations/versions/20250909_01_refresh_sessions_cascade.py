"""
Add ON DELETE CASCADE on refresh_sessions.user_id

Revision ID: 20250909_01
Revises: 
Create Date: 2025-09-09
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers, used by Alembic.
revision = "20250909_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Checking if refresh_sessions table exists first:
    conn = op.get_bind()
    table_check = conn.execute(sa.text(
        """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'refresh_sessions'
        )
        """
    ))
    if not table_check.scalar():
        return  # Table doesn't exist yet, skip this migration
    
    # Checking if FK already has CASCADE (no work needed):
    cascade_check = conn.execute(sa.text(
        """
        SELECT confdeltype
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        WHERE r.relname = 'refresh_sessions'
          AND c.contype = 'f'
        LIMIT 1
        """
    ))
    result = cascade_check.fetchone()
    if result and result[0] == 'c':  # 'c' means CASCADE
        return  # Already has CASCADE, nothing to do
    
    # Discovering existing FK name dynamically to be robust across environments:
    fk_result = conn.execute(sa.text(
        """
        SELECT conname
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = r.relnamespace
        WHERE r.relname = 'refresh_sessions'
          AND c.contype = 'f'
        """
    ))
    fk_names = [row[0] for row in fk_result]
    for fk_name in fk_names:
        op.drop_constraint(fk_name, "refresh_sessions", type_="foreignkey")

    # Recreate FK with ON DELETE CASCADE
    op.create_foreign_key(
        constraint_name=None,
        source_table="refresh_sessions",
        referent_table="users",
        local_cols=["user_id"],
        remote_cols=["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Checking if refresh_sessions table exists first:
    conn = op.get_bind()
    table_check = conn.execute(sa.text(
        """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'refresh_sessions'
        )
        """
    ))
    if not table_check.scalar():
        return  # Table doesn't exist, skip this migration
    
    # Checking if FK exists and what its delete action is:
    cascade_check = conn.execute(sa.text(
        """
        SELECT confdeltype
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        WHERE r.relname = 'refresh_sessions'
          AND c.contype = 'f'
        LIMIT 1
        """
    ))
    result = cascade_check.fetchone()
    if result and result[0] != 'c':  # Not CASCADE
        return  # Already doesn't have CASCADE, nothing to do
    
    # Dropping current FK and recreate without cascade:
    fk_result = conn.execute(sa.text(
        """
        SELECT conname
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = r.relnamespace
        WHERE r.relname = 'refresh_sessions'
          AND c.contype = 'f'
        """
    ))
    fk_names = [row[0] for row in fk_result]
    for fk_name in fk_names:
        op.drop_constraint(fk_name, "refresh_sessions", type_="foreignkey")

    op.create_foreign_key(
        constraint_name=None,
        source_table="refresh_sessions",
        referent_table="users",
        local_cols=["user_id"],
        remote_cols=["id"],
        ondelete=None,
    )


