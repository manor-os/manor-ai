"""Runtime-owned facade for workspace task and strategist actions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select

from packages.core.i18n import t
from packages.core.constants.goals import GoalStatus
from packages.core.constants.review import ReviewSkipReason
from packages.core.constants.task import CancelSource, TaskLogType, TaskRuntimeKind, TaskStatus, TaskType
from packages.core.ai.runtime.task_actions import runtime_normalize_task_priority
from packages.core.ai.runtime.execution_origin import runtime_tool_is_user_requested
from packages.core.ai.runtime.tool_context import runtime_active_user_message_from_context
from packages.core.services.workspace_autonomy import workspace_allows_manual_work
from packages.core.services.task_intents import (
    DRAFTED_NEXT,
    STARTED_NEXT,
    already_drafted,
    draft_only_prepares,
    drafted_in_open_draft,
    open_chat_draft_preparing,
    pending_chat_draft,
    record_chat_task_effect,
    resolve_task_start,
    start_pending_task,
)

logger = logging.getLogger(__name__)


def _workspace_chat_origin_message(
    raw_params: dict[str, Any], *, entity_id: str, workspace_id: str,
    conversation_id: str | None, user_id: str | None,
) -> str | None:
    """The user message this Workspace Chat turn answers, when the call is that turn's own.

    Read from the host's runtime envelope, never from model arguments.
    """
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    envelope = runtime_tool_call_context_from_kwargs(raw_params).runtime_envelope
    origin = (getattr(envelope, "metadata", None) or {}).get("origin_user_message_id")
    if (
        origin and user_id and conversation_id
        and getattr(envelope, "entity_id", None) == entity_id
        and getattr(envelope, "workspace_id", None) == workspace_id
        and getattr(envelope, "conversation_id", None) == conversation_id
        and getattr(envelope, "user_id", None) == user_id
        and getattr(envelope, "surface", None) == "workspace_chat"
    ):
        return str(origin)
    return None


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _as_clean_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _task_required_capability_values(params: dict[str, Any]) -> Any:
    return (
        params.get("required_capabilities")
        or params.get("business_capabilities")
        or params.get("runtime_capabilities")
        or params.get("capability_ids")
    )


def _task_required_capability_errors(values: Any) -> list[dict[str, str]]:
    if not values:
        return []
    from packages.core.ai.runtime.task_requirements import task_runtime_capability_validation_errors

    return task_runtime_capability_validation_errors(values)


def _rule_key(description: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(description or "").lower()).strip("_")
    return (text[:48] or "workspace_rule").strip("_")


def _normalise_rules(values: Any) -> list[dict[str, Any]]:
    raw_rules: list[dict[str, Any]] = []
    if isinstance(values, str):
        values = [{"description": values}]
    if not isinstance(values, list):
        return []
    for idx, value in enumerate(values):
        if isinstance(value, str):
            value = {"description": value}
        if not isinstance(value, dict):
            continue
        rule = dict(value)
        rule.setdefault("rule_key", _rule_key(rule.get("description") or f"rule_{idx + 1}"))
        raw_rules.append(rule)
    if not raw_rules:
        return []
    from packages.core.services.workspace_setup_service import _enrich_operating_rules

    return _enrich_operating_rules(raw_rules)


def _dedupe_rules(rules: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, value in enumerate(rules):
        if not isinstance(value, dict):
            continue
        rule = dict(value)
        key = str(rule.get("rule_key") or "").strip() or _rule_key(rule.get("description") or f"rule_{idx + 1}")
        dedupe_key = key or str(rule.get("description") or "")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rule["rule_key"] = key
        out.append(rule)
    return out


def _merge_runtime_context(
    existing: Any,
    *,
    instructions: str | None,
    required_refs: Any,
    rules: Any,
    knowledge_query: str | None,
    required_capabilities: Any = None,
    replace: bool = False,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    runtime = {} if replace or not isinstance(existing, dict) else dict(existing)

    instruction_text = str(instructions or "").strip()
    if instruction_text:
        old = str(runtime.get("instructions") or "").strip()
        runtime["instructions"] = instruction_text if replace or not old else f"{old}\n{instruction_text}"

    refs = _as_clean_list(required_refs)
    if refs:
        runtime["required_refs"] = refs if replace else _as_clean_list(list(runtime.get("required_refs") or []) + refs)

    query = str(knowledge_query or "").strip()
    if query:
        runtime["knowledge_query"] = query if replace else "\n".join(
            part for part in [str(runtime.get("knowledge_query") or "").strip(), query] if part
        )

    if required_capabilities:
        from packages.core.ai.runtime.task_requirements import merge_task_runtime_capabilities

        runtime = merge_task_runtime_capabilities(
            runtime,
            required_capabilities,
            replace=replace,
        )

    new_rules = _normalise_rules(rules)
    if new_rules:
        old_rules = [] if replace else list(runtime.get("rules") or [])
        runtime["rules"] = _dedupe_rules(old_rules + new_rules)

    source = dict(runtime.get("captured_from") or {})
    if conversation_id:
        source["conversation_id"] = conversation_id
    if user_id:
        source["user_id"] = user_id
    if source:
        runtime["captured_from"] = source
    return runtime


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "start", "run", "execute", "now"}


def _customer_context_from_runtime_tool_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract customer-safe channel context for support task records."""

    from packages.core.ai.runtime.principals import RuntimePrincipalKind
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = runtime_context.runtime_envelope
    principal = getattr(envelope, "principal", None)
    kind = getattr(principal, "kind", None)
    kind_value = getattr(kind, "value", kind)
    if kind_value not in {
        RuntimePrincipalKind.EXTERNAL_CONTACT.value,
        RuntimePrincipalKind.ANONYMOUS_PUBLIC.value,
    }:
        return {}

    metadata = getattr(principal, "metadata", None)
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    context: dict[str, Any] = {
        "source": "public_customer_chat",
        "channel_type": getattr(principal, "channel_type", None),
        "source_id": getattr(principal, "external_sender_id", None),
        "channel_contact_id": metadata.get("channel_contact_id"),
        "conversation_id": runtime_context.conversation_id or metadata.get("conversation_id"),
        "display_name": metadata.get("display_name"),
        "role": metadata.get("role"),
        "is_verified": bool(getattr(principal, "is_verified_external", False)),
    }
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _internal_task_creator_id_from_runtime_tool_kwargs(
    kwargs: dict[str, Any],
    *,
    fallback_user_id: str | None,
) -> str | None:
    """Return a User id only when the runtime principal is an internal human."""

    from packages.core.ai.runtime.principals import RuntimePrincipalKind
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = runtime_context.runtime_envelope
    if envelope is None:
        return str(fallback_user_id or "").strip() or None

    principal = getattr(envelope, "principal", None)
    kind = getattr(principal, "kind", None)
    kind_value = getattr(kind, "value", kind)
    if kind_value not in {
        RuntimePrincipalKind.OWNER.value,
        RuntimePrincipalKind.WORKSPACE_MEMBER.value,
    }:
        return None

    return str(
        getattr(principal, "execution_user_id", None)
        or getattr(principal, "actor_user_id", None)
        or fallback_user_id
        or ""
    ).strip() or None


