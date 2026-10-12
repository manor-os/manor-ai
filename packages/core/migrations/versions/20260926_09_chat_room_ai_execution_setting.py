"""Persist an owner-controlled group AI execution capability ceiling in public Manor AI.

Revision ID: 20260926_09
Revises: 20260926_08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_09"
down_revision: Union[str, None] = "20260926_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_rooms",
        sa.Column("ai_tool_execution_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "chat_rooms",
        sa.Column("ai_tool_execution_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_chat_rooms_ai_tool_execution_revision", "chat_rooms", "ai_tool_execution_revision >= 0",
    )
    op.create_check_constraint(
        "ck_chat_rooms_dm_ai_tool_execution_disabled", "chat_rooms",
        "kind = 'group' OR ai_tool_execution_enabled = false",
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_rooms_dm_ai_tool_execution_disabled", "chat_rooms", type_="check")
    op.drop_constraint("ck_chat_rooms_ai_tool_execution_revision", "chat_rooms", type_="check")
    op.drop_column("chat_rooms", "ai_tool_execution_revision")
    op.drop_column("chat_rooms", "ai_tool_execution_enabled")
