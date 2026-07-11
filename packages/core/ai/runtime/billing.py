from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.sources import (
    RUNTIME_CHAT_SOURCE,
    RUNTIME_PLAN_EXECUTOR_SOURCE,
    RUNTIME_SYSTEM_SOURCE,
)

_BILLING_CONTEXT_KEYS = {
    "user_id",
    "agent_id",
    "workspace_id",
    "conversation_id",
    "suppress",
    "byok",
}

_SYSTEM_TASK_USER_IDS = {"ai-agent"}


@dataclass(frozen=True)
class RuntimeBillingContextHandle:
    """Opaque handle for a Runtime-owned low-level billing context binding."""

    context: Any
    token: Any


@dataclass(frozen=True)
class RuntimeResolvedBillingScope:
    """Resolved owner scope for Runtime billing helpers."""

    entity_id: str | None
    workspace_id: str | None = None
    user_id: str | None = None
    byok: bool = False


def _normalized_billable_user_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text in _SYSTEM_TASK_USER_IDS:
        return None
    return text


def _row_value(row: Any | None, key: str, index: int | None = None) -> Any | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    value = getattr(row, key, None)
    if value is not None:
        return value
    if index is not None:
        try:
            return row[index]
        except Exception:
            return None
    return None


def runtime_task_billable_user_id(task_or_row: Any | None) -> str | None:
    """Return the real user whose preferences and credits own task execution."""

    for key in ("creator_id", "owner_id", "assignee_id", "user_id"):
        user_id = _normalized_billable_user_id(_row_value(task_or_row, key))
        if user_id:
            return user_id
    return None


def runtime_ensure_billing_context(
    entity_id: str,
    source: str = RUNTIME_SYSTEM_SOURCE,
    **kwargs: Any,
) -> None:
    """Set the LLM billing context through the Runtime boundary."""

    from packages.core.ai.llm_client import ensure_billing_context

    billing_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in _BILLING_CONTEXT_KEYS
    }
    ensure_billing_context(entity_id, source=source, **billing_kwargs)


def runtime_llm_billing_context(
    entity_id: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    source: str = RUNTIME_SYSTEM_SOURCE,
    byok: bool = False,
):
    """Return the async LLM billing context manager through the Runtime boundary."""

    from packages.core.ai.llm_client import llm_billing_context

    return llm_billing_context(
        entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        source=source,
        byok=byok,
    )


def runtime_set_suppressed_billing_context(
    entity_id: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    source: str = RUNTIME_CHAT_SOURCE,
) -> RuntimeBillingContextHandle:
    """Bind a suppressing billing context for runtimes that aggregate usage themselves."""

    from packages.core.ai.llm_client import LLMBillingContext, _billing_ctx_var

    context = LLMBillingContext(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        source=source,
        suppress=True,
    )
    return RuntimeBillingContextHandle(
        context=context,
        token=_billing_ctx_var.set(context),
    )


def runtime_release_billing_context(handle: RuntimeBillingContextHandle | None) -> None:
    """Release a billing context handle created by Runtime billing helpers."""

    if handle is None:
        return

    from packages.core.ai.llm_client import _billing_ctx_var, release_billing_in_flight

    release_billing_in_flight(handle.context)
    _billing_ctx_var.reset(handle.token)


def runtime_current_billing_context() -> Any | None:
    """Return the current low-level billing context without exposing its storage."""

    from packages.core.ai.llm_client import _billing_ctx_var

    return _billing_ctx_var.get()


async def runtime_assert_credit_available(
    entity_id: str,
    *,
    source: str = RUNTIME_SYSTEM_SOURCE,
    **kwargs: Any,
) -> None:
    """Run the platform credit preflight through the Runtime billing boundary."""

    from packages.core.ai.llm_client import assert_credit_available

    await assert_credit_available(entity_id, source=source, **kwargs)


