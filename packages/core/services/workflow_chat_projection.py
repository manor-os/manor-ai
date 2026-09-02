"""Project configured Workflow Run progress into its originating Workspace Chat."""
from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from typing import Any

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowRun
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
logger = logging.getLogger(__name__)


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


def workflow_chat_context(run: Any) -> dict[str, Any] | None:
    """Return the Chat that owns this run's human interaction surface.

    Workflows may start from a personal Chat, an Agent tool call, or a
    Workspace Chat.  The Workspace remains an execution/resource boundary;
    it is not implicitly the destination for HITL.  New runs record the
    generic ``_workflow_chat_origin`` key while the legacy Workspace key stays
    readable for existing runs and retries.
    """
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    context = trigger_data.get("_workflow_chat_origin")
    if not isinstance(context, dict):
        if str(getattr(run, "trigger_source", "") or "") != "workspace_chat":
            return None
        context = trigger_data.get("_workspace_chat_entrypoint")
    if not isinstance(context, dict) or not context.get("enabled"):
        return None
    if not context.get("conversation_id") or not context.get("activity_message_id"):
        return None
    return context


def _entrypoint_context(run: Any) -> dict[str, Any] | None:
    """Backward-compatible private alias used by the projectors below."""
    return workflow_chat_context(run)


def workflow_projection_settings(context: dict[str, Any]) -> dict[str, Any]:
    projection = context.get("projection") if isinstance(context.get("projection"), dict) else {}
    step_outputs = str(projection.get("step_outputs") or "explicit").lower()
    if step_outputs not in {"explicit", "all", "none"}:
        step_outputs = "explicit"
    approval_review = str(projection.get("approval_review") or "inline").lower()
    if approval_review not in {"inline", "history"}:
        approval_review = "inline"
    raw_final_output_fields = projection.get("final_output_fields")
    final_output_fields = (
        list(dict.fromkeys(
            str(field).strip()
            for field in raw_final_output_fields
            if str(field).strip() and len(str(field).strip()) <= 128
        ))[:32]
        if isinstance(raw_final_output_fields, list)
        else []
    )
    return {
        "progress": bool(projection.get("progress", True)),
        "step_outputs": step_outputs,
        "final_output": bool(projection.get("final_output", True)),
        "approval_review": approval_review,
        "final_output_fields": final_output_fields,
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


def _workflow_final_output_value(
    value: Any,
    *,
    final_output_fields: list[str] | None = None,
) -> Any:
    """Project explicitly allowlisted terminal fields when configured."""
    if not final_output_fields:
        return value
    if not isinstance(value, dict):
        return None
    source = value.get("input") if isinstance(value.get("input"), dict) else value
    compact = {
        field: deepcopy(source[field])
        for field in final_output_fields
        if field in source
    }
    return compact or None


def _compact_result_text(value: Any, *, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:max(0, limit - 1)].rstrip()}…"


