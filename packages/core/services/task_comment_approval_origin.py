"""Server-owned origins for foreground comments on ordinary Workspace Tasks.

A comment thread is not the Task's background execution conversation.  Never
rewrite that binding, or interpret a missing worker checkpoint as foreground.
"""
from __future__ import annotations

from sqlalchemy import select

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.approvals import ApprovalOriginKind
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.constants.task import TaskType
from packages.core.models.task import Conversation, Task
from packages.core.services.task_state_machine import TERMINAL_STATUSES
from packages.core.services.workspace_access import lock_workspace_access_boundary
from packages.core.services.workspace_autonomy import workspace_allows_manual_work


TASK_COMMENT_ORIGIN_CONTEXT_KEY = "task_comment_origin_v1"


class TaskCommentApprovalOriginError(ValueError):
    """The foreground request no longer has its exact executable origin."""


def has_task_comment_origin(request) -> bool:
    context = request.context if isinstance(request.context, dict) else {}
    return TASK_COMMENT_ORIGIN_CONTEXT_KEY in context


def task_comment_origin_matches_request(request) -> bool:
    context = request.context if isinstance(request.context, dict) else {}
    expected = {
        "entity_id": request.entity_id,
        "workspace_id": request.workspace_id,
        "task_id": request.origin_task_id,
        "conversation_id": request.origin_conversation_id,
    }
    return bool(
        request.origin_kind == ApprovalOriginKind.TOOL_CALL
        and all(expected.values())
        and context.get(TASK_COMMENT_ORIGIN_CONTEXT_KEY) == expected
        and not any(key.startswith("task_runtime_") for key in context)
    )


def is_task_comment_runtime(runtime_envelope) -> bool:
    return bool(
        runtime_envelope is not None
        and runtime_envelope.surface == ChatSurface.TASK_COMMENT_THREAD
        and runtime_envelope.profile == RuntimeProfile.TASK_WORKER_FEEDBACK
    )


async def lock_task_comment_workspace(db, *, entity_id, workspace_id):
    """Take lifecycle scope before Task advisory/request/Task row locks."""
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )

    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace = await lock_workspace_access_boundary(
        db, entity_id=entity_id, workspace_id=workspace_id,
    )
    if not workspace_allows_manual_work(workspace):
        raise TaskCommentApprovalOriginError("Task comment Workspace is unavailable")
    return workspace


async def lock_task_comment_turn_boundary(
    db, *, entity_id, conversation_id=None, workspace_id=None, task_id=None,
):
    """Fence foreground origins before legacy adapters lock Conversation.

    Request columns also fence a stale/retargeted thread's prior origin. This
    acquires no Task row and grants no authority: the request validator still
    checks the current exact binding after its request row is locked.
    """
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle
    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    statements = []
    if workspace_id and task_id:
        statements.append(select(Task.workspace_id, Task.id).where(
            Task.id == task_id, Task.entity_id == entity_id,
            Task.workspace_id == workspace_id, Task.task_type == TaskType.GENERAL.value,
        ))
    if conversation_id:
        statements.append(
            select(Task.workspace_id, Task.id).join(
                Conversation, Conversation.thread_ref_id == Task.id,
            ).where(
                Conversation.id == conversation_id, Conversation.entity_id == entity_id,
                Conversation.scope == "workspace_thread", Conversation.thread_ref_kind == "task",
                Conversation.workspace_id == Task.workspace_id,
                Task.entity_id == entity_id, Task.task_type == TaskType.GENERAL.value,
            )
        )
        statements.append(
            select(HitlRequest.workspace_id, HitlRequest.origin_task_id).where(
                HitlRequest.entity_id == entity_id,
                HitlRequest.origin_conversation_id == conversation_id,
                HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL,
                HitlRequest.origin_task_id.is_not(None),
                HitlRequest.workspace_id.is_not(None),
                HitlRequest.context.has_key(TASK_COMMENT_ORIGIN_CONTEXT_KEY),
            ).distinct()
        )
    scopes = set()
    for statement in statements:
        scopes.update((await db.execute(statement)).all())
    if not scopes:
        return
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    # A prior lifecycle writer may have changed/deleted the thread while we
    # waited. Fence its freshly read scope, not the pre-lock snapshot.
    scopes = set()
    for statement in statements:
        scopes.update((await db.execute(statement)).all())
    for workspace_id in sorted({scope[0] for scope in scopes}):
        await lock_task_comment_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
    for task_id in sorted({scope[1] for scope in scopes}):
        await lock_task_runtime_approval_boundary(db, task_id=task_id)


