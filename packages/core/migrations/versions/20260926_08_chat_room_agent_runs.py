"""Persist group Agent invocations and attributed replies in public Manor AI.

Revision ID: 20260926_08
Revises: 20260926_07
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_08"
down_revision: Union[str, None] = "20260926_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_room_messages", sa.Column("sender_agent_binding_id", sa.String(26)))
    op.add_column("chat_room_messages", sa.Column("sender_agent_name", sa.String(255)))
    op.add_column("chat_room_messages", sa.Column("sender_agent_avatar_url", sa.Text()))
    op.add_column("chat_room_messages", sa.Column(
        "reply_to_message_id", sa.String(26),
        sa.ForeignKey("chat_room_messages.id", ondelete="SET NULL"),
    ))
    op.add_column("chat_room_messages", sa.Column("agent_run_status", sa.String(16)))
    op.create_check_constraint(
        "ck_chat_room_messages_agent_sender", "chat_room_messages",
        "sender_agent_binding_id IS NULL OR (sender_user_id IS NULL AND sender_agent_name IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_chat_room_messages_agent_run_status", "chat_room_messages",
        "agent_run_status IS NULL OR (sender_agent_binding_id IS NOT NULL AND "
        "agent_run_status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted'))",
    )

    op.create_table(
        "chat_room_agent_runs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_message_id", sa.String(26), sa.ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("binding_id", sa.String(26), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("agent_avatar_url", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("audience_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_message_id", sa.String(26), sa.ForeignKey("chat_room_messages.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_message_id", "binding_id", name="uq_chat_room_agent_runs_source_binding"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')",
            name="ck_chat_room_agent_runs_status",
        ),
    )
    op.create_index("ix_chat_room_agent_runs_room_status", "chat_room_agent_runs", ["room_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_room_agent_runs_room_status", table_name="chat_room_agent_runs")
    op.drop_table("chat_room_agent_runs")
    op.drop_constraint("ck_chat_room_messages_agent_run_status", "chat_room_messages", type_="check")
    op.drop_constraint("ck_chat_room_messages_agent_sender", "chat_room_messages", type_="check")
    op.drop_column("chat_room_messages", "agent_run_status")
    op.drop_column("chat_room_messages", "reply_to_message_id")
    op.drop_column("chat_room_messages", "sender_agent_avatar_url")
    op.drop_column("chat_room_messages", "sender_agent_name")
    op.drop_column("chat_room_messages", "sender_agent_binding_id")
