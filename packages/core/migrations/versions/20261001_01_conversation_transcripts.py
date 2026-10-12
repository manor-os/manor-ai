"""Append-only model transcripts for conversations.

Revision ID: 20261001_01
Revises: 20260929_01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261001_01"
down_revision = "20260929_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_transcript_items",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column(
            "conversation_id", sa.String(26),
            sa.ForeignKey("conversations.id", ondelete="CASCADE", name="fk_conversation_transcript_conversation"),
            nullable=False,
        ),
        sa.Column("scope_key", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=True),
        sa.Column("agent_id", sa.String(26), nullable=True),
        sa.Column("workspace_id", sa.String(26), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(26), nullable=True),
        sa.Column("source_message_id", sa.String(26), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("conversation_id", "scope_key", "seq", name="uq_conversation_transcript_seq"),
    )


def downgrade() -> None:
    op.drop_table("conversation_transcript_items")
