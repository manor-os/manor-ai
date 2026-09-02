"""Entity and workspace service — CRUD operations."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.base import generate_ulid
from packages.core.constants.notification_types import NotificationOutboxStatus
from packages.core.constants.workspaces import WORKSPACE_SERVER_SETTING_KEYS
from packages.core.models.event import EventLog
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    settings_with_default_workspace_access,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_references,
    reusable_resource_ids_from_payload,
    reusable_skill_ids_from_runtime_state,
)


# How many days a soft-deleted workspace stays recoverable before the
# nightly purge task hard-deletes it. Override via env for staging.
WORKSPACE_PURGE_GRACE_DAYS = int(os.getenv("WORKSPACE_PURGE_GRACE_DAYS", "30"))


class ProtectedWorkspaceSettingsError(ValueError):
    """A generic update attempted to author server-owned Workspace state."""


def _generic_workspace_settings_update(
    current: object,
    requested: object,
) -> dict:
    """Replace user-owned settings while preserving server-owned contracts.

    Generic Workspace CRUD is intentionally not the writer for Blueprint
    provenance, setup admission, or creator identity. Dedicated creation,
    install, and setup services may write those keys directly on the locked
    ``Workspace`` row.
    """

    current_settings = dict(current) if isinstance(current, dict) else {}
    requested_settings = dict(requested) if isinstance(requested, dict) else {}
    for key in WORKSPACE_SERVER_SETTING_KEYS:
        if (
            key in requested_settings
            and requested_settings[key] != current_settings.get(key)
        ):
            raise ProtectedWorkspaceSettingsError(
                f"Workspace setting {key!r} is server-owned"
            )
        if key in current_settings:
            requested_settings[key] = current_settings[key]
        else:
            requested_settings.pop(key, None)
    return requested_settings


# ── Entity ──

async def get_entity(db: AsyncSession, entity_id: str) -> Optional[Entity]:
    # Check cache first
    cached = await cache.get(f"entity:{entity_id}")
    if cached is not None:
        return cached

    result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None)))
    entity = result.scalar_one_or_none()
    if entity:
        await cache.set(f"entity:{entity_id}", {
            "id": entity.id,
            "name": entity.name,
            "settings": entity.settings,
        }, ttl=300)
    return entity


async def update_entity(db: AsyncSession, entity_id: str, **fields) -> Optional[Entity]:
    entity = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None)))
    entity = entity.scalar_one_or_none()
    if not entity:
        return None
    for k, v in fields.items():
        if hasattr(entity, k) and v is not None:
            setattr(entity, k, v)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"entity:{entity_id}")
    return entity


# ── Workspace ──

async def list_workspaces(db: AsyncSession, entity_id: str) -> list[Workspace]:
    result = await db.execute(
        select(Workspace)
        .where(Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at.desc())
    )
    workspaces = list(result.scalars().all())
    missing = [workspace for workspace in workspaces if not workspace.artifact_folder_id]
    if missing:
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
        for workspace in missing:
            await ensure_workspace_artifact_folder(db, workspace)
            # Updating the binding expires server-managed columns such as
            # ``updated_at``. Refresh before API serializers access them.
            await db.refresh(workspace)
    return workspaces


async def get_workspace(db: AsyncSession, workspace_id: str, entity_id: str) -> Optional[Workspace]:
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )
    workspace = result.scalar_one_or_none()
    if workspace is not None and not workspace.artifact_folder_id:
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
        await ensure_workspace_artifact_folder(db, workspace)
        await db.refresh(workspace)
    return workspace


async def create_workspace(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    description: str = "",
    category: str = "",
    address: str = "",
    kind: str = "",
    operating_context: str = "",
    primary_work: str = "",
    match_business_ledgers: bool = True,
    **fields,
) -> Workspace:
    from packages.core.services.plan_gate import enforce_workspace_capacity

    await enforce_workspace_capacity(db, entity_id)
    operating_model = fields.get("operating_model")
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        skill_ids=reusable_skill_ids_from_runtime_state(operating_model),
    )
    from packages.core.services.workspace_ledger_matching import (
        settings_with_business_ledgers,
    )

    workspace_settings = settings_with_default_workspace_access(
        settings_with_business_ledgers(
            fields.pop("settings", None),
            infer_if_absent=match_business_ledgers,
            name=name,
            description=description,
            category=category,
            kind=kind,
            operating_context=operating_context,
            primary_work=primary_work,
            operating_model=operating_model,
        )
    )
    ws = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        description=description or None,
        category=category or None,
        address=address or None,
        kind=kind or None,
        operating_context=operating_context or None,
        primary_work=primary_work or None,
        settings=workspace_settings,
    )
    for k, v in fields.items():
        if hasattr(ws, k) and v is not None:
            setattr(ws, k, v)
    db.add(ws)
    await db.flush()
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
    await ensure_workspace_artifact_folder(db, ws)
    return ws


async def update_workspace(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    clear_fields: set[str] | None = None,
    **fields,
) -> Optional[Workspace]:
    if "operating_model" in fields:
        # Keep the global lifecycle -> Workspace row -> resource row order
        # shared with soft-delete/purge. Reversing the first two locks lets a
        # concurrent Workspace delete deadlock an operating-model update.
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    ws = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    if not ws or ws.deleted_at is not None:
        return None
    if "operating_model" in fields:
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            skill_ids=(
                reusable_skill_ids_from_runtime_state(fields["operating_model"])
                - reusable_skill_ids_from_runtime_state(ws.operating_model)
            ),
        )
    framing_fields = {"name", "primary_work", "operating_context"}
    framing_touched = False
    clear_fields = clear_fields or set()
    fields = dict(fields)
    if (
        "settings" in fields
        and (fields["settings"] is not None or "settings" in clear_fields)
    ):
        fields["settings"] = _generic_workspace_settings_update(
            ws.settings,
            fields["settings"],
        )
    for k, v in fields.items():
        if hasattr(ws, k) and (v is not None or k in clear_fields):
            old = getattr(ws, k, None)
            if k in framing_fields and old != v:
                framing_touched = True
            setattr(ws, k, v)
    await db.flush()
    if "name" in fields or not ws.artifact_folder_id:
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
        await ensure_workspace_artifact_folder(db, ws)
    if framing_touched:
        # Subscriptions cache an auto-generated identity blurb. When the
        # workspace's name / primary_work / operating_context changes, those
        # blurbs go stale and the agent introduces itself with the wrong
        # name.
        from packages.core.services.workspace_operation_service import (
            refresh_workspace_subscription_framings,
        )
        await refresh_workspace_subscription_framings(db, ws)
    await db.refresh(ws)
    return ws


async def soft_delete_workspace(
    db: AsyncSession, workspace_id: str, entity_id: str,
) -> bool:
    """Mark a workspace as deleted and remove its automation definitions.

    Other workspace data remains on disk. The nightly
    ``ops.purge_soft_deleted_workspaces`` task hard-deletes workspaces whose
    ``deleted_at`` is older than ``WORKSPACE_PURGE_GRACE_DAYS``. Until then,
    ``restore_workspace`` can restore the workspace and its built-in runtime
    jobs, but user-created automations stay deleted.
    """
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    ws = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not ws:
        return False
    ws.deleted_at = datetime.now(timezone.utc)
    from sqlalchemy import delete as sa_delete
    from packages.core.services.scheduler_service import delete_workspace_automations
    from packages.core.models.workflow import WorkflowActionGrant

    await db.execute(
        update(EventLog)
        .where(
            EventLog.entity_id == entity_id,
            EventLog.workspace_id == workspace_id,
            EventLog.external_delivery_status.in_([
                NotificationOutboxStatus.PENDING.value,
                NotificationOutboxStatus.PROCESSING.value,
            ]),
        )
        .values(
            external_delivery_status=NotificationOutboxStatus.CANCELED.value,
            external_delivery_locked_until=None,
            external_delivery_claim_token=None,
            external_delivery_last_error=(
                "workspace deleted before external event delivery"
            ),
        )
    )
    await db.execute(sa_delete(WorkflowActionGrant).where(
        WorkflowActionGrant.workspace_id == workspace_id,
    ))
    await delete_workspace_automations(db, workspace_id, entity_id)
    await db.flush()
    return True


async def restore_workspace(
    db: AsyncSession, workspace_id: str, entity_id: str,
) -> Optional[Workspace]:
    """Undo a soft delete — only succeeds if the workspace hasn't been
    purged yet. Returns the restored row or None if not found / already
    purged."""
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_not(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    ws = result.scalar_one_or_none()
    if not ws:
        return None
    from packages.core.services.plan_gate import enforce_workspace_capacity

    await enforce_workspace_capacity(db, entity_id)
    ws.deleted_at = None
    if not ws.artifact_folder_id:
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
        await ensure_workspace_artifact_folder(db, ws)
    from packages.core.services.workspace_runtime import sync_workspace_runtime_schedules
    await sync_workspace_runtime_schedules(db, ws)
    restored_embedding_document_ids = await _requeue_restored_workspace_documents(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    # The API dispatches these only after committing the lifecycle change.
    # Direct service callers still leave durable PENDING rows for the sweep.
    ws._restored_embedding_document_ids = tuple(restored_embedding_document_ids)
    await db.flush()
    await db.refresh(ws)
    return ws


async def _requeue_restored_workspace_documents(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> list[str]:
    """Make delete-blocked documents runnable only when their owner returns."""
    from packages.core.models.document import Document, VectorStatus
    from packages.core.models.workspace import Workspace
    from packages.core.services.document_access import document_workspace_ids_batched

    blocked_reason = Document.metadata_["indexing"]["blocked_reason"].astext
    documents = list((await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.vector_status == VectorStatus.PENDING,
            Document.is_trashed.is_(False),
            blocked_reason == "workspace_deleted",
        ).with_for_update()
    )).scalars())
    if not documents:
        return []

    workspace_ids_by_document = await document_workspace_ids_batched(db, documents)
    candidates = [
        document
        for document in documents
        if workspace_id in workspace_ids_by_document.get(str(document.id), set())
    ]
    if not candidates:
        return []

    candidate_workspace_ids = {
        owner_id
        for document in candidates
        for owner_id in workspace_ids_by_document.get(str(document.id), set())
    }
    deleted_workspace_ids = set((await db.execute(
        select(Workspace.id).where(
            Workspace.entity_id == entity_id,
            Workspace.id.in_(candidate_workspace_ids),
            Workspace.deleted_at.is_not(None),
        )
    )).scalars())

    queued_at = datetime.now(timezone.utc).isoformat()
    requeued: list[str] = []
    for document in candidates:
        owners = workspace_ids_by_document.get(str(document.id), set())
        if owners & deleted_workspace_ids:
            continue
        metadata = dict(document.metadata_ or {})
        indexing = dict(metadata.get("indexing") or {})
        indexing.pop("blocked_at", None)
        indexing.pop("blocked_reason", None)
        indexing.update({
            "step": "queued",
            "progress": 0,
            "current_chunk": 0,
            "queued_at": queued_at,
        })
        metadata["indexing"] = indexing
        document.metadata_ = metadata
        requeued.append(str(document.id))
    return requeued


async def list_trashed_workspaces(
    db: AsyncSession, entity_id: str,
) -> list[Workspace]:
    """Workspaces in the 30-day soft-delete grace window."""
    result = await db.execute(
        select(Workspace)
        .where(
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_not(None),
        )
        .order_by(Workspace.deleted_at.desc())
    )
    return list(result.scalars().all())


async def list_workspaces_due_for_purge(
    db: AsyncSession, *, grace_days: int = WORKSPACE_PURGE_GRACE_DAYS,
) -> list[Workspace]:
    """All soft-deleted workspaces whose grace window has expired.
    Used by the nightly purge Celery task."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    result = await db.execute(
        select(Workspace).where(
            Workspace.deleted_at.is_not(None),
            Workspace.deleted_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def purge_workspace(
    db: AsyncSession,
    workspace_id: str,
    *,
    expected_deleted_at: datetime | None = None,
) -> bool:
    """Hard-delete a workspace and cascade-clean ALL related rows.

    Called by the nightly purge task once the grace window elapses, or
    by tests / admin tools that need an immediate hard delete. Bypasses
    entity scoping intentionally — by the time we reach here, the
    workspace is already marked deleted.

    Deletion order matters for FK constraints — children first, then
    parents. Group by dependency level:
      1. Leaf rows (logs, messages, steps, leases, sub-worker bindings)
      2. Mid-level (tasks, conversations, plans, subscriptions, channels)
      3. Top-level (workspace itself)
    """
    from sqlalchemy import (
        and_ as sa_and_,
        delete as sa_delete,
        exists as sa_exists,
        func as sa_func,
        or_ as sa_or_,
        select as sa_select,
        update as sa_update,
    )
    from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
    from packages.core.models.mcp import AgentMCPBinding
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import (
        Agent,
        AgentSubscription,
        AgentToolBinding,
        WorkspaceActivity,
        WorkspaceOperationDraft,
        WorkspaceStaff,
        WorkspaceWorkBatch,
    )
    from packages.core.models.permission import (
        Capability,
        GrantStatus,
        ResourceGrant,
        ResourceGrantPending,
        ResourceType,
        SubjectType,
        Visibility,
    )
    from packages.core.models.channel_pairing import ChannelPairingCode
    from packages.core.models.custom_field import CustomFieldDefinition
    from packages.core.models.participant import (
        HumanCommitment,
        HumanContribution,
        ParticipantProfile,
    )
    from packages.core.models.runtime_run import (
        RuntimeOutboxEvent,
        RuntimeRun,
        RuntimeRunStatus,
        SandboxInstance,
        SandboxReservation,
    )
    from packages.core.models.task import Task, TaskLog, Conversation, Message
    from packages.core.models.task_template import TaskTemplate
    from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
    from packages.core.models.scheduler import (
        AgentExecution,
        ScheduledJob,
        ScheduledJobRun,
    )
    from packages.core.models.workspace_stat import (
        WorkspaceStat,
        WorkspaceStatObservation,
    )
    from packages.core.models.automation_revision import AutomationRevision
    from packages.core.models.consolidation_report import ConsolidationReport
    from packages.core.models.experiment import Experiment
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.workflow import (
        WorkflowActionGrant,
        WorkflowBinding,
        WorkflowDefinition,
        WorkflowProject,
        WorkflowRun,
        WorkflowTemplateInstallation,
    )
    from packages.core.models.memory import AgentMemory
    from packages.core.models.runtime_learning import (
        AgentLearningCandidate,
        RuntimeEvidence,
        RuntimeEventLog,
    )
    from packages.core.models.usage import TokenUsageLog, ToolCallLog
    from packages.core.models.document import DocumentGroup, Channel
    from packages.core.models.channel import (
        Announcement,
        ChannelConfig,
        TwilioVoiceCallSession,
    )
    from packages.core.models.site import Site, SiteEvent
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import WorkLease, SubscriptionWorker
    from packages.core.models.governance import GovernancePolicy, GovernanceRevision
    from packages.core.services.chat_feedback import (
        COMPLETION_FEEDBACK_EVIDENCE_TYPES,
    )
    from packages.core.models.notification import Notification
    from packages.core.models.workspace_event import WorkspaceEvent
    from packages.core.models.billing import (
        CreditReservation,
        CreditUsageAllocation,
        CreditUsageLog,
    )
    from packages.core.services.marketplace_resource_links import (
        SCOPE_ENTITY,
        SCOPE_RESOURCE,
        SCOPE_WORKSPACE,
    )
    entity_id = (await db.execute(
        sa_select(Workspace.entity_id).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if not entity_id:
        return False

    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    ws = (await db.execute(
        sa_select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not ws:
        return False
    if expected_deleted_at is not None and ws.deleted_at != expected_deleted_at:
        # The candidate was restored or deleted again after the nightly sweep
        # loaded it. Leave the current state alone and reconsider it next run.
        return False

    # Lock provenance rows before definition rows. Marketplace writers use the
    # same order, while deployment/binding writers only lock definitions. This
    # makes every concurrent outcome serial: purge sees the committed reuse,
    # or the writer revalidates after purge and rejects the missing resource.
    owned_agent_ids = set((await db.execute(
        sa_select(Agent.id).where(
            Agent.entity_id == ws.entity_id,
            Agent.workspace_id == workspace_id,
        )
    )).scalars().all())
    owned_skill_ids = set((await db.execute(
        sa_select(Skill.id).where(
            Skill.entity_id == ws.entity_id,
            Skill.workspace_id == workspace_id,
        )
    )).scalars().all())
    owned_workflow_ids = set((await db.execute(
        sa_select(WorkflowDefinition.id).where(
            WorkflowDefinition.entity_id == ws.entity_id,
            WorkflowDefinition.workspace_id == workspace_id,
        )
    )).scalars().all())
    await db.execute(
        sa_select(MarketplaceResourceLink.id)
        .where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            sa_or_(
                sa_and_(
                    MarketplaceResourceLink.local_resource_type == "agent",
                    MarketplaceResourceLink.local_resource_id.in_(owned_agent_ids),
                ),
                sa_and_(
                    MarketplaceResourceLink.local_resource_type == "skill",
                    MarketplaceResourceLink.local_resource_id.in_(owned_skill_ids),
                ),
                sa_and_(
                    MarketplaceResourceLink.local_resource_type == "workflow",
                    MarketplaceResourceLink.local_resource_id.in_(owned_workflow_ids),
                ),
            ),
        )
        .order_by(MarketplaceResourceLink.id)
        .with_for_update()
    )
    owned_agent_ids = set((await db.execute(
        sa_select(Agent.id).where(
            Agent.entity_id == ws.entity_id,
            Agent.workspace_id == workspace_id,
        ).order_by(Agent.id).with_for_update()
    )).scalars().all())
    owned_skill_ids = set((await db.execute(
        sa_select(Skill.id).where(
            Skill.entity_id == ws.entity_id,
            Skill.workspace_id == workspace_id,
        ).order_by(Skill.id).with_for_update()
    )).scalars().all())
    owned_workflow_ids = set((await db.execute(
        sa_select(WorkflowDefinition.id).where(
            WorkflowDefinition.entity_id == ws.entity_id,
            WorkflowDefinition.workspace_id == workspace_id,
        ).order_by(WorkflowDefinition.id).with_for_update()
    )).scalars().all())

    # ── Level 1: Leaf rows that FK into mid-level tables ──

    # Task logs (FK → task_id)
    task_ids_q = sa_select(Task.id).where(Task.workspace_id == workspace_id)
    await db.execute(sa_delete(TaskLog).where(TaskLog.task_id.in_(task_ids_q)))

    # Messages (FK → conversation_id)
    conv_ids_q = sa_select(Conversation.id).where(Conversation.workspace_id == workspace_id)
    # Feedback persistence always locks Message before its feedback/evidence
    # projections. Purge must use the same order to avoid a lock cycle.
    await db.execute(
        sa_delete(Message).where(Message.conversation_id.in_(conv_ids_q))
    )
    await db.execute(
        sa_delete(RuntimeEvidence).where(
            RuntimeEvidence.workspace_id == workspace_id,
            RuntimeEvidence.evidence_type.in_(
                COMPLETION_FEEDBACK_EVIDENCE_TYPES
            ),
        )
    )

    # Execution steps (FK → plan_id)
    plan_ids_q = sa_select(ExecutionPlan.id).where(ExecutionPlan.workspace_id == workspace_id)
    await db.execute(sa_delete(ExecutionStep).where(ExecutionStep.plan_id.in_(plan_ids_q)))

    # SubscriptionWorker bindings (FK → subscription_id)
    sub_ids_q = sa_select(AgentSubscription.id).where(AgentSubscription.workspace_id == workspace_id)
    await db.execute(sa_delete(SubscriptionWorker).where(SubscriptionWorker.subscription_id.in_(sub_ids_q)))

    # A Workspace is only the home/owner of these reusable resources. Preserve
    # anything deployed or installed in another scope, then detach it from the
    # Workspace being purged. Resources used only by this Workspace are deleted.
    agent_workspace_audiences: dict[str, set[str]] = {}
    agent_user_audiences: dict[str, set[str]] = {}
    entity_visible_agent_ids: set[str] = set()

    def add_audience(
        audiences: dict[str, set[str]],
        resource_id: str | None,
        audience_id: str | None,
    ) -> None:
        if resource_id and audience_id:
            audiences.setdefault(str(resource_id), set()).add(str(audience_id))

    subscription_uses = (await db.execute(
        sa_select(
            AgentSubscription.agent_id,
            AgentSubscription.workspace_id,
        ).where(
            AgentSubscription.entity_id == ws.entity_id,
            AgentSubscription.agent_id.in_(owned_agent_ids),
            sa_or_(
                AgentSubscription.workspace_id.is_(None),
                AgentSubscription.workspace_id != workspace_id,
            ),
        )
    )).all()
    shared_agent_ids = {str(row.agent_id) for row in subscription_uses}
    for agent_id, reuse_workspace_id in subscription_uses:
        if reuse_workspace_id:
            add_audience(agent_workspace_audiences, agent_id, reuse_workspace_id)
        else:
            entity_visible_agent_ids.add(str(agent_id))

    agent_link_uses = (await db.execute(
        sa_select(
            MarketplaceResourceLink.local_resource_id,
            MarketplaceResourceLink.scope_type,
            MarketplaceResourceLink.scope_id,
        ).where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            MarketplaceResourceLink.local_resource_type == "agent",
            MarketplaceResourceLink.local_resource_id.in_(owned_agent_ids),
            sa_or_(
                MarketplaceResourceLink.scope_type == SCOPE_ENTITY,
                sa_and_(
                    MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                    MarketplaceResourceLink.scope_id != workspace_id,
                ),
            ),
        )
    )).all()
    shared_agent_ids.update(str(row.local_resource_id) for row in agent_link_uses)
    for agent_id, scope_type, scope_id in agent_link_uses:
        if scope_type == SCOPE_WORKSPACE:
            add_audience(agent_workspace_audiences, agent_id, scope_id)
        elif scope_type == SCOPE_ENTITY:
            entity_visible_agent_ids.add(str(agent_id))

    scheduled_payload_uses = (await db.execute(
        sa_select(
            ScheduledJob.execution_target,
            ScheduledJob.workspace_id,
            ScheduledJob.user_id,
        ).where(
            ScheduledJob.entity_id == ws.entity_id,
            sa_or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id != workspace_id,
            ),
        )
    )).all()
    for payload, reuse_workspace_id, reuse_user_id in scheduled_payload_uses:
        agent_ids, _skill_ids, _workflow_ids = (
            reusable_resource_ids_from_payload(payload)
        )
        for agent_id in agent_ids & owned_agent_ids:
            shared_agent_ids.add(agent_id)
            if reuse_workspace_id:
                add_audience(
                    agent_workspace_audiences,
                    agent_id,
                    reuse_workspace_id,
                )
            add_audience(agent_user_audiences, agent_id, reuse_user_id)

    direct_agent_use_queries = [
        sa_select(
            Task.agent_id,
            Task.workspace_id,
            sa_func.coalesce(Task.owner_id, Task.creator_id),
        ).where(
            Task.entity_id == ws.entity_id,
            Task.agent_id.in_(owned_agent_ids),
            sa_or_(Task.workspace_id.is_(None), Task.workspace_id != workspace_id),
        ),
        sa_select(
            Conversation.agent_id,
            Conversation.workspace_id,
            Conversation.user_id,
        ).where(
            Conversation.entity_id == ws.entity_id,
            Conversation.agent_id.in_(owned_agent_ids),
            sa_or_(
                Conversation.workspace_id.is_(None),
                Conversation.workspace_id != workspace_id,
            ),
        ),
        sa_select(Channel.agent_id, Channel.workspace_id, Channel.user_id).where(
            Channel.entity_id == ws.entity_id,
            Channel.agent_id.in_(owned_agent_ids),
            sa_or_(
                Channel.workspace_id.is_(None),
                Channel.workspace_id != workspace_id,
            ),
        ),
        sa_select(
            ScheduledJob.agent_id,
            ScheduledJob.workspace_id,
            ScheduledJob.user_id,
        ).where(
            ScheduledJob.entity_id == ws.entity_id,
            ScheduledJob.agent_id.in_(owned_agent_ids),
            sa_or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id != workspace_id,
            ),
        ),
        sa_select(
            RuntimeRun.agent_id,
            RuntimeRun.workspace_id,
            RuntimeRun.user_id,
        ).where(
            RuntimeRun.entity_id == ws.entity_id,
            RuntimeRun.agent_id.in_(owned_agent_ids),
            RuntimeRun.status.in_(RuntimeRunStatus.active_root()),
            sa_or_(
                RuntimeRun.workspace_id.is_(None),
                RuntimeRun.workspace_id != workspace_id,
            ),
        ),
    ]
    for query in direct_agent_use_queries:
        for agent_id, reuse_workspace_id, reuse_user_id in (await db.execute(query)).all():
            shared_agent_ids.add(str(agent_id))
            if reuse_workspace_id:
                add_audience(agent_workspace_audiences, agent_id, reuse_workspace_id)
            add_audience(agent_user_audiences, agent_id, reuse_user_id)

    template_agent_ids = set((await db.execute(
        sa_select(TaskTemplate.default_agent_id).where(
            TaskTemplate.entity_id == ws.entity_id,
            TaskTemplate.default_agent_id.in_(owned_agent_ids),
        )
    )).scalars().all())
    shared_agent_ids.update(str(agent_id) for agent_id in template_agent_ids)
    entity_visible_agent_ids.update(str(agent_id) for agent_id in template_agent_ids)

    active_execution_uses = (await db.execute(
        sa_select(AgentExecution.agent_id, AgentExecution.workspace_id).where(
            AgentExecution.entity_id == ws.entity_id,
            AgentExecution.agent_id.in_(owned_agent_ids),
            AgentExecution.status == "running",
            sa_or_(
                AgentExecution.workspace_id.is_(None),
                AgentExecution.workspace_id != workspace_id,
            ),
        )
    )).all()
    shared_agent_ids.update(str(row.agent_id) for row in active_execution_uses)
    for agent_id, reuse_workspace_id in active_execution_uses:
        add_audience(agent_workspace_audiences, agent_id, reuse_workspace_id)
    exclusive_agent_ids = owned_agent_ids - shared_agent_ids

    skill_workspace_audiences: dict[str, set[str]] = {}
    skill_user_audiences: dict[str, set[str]] = {}
    entity_visible_skill_ids: set[str] = set()
    public_skill_ids = set((await db.execute(
        sa_select(Skill.id).where(
            Skill.id.in_(owned_skill_ids),
            Skill.is_public.is_(True),
        )
    )).scalars().all())
    shared_skill_ids = set(public_skill_ids)
    entity_visible_skill_ids.update(public_skill_ids)

    workspace_skill_uses = (await db.execute(
        sa_select(Workspace.id, Workspace.operating_model).where(
            Workspace.entity_id == ws.entity_id,
            Workspace.id != workspace_id,
        )
    )).all()
    for audience_workspace_id, operating_model in workspace_skill_uses:
        for skill_id in (
            reusable_skill_ids_from_runtime_state(operating_model)
            & owned_skill_ids
        ):
            shared_skill_ids.add(skill_id)
            add_audience(
                skill_workspace_audiences,
                skill_id,
                audience_workspace_id,
            )

    open_draft_skill_uses = (await db.execute(
        sa_select(
            WorkspaceOperationDraft.workspace_id,
            WorkspaceOperationDraft.created_by_user_id,
            WorkspaceOperationDraft.current_state,
        ).where(
            WorkspaceOperationDraft.entity_id == ws.entity_id,
            WorkspaceOperationDraft.workspace_id != workspace_id,
            WorkspaceOperationDraft.status == "open",
        )
    )).all()
    for audience_workspace_id, audience_user_id, current_state in open_draft_skill_uses:
        for skill_id in (
            reusable_skill_ids_from_runtime_state(current_state)
            & owned_skill_ids
        ):
            shared_skill_ids.add(skill_id)
            if audience_workspace_id:
                add_audience(
                    skill_workspace_audiences,
                    skill_id,
                    audience_workspace_id,
                )
            add_audience(skill_user_audiences, skill_id, audience_user_id)
    bound_skill_uses = (await db.execute(
        sa_select(AgentSkillBinding.skill_id, AgentSkillBinding.agent_id)
        .join(Agent, Agent.id == AgentSkillBinding.agent_id)
        .where(
            AgentSkillBinding.skill_id.in_(owned_skill_ids),
            Agent.entity_id == ws.entity_id,
            Agent.id.not_in(exclusive_agent_ids),
        )
    )).all()
    shared_skill_ids.update(str(row.skill_id) for row in bound_skill_uses)
    for skill_id, agent_id in bound_skill_uses:
        for audience_workspace_id in agent_workspace_audiences.get(str(agent_id), set()):
            add_audience(
                skill_workspace_audiences,
                skill_id,
                audience_workspace_id,
            )
        for audience_user_id in agent_user_audiences.get(str(agent_id), set()):
            add_audience(skill_user_audiences, skill_id, audience_user_id)
        if str(agent_id) in entity_visible_agent_ids:
            entity_visible_skill_ids.add(str(skill_id))

    skill_link_uses = (await db.execute(
        sa_select(
            MarketplaceResourceLink.local_resource_id,
            MarketplaceResourceLink.scope_type,
            MarketplaceResourceLink.scope_id,
        ).where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            MarketplaceResourceLink.local_resource_type == "skill",
            MarketplaceResourceLink.local_resource_id.in_(owned_skill_ids),
            sa_or_(
                MarketplaceResourceLink.scope_type == SCOPE_ENTITY,
                sa_and_(
                    MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                    MarketplaceResourceLink.scope_id != workspace_id,
                ),
            ),
        )
    )).all()
    shared_skill_ids.update(str(row.local_resource_id) for row in skill_link_uses)
    for skill_id, scope_type, scope_id in skill_link_uses:
        if scope_type == SCOPE_WORKSPACE:
            add_audience(skill_workspace_audiences, skill_id, scope_id)
        elif scope_type == SCOPE_ENTITY:
            entity_visible_skill_ids.add(str(skill_id))

    scheduled_skill_uses = (await db.execute(
        sa_select(
            ScheduledJob.execution_target["skill_id"].astext,
            ScheduledJob.workspace_id,
            ScheduledJob.user_id,
        ).where(
            ScheduledJob.entity_id == ws.entity_id,
            ScheduledJob.execution_target["skill_id"].astext.in_(owned_skill_ids),
            sa_or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id != workspace_id,
            ),
        )
    )).all()
    for skill_id, audience_workspace_id, audience_user_id in scheduled_skill_uses:
        shared_skill_ids.add(str(skill_id))
        if audience_workspace_id:
            add_audience(
                skill_workspace_audiences,
                skill_id,
                audience_workspace_id,
            )
        add_audience(skill_user_audiences, skill_id, audience_user_id)
    for payload, audience_workspace_id, audience_user_id in scheduled_payload_uses:
        _agent_ids, skill_ids, _workflow_ids = (
            reusable_resource_ids_from_payload(payload)
        )
        for skill_id in skill_ids & owned_skill_ids:
            shared_skill_ids.add(skill_id)
            if audience_workspace_id:
                add_audience(
                    skill_workspace_audiences,
                    skill_id,
                    audience_workspace_id,
                )
            add_audience(skill_user_audiences, skill_id, audience_user_id)
    exclusive_skill_ids = owned_skill_ids - shared_skill_ids

    workflow_workspace_audiences: dict[str, set[str]] = {}
    workflow_user_audiences: dict[str, set[str]] = {}
    entity_visible_workflow_ids: set[str] = set()
    workflow_binding_uses = (await db.execute(
        sa_select(WorkflowBinding.workflow_id, WorkflowBinding.workspace_id).where(
            WorkflowBinding.entity_id == ws.entity_id,
            WorkflowBinding.workflow_id.in_(owned_workflow_ids),
            sa_or_(
                WorkflowBinding.workspace_id.is_(None),
                WorkflowBinding.workspace_id != workspace_id,
            ),
        )
    )).all()
    shared_workflow_ids = {
        str(row.workflow_id) for row in workflow_binding_uses
    }
    for workflow_id, reuse_workspace_id in workflow_binding_uses:
        if reuse_workspace_id:
            add_audience(
                workflow_workspace_audiences,
                workflow_id,
                reuse_workspace_id,
            )
        else:
            entity_visible_workflow_ids.add(str(workflow_id))

    workflow_link_uses = (await db.execute(
        sa_select(
            MarketplaceResourceLink.local_resource_id,
            MarketplaceResourceLink.scope_type,
            MarketplaceResourceLink.scope_id,
        ).where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            MarketplaceResourceLink.local_resource_type == "workflow",
            MarketplaceResourceLink.local_resource_id.in_(owned_workflow_ids),
            sa_or_(
                MarketplaceResourceLink.scope_type == SCOPE_ENTITY,
                sa_and_(
                    MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                    MarketplaceResourceLink.scope_id != workspace_id,
                ),
            ),
        )
    )).all()
    shared_workflow_ids.update(
        str(row.local_resource_id) for row in workflow_link_uses
    )
    for workflow_id, scope_type, scope_id in workflow_link_uses:
        if scope_type == SCOPE_WORKSPACE:
            add_audience(workflow_workspace_audiences, workflow_id, scope_id)
        elif scope_type == SCOPE_ENTITY:
            entity_visible_workflow_ids.add(str(workflow_id))

    scheduled_workflow_uses = (await db.execute(
        sa_select(
            sa_func.coalesce(
                ScheduledJob.execution_target["workflow_id"].astext,
                ScheduledJob.goal_id,
            ),
            ScheduledJob.workspace_id,
            ScheduledJob.user_id,
        ).where(
            ScheduledJob.entity_id == ws.entity_id,
            ScheduledJob.execution_type == "workflow",
            sa_func.coalesce(
                ScheduledJob.execution_target["workflow_id"].astext,
                ScheduledJob.goal_id,
            ).in_(owned_workflow_ids),
            sa_or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id != workspace_id,
            ),
        )
    )).all()
    for workflow_id, audience_workspace_id, audience_user_id in scheduled_workflow_uses:
        shared_workflow_ids.add(str(workflow_id))
        if audience_workspace_id:
            add_audience(
                workflow_workspace_audiences,
                workflow_id,
                audience_workspace_id,
            )
        add_audience(
            workflow_user_audiences,
            workflow_id,
            audience_user_id,
        )
    for payload, audience_workspace_id, audience_user_id in scheduled_payload_uses:
        _agent_ids, _skill_ids, workflow_ids = (
            reusable_resource_ids_from_payload(payload)
        )
        for workflow_id in workflow_ids & owned_workflow_ids:
            shared_workflow_ids.add(workflow_id)
            if audience_workspace_id:
                add_audience(
                    workflow_workspace_audiences,
                    workflow_id,
                    audience_workspace_id,
                )
            add_audience(
                workflow_user_audiences,
                workflow_id,
                audience_user_id,
            )

    workflow_run_uses = (await db.execute(
        sa_select(
            WorkflowRun.workflow_id,
            WorkflowRun.workspace_id,
            WorkflowRun.started_by,
        ).where(
            WorkflowRun.entity_id == ws.entity_id,
            WorkflowRun.workflow_id.in_(owned_workflow_ids),
            sa_or_(
                WorkflowRun.workspace_id.is_(None),
                WorkflowRun.workspace_id != workspace_id,
            ),
        )
    )).all()
    shared_workflow_ids.update(str(row.workflow_id) for row in workflow_run_uses)
    for workflow_id, reuse_workspace_id, reuse_user_id in workflow_run_uses:
        if reuse_workspace_id:
            add_audience(
                workflow_workspace_audiences,
                workflow_id,
                reuse_workspace_id,
            )
        add_audience(workflow_user_audiences, workflow_id, reuse_user_id)

    owned_workflow_steps = dict((await db.execute(
        sa_select(WorkflowDefinition.id, WorkflowDefinition.steps).where(
            WorkflowDefinition.id.in_(owned_workflow_ids),
        )
    )).all())

    def workflow_resource_ids(
        payload: object,
    ) -> tuple[set[str], set[str], set[str]]:
        agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(
            payload
        )
        return (
            agent_ids & owned_agent_ids,
            skill_ids & owned_skill_ids,
            workflow_ids & owned_workflow_ids,
        )

    root_workflow_ids = set(shared_workflow_ids)
    for root_workflow_id in root_workflow_ids:
        root_workspace_ids = workflow_workspace_audiences.get(root_workflow_id, set())
        root_user_ids = workflow_user_audiences.get(root_workflow_id, set())
        root_is_entity_visible = root_workflow_id in entity_visible_workflow_ids
        pending_workflow_ids = {root_workflow_id}
        visited_workflow_ids: set[str] = set()
        while pending_workflow_ids:
            workflow_id = pending_workflow_ids.pop()
            if workflow_id in visited_workflow_ids:
                continue
            visited_workflow_ids.add(workflow_id)
            agent_ids, skill_ids, child_workflow_ids = workflow_resource_ids(
                owned_workflow_steps.get(workflow_id) or []
            )

            shared_agent_ids.update(agent_ids)
            for agent_id in agent_ids:
                for audience_workspace_id in root_workspace_ids:
                    add_audience(
                        agent_workspace_audiences,
                        agent_id,
                        audience_workspace_id,
                    )
                for audience_user_id in root_user_ids:
                    add_audience(agent_user_audiences, agent_id, audience_user_id)
                if root_is_entity_visible:
                    entity_visible_agent_ids.add(agent_id)

            shared_skill_ids.update(skill_ids)
            for skill_id in skill_ids:
                for audience_workspace_id in root_workspace_ids:
                    add_audience(
                        skill_workspace_audiences,
                        skill_id,
                        audience_workspace_id,
                    )
                for audience_user_id in root_user_ids:
                    add_audience(skill_user_audiences, skill_id, audience_user_id)
                if root_is_entity_visible:
                    entity_visible_skill_ids.add(skill_id)

            for child_workflow_id in child_workflow_ids:
                shared_workflow_ids.add(child_workflow_id)
                for audience_workspace_id in root_workspace_ids:
                    add_audience(
                        workflow_workspace_audiences,
                        child_workflow_id,
                        audience_workspace_id,
                    )
                for audience_user_id in root_user_ids:
                    add_audience(
                        workflow_user_audiences,
                        child_workflow_id,
                        audience_user_id,
                    )
                if root_is_entity_visible:
                    entity_visible_workflow_ids.add(child_workflow_id)
                pending_workflow_ids.add(child_workflow_id)

    # Workflow steps can make a previously home-only Agent reusable. Its bound
    # Skills must inherit the same audiences before exclusivity is finalized.
    workflow_agent_skill_uses = (await db.execute(
        sa_select(AgentSkillBinding.skill_id, AgentSkillBinding.agent_id).where(
            AgentSkillBinding.skill_id.in_(owned_skill_ids),
            AgentSkillBinding.agent_id.in_(shared_agent_ids),
        )
    )).all()
    shared_skill_ids.update(str(row.skill_id) for row in workflow_agent_skill_uses)
    for skill_id, agent_id in workflow_agent_skill_uses:
        for audience_workspace_id in agent_workspace_audiences.get(str(agent_id), set()):
            add_audience(
                skill_workspace_audiences,
                skill_id,
                audience_workspace_id,
            )
        for audience_user_id in agent_user_audiences.get(str(agent_id), set()):
            add_audience(skill_user_audiences, skill_id, audience_user_id)
        if str(agent_id) in entity_visible_agent_ids:
            entity_visible_skill_ids.add(str(skill_id))

    exclusive_agent_ids = owned_agent_ids - shared_agent_ids
    exclusive_skill_ids = owned_skill_ids - shared_skill_ids
    exclusive_workflow_ids = owned_workflow_ids - shared_workflow_ids

    desired_grants: set[tuple[str, str, str, str]] = set()
    workspace_roles = ("owner", "editor", "contributor", "viewer", "admin", "member")
    for resource_type, audiences in (
        (ResourceType.AGENT, agent_workspace_audiences),
        (ResourceType.SKILL, skill_workspace_audiences),
        (ResourceType.WORKFLOW, workflow_workspace_audiences),
    ):
        for resource_id, audience_workspace_ids in audiences.items():
            for audience_workspace_id in audience_workspace_ids:
                for role in workspace_roles:
                    desired_grants.add((
                        resource_type,
                        resource_id,
                        SubjectType.WORKSPACE_ROLE,
                        f"{audience_workspace_id}:{role}",
                    ))
    for resource_type, audiences in (
        (ResourceType.AGENT, agent_user_audiences),
        (ResourceType.SKILL, skill_user_audiences),
        (ResourceType.WORKFLOW, workflow_user_audiences),
    ):
        for resource_id, audience_user_ids in audiences.items():
            for audience_user_id in audience_user_ids:
                desired_grants.add((
                    resource_type,
                    resource_id,
                    SubjectType.USER,
                    audience_user_id,
                ))

    if desired_grants:
        now = datetime.now(timezone.utc)
        grant_resource_ids = {item[1] for item in desired_grants}
        grant_subject_ids = {item[3] for item in desired_grants}
        existing_grants = list((await db.execute(
            sa_select(ResourceGrant).where(
                ResourceGrant.entity_id == ws.entity_id,
                ResourceGrant.resource_id.in_(grant_resource_ids),
                ResourceGrant.subject_id.in_(grant_subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                sa_or_(
                    ResourceGrant.expires_at.is_(None),
                    ResourceGrant.expires_at > now,
                ),
            )
        )).scalars().all())
        for grant in existing_grants:
            key = (
                grant.resource_type,
                grant.resource_id,
                grant.subject_type,
                grant.subject_id,
            )
            if key not in desired_grants:
                continue
            if Capability.VIEW not in (grant.capabilities or []):
                grant.capabilities = [*(grant.capabilities or []), Capability.VIEW]
            desired_grants.discard(key)
        db.add_all([
            ResourceGrant(
                entity_id=ws.entity_id,
                resource_type=resource_type,
                resource_id=resource_id,
                subject_type=subject_type,
                subject_id=subject_id,
                capabilities=[Capability.VIEW],
                granted_at=now,
                status=GrantStatus.ACTIVE,
                metadata_={
                    "source": "workspace_purge_retained_resource",
                    "purged_workspace_id": workspace_id,
                },
            )
            for resource_type, resource_id, subject_type, subject_id in desired_grants
        ])

    exclusive_grant_target = sa_or_(
        sa_and_(
            ResourceGrant.resource_type == ResourceType.AGENT,
            ResourceGrant.resource_id.in_(exclusive_agent_ids),
        ),
        sa_and_(
            ResourceGrant.resource_type == ResourceType.SKILL,
            ResourceGrant.resource_id.in_(exclusive_skill_ids),
        ),
        sa_and_(
            ResourceGrant.resource_type == ResourceType.WORKFLOW,
            ResourceGrant.resource_id.in_(exclusive_workflow_ids),
        ),
    )
    await db.execute(sa_delete(ResourceGrant).where(
        ResourceGrant.entity_id == ws.entity_id,
        sa_or_(
            exclusive_grant_target,
            sa_and_(
                ResourceGrant.resource_type == ResourceType.WORKSPACE,
                ResourceGrant.resource_id == workspace_id,
            ),
            sa_and_(
                ResourceGrant.subject_type == SubjectType.WORKSPACE_ROLE,
                sa_func.lower(ResourceGrant.subject_id).like(
                    f"{workspace_id.lower()}:%"
                ),
            ),
        ),
    ))
    # Pending access requests have no workspace_id of their own. Remove
    # requests targeting the purged Workspace or exclusively owned resources;
    # requests for retained/shared resources remain actionable.
    pending_resource_target = sa_or_(
        sa_and_(
            ResourceGrantPending.resource_type == ResourceType.WORKSPACE,
            ResourceGrantPending.resource_id == workspace_id,
        ),
        sa_and_(
            ResourceGrantPending.resource_type == ResourceType.AGENT,
            ResourceGrantPending.resource_id.in_(exclusive_agent_ids),
        ),
        sa_and_(
            ResourceGrantPending.resource_type == ResourceType.SKILL,
            ResourceGrantPending.resource_id.in_(exclusive_skill_ids),
        ),
        sa_and_(
            ResourceGrantPending.resource_type == ResourceType.WORKFLOW,
            ResourceGrantPending.resource_id.in_(exclusive_workflow_ids),
        ),
        sa_and_(
            ResourceGrantPending.resource_type == ResourceType.TASK,
            ResourceGrantPending.resource_id.in_(task_ids_q),
        ),
    )
    await db.execute(sa_delete(ResourceGrantPending).where(
        ResourceGrantPending.entity_id == ws.entity_id,
        pending_resource_target,
    ))

    await db.execute(sa_delete(AgentSkillBinding).where(sa_or_(
        AgentSkillBinding.agent_id.in_(exclusive_agent_ids),
        AgentSkillBinding.skill_id.in_(exclusive_skill_ids),
    )))
    await db.execute(
        sa_delete(AgentMCPBinding).where(
            AgentMCPBinding.agent_id.in_(exclusive_agent_ids)
        )
    )
    await db.execute(
        sa_delete(AgentToolBinding).where(
            AgentToolBinding.agent_id.in_(exclusive_agent_ids)
        )
    )
    await db.execute(
        sa_delete(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.workflow_id.in_(exclusive_workflow_ids)
        )
    )

    # Preserve source provenance for retained components without keeping a
    # dangling install scope that names the deleted Workspace. Resource scope
    # is the exact local identity and does not imply deployment anywhere.
    shared_component_link = sa_or_(
        sa_and_(
            MarketplaceResourceLink.local_resource_type == "agent",
            MarketplaceResourceLink.local_resource_id.in_(shared_agent_ids),
        ),
        sa_and_(
            MarketplaceResourceLink.local_resource_type == "skill",
            MarketplaceResourceLink.local_resource_id.in_(shared_skill_ids),
        ),
        sa_and_(
            MarketplaceResourceLink.local_resource_type == "workflow",
            MarketplaceResourceLink.local_resource_id.in_(shared_workflow_ids),
        ),
    )
    home_shared_component_link = sa_and_(
        MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
        MarketplaceResourceLink.scope_id == workspace_id,
        shared_component_link,
    )
    existing_resource_link = MarketplaceResourceLink.__table__.alias(
        "existing_resource_link"
    )
    await db.execute(sa_delete(MarketplaceResourceLink).where(
        MarketplaceResourceLink.entity_id == ws.entity_id,
        home_shared_component_link,
        sa_exists(sa_select(existing_resource_link.c.id).where(
            existing_resource_link.c.id != MarketplaceResourceLink.id,
            existing_resource_link.c.entity_id == MarketplaceResourceLink.entity_id,
            existing_resource_link.c.relationship == MarketplaceResourceLink.relationship,
            existing_resource_link.c.scope_type == SCOPE_RESOURCE,
            existing_resource_link.c.scope_id
            == MarketplaceResourceLink.local_resource_id,
            existing_resource_link.c.local_resource_type
            == MarketplaceResourceLink.local_resource_type,
            existing_resource_link.c.local_resource_id
            == MarketplaceResourceLink.local_resource_id,
        )),
    ))
    await db.execute(
        sa_update(MarketplaceResourceLink)
        .where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            home_shared_component_link,
        )
        .values(
            scope_type=SCOPE_RESOURCE,
            scope_id=MarketplaceResourceLink.local_resource_id,
        )
    )
    await db.execute(sa_delete(MarketplaceResourceLink).where(
        MarketplaceResourceLink.entity_id == ws.entity_id,
        sa_or_(
            sa_and_(
                MarketplaceResourceLink.local_resource_type == "workspace",
                MarketplaceResourceLink.local_resource_id == workspace_id,
            ),
            sa_and_(
                MarketplaceResourceLink.local_resource_type == "agent",
                MarketplaceResourceLink.local_resource_id.in_(exclusive_agent_ids),
            ),
            sa_and_(
                MarketplaceResourceLink.local_resource_type == "skill",
                MarketplaceResourceLink.local_resource_id.in_(exclusive_skill_ids),
            ),
            sa_and_(
                MarketplaceResourceLink.local_resource_type == "workflow",
                MarketplaceResourceLink.local_resource_id.in_(exclusive_workflow_ids),
            ),
            sa_and_(
                MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                MarketplaceResourceLink.scope_id == workspace_id,
            ),
        ),
    ))

    for model, shared_ids, entity_visible_ids in [
        (Agent, shared_agent_ids, entity_visible_agent_ids),
        (Skill, shared_skill_ids, entity_visible_skill_ids),
        (WorkflowDefinition, shared_workflow_ids, entity_visible_workflow_ids),
    ]:
        if not shared_ids:
            continue
        scoped_ids = shared_ids - entity_visible_ids
        if entity_visible_ids:
            await db.execute(
                sa_update(model)
                .where(
                    model.id.in_(entity_visible_ids),
                    model.visibility != Visibility.PUBLIC,
                )
                .values(visibility=Visibility.ENTITY)
            )
        if scoped_ids:
            await db.execute(
                sa_update(model)
                .where(
                    model.id.in_(scoped_ids),
                    model.visibility == Visibility.WORKSPACE,
                )
                .values(visibility=Visibility.PRIVATE)
            )
        await db.execute(
            sa_update(model)
            .where(model.id.in_(shared_ids))
            .values(workspace_id=None)
        )

    # Work leases (workspace_id)
    await db.execute(sa_delete(WorkLease).where(WorkLease.workspace_id == workspace_id))

    # Workspace-specific human participation and configuration rows are not
    # linked by database FKs, so purge them explicitly. Entity-level defaults
    # (workspace_id IS NULL) intentionally remain available to other scopes.
    await db.execute(sa_delete(ParticipantProfile).where(
        ParticipantProfile.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(HumanCommitment).where(
        HumanCommitment.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(HumanContribution).where(
        HumanContribution.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(CustomFieldDefinition).where(
        CustomFieldDefinition.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ChannelPairingCode).where(
        ChannelPairingCode.workspace_id == workspace_id,
    ))

    # Workspace-owned public site runtime and events have no FK to Workspace.
    # Remove events by both explicit scope and their owned Site ids so legacy
    # rows without a workspace_id cannot outlive the site being purged.
    workspace_site_ids = sa_select(Site.id).where(
        Site.workspace_id == workspace_id,
    )
    await db.execute(sa_delete(SiteEvent).where(sa_or_(
        SiteEvent.workspace_id == workspace_id,
        SiteEvent.site_id.in_(workspace_site_ids),
    )))
    await db.execute(sa_delete(Site).where(
        Site.workspace_id == workspace_id,
    ))

    # Workspace ledgers are append-only during normal operation, but their
    # Workspace-scoped rows must not outlive a hard purge.
    await db.execute(sa_delete(AgentLearningCandidate).where(
        AgentLearningCandidate.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(WorkspaceEvent).where(
        WorkspaceEvent.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(RuntimeEvidence).where(
        RuntimeEvidence.workspace_id == workspace_id,
    ))
    workspace_credit_usage_ids = sa_select(CreditUsageLog.id).where(
        CreditUsageLog.workspace_id == workspace_id,
    )
    await db.execute(sa_delete(CreditUsageAllocation).where(
        CreditUsageAllocation.usage_log_id.in_(workspace_credit_usage_ids),
    ))
    await db.execute(sa_delete(CreditUsageLog).where(
        CreditUsageLog.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(CreditReservation).where(
        CreditReservation.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(TokenUsageLog).where(
        TokenUsageLog.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ToolCallLog).where(
        ToolCallLog.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(RuntimeEventLog).where(
        RuntimeEventLog.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ProposalItemRecord).where(
        ProposalItemRecord.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ProposalRecord).where(
        ProposalRecord.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(HitlRequest).where(
        HitlRequest.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(AutomationRevision).where(
        AutomationRevision.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(Experiment).where(
        Experiment.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ConsolidationReport).where(
        ConsolidationReport.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(ReviewRun).where(
        ReviewRun.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(TwilioVoiceCallSession).where(
        TwilioVoiceCallSession.workspace_id == workspace_id,
    ))

    # Durable runtime rows do not have database foreign keys to Workspace.
    # Resolve the complete run tree first so delegated child runs and their
    # sandbox/outbox state are removed with the purged workspace.
    workspace_runtime_root_ids = sa_select(RuntimeRun.root_run_id).where(
        RuntimeRun.workspace_id == workspace_id,
    )
    workspace_runtime_run_ids = sa_select(RuntimeRun.id).where(
        sa_or_(
            RuntimeRun.workspace_id == workspace_id,
            RuntimeRun.root_run_id.in_(workspace_runtime_root_ids),
        )
    )
    workspace_reservation_ids = sa_select(SandboxReservation.id).where(
        sa_or_(
            SandboxReservation.runtime_run_id.in_(workspace_runtime_run_ids),
            SandboxReservation.root_run_id.in_(workspace_runtime_root_ids),
        )
    )
    await db.execute(sa_delete(SandboxInstance).where(
        sa_or_(
            SandboxInstance.runtime_run_id.in_(workspace_runtime_run_ids),
            SandboxInstance.root_run_id.in_(workspace_runtime_root_ids),
        )
    ))
    await db.execute(sa_delete(RuntimeOutboxEvent).where(
        sa_or_(
            RuntimeOutboxEvent.aggregate_id.in_(workspace_runtime_run_ids),
            RuntimeOutboxEvent.aggregate_id.in_(workspace_reservation_ids),
        )
    ))
    await db.execute(sa_delete(SandboxReservation).where(
        sa_or_(
            SandboxReservation.id.in_(workspace_reservation_ids),
            SandboxReservation.runtime_run_id.in_(workspace_runtime_run_ids),
            SandboxReservation.root_run_id.in_(workspace_runtime_root_ids),
        )
    ))
    await db.execute(sa_delete(RuntimeRun).where(
        sa_or_(
            RuntimeRun.id.in_(workspace_runtime_run_ids),
            RuntimeRun.root_run_id.in_(workspace_runtime_root_ids),
        )
    ))
    await db.execute(sa_delete(WorkspaceOperationDraft).where(
        WorkspaceOperationDraft.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(WorkspaceWorkBatch).where(
        WorkspaceWorkBatch.workspace_id == workspace_id,
    ))

    # ── Level 2: Mid-level rows with workspace_id ──
    # Notification owns its outbox and actionable delivery rows through
    # ON DELETE CASCADE. Keep the metadata fallback for rows created before
    # Notification.workspace_id was introduced.
    await db.execute(sa_delete(Notification).where(sa_or_(
        Notification.workspace_id == workspace_id,
        sa_and_(
            Notification.workspace_id.is_(None),
            Notification.meta["workspace_id"].astext == workspace_id,
        ),
    )))

    workspace_goal_ids = sa_select(Goal.id).where(Goal.workspace_id == workspace_id)
    workspace_task_ids = sa_select(Task.id).where(Task.workspace_id == workspace_id)
    workspace_scheduled_job_ids = sa_select(ScheduledJob.job_id).where(
        ScheduledJob.workspace_id == workspace_id,
    )
    await db.execute(sa_delete(GoalMeasurement).where(
        GoalMeasurement.goal_id.in_(workspace_goal_ids),
    ))
    await db.execute(sa_delete(GoalTaskLink).where(sa_or_(
        GoalTaskLink.goal_id.in_(workspace_goal_ids),
        GoalTaskLink.task_id.in_(workspace_task_ids),
    )))
    await db.execute(sa_delete(ScheduledJobRun).where(
        ScheduledJobRun.job_id.in_(workspace_scheduled_job_ids),
    ))
    await db.execute(sa_delete(WorkflowActionGrant).where(
        WorkflowActionGrant.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(WorkflowRun).where(
        WorkflowRun.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(WorkflowProject).where(
        WorkflowProject.workspace_id == workspace_id,
    ))

    await db.execute(sa_delete(WorkspaceStatObservation).where(
        WorkspaceStatObservation.workspace_id == workspace_id,
    ))
    await db.execute(sa_delete(WorkspaceStat).where(
        WorkspaceStat.workspace_id == workspace_id,
    ))

    for model in [
        Task, Conversation, ExecutionPlan,
        GovernancePolicy, GovernanceRevision, AgentExecution,
        WorkspaceStaff, AgentSubscription, WorkspaceActivity,
        Goal, ScheduledJob, AgentMemory,
        WorkflowBinding,
        DocumentGroup, Channel, ChannelConfig, Announcement,
        WorkflowDefinition, Skill, Agent,
    ]:
        await db.execute(sa_delete(model).where(model.workspace_id == workspace_id))

    # ── Level 3: Workspace itself ──
    from packages.core.services.workspace_artifact_purge import (
        purge_workspace_artifacts,
    )

    await purge_workspace_artifacts(db, ws)
    await db.delete(ws)
    await db.flush()
    return True


# Back-compat alias: a few callers import ``delete_workspace``. Route
# them to the soft-delete path so behaviour stays consistent across
# the codebase. New callers should pick the explicit name.
delete_workspace = soft_delete_workspace