async def runtime_ensure_workspace_billing_context(
    db: Any,
    workspace_id: str | None,
    *,
    source: str,
) -> str | None:
    """Resolve workspace ownership and bind Runtime billing context."""

    if not db or not workspace_id:
        return None

    from sqlalchemy import select

    from packages.core.models.workspace import Workspace

    entity_id = (
        await db.execute(
            select(Workspace.entity_id).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if entity_id:
        runtime_ensure_billing_context(
            entity_id,
            source=source,
            workspace_id=workspace_id,
        )
    return entity_id


def runtime_ensure_plan_billing_context(
    plan: Any,
    *,
    source: str,
    user_id: str | None = None,
    byok: bool = False,
) -> RuntimeResolvedBillingScope:
    """Resolve an execution plan owner and bind Runtime billing context."""

    scope = RuntimeResolvedBillingScope(
        entity_id=str(getattr(plan, "entity_id", "") or "") or None,
        workspace_id=str(getattr(plan, "workspace_id", "") or "") or None,
        user_id=str(user_id or getattr(plan, "user_id", "") or "") or None,
        byok=byok,
    )
    _bind_resolved_billing_scope(scope, source=source)
    return scope


def runtime_ensure_plan_executor_billing_context(plan: Any) -> RuntimeResolvedBillingScope:
    """Bind billing context for a PlanExecutor cycle."""

    return runtime_ensure_plan_billing_context(plan, source=RUNTIME_PLAN_EXECUTOR_SOURCE)


def _billing_scope_from_row(row: Any | None) -> RuntimeResolvedBillingScope:
    if row is None:
        return RuntimeResolvedBillingScope(entity_id=None, workspace_id=None)
    entity_id = _row_value(row, "entity_id", 0)
    workspace_id = _row_value(row, "workspace_id", 1)
    user_id = runtime_task_billable_user_id(row)
    if user_id is None:
        user_id = _normalized_billable_user_id(_row_value(row, "user_id", 2))
    return RuntimeResolvedBillingScope(
        entity_id=str(entity_id) if entity_id else None,
        workspace_id=str(workspace_id) if workspace_id else None,
        user_id=user_id,
    )


def _bind_resolved_billing_scope(scope: RuntimeResolvedBillingScope, *, source: str) -> None:
    if not scope.entity_id:
        return
    kwargs: dict[str, Any] = {
        "workspace_id": scope.workspace_id,
    }
    if scope.user_id:
        kwargs["user_id"] = scope.user_id
    if scope.byok:
        kwargs["byok"] = True
    runtime_ensure_billing_context(
        scope.entity_id,
        source=source,
        **kwargs,
    )


async def _resolve_scope_byok(
    db: Any,
    scope: RuntimeResolvedBillingScope,
    *,
    model_role: str | None,
) -> RuntimeResolvedBillingScope:
    if not model_role or not scope.entity_id:
        return scope
    try:
        from packages.core.services.model_resolver import resolve_llm_metadata_for_user

        metadata = await resolve_llm_metadata_for_user(
            model_role,
            user_id=scope.user_id,
            entity_id=scope.entity_id,
            db=db,
        )
    except Exception:
        return scope
    if not metadata:
        return scope
    return RuntimeResolvedBillingScope(
        entity_id=scope.entity_id,
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        byok=True,
    )


async def runtime_ensure_goal_billing_context(
    db: Any,
    goal_id: str | None,
    *,
    source: str,
) -> RuntimeResolvedBillingScope:
    """Resolve a goal owner and bind Runtime billing context."""

    if not db or not goal_id:
        return RuntimeResolvedBillingScope(entity_id=None, workspace_id=None)

    from sqlalchemy import select

    from packages.core.models.goal import Goal

    result = await db.execute(
        select(Goal.entity_id, Goal.workspace_id).where(Goal.id == goal_id)
    )
    scope = _billing_scope_from_row(result.first())
    _bind_resolved_billing_scope(scope, source=source)
    return scope


async def runtime_ensure_task_billing_context(
    db: Any,
    task_id: str | None,
    *,
    source: str,
    model_role: str | None = None,
) -> RuntimeResolvedBillingScope:
    """Resolve a task owner and bind Runtime billing context."""

    if not db or not task_id:
        return RuntimeResolvedBillingScope(entity_id=None, workspace_id=None)

    from sqlalchemy import select

    from packages.core.models.task import Task

    result = await db.execute(
        select(
            Task.entity_id,
            Task.workspace_id,
            Task.creator_id,
            Task.owner_id,
            Task.assignee_id,
        ).where(Task.id == task_id)
    )
    scope = _billing_scope_from_row(result.first())
    scope = await _resolve_scope_byok(db, scope, model_role=model_role)
    _bind_resolved_billing_scope(scope, source=source)
    return scope


def runtime_is_byok_call_active() -> bool:
    """Return whether the current low-level LLM call is using BYOK routing."""

    from packages.core.ai.llm_client import _is_byok_call

    return bool(_is_byok_call.get(False))
