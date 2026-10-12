"""Store group-chat attachments outside Workspace Knowledge in public Manor AI.

Revision ID: 20260926_07
Revises: 20260926_06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_07"
down_revision: Union[str, None] = "20260926_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_room_files",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("message_id", sa.String(26), sa.ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("size > 0 AND size <= 10485760", name="ck_chat_room_files_size"),
    )
    op.create_index("ix_chat_room_files_message", "chat_room_files", ["message_id"])


def downgrade() -> None:
    op.drop_table("chat_room_files")
