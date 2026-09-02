"""Workflow definition and run models for the agent workflow engine."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, query_expression

from packages.core.constants.workflow import WorkflowRunStatus

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
    # workspace_id records the owning/home Workspace for access and lifecycle.
    # Deployment is separate: WorkflowBindings may reuse this definition in
    # other Workspaces, and NULL means it has no home Workspace.
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


class WorkflowTemplateInstallation(Base, TimestampMixin):
    """Stable template identity → entity-owned workflow instance.

    Marketplace/template ids identify portable source content. Runtime callers
    must never execute those ids directly: installing a template mints (or
    reuses) a normal ``WorkflowDefinition.id`` and records the relationship
    here. ``component_key`` lets one Blueprint own several independently
    installable Flows without falling back to a mutable name or slug for
    identity.
    """

    __tablename__ = "workflow_template_installations"
    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "template_id",
            "component_key",
            name="uq_workflow_template_installations_source",
        ),
        Index(
            "ix_workflow_template_installations_workflow",
            "workflow_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(String(160), nullable=False)
    component_key: Mapped[str] = mapped_column(
        String(120), nullable=False, default="main", server_default="main",
    )
    workflow_id: Mapped[str] = mapped_column(String(26), nullable=False)
    installed_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1.0.0", server_default="1.0.0",
    )
    installed_by: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="flow_template", server_default="flow_template",
    )
    installation_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
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
        Index(
            "uq_workflow_runs_webchat_submission",
            "binding_id",
            "webchat_session_id",
            "webchat_module_id",
            "webchat_submission_id",
            unique=True,
            postgresql_where=text(
                "trigger_source = 'public_webchat' "
                "AND webchat_session_id IS NOT NULL "
                "AND webchat_module_id IS NOT NULL "
                "AND webchat_submission_id IS NOT NULL"
            ),
            sqlite_where=text(
                "trigger_source = 'public_webchat' "
                "AND webchat_session_id IS NOT NULL "
                "AND webchat_module_id IS NOT NULL "
                "AND webchat_submission_id IS NOT NULL"
            ),
        ),
        Index("ix_workflow_runs_retry_of_run_id", "retry_of_run_id"),
        Index("ix_workflow_runs_lineage_root_run_id", "lineage_root_run_id"),
        Index(
            "ix_workflow_runs_continuation_due",
            "continuation_next_attempt_at",
        ),
        Index(
            "ix_workflow_runs_terminal_effects_due",
            "terminal_effects_completed_at",
            "terminal_effects_next_attempt_at",
            "id",
        ),
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
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=WorkflowRunStatus.PENDING,
    )
    current_step_id: Mapped[Optional[str]] = mapped_column(String(100))
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    step_results: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    trigger_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    webchat_session_id: Mapped[Optional[str]] = mapped_column(String(128))
    webchat_module_id: Mapped[Optional[str]] = mapped_column(String(80))
    webchat_submission_id: Mapped[Optional[str]] = mapped_column(String(80))
    definition_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    execution_snapshot: Mapped[dict] = mapped_column(
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
    continuation_token: Mapped[Optional[str]] = mapped_column(String(26))
    continuation_due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    continuation_next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    terminal_effects_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    terminal_effects_next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
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
        Index(
            "uq_workflow_projects_business_key",
            "entity_id",
            "workspace_id",
            "project_type",
            "project_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    project_type: Mapped[str] = mapped_column(String(80), nullable=False)
    project_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
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
        Index(
            "ix_workflow_action_grants_lineage",
            "workflow_lineage_root_run_id",
            "grant_type",
        ),
        Index(
            "uq_workflow_action_grants_proposal_item",
            "proposal_item_id",
            unique=True,
            postgresql_where=text("proposal_item_id IS NOT NULL"),
            sqlite_where=text("proposal_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workflow_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    project_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    proposal_item_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    workflow_lineage_root_run_id: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True
    )
    action_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    grant_type: Mapped[str] = mapped_column(String(80), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    granted_by: Mapped[str] = mapped_column(String(26), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_run_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