def _workspace_task_to_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "task_type": task.task_type,
        "assignee_id": task.assignee_id,
        "agent_id": task.agent_id,
        "agent_type": task.agent_type,
        "creator_id": task.creator_id,
        "owner_service_key": task.owner_service_key,
        "owner_subscription_id": task.owner_subscription_id,
        "delegate_service_keys": task.delegate_service_keys or [],
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "details": task.details or {},
    }


async def _load_workspace(db: Any, *, entity_id: str, workspace_id: str) -> Any:
    from packages.core.models.workspace import Workspace

    return (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()


async def _resolve_owner_binding(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    requested_service_key: str | None,
) -> tuple[str | None, str | None, list[str]]:
    from packages.core.models.workspace import AgentSubscription

    subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    by_key = {
        str(sub.service_key): sub
        for sub in subs
        if getattr(sub, "service_key", None)
    }
    service_key = str(requested_service_key or "").strip()
    if service_key and service_key in by_key:
        sub = by_key[service_key]
        return service_key, sub.id, sorted(by_key)
    if not service_key and len(by_key) == 1:
        key, sub = next(iter(by_key.items()))
        return key, sub.id, [key]
    return None, None, sorted(by_key)


def _missing_service_keys(requested: list[str], available: list[str]) -> list[str]:
    available_set = set(available)
    return [key for key in requested if key not in available_set]


def _goal_match_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(value or "").lower()).strip()


