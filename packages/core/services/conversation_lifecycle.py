from __future__ import annotations

from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_records import get_conversation


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
) -> Conversation:
    await ensure_active_workspace(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )

    if conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
        )
        conv = result.scalar_one_or_none()
        if conv:
            if not conversation_matches_workspace_request(
                conv,
                workspace_id=workspace_id,
                thread_ref_kind=thread_ref_kind,
                thread_ref_id=thread_ref_id,
            ):
                raise PermissionError("Conversation not found")
            if conv.workspace_id is None and conv.user_id != user_id:
                raise PermissionError("Conversation not found")
            return conv
        raise LookupError("Conversation not found")

    if workspace_id and thread_ref_kind and thread_ref_id and not conversation_id:
        from packages.core.workspace_chat.service import spawn_thread

        return await spawn_thread(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            title=title,
        )

    if workspace_id and not conversation_id:
        from packages.core.workspace_chat.service import ensure_main_conversation

        return await ensure_main_conversation(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )

    conv = Conversation(
        id=conversation_id or generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        title=title,
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
) -> bool:
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        return False

    from packages.core.models.conversation_share import ConversationShare
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import CredentialSublease, WorkLease, WorkerActivityLog

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
    await db.execute(
        delete(Message).where(Message.conversation_id == conversation_id)
    )
    await db.delete(conv)
    await db.flush()
    return True