def _workflow_result_record(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def workflow_result_chat_projection(
    run: Any,
    value: Any,
) -> tuple[str, dict[str, Any]]:
    """Return concise Chat copy plus a durable link to the full run result."""
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    workflow_name = str(snapshot.get("name") or run.workflow_id or "Workflow").strip()
    record = _workflow_result_record(value)
    result_title = ""
    result_summary = ""
    result_kind = "result"
    platforms: list[str] = []
    from packages.core.services.workflow_publication_receipts import (
        publication_receipts_from_step_results,
    )

    publication_receipts = publication_receipts_from_step_results(
        getattr(run, "step_results", None)
    )
    publication_platforms = list(dict.fromkeys(
        str(item.get("platform") or "").strip()
        for item in publication_receipts
        if str(item.get("platform") or "").strip()
    ))
    verified_publications = sum(
        1
        for item in publication_receipts
        if str(item.get("verification_status") or "").lower() == "verified"
    )

    if record:
        result_title = _compact_result_text(
            record.get("title") or record.get("name") or record.get("topic_id") or "",
            limit=180,
        )
        result_summary = _compact_result_text(
            record.get("thesis")
            or record.get("summary")
            or record.get("objective")
            or record.get("message")
            or "",
        )
        raw_platforms = record.get("target_platforms")
        if isinstance(raw_platforms, list):
            platforms = [
                _compact_result_text(item, limit=48)
                for item in raw_platforms[:6]
                if _compact_result_text(item, limit=48)
            ]
        if record.get("topic_id") or (
            record.get("title")
            and record.get("thesis")
            and record.get("target_platforms")
        ):
            result_kind = "topic_brief"
        if record.get("schema_version") == "publication-receipt/v1":
            result_kind = "publication_receipt"
    elif isinstance(value, str):
        result_summary = _compact_result_text(value)
    elif value is not None:
        result_summary = _compact_result_text(_display_output(value))

    if publication_receipts:
        result_kind = "publication_receipt"
        if len(publication_receipts) == 1:
            platform = publication_platforms[0] if publication_platforms else "Platform"
            lead = f"Publication verified: {platform}."
        else:
            lead = f"{verified_publications} publications verified."
    elif result_kind == "topic_brief":
        lead = f"Topic brief ready: {result_title}" if result_title else "Topic brief ready."
    elif result_title:
        lead = f"{workflow_name} completed: {result_title}"
    else:
        lead = f"{workflow_name} completed."

    lines = [lead]
    if result_summary and result_summary != result_title:
        lines.append(result_summary)
    if platforms:
        lines.append(f"Target platforms: {', '.join(platforms)}")
    if publication_platforms:
        lines.append(f"Published to: {', '.join(publication_platforms)}")

    run_id = str(run.id)
    workflow_id = str(run.workflow_id)
    reference = {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "run_id": run_id,
        "url": f"/flows?workflow={workflow_id}&run={run_id}",
        "status": str(getattr(run, "status", None) or "completed"),
        "kind": result_kind,
        "title": result_title or None,
        "summary": result_summary or None,
        "publication_summary": {
            "count": len(publication_receipts),
            "verified": verified_publications,
            "platforms": publication_platforms,
        } if publication_receipts else None,
    }
    return "\n\n".join(line for line in lines if line), reference


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
    activity = (await db.execute(
        select(Message).join(
            Conversation,
            Conversation.id == Message.conversation_id,
        ).where(
            Message.id == str(context["activity_message_id"]),
            Message.conversation_id == str(context["conversation_id"]),
            Message.message_kind == "workflow_activity",
            Message.meta["workflow_binding_id"].as_string() == str(run.binding_id),
            Conversation.entity_id == str(run.entity_id),
        ).with_for_update()
    )).scalar_one_or_none()
    if activity is None:
        return None

    activity_meta = activity.meta if isinstance(activity.meta, dict) else {}
    projected_run_id = str(activity_meta.get("workflow_run_id") or "").strip()
    if projected_run_id == str(run.id):
        return activity
    if not projected_run_id:
        return None

    lineage_root_run_id = str(run.lineage_root_run_id or run.id)
    same_family_run_id = (await db.execute(
        select(WorkflowRun.id).where(
            WorkflowRun.id == projected_run_id,
            WorkflowRun.entity_id == run.entity_id,
            WorkflowRun.workflow_id == run.workflow_id,
            WorkflowRun.workspace_id == run.workspace_id,
            WorkflowRun.binding_id == run.binding_id,
            or_(
                WorkflowRun.id == lineage_root_run_id,
                WorkflowRun.lineage_root_run_id == lineage_root_run_id,
            ),
        ).limit(1)
    )).scalar_one_or_none()
    return activity if same_family_run_id else None


async def _project_cancelled_family_activities(
    db: AsyncSession,
    *,
    run: Any,
    context: dict[str, Any],
    current_activity: Message,
) -> None:
    entity_id = getattr(run, "entity_id", None)
    workflow_id = getattr(run, "workflow_id", None)
    workspace_id = getattr(run, "workspace_id", None)
    binding_id = getattr(run, "binding_id", None)
    if not all((entity_id, workflow_id, workspace_id, binding_id)):
        return

    lineage_root_run_id = str(getattr(run, "lineage_root_run_id", None) or run.id)
    family_run_ids = select(WorkflowRun.id).where(
        WorkflowRun.entity_id == entity_id,
        WorkflowRun.workflow_id == workflow_id,
        WorkflowRun.workspace_id == workspace_id,
        WorkflowRun.binding_id == binding_id,
        or_(
            WorkflowRun.id == lineage_root_run_id,
            WorkflowRun.lineage_root_run_id == lineage_root_run_id,
        ),
    )
    older_activities = list((await db.execute(
        select(Message).join(
            Conversation,
            Conversation.id == Message.conversation_id,
        ).where(
            Message.id != current_activity.id,
            Message.conversation_id == str(context["conversation_id"]),
            Message.message_kind == "workflow_activity",
            Message.meta["workflow_binding_id"].as_string() == str(binding_id),
            Message.meta["workflow_run_id"].as_string().in_(family_run_ids),
            Conversation.entity_id == str(entity_id),
        ).with_for_update()
    )).scalars().all())
    for activity in older_activities:
        meta = dict(activity.meta or {})
        meta["workflow_status"] = "cancelled"
        activity.meta = meta
        title = str(meta.get("workflow_title") or "Workflow")
        activity.content = f"{title} was cancelled."
        activity.pending_action = None
        await _notify_update(db, activity)


async def _notify_update(db: AsyncSession, message: Message) -> None:
    try:
        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.id == message.conversation_id,
            )
        )).scalar_one_or_none()
        if not conversation:
            return
        loop = asyncio.get_running_loop()
        if conversation.workspace_id:
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
        else:
            payload = {
                "target": "user",
                "user_id": conversation.user_id,
                "entity_id": conversation.entity_id,
                "event": "conversation_message",
                "data": {
                    "conversation_id": conversation.id,
                    "message_id": message.id,
                    "message_kind": message.message_kind,
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
    if (
        kind == PendingActionKind.WORKFLOW_APPROVAL
        and config.get("allow_always")
        and (config.get("approval_action_key") or config.get("approval_capability_id"))
        and "always_approve" not in options
    ):
        options = [
            *options[:1],
            "always_approve",
            *options[1:],
        ]
    pending_action = {
        "kind": kind,
        "workflow_run_id": run.id,
        "workflow_binding_id": run.binding_id,
        "step_id": step_id,
        "response_variable": response_variable,
        "prompt": message,
        "options": [str(option) for option in options],
    }
    review_in_history = (
        kind == PendingActionKind.WORKFLOW_APPROVAL
        and result.get("review") is not None
        and workflow_projection_settings(context)["approval_review"] == "history"
    )
    if review_in_history:
        pending_action["review_location"] = "workflow_history"
    elif result.get("review") is not None:
        pending_action["review"] = result["review"]
    if result.get("review_title") is not None:
        pending_action["review_title"] = str(result["review_title"])
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    workflow_name = str(snapshot.get("name") or run.workflow_id)
    workflow_url = f"/flows?workflow={run.workflow_id}"
    node_name = str(step.get("name") or step_id)
    hitl_message = await add_message(
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
    # Personal Chat reads its interactive cards from message.meta.hitl_requests
    # while Workspace Chat reads pending_action. Persist both projections on
    # the same message so the run always returns to its actual origin surface.
    hitl_request = {
        "id": hitl_message.id,
        "type": "approval" if wait_type == "approval" else "human_input",
        "prompt": message,
        "action": str(config.get("approval_action_key") or "workflow.review"),
        "tool": "workflow",
        "options": [str(option) for option in options],
        "review_title": str(result.get("review_title") or node_name),
        "workflow": {
            "id": run.workflow_id,
            "name": workflow_name,
            "run_id": run.id,
            "url": workflow_url,
        },
        "node": {
            "id": step_id,
            "name": node_name,
            "type": str(step.get("type") or "wait"),
        },
        "workflow_run_id": run.id,
        "workflow_step_id": step_id,
        "resolved": False,
    }
    if review_in_history:
        hitl_request["review_location"] = "workflow_history"
    elif result.get("review") is not None:
        hitl_request["review"] = result["review"]
    hitl_message.meta = {
        **dict(hitl_message.meta or {}),
        "hitl_requests": [hitl_request],
    }
    flag_modified(hitl_message, "meta")


async def _project_final_output(
    db: AsyncSession,
    *,
    run: Any,
    context: dict[str, Any],
    activity: Message,
) -> None:
    raw_output = _workflow_final_output_value(
        (run.variables or {}).get("__result"),
        final_output_fields=workflow_projection_settings(context)[
            "final_output_fields"
        ],
    )
    if not _display_output(raw_output):
        return
    existing = (await db.execute(
        select(Message).where(
            Message.conversation_id == str(context["conversation_id"]),
            Message.meta["workflow_run_id"].as_string() == str(run.id),
            Message.meta["workflow_final_output"].as_boolean().is_(True),
        ).limit(1)
    )).scalar_one_or_none()
    output, workflow_result = workflow_result_chat_projection(run, raw_output)
    if existing:
        # Runs completed before structured result cards were introduced stored
        # the full JSON payload directly in Chat. Upgrade that projection when
        # the same run is observed again without creating a duplicate message.
        existing_meta = dict(existing.meta or {})
        if not isinstance(existing_meta.get("workflow_result"), dict):
            existing.content = output
            existing.refs = [
                {
                    "type": "workflow",
                    "id": run.workflow_id,
                    "title": workflow_result["workflow_name"],
                },
                {"type": "workflow_run", "id": run.id},
            ]
            existing.meta = {
                **existing_meta,
                "workflow_run_id": run.id,
                "workflow_final_output": True,
                "workflow_result": workflow_result,
            }
            flag_modified(existing, "meta")
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
        refs=[
            {
                "type": "workflow",
                "id": run.workflow_id,
                "title": workflow_result["workflow_name"],
            },
            {"type": "workflow_run", "id": run.id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_final_output": True,
            "workflow_result": workflow_result,
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
    """Settle authoritative run relations, then project Chat best-effort."""
    from packages.core.services.scheduler_service import (
        finalize_scheduled_workflow_run,
    )
    from packages.core.services.proposal_workflow_runs import (
        sync_proposal_workflow_run_item,
    )

    await finalize_scheduled_workflow_run(db, run)
    await sync_proposal_workflow_run_item(db, run)
    # Keep scheduler/proposal settlement outside the UI savepoint. A broken
    # Message row or notification projection must not orphan authoritative
    # Workflow relationships when the caller commits the run terminal state.
    await db.flush()
    try:
        async with db.begin_nested():
            await _project_workflow_run_status_chat(db, run=run)
    except Exception:
        logger.debug("Workflow Chat run projection skipped", exc_info=True)


async def _project_workflow_run_status_chat(
    db: AsyncSession,
    *,
    run: Any,
) -> None:
    """Project run status into Chat inside the caller's UI savepoint."""
    context = _entrypoint_context(run)
    if context is None:
        return
    activity = await _activity_message(db, run, context)
    if activity is None:
        return
    settings = workflow_projection_settings(context)
    meta = dict(activity.meta or {})
    meta["workflow_run_id"] = str(run.id)
    meta["workflow_binding_id"] = str(run.binding_id)
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
    if run.status == "cancelled":
        activity.content = f"{title} was cancelled."
    elif business_outcome == "needs_input":
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
    if run.status == "cancelled":
        await _project_cancelled_family_activities(
            db,
            run=run,
            context=context,
            current_activity=activity,
        )
    await _notify_update(db, activity)
    if (
        run.status == "completed"
        and business_outcome not in _ACTIONABLE_COMPLETED_OUTCOMES
        and settings["final_output"]
    ):
        await _project_final_output(
            db,
            run=run,
            context=context,
            activity=activity,
        )
