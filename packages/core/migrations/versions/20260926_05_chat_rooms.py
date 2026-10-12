"""Add human chat rooms to the public Manor AI schema.

Revision ID: 20260926_05
Revises: 20260926_04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260926_05"
down_revision: Union[str, None] = "20260926_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_rooms",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("owner_entity_id", sa.String(26), sa.ForeignKey("entities.id", ondelete="CASCADE")),
        sa.Column("owner_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("created_by_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("dm_key", sa.String(320)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('dm', 'group')", name="ck_chat_rooms_kind"),
        sa.CheckConstraint("status IN ('active', 'closed')", name="ck_chat_rooms_status"),
        sa.CheckConstraint(
            "(kind = 'dm' AND owner_user_id IS NOT NULL AND owner_entity_id IS NULL) OR "
            "(kind = 'group' AND ((owner_user_id IS NULL) <> (owner_entity_id IS NULL)))",
            name="ck_chat_rooms_owner",
        ),
    )
    op.create_index("ix_chat_rooms_owner_entity", "chat_rooms", ["owner_entity_id"])
    op.create_index("uq_chat_rooms_dm_key", "chat_rooms", ["dm_key"], unique=True)

    op.create_table(
        "chat_room_members",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("visible_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("room_id", "user_id", name="uq_chat_room_members_room_user"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_chat_room_members_role"),
        sa.CheckConstraint("status IN ('active', 'left')", name="ck_chat_room_members_status"),
    )
    op.create_index("ix_chat_room_members_user_status", "chat_room_members", ["user_id", "status"])

    op.create_table(
        "chat_room_invites",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_by_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'revoked')", name="ck_chat_room_invites_status"),
    )
    op.create_index("uq_chat_room_invites_token", "chat_room_invites", ["token_hash"], unique=True)
    op.create_index("ix_chat_room_invites_room_status", "chat_room_invites", ["room_id", "status"])

    op.create_table(
        "chat_room_agent_bindings",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.String(26), sa.ForeignKey("agent_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("room_id", "subscription_id", name="uq_chat_room_agent_binding_subscription"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_chat_room_agent_binding_status"),
    )

    op.create_table(
        "chat_room_messages",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("room_id", sa.String(26), sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_user_id", sa.String(26), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mentioned_user_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_room_messages_room_time", "chat_room_messages", ["room_id", "created_at", "id"])



def downgrade() -> None:
    op.drop_table("chat_room_messages")
    op.drop_table("chat_room_agent_bindings")
    op.drop_table("chat_room_invites")
    op.drop_table("chat_room_members")
    op.drop_table("chat_rooms")
