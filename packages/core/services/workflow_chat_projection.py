"""Project configured Workflow Run progress into its originating Workspace Chat."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import AgentSubscription
from packages.core.services.workflow_run_trace import (
    summarize_trace_text,
    summarize_trace_value,
    workflow_chat_projection_visibility,
)


_NOTIFICATION_QUEUE_KEY = "workflow_chat_projection_notifications"
_NOTIFICATION_LISTENER_KEY = "workflow_chat_projection_listeners"
_ACTIONABLE_COMPLETED_OUTCOMES = {
    "needs_input",
}


async def _push_notification(payload: dict[str, Any]) -> None:
    from packages.core.cache import _get_redis

    redis = await _get_redis()
    if redis:
        await redis.publish("manor:ws_broadcast", json.dumps(payload))


def _publish_notifications_after_root_commit(session) -> None:
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_NOTIFICATION_QUEUE_KEY, [])
    for loop, payload, _owner in pending:
        loop.call_soon_threadsafe(
            lambda payload=payload: asyncio.create_task(_push_notification(payload))
        )


def _clear_notifications_after_root_rollback(session) -> None:
    nested_transaction = session.get_nested_transaction()
    if nested_transaction is None:
        session.info.pop(_NOTIFICATION_QUEUE_KEY, None)
        return
    pending = session.info.get(_NOTIFICATION_QUEUE_KEY, [])
    session.info[_NOTIFICATION_QUEUE_KEY] = [
        item
        for item in pending
        if not _transaction_descends_from(item[2], nested_transaction)
    ]


def _transaction_descends_from(transaction, ancestor) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _entrypoint_context(run: Any) -> dict[str, Any] | None:
    if str(getattr(run, "trigger_source", "") or "") != "workspace_chat":
        return None
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    context = trigger_data.get("_workspace_chat_entrypoint")
    if not isinstance(context, dict) or not context.get("enabled"):
        return None
    if not context.get("conversation_id") or not context.get("activity_message_id"):
        return None
    return context


def workflow_projection_settings(context: dict[str, Any]) -> dict[str, Any]:
    projection = context.get("projection") if isinstance(context.get("projection"), dict) else {}
    step_outputs = str(projection.get("step_outputs") or "explicit").lower()
    if step_outputs not in {"explicit", "all", "none"}:
        step_outputs = "explicit"
    return {
        "progress": bool(projection.get("progress", True)),
        "step_outputs": step_outputs,
        "final_output": bool(projection.get("final_output", True)),
    }


def workflow_progress_steps(
    run: Any,
    *,
    activity_status: str | None = None,
    existing_steps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project the run snapshot into stable, display-safe progress rows."""
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    if not nodes and isinstance(existing_steps, list):
        nodes = existing_steps

    results = run.step_results if isinstance(run.step_results, dict) else {}
    existing_by_id = {
        str(step.get("id") or ""): step
        for step in (existing_steps or [])
        if isinstance(step, dict) and step.get("id")
    }
    run_status = str(getattr(run, "status", "") or "").lower()
    active_status = str(activity_status or run_status).lower()
    active_id = str(getattr(run, "current_step_id", "") or "")
    variables = run.variables if isinstance(getattr(run, "variables", None), dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    business_outcome = str(state.get("business_outcome") or "").strip().lower()
    preserves_unreached_nodes = (
        run_status == "completed"
        and business_outcome in _ACTIONABLE_COMPLETED_OUTCOMES
    )
    result_frontier = -1
    if run_status in {"running", "paused", "failed"} or preserves_unreached_nodes:
        for index, node in enumerate(nodes):
            if not isinstance(node, dict) or str(node.get("type") or "").lower() == "end":
                continue
            result = results.get(str(node.get("id") or ""))
            if not isinstance(result, dict) or result.get("skipped"):
                continue
            result_status = str(result.get("status") or "").lower()
            if result_status in {"completed", "failed", "paused"}:
                result_frontier = index
    if not active_id:
        active_id = next(
            (
                str(node.get("id"))
                for node in nodes
                if isinstance(node, dict)
                and node.get("id")
                and node.get("type") in {"trigger", "webhook"}
            ),
            "",
        )

    projected: list[dict[str, Any]] = []
    for node_index, node in enumerate(nodes):
        if (
            not isinstance(node, dict)
            or workflow_chat_projection_visibility(node) == "hidden"
        ):
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        result = results.get(node_id)
        status = "pending"
        if isinstance(result, dict):
            result_status = str(result.get("status") or "").lower()
            if result.get("skipped") or result_status == "skipped":
                status = "skipped"
            elif result_status in {"completed", "failed", "paused"}:
                status = result_status
        elif run_status == "completed" and not preserves_unreached_nodes:
            status = "skipped"
        elif (
            result_frontier >= 0
            and node_index < result_frontier
            and node_id != active_id
        ):
            status = "skipped"
        if (
            status == "pending"
            and run_status in {"pending", "running"}
            and node_id == active_id
            and active_status in {"queued", "running"}
        ):
            status = active_status

        row = {
            "id": node_id,
            "name": str(node.get("name") or node_id),
            "type": str(node.get("type") or ""),
            "status": status,
        }
        existing = existing_by_id.get(node_id)
        if isinstance(existing, dict) and existing.get("subscription_id") is not None:
            row["subscription_id"] = existing["subscription_id"]
        projected.append(row)
    return projected


def _step_visibility(step: dict[str, Any]) -> str:
    return workflow_chat_projection_visibility(step)


def _step_service_key(step: dict[str, Any]) -> str:
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    return str(config.get("service_key") or step.get("service_key") or "").strip()


def _display_output(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value or "").strip()


def _workflow_business_state(run: Any) -> tuple[str, dict[str, Any]]:
    variables = run.variables if isinstance(run.variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    outcome = str(state.get("business_outcome") or "in_progress").strip().lower()
    retry_state = state.get("retry_state")
    return outcome or "in_progress", retry_state if isinstance(retry_state, dict) else {}


def _workflow_error_payload(run: Any) -> Any:
    results = run.step_results if isinstance(run.step_results, dict) else {}

    def result_error(step_id: str) -> Any:
        result = results.get(step_id)
        if not isinstance(result, dict):
            return None
        value = result.get("error")
        return value if value is not None and value != "" else None

    current_step_id = str(getattr(run, "current_step_id", "") or "")
    current_error = result_error(current_step_id)
    if current_error is not None:
        return current_error

    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    snapshot_ids = [
        str(node.get("id"))
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    ]
    checked = {current_step_id} if current_step_id else set()
    for step_id in reversed(snapshot_ids):
        if step_id in checked:
            continue
        checked.add(step_id)
        result = results.get(step_id)
        if isinstance(result, dict) and result.get("status") == "failed":
            error = result_error(step_id)
            if error is not None:
                return error
    for step_id, result in results.items():
        if str(step_id) in checked:
            continue
        if isinstance(result, dict) and result.get("status") == "failed":
            error = result_error(str(step_id))
            if error is not None:
                return error

    run_error = getattr(run, "error", None)
    return run_error if isinstance(run_error, str) and run_error else None


def _workflow_retry_input_schema(
    run: Any,
    retry_state: dict[str, Any],
) -> dict[str, Any]:
    raw_schema = retry_state.get("editable_input_schema")
    if not isinstance(raw_schema, dict):
        return {"type": "object", "properties": {}}
    schema = deepcopy(raw_schema)
    properties = schema.get("properties")
    variables = run.variables if isinstance(run.variables, dict) else {}
    request = variables.get("request")
    project = variables.get("project")
    required = schema.get("required")
    legacy_request_schema = (
        isinstance(project, dict)
        and project.get("project_type") == "product_video"
        and isinstance(properties, dict)
        and "request" not in properties
        and isinstance(request, dict)
        and isinstance(required, list)
        and bool(required)
        and set(required).issubset(request)
    )
    if not legacy_request_schema:
        return schema
    wrapper_properties: dict[str, Any] = {"request": schema}
    if "revision_notes" in variables:
        wrapper_properties["revision_notes"] = {
            "type": "string",
            "title": "Revision notes",
        }
    return {
        "type": "object",
        "properties": wrapper_properties,
        "required": ["request"],
        "additionalProperties": False,
    }


def _workflow_retry_values(
    run: Any,
    retry_state: dict[str, Any],
    editable_input_schema: dict[str, Any],
) -> dict[str, Any]:
    properties = editable_input_schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    variables = run.variables if isinstance(run.variables, dict) else {}
    values = {
        key: deepcopy(variables[key])
        for key in properties
        if key in variables
    }
    if "request" in properties and "request" not in values:
        project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
        state = project.get("state") if isinstance(project.get("state"), dict) else {}
        if isinstance(state.get("request"), dict):
            values["request"] = deepcopy(state["request"])
    if "retry_segment_ids" in properties:
        values["retry_segment_ids"] = deepcopy(retry_state.get("segment_ids") or [])
    return values


async def _activity_message(db: AsyncSession, run: Any, context: dict[str, Any]) -> Message | None:
    return (await db.execute(
        select(Message).join(
            Conversation,
            Conversation.id == Message.conversation_id,
        ).where(
            Message.id == str(context["activity_message_id"]),
            Message.conversation_id == str(context["conversation_id"]),
            Message.message_kind == "workflow_activity",
            Message.meta["workflow_run_id"].as_string() == str(run.id),
            Message.meta["workflow_binding_id"].as_string() == str(run.binding_id),
            Conversation.entity_id == str(run.entity_id),
            Conversation.workspace_id == str(run.workspace_id),
        ).with_for_update()
    )).scalar_one_or_none()


async def _notify_update(db: AsyncSession, message: Message) -> None:
    try:
        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.id == message.conversation_id,
            )
        )).scalar_one_or_none()
        if not conversation or not conversation.workspace_id:
            return
        loop = asyncio.get_running_loop()
        payload = {
            "entity_id": conversation.entity_id,
            "event": "workspace_chat_message",
            "data": {
                "workspace_id": conversation.workspace_id,
                "message_id": message.id,
                "message_kind": message.message_kind,
                "author_kind": message.author_kind,
                "has_pending_action": bool(message.pending_action),
            },
        }

        sync_session = db.sync_session
        owner = sync_session.get_nested_transaction()
        sync_session.info.setdefault(_NOTIFICATION_QUEUE_KEY, []).append(
            (loop, payload, owner)
        )
        if not sync_session.info.get(_NOTIFICATION_LISTENER_KEY):
            event.listen(
                sync_session,
                "after_commit",
                _publish_notifications_after_root_commit,
            )
            event.listen(
                sync_session,
                "after_rollback",
                _clear_notifications_after_root_rollback,
            )
            sync_session.info[_NOTIFICATION_LISTENER_KEY] = True
    except Exception:
        return


