from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.constants.conversation import ConversationSurfaceKind
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_records import get_conversation
from packages.core.services.conversation_surfaces import (
    AiEditTargetIdentity,
    ConversationSurfaceMetadataFactory,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_references,
)


async def ensure_active_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> None:
    """Fail closed before binding a conversation to a workspace runtime."""

    if not workspace_id:
        return
    from packages.core.models.workspace import Workspace

    workspace = (await db.execute(
        select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not workspace:
        raise PermissionError("Workspace not found")


def conversation_matches_workspace_request(
    conv: Conversation,
    *,
    workspace_id: str | None,
    thread_ref_kind: str | None,
    thread_ref_id: str | None,
) -> bool:
    """Return whether an existing conversation belongs to the requested scope."""

    if not workspace_id:
        return True
    if conv.workspace_id != workspace_id:
        return False
    if thread_ref_kind or thread_ref_id:
        return (
            conv.thread_ref_kind == thread_ref_kind
            and conv.thread_ref_id == thread_ref_id
        )
    return True


async def get_or_create_conversation(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    *,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    title: str | None = None,
    conversation_surface: ConversationSurfaceKind | None = None,
    ai_edit_target: AiEditTargetIdentity | None = None,
) -> Conversation:
    await ensure_active_workspace(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if (
        conversation_surface is ConversationSurfaceKind.AI_EDIT
        and ai_edit_target is None
    ):
        raise ValueError("AI Edit requires a target")

    if conversation_id:
        conversation_query = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
        if conversation_surface is ConversationSurfaceKind.AI_EDIT:
            # Serialize a resumed editor turn with the TTL cleanup sweep and
            # with any concurrent turn that targets the same editing session.
            conversation_query = conversation_query.with_for_update()
        result = await db.execute(
            conversation_query
        )
        conv = result.scalar_one_or_none()
        if conv:
            if (
                conversation_surface is not None
                and not ConversationSurfaceMetadataFactory.matches(
                    conv.meta,
                    conversation_surface,
                )
            ):
                raise PermissionError("Conversation not found")
            if (
                conversation_surface is ConversationSurfaceKind.AI_EDIT
                and ai_edit_target is not None
                and not ConversationSurfaceMetadataFactory.matches_ai_edit_target(
                    conv.meta,
                    ai_edit_target,
                )
            ):
                raise PermissionError("Conversation not found")
            if not conversation_matches_workspace_request(
                conv,
                workspace_id=workspace_id,
                thread_ref_kind=thread_ref_kind,
                thread_ref_id=thread_ref_id,
            ):
                raise PermissionError("Conversation not found")
            if conv.workspace_id is None and conv.user_id != user_id:
                raise PermissionError("Conversation not found")
            if conv.workspace_id and conv.thread_ref_kind == "task":
                from packages.core.services.task_session import (
                    bind_task_session_conversation,
                )

                await bind_task_session_conversation(db, conv)
            if conversation_surface is not None:
                # Reassigning the canonical metadata also refreshes updated_at,
                # which is the inactivity clock for temporary AI Edit sessions.
                conv.meta = ConversationSurfaceMetadataFactory.build(
                    conversation_surface,
                    current=conv.meta,
                    ai_edit_target=ai_edit_target,
                )
                if conversation_surface is ConversationSurfaceKind.AI_EDIT:
                    conv.updated_at = datetime.now(timezone.utc)
            return conv
        raise LookupError("Conversation not found")

    if workspace_id and thread_ref_kind and thread_ref_id and not conversation_id:
        from packages.core.workspace_chat.service import spawn_thread

        conv = await spawn_thread(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            title=title,
        )
        if thread_ref_kind == "task":
            from packages.core.services.task_session import (
                bind_task_session_conversation,
            )

            await bind_task_session_conversation(db, conv)
        return conv

    if workspace_id and not conversation_id:
        from packages.core.workspace_chat.service import ensure_main_conversation

        return await ensure_main_conversation(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )

    if agent_id and not is_master_agent(agent_id):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(agent_id,),
        )

    conversation_metadata = {}
    if conversation_surface is not None:
        conversation_metadata = ConversationSurfaceMetadataFactory.build(
            conversation_surface,
            ai_edit_target=ai_edit_target,
        )

    conv = Conversation(
        id=conversation_id or generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        title=title,
        meta=conversation_metadata,
    )
    db.add(conv)
    await db.flush()
    return conv


async def rename_conversation(
    db: AsyncSession,
    conversation_id: str,
    entity_id: str,
    title: str,
) -> Optional[Conversation]:
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        return None
    clean_title = (title or "").strip()[:500]
    conv.title = clean_title or "Untitled"
    await db.flush()
    await db.refresh(conv)
    return conv


async def delete_conversation(
    db: AsyncSession,
    conversation_id: str,
    entity_id: str,
    *,
    cancelled_runtime_runs: list[RuntimeRun] | None = None,
) -> bool:
    # Serialize deletion with resumed AI Edit turns. Once this lock is held,
    # no concurrent turn can create a new RuntimeRun after the cancellation
    # snapshot and leave an orphan execution behind.
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if not conv:
        return False
    is_ai_edit = ConversationSurfaceMetadataFactory.matches(
        conv.meta,
        ConversationSurfaceKind.AI_EDIT,
    )

    # Runtime cancellation owns the canonical reservation -> run lock order.
    # Read identities here without pre-locking RuntimeRun rows so a concurrent
    # direct cancellation cannot deadlock with conversation deletion.
    root_run_ids = list((await db.execute(
        select(RuntimeRun.id).where(
            RuntimeRun.entity_id == entity_id,
            RuntimeRun.conversation_id == conversation_id,
            RuntimeRun.parent_run_id.is_(None),
            RuntimeRun.status.in_(RuntimeRunStatus.active_root()),
        ).order_by(RuntimeRun.id.asc())
    )).scalars().all())
    if root_run_ids:
        from packages.core.services.runtime_run_service import request_runtime_run_cancel

        for run_id in root_run_ids:
            cancelled = await request_runtime_run_cancel(
                db,
                run_id=run_id,
                entity_id=entity_id,
                reason="conversation_deleted",
            )
            if cancelled_runtime_runs is not None:
                cancelled_runtime_runs.append(cancelled)

    if is_ai_edit:
        # AI Edit runs can contain the entire temporary editor document. Keep
        # the durable run/status audit row, but remove document-bearing data as
        # part of the same deletion transaction as the session and messages.
        await db.execute(
            update(RuntimeRun).where(
                RuntimeRun.entity_id == entity_id,
                RuntimeRun.conversation_id == conversation_id,
            ).values(
                assistant_message_id=None,
                execution_payload={
                    "redacted": True,
                    "reason": "ai_edit_conversation_deleted",
                },
                checkpoint=None,
                result=None,
                error=None,
            )
        )

    from packages.core.models.conversation_share import ConversationShare
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.worker import CredentialSublease, WorkLease, WorkerActivityLog
    from packages.core.services.chat_feedback import (
        COMPLETION_FEEDBACK_EVIDENCE_TYPES,
    )

    step_rows = (
        await db.execute(
            select(ExecutionStep.id, ExecutionStep.plan_id, ExecutionStep.params, ExecutionPlan.task_id)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
            .where(
                ExecutionStep.entity_id == entity_id,
                ExecutionPlan.entity_id == entity_id,
                ExecutionPlan.task_id.is_(None),
            )
        )
    ).all()
    step_ids: list[str] = []
    plan_ids: set[str] = set()
    for step_id, plan_id, params, _task_id in step_rows:
        if isinstance(params, dict) and str(params.get("conversation_id") or "") == conversation_id:
            step_ids.append(step_id)
            plan_ids.add(plan_id)

    if step_ids:
        lease_ids = list((
            await db.execute(
                select(WorkLease.id).where(
                    WorkLease.entity_id == entity_id,
                    WorkLease.step_id.in_(step_ids),
                )
            )
        ).scalars().all())
        if lease_ids:
            await db.execute(
                delete(CredentialSublease).where(CredentialSublease.work_lease_id.in_(lease_ids))
            )
            await db.execute(
                delete(WorkerActivityLog).where(WorkerActivityLog.lease_id.in_(lease_ids))
            )
            await db.execute(
                delete(WorkLease).where(WorkLease.id.in_(lease_ids))
            )
        await db.execute(
            delete(ExecutionStep).where(ExecutionStep.id.in_(step_ids))
        )
    if plan_ids:
        await db.execute(
            delete(ExecutionPlan).where(
                ExecutionPlan.entity_id == entity_id,
                ExecutionPlan.task_id.is_(None),
                ExecutionPlan.id.in_(plan_ids),
            )
        )
    await db.execute(
        delete(ConversationShare).where(
            ConversationShare.conversation_id == conversation_id,
            ConversationShare.entity_id == entity_id,
        )
    )
    # Keep the same parent-to-child lock order as feedback persistence:
    # Message first, then its cascading feedback rows, then evidence.
    await db.execute(
        delete(Message).where(Message.conversation_id == conversation_id)
    )
    # RuntimeEvidence intentionally has no FK to conversations/messages. It is
    # a projection of the feedback row and must follow the same lifecycle.
    await db.execute(
        delete(RuntimeEvidence).where(
            RuntimeEvidence.conversation_id == conversation_id,
            RuntimeEvidence.evidence_type.in_(
                COMPLETION_FEEDBACK_EVIDENCE_TYPES
            ),
        )
    )
    await db.delete(conv)
    await db.flush()
    return True


AI_EDIT_SESSION_TTL = timedelta(hours=24)


async def cleanup_expired_ai_edit_conversations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
    cancelled_runtime_runs: list[RuntimeRun] | None = None,
) -> int:
    """Delete bounded, inactive AI Edit sessions through normal lifecycle cleanup."""

    cutoff = (now or datetime.now(timezone.utc)) - AI_EDIT_SESSION_TTL
    conversations = list((
        await db.execute(
            select(Conversation)
            .where(
                Conversation.meta[
                    ConversationSurfaceMetadataFactory.META_KEY
                ].astext
                == ConversationSurfaceKind.AI_EDIT.value,
                func.coalesce(Conversation.updated_at, Conversation.created_at) < cutoff,
            )
            .order_by(
                func.coalesce(Conversation.updated_at, Conversation.created_at),
                Conversation.id,
            )
            .limit(max(1, min(limit, 500)))
            .with_for_update(skip_locked=True)
        )
    ).scalars().all())
    deleted_count = 0
    for conversation in conversations:
        if await delete_conversation(
            db,
            conversation.id,
            conversation.entity_id,
            cancelled_runtime_runs=cancelled_runtime_runs,
        ):
            deleted_count += 1
    return deleted_count
