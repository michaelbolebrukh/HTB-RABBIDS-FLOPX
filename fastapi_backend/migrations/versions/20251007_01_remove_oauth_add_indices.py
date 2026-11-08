"""
Remove deprecated oauth_provider column and add indices to searched columns

Revision ID: 20251007_01
Revises: 20250909_01
Create Date: 2025-10-07
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers, used by Alembic.
revision = "20251007_01"
down_revision = "20250909_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Removing deprecated oauth_provider column from users table (only if it exists):
    conn = op.get_bind()
    result = conn.execute(sa.text(
        """
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='users' AND column_name='oauth_provider'
        """
    ))
    if result.fetchone():
        op.drop_column("users", "oauth_provider")
    
    # Adding indices to frequently searched columns (skip if already exist):
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('users')}
    
    if 'ix_users_is_verified' not in existing_indexes:
        op.create_index(op.f("ix_users_is_verified"), "users", ["is_verified"], unique=False)
    if 'ix_users_marketing_consent' not in existing_indexes:
        op.create_index(op.f("ix_users_marketing_consent"), "users", ["marketing_consent"], unique=False)
    
    existing_contact_indexes = {idx['name'] for idx in inspector.get_indexes('contact_submissions')}
    if 'ix_contact_submissions_resolved' not in existing_contact_indexes:
        op.create_index(op.f("ix_contact_submissions_resolved"), "contact_submissions", ["resolved"], unique=False)
    if 'ix_contact_submissions_created_at' not in existing_contact_indexes:
        op.create_index(op.f("ix_contact_submissions_created_at"), "contact_submissions", ["created_at"], unique=False)
    
    existing_session_indexes = {idx['name'] for idx in inspector.get_indexes('refresh_sessions')}
    if 'ix_refresh_sessions_expires_at' not in existing_session_indexes:
        op.create_index(op.f("ix_refresh_sessions_expires_at"), "refresh_sessions", ["expires_at"], unique=False)
    if 'ix_refresh_sessions_revoked_at' not in existing_session_indexes:
        op.create_index(op.f("ix_refresh_sessions_revoked_at"), "refresh_sessions", ["revoked_at"], unique=False)


def downgrade() -> None:
    # Dropping indices (only if they exist):
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    existing_session_indexes = {idx['name'] for idx in inspector.get_indexes('refresh_sessions')}
    if 'ix_refresh_sessions_revoked_at' in existing_session_indexes:
        op.drop_index(op.f("ix_refresh_sessions_revoked_at"), table_name="refresh_sessions")
    if 'ix_refresh_sessions_expires_at' in existing_session_indexes:
        op.drop_index(op.f("ix_refresh_sessions_expires_at"), table_name="refresh_sessions")
    
    existing_contact_indexes = {idx['name'] for idx in inspector.get_indexes('contact_submissions')}
    if 'ix_contact_submissions_created_at' in existing_contact_indexes:
        op.drop_index(op.f("ix_contact_submissions_created_at"), table_name="contact_submissions")
    if 'ix_contact_submissions_resolved' in existing_contact_indexes:
        op.drop_index(op.f("ix_contact_submissions_resolved"), table_name="contact_submissions")
    
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('users')}
    if 'ix_users_marketing_consent' in existing_indexes:
        op.drop_index(op.f("ix_users_marketing_consent"), table_name="users")
    if 'ix_users_is_verified' in existing_indexes:
        op.drop_index(op.f("ix_users_is_verified"), table_name="users")
    
    # Re-adding oauth_provider column (only if it doesn't exist):
    result = conn.execute(sa.text(
        """
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='users' AND column_name='oauth_provider'
        """
    ))
    if not result.fetchone():
        op.add_column("users", sa.Column("oauth_provider", sa.String(), nullable=True))