async def _resolve_subscription_id(
    db: AsyncSession,
    *,
    run: Any,
    step: dict[str, Any],
) -> str | None:
    service_key = _step_service_key(step)
    if not service_key or not run.workspace_id:
        return None
    return (await db.execute(
        select(AgentSubscription.id).where(
            AgentSubscription.entity_id == run.entity_id,
            AgentSubscription.workspace_id == run.workspace_id,
            AgentSubscription.service_key == service_key,
            AgentSubscription.status == "active",
        ).limit(1)
    )).scalar_one_or_none()


async def _project_step_output(
    db: AsyncSession,
    *,
    run: Any,
    context: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if result.get("status") != "completed":
        return
    output = _display_output(result.get("output"))
    if not output:
        return
    step_id = str(step.get("id") or "")
    existing = (await db.execute(
        select(Message.id).where(
            Message.conversation_id == str(context["conversation_id"]),
            Message.meta["workflow_run_id"].as_string() == str(run.id),
            Message.meta["workflow_step_id"].as_string() == step_id,
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return
    from packages.core.services.conversation_messages import add_message

    subscription_id = await _resolve_subscription_id(db, run=run, step=step)
    await add_message(
        db,
        str(context["conversation_id"]),
        role="assistant" if subscription_id else "system",
        content=output,
        author_subscription_id=subscription_id,
        message_kind="agent_update" if subscription_id else "step_event",
        refs=[
            {"type": "workflow_run", "id": run.id},
            {"type": "workflow_step", "id": step_id, "title": step.get("name") or step_id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_step_id": step_id,
            "workflow_step_status": "completed",
        },
    )


async def _project_wait_action(
    db: AsyncSession,
    *,
    run: Any,
    context: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if not context.get("wait_bridge") or result.get("status") != "paused":
        return
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    wait_config = (
        result.get("wait_config")
        if isinstance(result.get("wait_config"), dict)
        else {}
    )
    wait_type = str(result.get("wait_type") or config.get("wait_type") or "approval").lower()
    if wait_type not in {"approval", "input", "human_input"}:
        return
    step_id = str(step.get("id") or "")
    existing = (await db.execute(
        select(Message.id).where(
            Message.conversation_id == str(context["conversation_id"]),
            Message.meta["workflow_run_id"].as_string() == str(run.id),
            Message.meta["workflow_step_id"].as_string() == step_id,
            Message.pending_action.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return
    from packages.core.services.conversation_messages import add_message

    message = str(result.get("output") or config.get("message") or step.get("name") or "Input required")
    response_variable = str(
        wait_config.get("response_variable")
        or config.get("response_variable")
        or f"{step_id}_response"
    )
    kind = (
        PendingActionKind.WORKFLOW_APPROVAL.value
        if wait_type == "approval"
        else PendingActionKind.WORKFLOW_INPUT.value
    )
    options = wait_config.get("options", config.get("options"))
    if not isinstance(options, list) or not options:
        options = (
            ["approve", "cancel"]
            if kind == PendingActionKind.WORKFLOW_APPROVAL
            else ["respond", "cancel"]
        )
    pending_action = {
        "kind": kind,
        "workflow_run_id": run.id,
        "workflow_binding_id": run.binding_id,
        "step_id": step_id,
        "response_variable": response_variable,
        "prompt": message,
        "options": [str(option) for option in options],
    }
    if result.get("review") is not None:
        pending_action["review"] = result["review"]
    if result.get("review_title") is not None:
        pending_action["review_title"] = str(result["review_title"])
    await add_message(
        db,
        str(context["conversation_id"]),
        role="system",
        content=message,
        message_kind="hitl_request",
        refs=[
            {"type": "workflow_run", "id": run.id},
            {"type": "workflow_step", "id": step_id, "title": step.get("name") or step_id},
        ],
        pending_action=pending_action,
        meta={
            "workflow_run_id": run.id,
            "workflow_step_id": step_id,
            "workflow_step_status": "paused",
        },
    )


async def _project_final_output(
    db: AsyncSession,
    *,
    run: Any,
    context: dict[str, Any],
    activity: Message,
) -> None:
    output = _display_output((run.variables or {}).get("__result"))
    if not output:
        return
    existing = (await db.execute(
        select(Message.id).where(
            Message.conversation_id == str(context["conversation_id"]),
            Message.meta["workflow_run_id"].as_string() == str(run.id),
            Message.meta["workflow_final_output"].as_boolean().is_(True),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return
    subscription_id = str((activity.meta or {}).get("workflow_current_subscription_id") or "") or None
    from packages.core.services.conversation_messages import add_message

    await add_message(
        db,
        str(context["conversation_id"]),
        role="assistant" if subscription_id else "system",
        content=output,
        author_subscription_id=subscription_id,
        message_kind="agent_update",
        refs=[{"type": "workflow_run", "id": run.id}],
        meta={
            "workflow_run_id": run.id,
            "workflow_final_output": True,
        },
    )


async def project_workflow_step(
    db: AsyncSession,
    *,
    run: Any,
    step: dict[str, Any],
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    context = _entrypoint_context(run)
    visibility = _step_visibility(step)
    if context is None or visibility == "hidden":
        return
    settings = workflow_projection_settings(context)
    activity = await _activity_message(db, run, context)
    if activity is None:
        return
    step_id = str(step.get("id") or "")
    subscription_id = await _resolve_subscription_id(db, run=run, step=step)
    row = None
    if settings["progress"]:
        meta = dict(activity.meta or {})
        snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
        snapshot_nodes = (
            snapshot.get("nodes")
            if isinstance(snapshot.get("nodes"), list)
            else []
        )
        steps = workflow_progress_steps(
            run,
            activity_status=str(meta.get("workflow_status") or ""),
            existing_steps=meta.get("workflow_steps"),
        )
        for current in steps:
            if str(current.get("id") or "") == step_id:
                current["status"] = str(status or "running")
                if subscription_id:
                    current["subscription_id"] = subscription_id
                row = current
                break
        if row is None and not snapshot_nodes:
            row = {
                "id": step_id,
                "name": str(step.get("name") or step_id),
                "type": str(step.get("type") or ""),
                "status": str(status or "running"),
                **({"subscription_id": subscription_id} if subscription_id else {}),
            }
            steps.append(row)
        meta["workflow_steps"] = steps
        meta["workflow_status"] = "paused" if status == "paused" else "running"
        meta["workflow_current_step_id"] = step_id
        if subscription_id:
            meta["workflow_current_subscription_id"] = subscription_id
        activity.meta = meta
        if row is not None:
            activity.content = (
                f"{meta.get('workflow_title') or 'Workflow'}: "
                f"{row['name']} is {row['status']}."
            )
        await _notify_update(db, activity)
    if result is not None:
        if (
            settings["step_outputs"] == "all"
            or (settings["step_outputs"] == "explicit" and visibility == "output")
        ):
            await _project_step_output(db, run=run, context=context, step=step, result=result)
        await _project_wait_action(db, run=run, context=context, step=step, result=result)


async def project_workflow_run_status(
    db: AsyncSession,
    *,
    run: Any,
) -> None:
    context = _entrypoint_context(run)
    if context is None:
        return
    activity = await _activity_message(db, run, context)
    if activity is None:
        return
    settings = workflow_projection_settings(context)
    meta = dict(activity.meta or {})
    meta["workflow_steps"] = workflow_progress_steps(
        run,
        activity_status=str(meta.get("workflow_status") or ""),
        existing_steps=meta.get("workflow_steps"),
    )
    meta["workflow_status"] = str(run.status)
    business_outcome, retry_state = _workflow_business_state(run)
    meta["workflow_business_outcome"] = business_outcome
    meta["workflow_attempt_number"] = run.effective_attempt_number
    if run.effective_retry_of_run_id:
        meta["workflow_retry_of_run_id"] = run.effective_retry_of_run_id
    retry_from_step_id = str(
        retry_state.get("retry_from_step_id")
        or run.effective_retry_from_step_id
        or (run.current_step_id if run.status == "failed" else "")
        or ""
    ).strip()
    if retry_from_step_id:
        meta["workflow_retry_from_step_id"] = retry_from_step_id
    raw_workflow_error = _workflow_error_payload(run)
    workflow_error = (
        summarize_trace_value(raw_workflow_error)
        if raw_workflow_error is not None
        else None
    )
    if workflow_error:
        meta["workflow_error"] = workflow_error
    else:
        meta.pop("workflow_error", None)
    activity.meta = meta
    title = str(meta.get("workflow_title") or "Workflow")
    if business_outcome == "needs_input":
        activity.content = f"{title} needs input before it can continue."
    elif business_outcome == "revision_required":
        activity.content = f"{title} requires a revision."
    elif business_outcome == "ready_for_acceptance":
        activity.content = f"{title} is ready for playback and acceptance."
    elif business_outcome == "accepted":
        activity.content = f"{title} was accepted."
    elif run.status == "completed":
        activity.content = f"{title} completed."
    elif run.status == "running":
        activity.content = f"{title} is starting."
    elif run.status == "failed":
        error_message = summarize_trace_text(
            raw_workflow_error,
            fallback="Workflow failed",
        )
        activity.content = f"{title} failed: {error_message}"
    elif run.status == "cancelled":
        activity.content = f"{title} was cancelled."
    elif run.status == "paused":
        activity.content = f"{title} is waiting for input."
    retryable = run.status == "failed" or (
        run.status == "completed"
        and business_outcome == "needs_input"
    )
    if retryable and retry_from_step_id:
        editable_input_schema = _workflow_retry_input_schema(run, retry_state)
        observed_problem = retry_state.get("observed_problem")
        if observed_problem is None:
            observed_problem = workflow_error
        else:
            observed_problem = summarize_trace_value(observed_problem)
        required_change = summarize_trace_value(
            retry_state.get("required_change")
            or "Correct the failed input or external state, then retry this step."
        )
        preserved_receipts = summarize_trace_value(
            retry_state.get("preserved_receipts") or []
        )
        activity.pending_action = {
            "kind": PendingActionKind.WORKFLOW_RETRY.value,
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "business_outcome": business_outcome,
            "phase": retry_state.get("phase") or "execution",
            "step_id": retry_state.get("step_id") or run.current_step_id,
            "retry_from_step_id": retry_from_step_id,
            "retry_segment_ids": retry_state.get("segment_ids") or [],
            "observed_problem": observed_problem,
            "required_change": required_change,
            "editable_input_schema": editable_input_schema,
            "preserved_receipts": preserved_receipts,
            "values": _workflow_retry_values(run, retry_state, editable_input_schema),
            "options": ["retry", "cancel"],
        }
    elif activity.pending_action and activity.pending_action.get("kind") == PendingActionKind.WORKFLOW_RETRY:
        activity.pending_action = None
    await _notify_update(db, activity)
    if run.status == "completed" and settings["final_output"]:
        await _project_final_output(
            db,
            run=run,
            context=context,
            activity=activity,
        )
