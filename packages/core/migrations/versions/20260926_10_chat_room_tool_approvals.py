"""Persist exact room Agent tool approvals and resumable checkpoints.

Revision ID: 20260926_10
Revises: 20260926_09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "20260926_10"
down_revision: Union[str, None] = "20260926_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_room_agent_runs", sa.Column("ai_tool_execution_revision", sa.Integer()))
    op.add_column("chat_room_agent_runs", sa.Column("checkpoint", JSONB()))
    op.drop_constraint("ck_chat_room_agent_runs_status", "chat_room_agent_runs", type_="check")
    op.create_check_constraint(
        "ck_chat_room_agent_runs_status", "chat_room_agent_runs",
        "status IN ('pending', 'running', 'waiting_approval', 'succeeded', 'failed', 'interrupted')",
    )
    op.create_table(
        "chat_room_tool_approvals",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(26), sa.ForeignKey("chat_room_agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requester_user_id", sa.String(26), nullable=False),
        sa.Column("binding_id", sa.String(26), nullable=False),
        sa.Column("audience_fingerprint", sa.String(64), nullable=False),
        sa.Column("ai_tool_execution_revision", sa.Integer(), nullable=False),
        sa.Column("tool_call_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("display_summary", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by_user_id", sa.String(26)),
        sa.Column("result_document_id", sa.String(26)),
        sa.Column("result_media_job_id", sa.String(26)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_chat_room_tool_approval_call"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'claimed', 'succeeded', 'failed', 'invalidated')",
            name="ck_chat_room_tool_approvals_status",
        ),
        sa.CheckConstraint("ai_tool_execution_revision >= 0", name="ck_chat_room_tool_approvals_revision"),
    )
    op.create_index(
        "ix_chat_room_tool_approvals_room_status", "chat_room_tool_approvals",
        ["room_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_room_tool_approvals_room_status", table_name="chat_room_tool_approvals")
    op.drop_table("chat_room_tool_approvals")
    op.drop_constraint("ck_chat_room_agent_runs_status", "chat_room_agent_runs", type_="check")
    op.create_check_constraint(
        "ck_chat_room_agent_runs_status", "chat_room_agent_runs",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')",
    )
    op.drop_column("chat_room_agent_runs", "checkpoint")
    op.drop_column("chat_room_agent_runs", "ai_tool_execution_revision")