def _goal_text_score(goal: Any, task_text: str) -> int:
    """Conservatively infer goal linkage from task wording."""

    text = _goal_match_key(task_text)
    if not text:
        return 0
    title = _goal_match_key(getattr(goal, "title", ""))
    description = _goal_match_key(getattr(goal, "description", ""))
    metric = _goal_match_key(getattr(goal, "metric_key", ""))
    goal_blob = f"{title} {description} {metric}".strip()
    score = 0

    if "draft" in goal_blob and any(term in text for term in ["draft", "drafts", "草稿", "内容包", "帖子"]):
        score += 6
    if "competitor" in goal_blob and any(term in text for term in ["capture competitor", "competitor scan", "竞品追踪", "竞品分析"]):
        score += 4
    if "shortlist" in goal_blob and any(term in text for term in ["produce shortlist", "rank", "shortlist", "选品 shortlist", "生成选品"]):
        if any(term in text for term in ["based on", "基于", "上游", "reference", "引用"]):
            score += 1
        else:
            score += 4

    stop = {"the", "and", "for", "per", "week", "one", "with", "ready", "prepare", "produce"}
    for token in set(goal_blob.split()):
        if len(token) < 4 or token in stop:
            continue
        if token in text:
            score += 1
    return score


async def _resolve_goal_links_for_task(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    goal_refs: list[str],
    task_text: str,
) -> tuple[list[str], str]:
    from packages.core.models.goal import Goal

    goals = list((await db.execute(
        select(Goal).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == GoalStatus.ACTIVE.value,
        )
    )).scalars().all())
    if not goals:
        return [], "none"

    matched: list[str] = []
    if goal_refs:
        ref_keys = {_goal_match_key(ref) for ref in goal_refs if _goal_match_key(ref)}
        for goal in goals:
            candidates = {
                _goal_match_key(goal.id),
                _goal_match_key(goal.metric_key),
                _goal_match_key(goal.title),
            }
            if candidates & ref_keys:
                matched.append(goal.id)
        return matched, "explicit"

    scored = sorted(
        ((goal, _goal_text_score(goal, task_text)) for goal in goals),
        key=lambda item: item[1],
        reverse=True,
    )
    inferred = [goal.id for goal, score in scored if score >= 5]
    return inferred[:3], "inferred" if inferred else "none"


async def _link_task_to_goals(
    db: Any,
    *,
    task_id: str,
    goal_ids: list[str],
    contribution: str = "direct",
) -> None:
    if not task_id or not goal_ids:
        return
    from packages.core.goals.service import link_task_to_goal

    for goal_id in goal_ids:
        try:
            await link_task_to_goal(
                db,
                goal_id=goal_id,
                task_id=task_id,
                contribution=contribution,
            )
        except ValueError:
            # A Goal may be deleted after link inference but before the task
            # is flushed. The task remains valid without that stale link.
            continue


