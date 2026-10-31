"""One contract for the Tasks a conversation drafts and later starts.

Every chat-reachable way to create a Workspace Task (the Workspace
``create_task`` action, the ``manor`` ``create_task`` action, and an applied
operation draft's ``task.prepare``) records the conversation it came from on
``Task.conversation_id``. That host-written column is what lets a later
"start it" find the drafted Task instead of creating a copy, whichever path
drafted it. Starting goes through ``start_pending_task`` only: the same gates
(executor, dependencies) and the same durable start intent everywhere.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import CancelSource, TaskRuntimeKind, TaskStatus
from packages.core.models.task import Task

# What an Agent is told once a Task starts: it delivers its own output.
STARTED_NEXT = (
    "The Task now runs on its own with its Agents and delivers its own output. "
    "Do not do its work in Chat; report it with an execution_summary reference."
)
# What an Agent is told when a drafted Task is left pending.
DRAFTED_NEXT = "Start it later with the Workspace start_task action and its task_id."


async def pending_chat_draft(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None,
    conversation_id: str | None, title: str,
) -> Task | None:
    """A pending Task with this exact title drafted earlier in this conversation."""
    if not conversation_id or not workspace_id or not title.strip():
        return None
    return (await db.execute(
        select(Task).where(
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
            Task.status == TaskStatus.PENDING.value,
            Task.title == title.strip(),
            Task.conversation_id == conversation_id,
        ).order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()


async def open_chat_draft_preparing(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None,
    conversation_id: str | None, title: str,
) -> Any | None:
    """An open operation draft from this conversation that prepares this Task.

    Creating the Task beside it leaves a review card whose approval would
    later make a second copy.
    """
    from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
    from packages.core.models.workspace import WorkspaceOperationDraft

    if not conversation_id or not workspace_id or not title.strip():
        return None
    drafts = (await db.execute(
        select(WorkspaceOperationDraft).where(
            WorkspaceOperationDraft.entity_id == entity_id,
            WorkspaceOperationDraft.workspace_id == workspace_id,
            WorkspaceOperationDraft.status == WorkspaceOperationDraftStatus.OPEN,
            WorkspaceOperationDraft.origin_conversation_id == conversation_id,
        ).order_by(WorkspaceOperationDraft.created_at.desc())
    )).scalars().all()
    for draft in drafts:
        for patch in draft.patches or []:
            payload = patch.get("payload") if isinstance(patch, dict) else None
            if (patch.get("op") == "task.prepare" and isinstance(payload, dict)
                    and str(payload.get("title") or "").strip() == title.strip()):
                return draft
    return None


def draft_only_prepares(draft: Any, title: str) -> bool:
    """The draft's one change is preparing this Task, so creating it supersedes the draft."""
    patches = draft.patches or []
    return len(patches) == 1 and isinstance(patches[0], dict) and patches[0].get("op") == "task.prepare" and (
        str((patches[0].get("payload") or {}).get("title") or "").strip() == title.strip()
    )


def drafted_in_open_draft(draft: Any) -> dict[str, Any]:
    """The refusal for a Task prepared in an open draft that also changes other things."""
    return {
        "error": "task_in_open_draft",
        "draft_id": draft.id,
        "message": (
            "This conversation prepared this Task in an open Workspace draft that also changes "
            "other things. Do not create it separately: approving that draft creates it. To "
            "create only the Task, patch the Task out of the draft first."
        ),
    }


def already_drafted(task: Task) -> dict[str, Any]:
    """The refusal for a second Task where a drafted one should be started."""
    return {
        "error": "task_already_drafted",
        "task_id": task.id,
        "message": (
            "This Task was drafted in this conversation and is pending. Start it: "
            "Workspace start_task with params {task_id}."
        ),
    }


async def resolve_task_start(
    db: AsyncSession, task: Task, details: dict[str, Any],
) -> tuple[Any, dict[str, Any] | None]:
    """How a pending Task starts its own run, or why it cannot.

    ``details`` (the row's next details) gains the dependency state.
    """
    from packages.core.services.task_dependencies import details_with_dependency_state
    from packages.core.services.task_execution import resolve_task_execution

    if task.status != TaskStatus.PENDING:
        return None, {
            "error": "task_not_startable",
            "status": task.status,
            "message": (
                "Only a pending Task starts here. A running or finished Task needs no start; "
                "a failed or blocked one resumes through its own recovery, not a new start."
            ),
        }
    details.update(await details_with_dependency_state(db, task, details))
    if details.get("dependency_status") not in (None, "completed"):
        return None, {
            "error": "task_dependencies_pending",
            "message": "This Task waits for its predecessor Tasks; it starts when they complete.",
            "dependency_statuses": details.get("dependency_statuses"),
        }
    execution = await resolve_task_execution(db, task)
    if execution.runtime_kind is TaskRuntimeKind.MANUAL:
        return None, {
            "error": "task_executor_unavailable",
            "message": "Assign an active workspace Agent before starting this task.",
        }
    if not execution.is_dispatchable:
        return None, {
            "error": "task_not_dispatchable",
            "message": "This Task runs as a foreground session; the user starts it from the Task page.",
        }
    return execution, None


async def start_pending_task(
    db: AsyncSession, task: Task, execution: Any, *, user_id: str | None, manual_execution: bool,
) -> Task:
    """Move a resolved pending Task to in_progress and stage its durable start.

    The start intent commits with the caller's transaction: a broker outage
    delays the run but cannot strand an in-progress Task without a start.
    """
    from packages.core.services import task_service
    from packages.core.services.task_execution import stage_task_execution_intent

    started = await task_service.update_task(
        db, task.id, task.entity_id,
        user_id=user_id,
        manual_execution=manual_execution,
        cancel_source=CancelSource.SYSTEM.value,
        dispatch_plan=False,
        status=TaskStatus.IN_PROGRESS.value,
    ) or task
    await stage_task_execution_intent(db, started, execution)
    return started


async def record_chat_task_effect(
    db: AsyncSession, task: Task, *, effect: str, origin_user_message_id: str,
    user_id: str, conversation_id: str,
) -> None:
    """Ledger the Task a Workspace Chat turn created or started, caused by its user message.

    The host reads these to write the turn's verified Task status, so a
    started Task is reported by the server rather than by the model's prose.
    """
    from packages.core.ledger.event_types import EXECUTION_REQUESTED
    from packages.core.ledger.service import record_event

    key = (
        f"chat-task-created:{task.id}" if effect == "created"
        else f"chat-task-{effect}:{task.id}:{origin_user_message_id}"
    )
    await record_event(
        db, entity_id=task.entity_id, workspace_id=task.workspace_id,
        event_type=EXECUTION_REQUESTED, source_kind="task", source_id=task.id,
        idempotency_key=key, causation_id=origin_user_message_id, root_execution_id=task.id,
        actor_kind="user", actor_id=user_id,
        payload={
            "kind": f"workspace_chat_{'create' if effect == 'created' else effect}",
            "entity_id": task.entity_id, "workspace_id": task.workspace_id, "user_id": user_id,
            "conversation_id": conversation_id, "origin_user_message_id": origin_user_message_id,
        },
    )
