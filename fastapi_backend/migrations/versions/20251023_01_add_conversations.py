"""
Add conversation tables for generative AI

Revision ID: 20251023_01
Revises: 20251007_01
Create Date: 2025-10-23
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers, used by Alembic.
revision = "20251023_01"
down_revision = "20251007_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Creating conversations table:
    op.execute("CREATE TABLE IF NOT EXISTS conversations (id SERIAL NOT NULL, user_id INTEGER NOT NULL, title VARCHAR, created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversations_user_id ON conversations (user_id)")
    
    # Creating conversation_nodes table:
    op.execute("CREATE TABLE IF NOT EXISTS conversation_nodes (id SERIAL NOT NULL, conversation_id INTEGER NOT NULL, parent_id INTEGER, role VARCHAR NOT NULL, summary TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id), FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE, FOREIGN KEY(parent_id) REFERENCES conversation_nodes (id) ON DELETE CASCADE)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversation_nodes_conversation_id ON conversation_nodes (conversation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversation_nodes_parent_id ON conversation_nodes (parent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversation_nodes_role ON conversation_nodes (role)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversation_nodes_created_at ON conversation_nodes (created_at)")
    
    # Creating message_contents table:
    op.execute("CREATE TABLE IF NOT EXISTS message_contents (id SERIAL NOT NULL, node_id INTEGER NOT NULL, position INTEGER NOT NULL, content_type VARCHAR NOT NULL, text_content TEXT, image_url VARCHAR, image_width INTEGER, image_height INTEGER, audio_data TEXT, audio_format VARCHAR, audio_transcript TEXT, tool_call_id VARCHAR, tool_name VARCHAR, tool_arguments TEXT, file_id VARCHAR, file_data TEXT, filename VARCHAR, message_name VARCHAR, created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id), FOREIGN KEY(node_id) REFERENCES conversation_nodes (id) ON DELETE CASCADE)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_message_contents_node_id ON message_contents (node_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_message_contents_content_type ON message_contents (content_type)")


def downgrade() -> None:
    # Dropping tables in reverse order:
    op.drop_index(op.f("ix_message_contents_content_type"), table_name="message_contents")
    op.drop_index(op.f("ix_message_contents_node_id"), table_name="message_contents")
    op.drop_table("message_contents")
    
    op.drop_index(op.f("ix_conversation_nodes_created_at"), table_name="conversation_nodes")
    op.drop_index(op.f("ix_conversation_nodes_role"), table_name="conversation_nodes")
    op.drop_index(op.f("ix_conversation_nodes_parent_id"), table_name="conversation_nodes")
    op.drop_index(op.f("ix_conversation_nodes_conversation_id"), table_name="conversation_nodes")
    op.drop_table("conversation_nodes")
    
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_table("conversations")