async def runtime_workspace_create_task_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    actor_agent_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    raw_params = dict(params or {})
    # Composite callers also use "instructions" for the Task's output contract.
    if "instructions" in raw_params and not str(raw_params.get("runtime_instructions") or "").strip():
        raw_params["runtime_instructions"] = raw_params["instructions"]
    customer_context = _customer_context_from_runtime_tool_kwargs(raw_params)
    creator_user_id = _internal_task_creator_id_from_runtime_tool_kwargs(
        raw_params,
        fallback_user_id=user_id,
    )
    title = str(raw_params.get("title") or "").strip()
    if not title:
        return _dumps({"error": "title is required"})
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.ai.runtime.execution_origin import runtime_origin_task_id
        from packages.core.database import async_session
        from packages.core.services import task_service
        from packages.core.services.workspace_service import record_activity

        start_requested = _truthy(
            raw_params.get("start")
            if raw_params.get("start") is not None
            else raw_params.get("run_now") or raw_params.get("execute")
        )
        task_type = str(raw_params.get("task_type") or TaskType.GENERAL.value)
        requested_owner = str(
            raw_params.get("owner_service_key") or raw_params.get("service_key") or raw_params.get("agent_type") or ""
        ).strip() or None
        delegate_service_keys = _as_clean_list(raw_params.get("delegate_service_keys"))
        dependency_task_ids = _as_clean_list(
            raw_params.get("depends_on_task_ids")
            or raw_params.get("dependency_task_ids")
            or raw_params.get("predecessor_task_ids")
        )
        required_capabilities = _task_required_capability_values(raw_params)
        capability_errors = _task_required_capability_errors(required_capabilities)
        if capability_errors:
            return _dumps({
                "error": "invalid_required_capabilities",
                "details": capability_errors,
            })
        goal_refs = _as_clean_list(
            raw_params.get("goal_ids")
            or raw_params.get("goal_keys")
            or raw_params.get("goals")
            or raw_params.get("linked_goal_ids")
        )
        dispatch_after_commit = False
        dispatch_blocked_by_dependencies = False

        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})
            from packages.core.ai.runtime.manor_actions import collaborator_assignee_error

            if assignee_error := await collaborator_assignee_error(
                db, entity_id, workspace_id, raw_params.get("assignee_id"), user_id=user_id,
            ):
                return _dumps(assignee_error)
            if start_requested and (drafted := await pending_chat_draft(
                db, entity_id=entity_id, workspace_id=workspace_id,
                conversation_id=conversation_id, title=title,
            )):
                # "Start it" after a draft must run that Task, not a copy of it.
                return _dumps(already_drafted(drafted))
            open_draft = await open_chat_draft_preparing(
                db, entity_id=entity_id, workspace_id=workspace_id,
                conversation_id=conversation_id, title=title,
            )
            if open_draft and not draft_only_prepares(open_draft, title):
                return _dumps(drafted_in_open_draft(open_draft))
            author_created_by, author_meta, author_actor = await task_service.agent_log_authorship(
                db, actor_agent_id, fallback=creator_user_id,
            )
            owner_service_key, owner_subscription_id, available_service_keys = await _resolve_owner_binding(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                requested_service_key=requested_owner,
            )
            if (
                task_type == TaskType.INTERACTIVE.value
                and raw_params.get("agent_id")
                and not requested_owner
            ):
                # A directly assigned session Host is already unambiguous; do
                # not attach the Workspace's sole service implicitly.
                owner_service_key = None
                owner_subscription_id = None
            if requested_owner and not owner_service_key:
                return _dumps({
                    "error": "owner_service_not_found",
                    "requested_owner_service_key": requested_owner,
                    "available_service_keys": available_service_keys,
                    "message": (
                        "The requested owner_service_key is not active in this workspace. "
                        "Choose one of available_service_keys or ask the user to clarify."
                    ),
                })
            missing_delegate_keys = _missing_service_keys(delegate_service_keys, available_service_keys)
            if missing_delegate_keys:
                return _dumps({
                    "error": "delegate_service_not_found",
                    "missing_delegate_service_keys": missing_delegate_keys,
                    "available_service_keys": available_service_keys,
                    "message": (
                        "One or more delegate_service_keys are not active in this workspace. "
                        "Choose from available_service_keys before creating or starting the task."
                    ),
                })
            if owner_service_key and not delegate_service_keys:
                delegate_service_keys = [owner_service_key]

            runtime_context = _merge_runtime_context(
                {},
                instructions=raw_params.get("runtime_instructions"),
                required_refs=raw_params.get("required_refs"),
                rules=raw_params.get("rules"),
                knowledge_query=raw_params.get("knowledge_query"),
                required_capabilities=required_capabilities,
                replace=True,
                conversation_id=conversation_id or None,
                user_id=creator_user_id,
            )
            goal_link_ids, goal_link_source = await _resolve_goal_links_for_task(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                goal_refs=goal_refs,
                task_text="\n".join([
                    title,
                    str(raw_params.get("description") or ""),
                    str(raw_params.get("runtime_instructions") or ""),
                    str(raw_params.get("knowledge_query") or ""),
                ]),
            )
            details = {"created_from": "workspace_agent"}
            if customer_context:
                details["customer_context"] = customer_context
            if dependency_task_ids:
                details["depends_on_task_ids"] = dependency_task_ids
            if runtime_context:
                details["runtime_context"] = runtime_context
            if goal_link_ids:
                details["goal_ids"] = goal_link_ids
                details["goal_link_source"] = goal_link_source
            session_config = raw_params.get("session_config")
            if isinstance(session_config, dict) and session_config:
                details["session"] = session_config

            task = await task_service.create_task(
                db,
                entity_id,
                title=title,
                description=raw_params.get("description") or "",
                priority=runtime_normalize_task_priority(raw_params.get("priority") or 3),
                task_type=task_type,
                workspace_id=workspace_id,
                assignee_id=raw_params.get("assignee_id") or None,
                agent_id=raw_params.get("agent_id") or None,
                agent_type=raw_params.get("agent_type") or None,
                # ``creator_id`` is an internal User identity. Agent authorship
                # belongs on the creation log; external identity stays in the
                # customer context instead of inheriting workspace membership.
                creator_id=creator_user_id,
                creator_agent_id=actor_agent_id,
                conversation_id=(
                    None
                    if task_type == TaskType.INTERACTIVE.value
                    else conversation_id or None
                ),
                owner_service_key=owner_service_key,
                owner_subscription_id=owner_subscription_id,
                delegate_service_keys=delegate_service_keys,
                details=details,
                deadline=raw_params.get("deadline") or None,
                # Created from inside a Task run (manual or automatic): link
                # it to that Task, so it is never an orphan nobody can trace.
                origin_task_id=runtime_origin_task_id(raw_params, entity_id=entity_id),
            )
            from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
            from packages.core.services.task_execution_origin import (
                record_manual_task_execution,
            )

            call_context = runtime_tool_call_context_from_kwargs(raw_params)
            if origin_message_id := _workspace_chat_origin_message(
                raw_params, entity_id=entity_id, workspace_id=workspace_id,
                conversation_id=conversation_id, user_id=creator_user_id,
            ):
                # Never copy a model-supplied details/origin object. This is
                # provenance, not authorization, and survives a Chat resume.
                task.details = {**task.details, "execution_origin": {
                    "kind": "workspace_chat_create", "entity_id": entity_id,
                    "workspace_id": workspace_id, "user_id": creator_user_id,
                    "conversation_id": conversation_id,
                    "origin_user_message_id": origin_message_id,
                }}
                await record_chat_task_effect(
                    db, task, effect="created", origin_user_message_id=origin_message_id,
                    user_id=creator_user_id, conversation_id=conversation_id,
                )
            manual_request = await runtime_tool_is_user_requested(
                db, entity_id=entity_id, workspace_id=workspace_id, kwargs=raw_params,
            )
            if creator_user_id and manual_request:
                record_manual_task_execution(task, user_id=creator_user_id, action="workspace_chat_task")
            elif not manual_request:
                task.details = {
                    **task.details,
                    "automation_origin": {
                        "task_id": call_context.task_id,
                        "workflow_run_id": call_context.workflow_run_id,
                    },
                }
            if goal_link_ids:
                await _link_task_to_goals(db, task_id=task.id, goal_ids=goal_link_ids)
            if dependency_task_ids:
                from packages.core.services.task_dependencies import details_with_dependency_state

                task.details = await details_with_dependency_state(db, task, dict(task.details or {}))
            from packages.core.services.task_execution import (
                resolve_task_execution,
                stage_task_execution_intent,
            )

            execution = await resolve_task_execution(db, task) if start_requested else None
            if (
                execution is not None
                and execution.runtime_kind is TaskRuntimeKind.MANUAL
            ):
                return _dumps({
                    "error": "task_executor_unavailable",
                    "message": "Assign an active workspace Agent before starting this task.",
                })
            # Foreground interactive and human-decision adapters share the
            # factory/lifecycle boundary but are not background dispatches.
            dispatch_requested = bool(
                execution is not None and execution.is_dispatchable
            )
            log_locale = await task_service._task_log_locale(db, creator_user_id)
            if runtime_context:
                await task_service.add_task_log(
                    db,
                    task.id,
                    TaskLogType.RUNTIME_CONTEXT,
                    t("task.log.runtime_requirements", locale=log_locale),
                    actor=author_actor,
                    created_by=author_created_by,
                    metadata={"runtime_context": runtime_context, **(author_meta or {})},
                )
            if dispatch_requested:
                if dependency_task_ids and (task.details or {}).get("dependency_status") != "completed":
                    dispatch_blocked_by_dependencies = True
                    await task_service.add_task_log(
                        db,
                        task.id,
                        TaskLogType.DEPENDENCY_WAIT,
                        t("task.log.dependency_wait", locale=log_locale),
                        actor=author_actor,
                        created_by=author_created_by,
                        metadata={
                            "depends_on_task_ids": dependency_task_ids,
                            "dependency_status": (task.details or {}).get("dependency_status"),
                            "dependency_statuses": (task.details or {}).get("dependency_statuses"),
                            **(author_meta or {}),
                        },
                    )
                else:
                    updated = await task_service.update_task(
                        db,
                        task.id,
                        entity_id,
                        user_id=creator_user_id,
                        manual_execution=manual_request,
                        cancel_source=CancelSource.SYSTEM.value,  # an agent call
                        dispatch_plan=False,
                        status="in_progress",
                        details=dict(task.details or {}),
                    )
                    if updated:
                        task = updated
                    dispatch_after_commit = True
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.task_created",
                summary=f"Workspace Agent created task: {task.title}",
                details={
                    "task_id": task.id,
                    "runtime_context": runtime_context,
                    "start": dispatch_requested,
                    "dispatch_blocked_by_dependencies": dispatch_blocked_by_dependencies,
                    "owner_service_key": owner_service_key,
                    "depends_on_task_ids": dependency_task_ids,
                    "goal_ids": goal_link_ids,
                    "goal_link_source": goal_link_source,
                    "available_service_keys": available_service_keys,
                },
                user_id=creator_user_id,
                agent_id=actor_agent_id,
            )
            if dispatch_after_commit:
                assert execution is not None
                await stage_task_execution_intent(db, task, execution)
            if open_draft:
                # The user confirmed the Task in words; its draft card is done.
                from packages.core.services.workspace_operation_service import supersede_task_draft

                await supersede_task_draft(
                    db, open_draft, conversation_id=conversation_id or "", user_id=user_id,
                )
            await db.commit()
            await db.refresh(task)

        # ``dispatched`` retains the public response vocabulary. It now means
        # the start was committed to the durable publisher, not that a
        # process-local broker call happened to return successfully.
        dispatched = bool(dispatch_after_commit)

        return _dumps({
            "created": True,
            "task": _workspace_task_to_dict(task),
            **({"superseded_draft_id": open_draft.id} if open_draft else {}),
            "next": STARTED_NEXT if dispatched else (
                "It waits for its predecessor Tasks and starts when they complete."
                if dispatch_blocked_by_dependencies else DRAFTED_NEXT
            ),
            "dispatched": dispatched,
            "dispatch_mode": "planner" if dispatch_after_commit else None,
            "dispatch_blocked_by_dependencies": dispatch_blocked_by_dependencies,
            "owner_resolution": {
                "owner_service_key": task.owner_service_key,
                "owner_subscription_id": task.owner_subscription_id,
            },
            "goal_links": {
                "goal_ids": (task.details or {}).get("goal_ids") or [],
                "source": (task.details or {}).get("goal_link_source") or "none",
            },
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_create_task failed")
        return _dumps({"error": f"failed to create workspace task: {exc}"})


async def runtime_workspace_update_task_runtime_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    actor_agent_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    raw_params = dict(params or {})
    task_id = str(raw_params.get("task_id") or "").strip()
    if not task_id:
        return _dumps({"error": "task_id is required"})
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.models.task import Task
        from packages.core.services import task_service
        from packages.core.services.workspace_service import record_activity

        async with async_session() as db:
            task = (await db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.entity_id == entity_id,
                    Task.workspace_id == workspace_id,
                )
            )).scalar_one_or_none()
            if not task:
                return _dumps({"error": "task not found in this workspace"})

            required_capabilities = _task_required_capability_values(raw_params)
            capability_errors = _task_required_capability_errors(required_capabilities)
            if capability_errors:
                return _dumps({
                    "error": "invalid_required_capabilities",
                    "details": capability_errors,
                })

            details = dict(task.details or {})
            runtime = _merge_runtime_context(
                details.get("runtime_context") or {},
                instructions=raw_params.get("runtime_instructions"),
                required_refs=raw_params.get("required_refs"),
                rules=raw_params.get("rules"),
                knowledge_query=raw_params.get("knowledge_query"),
                required_capabilities=required_capabilities,
                replace=bool(raw_params.get("replace")),
                conversation_id=conversation_id or None,
                user_id=user_id or None,
            )
            details["runtime_context"] = runtime

            execution = manual = None
            if _truthy(raw_params.get("start")):
                execution, refusal = await resolve_task_start(db, task, details)
                if refusal:
                    return _dumps(refusal)
                manual = await runtime_tool_is_user_requested(
                    db, entity_id=entity_id, workspace_id=workspace_id, kwargs=raw_params,
                )
                if manual and user_id:
                    from packages.core.services.task_execution_origin import (
                        MANUAL_EXECUTION_KEY,
                        record_manual_task_execution,
                    )

                    record_manual_task_execution(task, user_id=user_id, action="workspace_chat_task_start")
                    details[MANUAL_EXECUTION_KEY] = task.details[MANUAL_EXECUTION_KEY]

            updated = await task_service.update_task(
                db,
                task_id,
                entity_id,
                user_id=user_id or None,
                cancel_source=CancelSource.SYSTEM.value,  # an agent call
                details=details,
            )
            if not updated:
                return _dumps({"error": "task not found"})
            started = execution is not None
            if started:
                updated = await start_pending_task(
                    db, updated, execution, user_id=user_id or None, manual_execution=bool(manual),
                )
                starter = _internal_task_creator_id_from_runtime_tool_kwargs(
                    raw_params, fallback_user_id=user_id,
                )
                if origin_message_id := _workspace_chat_origin_message(
                    raw_params, entity_id=entity_id, workspace_id=workspace_id,
                    conversation_id=conversation_id, user_id=starter,
                ):
                    await record_chat_task_effect(
                        db, updated, effect="started", origin_user_message_id=origin_message_id,
                        user_id=starter, conversation_id=conversation_id,
                    )
            author_created_by, author_meta, author_actor = await task_service.agent_log_authorship(
                db, actor_agent_id, fallback=user_id,
            )
            await task_service.add_task_log(
                db,
                task_id,
                TaskLogType.RUNTIME_CONTEXT,
                "Workspace Agent updated task runtime requirements.",
                actor=author_actor,
                created_by=author_created_by,
                metadata={"runtime_context": runtime, **(author_meta or {})},
            )
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.task_runtime_updated",
                summary=f"Workspace Agent updated runtime requirements for: {updated.title}",
                details={"task_id": task_id, "runtime_context": runtime},
                user_id=user_id or None,
            )
            await db.commit()
            await db.refresh(updated)

        payload = {"updated": True, "started": started, "task": _workspace_task_to_dict(updated)}
        if started:
            payload["next"] = STARTED_NEXT
        return _dumps(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_update_task_runtime failed")
        return _dumps({"error": f"failed to update task runtime requirements: {exc}"})


async def runtime_workspace_request_strategist_review_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Request a Strategist review on a person's behalf — a two-step contract.

    A human asking for a review is never silently dropped. When proposals
    from an earlier review are still undecided, this returns
    ``needs_decision`` with the conflicting cohort instead of enqueueing,
    so the agent can ask in chat. ``supersede=True`` on the follow-up call
    rejects that cohort (reason_code SUPERSEDED) and runs the review.
    """
    from packages.core.strategist import ReviewTrigger, ReviewTriggerKind

    raw_params = dict(params or {})
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    reason = str(raw_params.get("reason") or "").strip()[:500]
    supersede = bool(raw_params.get("supersede") is True)
    try:
        countdown = int(raw_params.get("countdown_seconds") if raw_params.get("countdown_seconds") is not None else 1)
    except (TypeError, ValueError):
        countdown = 1
    countdown = max(0, min(countdown, 300))
    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_service import record_activity
        from packages.core.strategist.service import (
            list_open_proposals,
            open_proposal_conflict,
            supersede_open_proposals,
        )
        from packages.core.tasks.ai_tasks import run_strategist_review

        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})
            human_requested = bool(user_id) and await runtime_tool_is_user_requested(
                db, entity_id=entity_id, workspace_id=workspace_id, kwargs=raw_params,
            )
            if human_requested:
                active_request = (runtime_active_user_message_from_context(raw_params) or "").strip()
                if active_request:
                    reason = active_request[:500]
            trigger = ReviewTrigger(
                kind=ReviewTriggerKind.HUMAN_REQUESTED if human_requested else ReviewTriggerKind.EVENT,
                detail=reason or ("requested in chat" if human_requested else "requested by runtime"),
            )
            if not workspace_allows_manual_work(workspace) or (
                workspace.status == "paused" and not human_requested
            ):
                return _dumps({
                    "requested": False,
                    "blocked": True,
                    "reason": ReviewSkipReason.WORKSPACE_INACTIVE,
                    "workspace_id": workspace_id,
                    "workspace_status": workspace.status,
                    "next_step": (
                        "Tell the user the Strategist review did not start because "
                        f"the workspace is {workspace.status}. Do not claim that the "
                        "review was requested or is running. Paused workspaces require "
                        "an explicit user request; unavailable workspaces must be restored first."
                    ),
                })

            open_tasks = await list_open_proposals(db, workspace_id)
            superseded: dict | None = None
            if open_tasks and (not supersede or not human_requested):
                # Do NOT enqueue: the review would be blocked on arrival and
                # the person would never hear back. Hand the conflict to the
                # model so it can ask, then call again with supersede=true.
                conflict = open_proposal_conflict(open_tasks)
                return _dumps({
                    "requested": False,
                    "needs_decision": True,
                    "workspace_id": workspace_id,
                    "conflict": conflict,
                    "next_step": (
                        "Tell the user how many proposals are still open (and "
                        "their titles), then ask whether to start a new review "
                        "and reject the current ones. If they agree, call this "
                        "tool again with supersede=true."
                    ),
                })
            if open_tasks and supersede:
                superseded = await supersede_open_proposals(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    actor_id=user_id or None,
                )

            async_result = run_strategist_review.apply_async(
                args=[workspace_id],
                kwargs={**trigger.celery_kwargs(), "user_id": user_id},
                countdown=countdown,
            )
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.strategist_requested",
                summary=f"Workspace Agent requested strategist review: {reason[:160]}",
                details={
                    "trigger_kind": trigger.kind.value,
                    "trigger_detail": trigger.detail,
                    "countdown_seconds": countdown,
                    "superseded_task_ids": (superseded or {}).get("rejected_task_ids") or [],
                    "celery_task_id": getattr(async_result, "id", None),
                },
                user_id=user_id or None,
            )
            await db.commit()

        return _dumps({
            "requested": True,
            "workspace_id": workspace_id,
            "trigger_kind": trigger.kind.value,
            "trigger_detail": trigger.detail,
            "superseded": superseded,
            "countdown_seconds": countdown,
            "celery_task_id": getattr(async_result, "id", None),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_request_strategist_review failed")
        return _dumps({"error": f"failed to request strategist review: {exc}"})
