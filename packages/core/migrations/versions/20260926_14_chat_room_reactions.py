"""Add per-user emoji reactions to room messages.

Revision ID: 20260926_14
Revises: 20260926_13
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_14"
down_revision: Union[str, None] = "20260926_13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_room_message_reactions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("message_id", sa.String(26), sa.ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("emoji", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "user_id", "emoji", name="uq_chat_room_message_reaction"),
    )
    op.create_index("ix_chat_room_message_reactions_message", "chat_room_message_reactions", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_room_message_reactions_message", table_name="chat_room_message_reactions")
    op.drop_table("chat_room_message_reactions")