def _runtime_matches(runtime_envelope, *, origin, user_id):
    if not is_task_comment_runtime(runtime_envelope) or not user_id:
        return False
    principal = runtime_envelope.principal
    return bool(
        principal.kind == RuntimePrincipalKind.WORKSPACE_MEMBER
        and principal.actor_user_id == user_id
        and principal.execution_user_id == user_id
        and principal.entity_id == origin["entity_id"]
        and principal.workspace_id == origin["workspace_id"]
        and runtime_envelope.user_id == user_id
        and all(
            getattr(runtime_envelope, key, None) == origin[key]
            for key in ("entity_id", "workspace_id", "task_id", "conversation_id")
        )
        and runtime_envelope.thread_ref_kind == "task"
        and runtime_envelope.thread_ref_id == origin["task_id"]
    )


async def _validate_scope(db, *, origin):
    from packages.core.services.task_runtime_approvals import (
        lock_task_runtime_approval_boundary,
    )

    # The caller already owns entity lifecycle / Workspace scope, acquired
    # before the Task advisory lock and any request row lock.
    await lock_task_runtime_approval_boundary(db, task_id=origin["task_id"])
    task = (await db.execute(
        select(Task).where(
            Task.id == origin["task_id"],
            Task.entity_id == origin["entity_id"],
            Task.workspace_id == origin["workspace_id"],
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        task is None
        or task.task_type != TaskType.GENERAL.value
        or task.status in TERMINAL_STATUSES
    ):
        raise TaskCommentApprovalOriginError("Task comment origin is no longer executable")
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == origin["conversation_id"],
            Conversation.entity_id == origin["entity_id"],
            Conversation.workspace_id == origin["workspace_id"],
            Conversation.scope == "workspace_thread",
            Conversation.status == "active",
            Conversation.thread_ref_kind == "task",
            Conversation.thread_ref_id == origin["task_id"],
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if conversation is None:
        raise TaskCommentApprovalOriginError("Task comment thread binding changed")


async def build_task_comment_origin(
    db, *, task, conversation_id, entity_id, workspace_id, user_id, runtime_envelope,
):
    """Mint only from a trusted foreground envelope and durable exact thread."""
    if not is_task_comment_runtime(runtime_envelope):
        return None
    origin = {
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "task_id": task.id if task is not None else None,
        "conversation_id": conversation_id,
    }
    if not all(origin.values()) or not _runtime_matches(
        runtime_envelope, origin=origin, user_id=user_id,
    ):
        raise TaskCommentApprovalOriginError("Task comment runtime binding is invalid")
    await _validate_scope(db, origin=origin)
    return origin


async def validate_task_comment_approval_origin(
    db, *, request, runtime_envelope=None, require_runtime=False,
):
    """Revalidate explicit persisted lineage; never downgrade a worker gate."""
    context = request.context if isinstance(request.context, dict) else {}
    origin = context.get(TASK_COMMENT_ORIGIN_CONTEXT_KEY)
    if not task_comment_origin_matches_request(request):
        raise TaskCommentApprovalOriginError("Task comment approval lineage is invalid")
    if require_runtime and not _runtime_matches(
        runtime_envelope, origin=origin,
        user_id=getattr(runtime_envelope, "user_id", None),
    ):
        raise TaskCommentApprovalOriginError("Task comment continuation requires its foreground runtime")
    await _validate_scope(db, origin=origin)
    return True
