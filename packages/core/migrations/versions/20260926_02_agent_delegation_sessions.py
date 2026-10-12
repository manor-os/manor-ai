"""Persist Workspace service Agent transcripts across delegated turns.

Revision ID: 20260926_02
Revises: 20260926_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260926_02"
down_revision = "20260926_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_delegation_sessions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=False),
        sa.Column("conversation_id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("agent_subscription_id", sa.String(26), nullable=False),
        sa.Column("agent_id", sa.String(26), nullable=False),
        sa.Column("service_key", sa.String(100)),
        sa.Column("messages", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("context_usage", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("context_state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("turn_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("active_run_id", sa.String(26)),
        sa.Column("active_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "conversation_id", "user_id", "agent_subscription_id",
            name="uq_agent_delegation_session_scope",
        ),
    )
    op.create_index(
        "ix_agent_delegation_sessions_workspace",
        "agent_delegation_sessions", ["workspace_id"],
    )
    op.create_table(
        "agent_context_archives",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("session_id", sa.String(26), sa.ForeignKey("agent_delegation_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("messages", JSONB, nullable=False),
        sa.Column("categories", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "digest", name="uq_agent_context_archive_digest"),
    )


def downgrade() -> None:
    op.drop_table("agent_context_archives")
    op.drop_index("ix_agent_delegation_sessions_workspace", table_name="agent_delegation_sessions")
    op.drop_table("agent_delegation_sessions")
