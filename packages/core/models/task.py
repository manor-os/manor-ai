"""Task, conversation, and message models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class TaskCategory(Base, TimestampMixin):
    __tablename__ = "task_categories"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[Optional[str]] = mapped_column(String(50))
    color: Mapped[Optional[str]] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)


class Task(Base, TimestampMixin):
    """A task ticket."""
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_entity_status", "entity_id", "status"),
        Index("ix_tasks_workspace", "workspace_id"),
        Index("ix_tasks_details", "details", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    category_id: Mapped[Optional[str]] = mapped_column(String(26))

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    task_type: Mapped[str] = mapped_column(String(50), default="general")

    assignee_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_type: Mapped[Optional[str]] = mapped_column(String(50))
    creator_id: Mapped[Optional[str]] = mapped_column(String(26))

    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))

    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Automation fields
    sla_policy_id: Mapped[Optional[str]] = mapped_column(String(26))
    sla_breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    template_id: Mapped[Optional[str]] = mapped_column(String(26))
    vendor_id: Mapped[Optional[str]] = mapped_column(String(26))
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float)
    parent_task_id: Mapped[Optional[str]] = mapped_column(String(26))
    required_skills: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")

    # ── Goal-driven runtime fields ─────────────────────────────────────
    # Owner subscription = the workspace service primarily accountable
    # for this task. owner_service_key is the semantic role
    # ('content_creator'); owner_subscription_id is the resolved
    # subscription at creation time (cached for UI / audit).
    owner_service_key: Mapped[Optional[str]] = mapped_column(String(100))
    owner_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Plan steps may delegate to other subscriptions, but only those
    # whose service_key appears here. Strategist sets this list at
    # task creation; Planner is bound by it.
    delegate_service_keys: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    # Soft I/O contracts (JSON Schema) — Strategist→Planner chain uses
    # these to propagate one task's actual_output into the next task's
    # params. Optional; pure manual tasks leave them null.
    input_contract: Mapped[Optional[dict]] = mapped_column(JSONB)
    expected_output: Mapped[Optional[dict]] = mapped_column(JSONB)
    actual_output: Mapped[Optional[dict]] = mapped_column(JSONB)

    # ── Permission-v1 fields (see docs/PERMISSIONS_DESIGN_ZH.md §7.2) ────
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default="entity")
    owner_id: Mapped[Optional[str]] = mapped_column(String(26))
    client_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class TaskSlaPolicy(Base, TimestampMixin):
    """SLA policy for task response/resolution times."""
    __tablename__ = "task_sla_policies"
    __table_args__ = (
        Index("ix_task_sla_policies_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[Optional[str]] = mapped_column(String(20))
    category_id: Mapped[Optional[str]] = mapped_column(String(26))
    response_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    resolution_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class TaskEscalationRule(Base, TimestampMixin):
    """Escalation rule linked to an SLA policy."""
    __tablename__ = "task_escalation_rules"
    __table_args__ = (
        Index("ix_task_escalation_rules_sla", "sla_policy_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    sla_policy_id: Mapped[str] = mapped_column(String(26), nullable=False)
    escalation_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_user_ids: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    notify_email: Mapped[Optional[str]] = mapped_column(String(500))
    action_type: Mapped[str] = mapped_column(String(20), nullable=False, default="notify")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class TaskChecklist(Base, TimestampMixin):
    """Checklist item for a task."""
    __tablename__ = "task_checklists"
    __table_args__ = (
        Index("ix_task_checklists_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(String(26), nullable=False)
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class TaskLog(Base):
    __tablename__ = "task_logs"
    __table_args__ = (
        Index("ix_task_logs_task", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(String(26), nullable=False)
    log_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(String)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Conversation(Base, TimestampMixin):
    """Chat container — three flavours via ``scope``:

    * ``channel``           — the legacy 1:1 user↔agent thread for an
                              external messaging channel (Telegram bot,
                              email, etc.). What ``Channel`` rows
                              originally fed into.
    * ``workspace_main``    — the primary group chat for a workspace.
                              All subscriptions (agents) participate
                              alongside the user. One per workspace by
                              default; created lazily on first message.
    * ``workspace_thread``  — per-task or per-plan child thread spawned
                              from the workspace_main when execution
                              produces a lot of chatter. ``thread_ref_*``
                              identifies what the thread is about.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_entity", "entity_id"),
        Index("ix_conversations_workspace_scope", "workspace_id", "scope"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    title: Mapped[Optional[str]] = mapped_column(String(500))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(50), default="web")
    status: Mapped[str] = mapped_column(String(20), default="active")
    meta: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")

    # ── Workspace chat ─────────────────────────────────────────────────
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="channel", server_default="channel"
    )
    thread_ref_kind: Mapped[Optional[str]] = mapped_column(String(16))
    # 'task' | 'plan' | 'goal' — what this thread is about
    thread_ref_id: Mapped[Optional[str]] = mapped_column(String(26))


class Message(Base):
    """Conversation message.

    The ``role`` field is the LLM-protocol role
    ('user' | 'assistant' | 'system' | 'tool') — kept for backward
    compatibility with everything that already builds chat history
    from this table.

    The ``author_*`` + ``message_kind`` fields are the workspace-chat
    overlay: who sent this (user / which agent subscription / system),
    what kind of message it is (free text / agent update / proposal
    card / HITL prompt / step receipt), plus interactive primitives
    (``pending_action`` for buttons; ``resolved_*`` records the user's
    response).
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(String)
    tool_calls: Mapped[Optional[dict]] = mapped_column(JSONB)
    attachments: Mapped[Optional[dict]] = mapped_column(JSONB)
    token_usage: Mapped[Optional[dict]] = mapped_column(JSONB)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Workspace chat overlay ─────────────────────────────────────────
    author_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    # 'user' | 'agent' | 'system'
    author_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    # The workspace subscription that "spoke" this message — null when
    # author_kind='user' or 'system'.

    message_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text", server_default="text"
    )
    # 'text' | 'agent_update' | 'proposal' | 'hitl_request'
    # | 'step_event' | 'goal_alert' | 'system'

    refs: Mapped[Optional[list]] = mapped_column(JSONB)
    # [{"type": "task", "id": "..."}, {"type": "step", "id": "..."}]
    # Backlinks for clickable mentions in the chat UI.

    pending_action: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    # When set, the message is interactive — UI renders buttons. Shape:
    # {"kind": "approve_plan", "plan_id": "...",
    #  "options": ["approve", "reject", "modify"]}.
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    resolution: Mapped[Optional[dict]] = mapped_column(JSONB)
    # {"choice": "approve", "note": "..."}
