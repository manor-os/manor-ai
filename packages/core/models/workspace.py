"""Workspace (operation) and agent models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, generate_ulid


class Workspace(Base, TimestampMixin, SoftDeleteMixin):
    """A workspace (operation) within an entity."""
    __tablename__ = "workspaces"
    __table_args__ = (
        Index("ix_workspaces_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    address: Mapped[Optional[str]] = mapped_column(String)
    kind: Mapped[Optional[str]] = mapped_column(String(100))  # property, project, campaign, etc.
    operating_context: Mapped[Optional[str]] = mapped_column(String)  # location/context description
    primary_work: Mapped[Optional[str]] = mapped_column(String)  # core responsibilities
    operating_model: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # services, goals, rules, automations, evaluation
    operation_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")

    # ── Extended fields ──────────────────────────────────────────────────────
    longitude: Mapped[Optional[float]] = mapped_column(Numeric(10, 7))
    latitude: Mapped[Optional[float]] = mapped_column(Numeric(10, 7))
    cover_image_url: Mapped[Optional[str]] = mapped_column(String(500))
    attribute_tags: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    identity_label: Mapped[Optional[str]] = mapped_column(String(255))
    property_type: Mapped[Optional[str]] = mapped_column(String(50))
    occupancy_status: Mapped[Optional[str]] = mapped_column(String(50))
    pms_property_id: Mapped[Optional[str]] = mapped_column(String(100))
    pms_unit_id: Mapped[Optional[str]] = mapped_column(String(100))
    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_cadence: Mapped[Optional[str]] = mapped_column(String(50))
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Budget tracking (M8) ──────────────────────────────────────────
    # Workspace-level cost cap. Null = no cap. Enforced in
    # Dispatcher.checkout_steps_for_worker — when monthly_spent_usd
    # crosses budget, new leases for this workspace are refused (and
    # if auto_pause_on_budget=True, the workspace is paused until
    # the operator raises the cap).
    monthly_budget_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    monthly_spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default="0",
    )
    budget_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    auto_pause_on_budget: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    budget_alert_state: Mapped[Optional[str]] = mapped_column(String(20))
    # 'normal' | 'warning_80' | 'critical_100' — dedupes chat alerts


class WorkspaceStaff(Base, TimestampMixin):
    """Workspace membership.

    Originally a loose staff↔workspace assignment table; permissions-v1
    promotes it to the canonical workspace membership record. ``user_id``
    is the new direct identity link (preferred for new code); ``staff_id``
    stays for backwards compatibility. ``role`` becomes the workspace-level
    role (owner / editor / contributor / viewer / external_client) — see
    docs/PERMISSIONS_DESIGN_ZH.md §5.
    """
    __tablename__ = "workspace_staff"
    __table_args__ = (
        Index("ix_workspace_staff_workspace_user", "workspace_id", "user_id"),
        Index("ix_workspace_staff_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    staff_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    role: Mapped[Optional[str]] = mapped_column(String(50))
    added_by: Mapped[Optional[str]] = mapped_column(String(26))
    added_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")


class Agent(Base, TimestampMixin, SoftDeleteMixin):
    """An AI agent (template or entity-owned)."""
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_entity", "entity_id"),
        Index("ix_agents_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))  # NULL = platform template
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(String)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500))
    system_prompt: Mapped[Optional[str]] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    tags: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    source: Mapped[str] = mapped_column(String(20), default="custom")
    status: Mapped[str] = mapped_column(String(20), default="active")
    # Author-managed version label. Mirrors Skill.version so blueprints
    # can reference agents by (slug, min_version) the same way they
    # reference skills. Convention: semver-ish strings ("1.0", "2.1.3");
    # comparison is string-lexicographic — that's fine for the common
    # case where authors stick to N.N.N. Bump on breaking changes to
    # system_prompt / tools / config that downstream blueprints depend
    # on. There is no agent_versions table — to ship a v2, create a new
    # Agent row with a new slug (e.g. ``x-poster-v2``) OR bump the
    # version field and update consumers.
    version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="1.0",
    )


class AgentSubscription(Base, TimestampMixin):
    """Deployment of an agent into a specific (entity, workspace) pair.

    One ``Agent`` row is the brain (system prompt, avatar, base config);
    each ``AgentSubscription`` is a place that agent is actually running,
    with its own optional overrides — custom system prompt, tool config,
    and eventually memory scope. Channel bindings and conversations
    reference *this* row, not the ``Agent`` directly, so the same agent
    can serve multiple workspaces without cross-talk.
    """
    __tablename__ = "agent_subscriptions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    name: Mapped[Optional[str]] = mapped_column(String(255))  # friendly label shown in pickers
    service_key: Mapped[Optional[str]] = mapped_column(String(100))  # which workspace service this agent is bound to
    custom_prompt: Mapped[Optional[str]] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")


class ToolDefinition(Base, TimestampMixin):
    """Global tool catalog."""
    __tablename__ = "tool_definitions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(String)
    category: Mapped[Optional[str]] = mapped_column(String(50))
    schema: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="active")


class AgentToolBinding(Base):
    """Which tools each agent can use."""
    __tablename__ = "agent_tool_bindings"

    agent_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(26), primary_key=True)


class WorkspaceOperationDraft(Base, TimestampMixin):
    """Drafted mutation to a workspace's operating runtime.

    This is the staging area between natural-language workspace chat/API
    requests and durable runtime changes. Callers patch ``current_state`` and
    only apply after validation plus explicit user confirmation.
    """
    __tablename__ = "workspace_operation_drafts"
    __table_args__ = (
        Index("ix_ws_operation_drafts_workspace_status", "workspace_id", "status"),
        Index("ix_ws_operation_drafts_entity_workspace", "entity_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    source_event_id: Mapped[Optional[str]] = mapped_column(String(100))
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    current_state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    patches: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    validation: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    diff: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    discarded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class WorkspaceWorkBatch(Base, TimestampMixin):
    """A cohort of tasks that should complete before Strategist reviews again."""
    __tablename__ = "workspace_work_batches"
    __table_args__ = (
        Index("ix_ws_work_batches_workspace_status", "workspace_id", "status"),
        Index("ix_ws_work_batches_entity_workspace", "entity_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    source_draft_id: Mapped[Optional[str]] = mapped_column(String(26))
    source_kind: Mapped[Optional[str]] = mapped_column(String(50))
    summary: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default="active")
    task_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class WorkspaceActivity(Base, TimestampMixin):
    """Activity log entry for a workspace."""
    __tablename__ = "workspace_activities"
    __table_args__ = (
        Index("ix_ws_activity_workspace", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
