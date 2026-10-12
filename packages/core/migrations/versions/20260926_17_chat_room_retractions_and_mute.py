"""Audited room-message retraction and per-member notification mute.

Revision ID: 20260926_17
Revises: 20260926_16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_17"
down_revision: Union[str, None] = "20260926_16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_room_members", sa.Column("notifications_muted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("chat_room_messages", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("chat_room_messages", sa.Column("deleted_by_user_id", sa.String(length=26)))
    op.create_foreign_key(
        "fk_chat_room_messages_deleted_by_user_id_users",
        "chat_room_messages", "users", ["deleted_by_user_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_chat_room_messages_deleted_by_user_id_users", "chat_room_messages", type_="foreignkey")
    op.drop_column("chat_room_messages", "deleted_by_user_id")
    op.drop_column("chat_room_messages", "deleted_at")
    op.drop_column("chat_room_members", "notifications_muted")
