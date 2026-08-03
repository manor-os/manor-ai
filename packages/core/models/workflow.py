"""Workflow definition and run models for the agent workflow engine."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, query_expression

from .base import Base, TimestampMixin, generate_ulid


class WorkflowDefinition(Base, TimestampMixin):
    """A reusable workflow template with ordered steps."""
    __tablename__ = "workflow_definitions"
    __table_args__ = (
        Index("ix_workflow_definitions_entity", "entity_id"),
        Index("ix_workflow_definitions_workspace", "entity_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # ``created_by`` doubles as the owner for resource_access.py — this table
    # already tracked its creator, so no separate owner column is needed.
    created_by: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    # NULL workspace = shared entity-wide rather than scoped to one workspace.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="entity"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="flow", server_default="flow")
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    trigger_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # steps schema:
    # [
    #   {"id": "step1", "type": "agent", "name": "Research", "config": {"skill": "research_topic"}, "next": ["step2"]},
    #   {"id": "step2", "type": "condition", "name": "Check quality", "config": {"expression": "score > 0.7"}, "true_next": ["step3"], "false_next": ["step1"]},
    #   {"id": "step3", "type": "tool", "name": "Send email", "config": {"tool": "send_email"}, "next": []},
    # ]
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    category: Mapped[Optional[str]] = mapped_column(String(50))
    tags: Mapped[list] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # M11 config revision — bumped via packages.core.revisions.bump_revision
    # on REAL template-content changes only (steps / name / variables);
    # cosmetic edits (description, icon, tags) don't create a new revision.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )


class WorkflowBinding(Base, TimestampMixin):
    """Deployment of a workflow definition into a specific run context.

    A ``WorkflowDefinition`` is a reusable, context-free graph (the template).
    A ``WorkflowBinding`` is *where and how* that template actually runs —
    mirroring how ``AgentSubscription`` deploys an ``Agent`` into a workspace.

    ``workspace_id`` is optional:
      - ``NULL``  → entity / automation-level binding (run standalone or on a
                    schedule/webhook without a workspace context).
      - set       → workspace-level binding; the run resolves that workspace's
                    connectors, RAG, approvers and budget at execution time.

    The same definition can have many bindings, so one workflow serves multiple
    workspaces (and multiple business lines via ``business_line``) without
    cross-talk. ``operating_model.automations`` references *this* row, not the
    definition directly.
    """
    __tablename__ = "workflow_bindings"
    __table_args__ = (
        Index("ix_workflow_bindings_entity_workspace", "entity_id", "workspace_id"),
        Index("ix_workflow_bindings_workflow", "workflow_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    name: Mapped[Optional[str]] = mapped_column(String(255))  # friendly label shown in pickers
    business_line: Mapped[Optional[str]] = mapped_column(String(50))  # which workspace business line this serves
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    # trigger_type: manual | webhook | schedule | event | workspace_event
    trigger_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # binding-level variable defaults
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # context overrides (tool/credential scope)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # M11 config revision — bumped via packages.core.revisions.bump_revision.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )


class WorkflowRun(Base, TimestampMixin):
    """A single execution of a workflow."""
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_workflow", "workflow_id", "created_at"),
        Index("ix_workflow_runs_entity_status", "entity_id", "status"),
        Index("ix_workflow_runs_workspace", "workspace_id"),
        Index("ix_workflow_runs_retry_of_run_id", "retry_of_run_id"),
        Index("ix_workflow_runs_lineage_root_run_id", "lineage_root_run_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workflow_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Run context (resolved at execution time, not stored on the definition):
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    binding_id: Mapped[Optional[str]] = mapped_column(String(26))
    trigger_source: Mapped[Optional[str]] = mapped_column(String(50))  # manual | webhook | schedule | event | workspace_event
    retry_of_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    retry_from_step_id: Mapped[Optional[str]] = mapped_column(String(100))
    lineage_root_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    lineage_is_legacy: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    current_step_id: Mapped[Optional[str]] = mapped_column(String(100))
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    step_results: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    trigger_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    definition_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    execution_trace: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_by: Mapped[Optional[str]] = mapped_column(String(26))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    summary_workflow_name: Mapped[Optional[str]] = query_expression()
    summary_current_step_name: Mapped[Optional[str]] = query_expression()
    summary_history_state: Mapped[Optional[dict]] = query_expression()
    summary_legacy_retry_of_run_id: Mapped[Optional[str]] = query_expression()
    summary_legacy_retry_from_step_id: Mapped[Optional[str]] = query_expression()

    def _loaded_trigger_value(self, key: str) -> object:
        trigger_data = self.__dict__.get("trigger_data")
        return trigger_data.get(key) if isinstance(trigger_data, dict) else None

    @property
    def summary_effective_retry_of_run_id(self) -> Optional[str]:
        value = str(self.retry_of_run_id or "").strip()
        if value:
            return value
        value = str(
            self.summary_legacy_retry_of_run_id
            or self._loaded_trigger_value("retry_of_run_id")
            or ""
        ).strip()
        return value or None

    @property
    def summary_effective_retry_from_step_id(self) -> Optional[str]:
        value = str(self.retry_from_step_id or "").strip()
        if value:
            return value
        value = str(
            self.summary_legacy_retry_from_step_id
            or self._loaded_trigger_value("retry_from_step_id")
            or ""
        ).strip()
        return value or None

    @property
    def effective_retry_of_run_id(self) -> Optional[str]:
        value = str(self.retry_of_run_id or "").strip()
        if value:
            return value
        legacy = self.trigger_data if isinstance(self.trigger_data, dict) else {}
        value = str(legacy.get("retry_of_run_id") or "").strip()
        return value or None

    @property
    def effective_retry_from_step_id(self) -> Optional[str]:
        value = str(self.retry_from_step_id or "").strip()
        if value:
            return value
        legacy = self.trigger_data if isinstance(self.trigger_data, dict) else {}
        value = str(legacy.get("retry_from_step_id") or "").strip()
        return value or None

    @property
    def effective_attempt_number(self) -> int:
        if self.attempt_number is not None:
            return max(1, int(self.attempt_number))
        legacy = self.trigger_data if isinstance(self.trigger_data, dict) else {}
        try:
            return max(1, int(legacy.get("attempt_number") or 1))
        except (TypeError, ValueError):
            return 1

    @property
    def lineage_status(self) -> str:
        if self.lineage_is_legacy is not False or not self.lineage_root_run_id:
            return "legacy_untrusted_incomplete"
        return "canonical"


class WorkflowProject(Base, TimestampMixin):
    """Durable, schema-versioned state shared by related workflow runs."""

    __tablename__ = "workflow_projects"
    __table_args__ = (
        Index("ix_workflow_projects_workspace_stage", "workspace_id", "current_stage"),
        Index("ix_workflow_projects_entity_type", "entity_id", "project_type"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    project_type: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_stage: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)


class WorkflowActionGrant(Base, TimestampMixin):
    """Durable approval scope for actions performed by workflow runs."""

    __tablename__ = "workflow_action_grants"
    __table_args__ = (
        Index("ix_workflow_action_grants_project", "project_id", "grant_type"),
        Index("ix_workflow_action_grants_workspace", "workspace_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workflow_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    project_id: Mapped[str] = mapped_column(String(26), nullable=False)
    grant_type: Mapped[str] = mapped_column(String(80), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    granted_by: Mapped[str] = mapped_column(String(26), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
