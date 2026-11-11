"""Project configured Workflow Run progress into its originating Workspace Chat."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.ai.workflow_failure import user_facing_run_error, user_facing_step_error
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.i18n import t, text_locale
from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import AgentSubscription
from packages.core.services.workflow_contracts import workflow_step_label, workflow_wait_options
from packages.core.services.workflow_step_resilience import (
    is_transient_retry_record,
    transient_retry_status_message,
)
from packages.core.services.workflow_run_trace import (
    STALLED_PROBLEM,
    STALLED_RESTART_CHANGE,
    STALLED_RETRY_ERROR,
    summarize_trace_text,
    summarize_trace_value,
    workflow_chat_projection_visibility,
    workflow_execution_steps,
    workflow_run_restarted_as,
    workflow_run_stalled,
)


_NOTIFICATION_QUEUE_KEY = "workflow_chat_projection_notifications"
_NOTIFICATION_LISTENER_KEY = "workflow_chat_projection_listeners"
_ACTIONABLE_COMPLETED_OUTCOMES = {
    "needs_input",
}
_FINAL_ARTIFACT_CONTAINER_KEYS = {
    "artifact",
    "artifacts",
    "assets",
    "deliverable",
    "deliverables",
    "documents",
    "files",
    "final_artifact",
    "final_artifacts",
    "final_document",
    "final_documents",
    "final_file",
    "final_files",
    "final_video",
    "final_videos",
    "knowledge_artifacts",
    "outputs",
}
# Fields that name what a result was made from (the Knowledge it read, the
# claims it cites). Their file links are sources, never something delivered.
_SOURCE_REFERENCE_KEYS = {
    "citations",
    "evidence",
    "knowledge_refs",
    "references",
    "source_refs",
    "sources",
    "supported_claims",
}
logger = logging.getLogger(__name__)


def _workflow_run_url(run: Any) -> str:
    workflow_id = quote(str(run.workflow_id), safe="")
    run_id = quote(str(run.id), safe="")
    return f"/flows/{workflow_id}/runs/{run_id}"


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


def scheduled_workflow_chat_origin(conversation_id: str | None, workspace_id: str | None) -> dict[str, Any]:
    """The chat a scheduled run reports to, attached only once it needs a person.

    A schedule starts the same Flow every few minutes; posting each run would
    bury the chat. The run carries this dormant origin, and the first time it
    stops for a person (see ``attach_workflow_chat_on_wait``) the run gets its
    chat row and the decision card, as a run started from chat would. Without
    a chat of its own it uses the Workspace chat, made then if nobody has
    written there yet.
    """
    return {
        "enabled": True,
        "route_source": "schedule",
        "conversation_id": conversation_id,
        "workspace_id": workspace_id,
        "wait_bridge": True,
        "attach_on_wait": True,
        "projection": {"progress": True, "step_outputs": "none", "final_output": True},
    }


async def attach_workflow_chat_on_wait(
    db: AsyncSession,
    *,
    run: Any,
    step: dict[str, Any],
    result: dict[str, Any] | None,
) -> bool:
    """Give a dormant-origin run its chat row when it pauses on a person."""
    if workflow_chat_context(run) is not None or not isinstance(result, dict):
        return False
    if result.get("status") != "paused":
        return False
    trigger_data = dict(run.trigger_data) if isinstance(run.trigger_data, dict) else {}
    origin = trigger_data.get("_workflow_chat_origin")
    if not isinstance(origin, dict) or not origin.get("attach_on_wait"):
        return False
    from packages.core.services.workflow_chat_approvals import workflow_chat_wait_config

    if workflow_chat_wait_config(run, step) is None:
        return False
    if origin.get("conversation_id"):
        conversation = await db.get(Conversation, str(origin["conversation_id"]))
    elif origin.get("workspace_id") and str(origin["workspace_id"]) == str(run.workspace_id or ""):
        from packages.core.workspace_chat.service import ensure_main_conversation

        conversation = await ensure_main_conversation(
            db, entity_id=str(run.entity_id), workspace_id=str(run.workspace_id),
        )
    else:
        return False
    if conversation is None or str(conversation.entity_id) != str(run.entity_id):
        return False
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.conversation_messages import add_message

    workflow = await db.get(WorkflowDefinition, run.workflow_id) if run.workflow_id else None
    title = str(getattr(workflow, "name", "") or "Workflow")
    activity = await add_message(
        db,
        conversation.id,
        role="system",
        content=f"{title}: {workflow_step_label(step) or step.get('id')} needs you.",
        message_kind="workflow_activity",
        refs=[
            {"type": "workflow", "id": run.workflow_id, "title": title},
            {"type": "workflow_run", "id": run.id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "workflow_title": title,
            "workflow_status": "paused",
            "workflow_route_source": "schedule",
            "workflow_steps": workflow_progress_steps(run, activity_status="paused"),
        },
    )
    origin = {**origin, "conversation_id": conversation.id, "activity_message_id": activity.id}
    trigger_data["_workflow_chat_origin"] = origin
    run.trigger_data = trigger_data
    return True


def _entrypoint_context(run: Any) -> dict[str, Any] | None:
    """Backward-compatible private alias used by the projectors below."""
    return workflow_chat_context(run)


def _agent_recovery_checkpoint(run: Any, step_id: str, result: dict[str, Any]) -> bool:
    snapshot = getattr(run, "execution_snapshot", None)
    steps = snapshot.get("steps") if isinstance(snapshot, dict) else None
    step = next(
        (
            item for item in steps or []
            if isinstance(item, dict) and str(item.get("id") or "") == step_id
        ),
        {},
    )
    return (
        step.get("type") == "agent"
        and result.get("producer_completed") is not True
        and isinstance(result.get("chrome_outcome"), dict)
    )


def _failed_effect_requires_reconciliation(
    run: Any,
    step_id: str,
    result: dict[str, Any],
) -> bool:
    if result.get("status") != "failed" or _agent_recovery_checkpoint(run, step_id, result):
        return False
    from packages.core.ai.runtime.outcome import attempt_forbids_replay

    return attempt_forbids_replay(result)


def workflow_step_display_name(run: Any, step_id: str) -> str | None:
    """Return the name people gave a step in the run's Flow, if it has one."""
    step_id = str(step_id or "").strip()
    if not step_id:
        return None
    snapshot = getattr(run, "definition_snapshot", None)
    for node in (snapshot.get("nodes") or []) if isinstance(snapshot, dict) else []:
        if isinstance(node, dict) and str(node.get("id") or "") == step_id:
            name = str(node.get("name") or "").strip()
            if name:
                return name
    # Run lists can leave trigger_data unloaded; never lazy-load it here.
    trigger_data = getattr(run, "__dict__", {}).get("trigger_data")
    names = trigger_data.get("_workflow_node_names") if isinstance(trigger_data, dict) else None
    name = names.get(step_id) if isinstance(names, dict) else None
    name = str(name or "").strip()
    return name if name and name != step_id else None


def workflow_retry_from_step_id(run: Any, retry_state: dict[str, Any]) -> str:
    current_step_id = str(getattr(run, "current_step_id", "") or "").strip()
    step_results = getattr(run, "step_results", None) or {}
    current_result = step_results.get(current_step_id)
    if (
        isinstance(current_result, dict)
        and (
            (
                current_result.get("retry_blocked") is True
                and not _agent_recovery_checkpoint(run, current_step_id, current_result)
            )
            or _failed_effect_requires_reconciliation(
                run, current_step_id, current_result,
            )
        )
    ):
        return ""
    configured = str(retry_state.get("retry_from_step_id") or "").strip()
    if configured:
        return configured
    if isinstance(current_result, dict):
        configured = str(current_result.get("retry_from_step_id") or "").strip()
        if configured:
            return configured
    status = str(getattr(run, "status", "") or "")
    if status == "cancelled" and current_step_id and current_result is None:
        from packages.core.services.workflow_agent_recovery import (
            workflow_cancelled_agent_requires_review,
        )

        if workflow_cancelled_agent_requires_review(run):
            return ""
        return current_step_id
    if status == "failed" and current_step_id:
        is_retry_attempt = bool(
            str(getattr(run, "effective_retry_of_run_id", "") or "").strip()
        )
        prior_retry_from = str(
            getattr(run, "effective_retry_from_step_id", "") or ""
        ).strip()
        if prior_retry_from and not is_retry_attempt:
            return prior_retry_from
        # A stop only reports why the run ended; the retry redoes the work before it.
        snapshot = getattr(run, "execution_snapshot", None)
        steps = [item for item in (snapshot.get("steps") if isinstance(snapshot, dict) else None) or [] if isinstance(item, dict)]
        from packages.core.services.workflow_retry import TERMINAL_STEP_TYPES, error_route_origin, work_step_before

        current_step_id = error_route_origin(steps, step_results, current_step_id) or current_step_id
        if any(item.get("id") == current_step_id and item.get("type") in TERMINAL_STEP_TYPES for item in steps):

            return work_step_before(steps, step_results, current_step_id) or current_step_id
        return current_step_id
    return str(getattr(run, "effective_retry_from_step_id", "") or "").strip()


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
    """Project the stable, display-safe execution path into Workspace Chat."""
    steps = workflow_execution_steps(
        run,
        activity_status=activity_status,
        existing_steps=existing_steps,
    )
    steps = _workflow_chat_path_steps(run, steps)
    if str(getattr(run, "status", "") or "").strip().lower() != "completed":
        return steps

    business_outcome, _retry_state = _workflow_business_state(run)
    if business_outcome not in _ACTIONABLE_COMPLETED_OUTCOMES:
        return steps

    results = run.step_results if isinstance(run.step_results, dict) else {}
    reached_ids = {
        str(step_id)
        for step_id, result in results.items()
        if isinstance(result, dict)
        and result.get("skipped") is not True
        and str(result.get("status") or "").strip().lower() != "skipped"
    }
    return [step for step in steps if step.get("id") in reached_ids]


def _workflow_chat_path_steps(
    run: Any,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide pending branches excluded by durable routing receipts in Chat."""
    snapshot = getattr(run, "definition_snapshot", None)
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
    if not isinstance(nodes, list) or not any(
        isinstance(node, dict) and "targets" in node for node in nodes
    ):
        return steps

    node_ids = [
        str(node.get("id") or "").strip()
        for node in nodes
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    ]
    node_id_set = set(node_ids)
    targets_by_id = {
        str(node.get("id") or "").strip(): [
            str(target)
            for target in (node.get("targets") or [])
            if str(target).strip() in node_id_set
        ]
        for node in nodes
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }
    condition_ids = {
        str(node.get("id") or "").strip()
        for node in nodes
        if isinstance(node, dict)
        and str(node.get("type") or "").strip().lower() == "condition"
    }
    incoming_ids = {
        target
        for targets in targets_by_id.values()
        for target in targets
    }
    entry_ids = [node_id for node_id in node_ids if node_id not in incoming_ids]
    results = getattr(run, "step_results", None)
    results = results if isinstance(results, dict) else {}

    def selected_targets(node_id: str) -> list[str]:
        result = results.get(node_id)
        if not isinstance(result, dict) or "next_override" not in result:
            if node_id in condition_ids:
                return []
            return targets_by_id.get(node_id, [])
        value = result.get("next_override")
        values = value if isinstance(value, list) else [value]
        return [
            str(target)
            for target in values
            if str(target or "").strip() in node_id_set
        ]

    current_id = str(getattr(run, "current_step_id", "") or "").strip()
    pending = list(entry_ids)
    if current_id in node_id_set and current_id not in pending:
        pending.append(current_id)
    active_ids: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in active_ids:
            continue
        active_ids.add(node_id)
        pending.extend(selected_targets(node_id))

    return [
        step
        for step in steps
        if str(step.get("status") or "").strip().lower() != "pending"
        or str(step.get("id") or "") in active_ids
    ]


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


# The completed message shows the result itself, so nobody has to open the run to read it.
_RESULT_MESSAGE_LIMIT = 4000


_text_locale = text_locale


# What a person reads when a run failed. The raw error stays on the run
# (meta.workflow_error and the run page); Chat drops embedded payloads.
_USER_ERROR_LIMIT = 360
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s")


def _drop_json_payloads(text: str) -> str:
    """Remove embedded JSON objects/arrays (tool error envelopes and the like)."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "{[" and text[index + 1:index + 2] in {'"', "{", "\'"}:
            depth, quote, end = 0, False, None
            for cursor in range(index, len(text)):
                current = text[cursor]
                if quote:
                    if current == "\\":
                        continue
                    if current == '"' and text[cursor - 1] != "\\":
                        quote = False
                    continue
                if current == '"':
                    quote = True
                elif current in "{[":
                    depth += 1
                elif current in "}]":
                    depth -= 1
                    if depth == 0:
                        end = cursor
                        break
            if end is not None and end - index > 30:
                index = end + 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def default_retry_change(context: Any) -> str:
    """What to do before Retry when the failure did not say: in its language.

    Most failures need no edit (a busy machine, a provider hiccup); telling
    the person to "correct the failed input" sent them looking for a mistake
    that was not theirs.
    """
    return t("workflow.retry.required_change_default", locale=_text_locale(str(context or "")))


def user_facing_error_text(value: Any) -> tuple[str, bool]:
    """(readable error, whether anything was left out)."""
    # A bulleted list ("- item" lines) reads as "a; b" in the one-line Chat summary.
    listed = re.sub(r":[ \t]*\n[ \t]*-[ \t]+", ": ", str(value or ""))
    # Items that already end a sentence just follow each other; fragments get "; ".
    listed = re.sub(r"([.。!！?？])?[ \t]*\n[ \t]*-[ \t]+", lambda m: f"{m.group(1)} " if m.group(1) else "; ", listed)
    raw = " ".join(listed.split())
    text = " ".join(_drop_json_payloads(raw).split())
    # A clause that only introduced a removed payload ("… returned:") goes too.
    text = re.sub(r"(?:(?<=[.。！？!?])|^)[^.。！？!?:：]*[:：]\s*(?=[.。]|$)[.。]?", "", text)
    text = re.sub(r"\s+([.。,，;；])", r"\1", text)
    text = re.sub(r"([.。])(?:\s*[.。])+", r"\1", text).strip(" :：")
    if not text:
        # The whole error was "<what>: <payload>": keep what failed.
        text = " ".join(_drop_json_payloads(raw).split()).strip(" :：.。") or raw[:_USER_ERROR_LIMIT]
    shortened = text != raw
    if len(text) > _USER_ERROR_LIMIT:
        cut = text[:_USER_ERROR_LIMIT]
        boundary = max((match.end() for match in _SENTENCE_END_RE.finditer(cut)), default=0)
        text = (cut[:boundary] if boundary > _USER_ERROR_LIMIT // 3 else cut).rstrip() + "…"
        shortened = True
    return text, shortened


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


def _workflow_final_output_attachments(
    value: Any,
    *,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Convert durable file records in the final result to Chat attachments."""
    from packages.core.workspace_chat.notifiers import (
        _artifacts_as_attachments,
        extract_artifacts_for_chat,
    )

    from packages.core.services.file_refs import reply_file_links

    root = _workflow_result_record(value) if isinstance(value, str) else value
    artifacts: list[dict[str, Any]] = []
    preferred_assets: list[dict[str, Any]] = []
    preferred_artifacts: list[dict[str, Any]] = []
    # Agents hand over files as the standard markdown file link (e.g.
    # ``[EP.04.mp4](/viewer/<id>)``) inside text fields; those are deliverables.
    linked: list[dict[str, Any]] = []
    visited: set[int] = set()

    def collect_links(text: str) -> None:
        for label, ref in reply_file_links(text, entity_id=entity_id):
            if ref.document_id:
                linked.append({"kind": "document", "value": ref.document_id, "name": label})
            elif ref.fs_path and (not ref.entity_id or ref.entity_id == entity_id):
                linked.append({"kind": "file", "value": ref.fs_path, "name": label})

    def extract(candidate: Any) -> list[dict[str, Any]]:
        if isinstance(candidate, dict):
            return extract_artifacts_for_chat(candidate)
        if isinstance(candidate, (list, tuple)):
            return [
                artifact
                for item in candidate
                for artifact in extract(item)
            ]
        return []

    def visit(candidate: Any, depth: int) -> None:
        if candidate is None or depth > 6:
            return
        if isinstance(candidate, str):
            collect_links(candidate)
            return
        if isinstance(candidate, (list, tuple)):
            for item in candidate:
                visit(item, depth + 1)
            return
        if not isinstance(candidate, dict) or id(candidate) in visited:
            return
        visited.add(id(candidate))
        artifacts.extend(extract_artifacts_for_chat(candidate))
        for key, nested in candidate.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _SOURCE_REFERENCE_KEYS:
                continue
            if normalized_key == "assets":
                preferred_assets.extend(extract(nested))
            elif normalized_key in _FINAL_ARTIFACT_CONTAINER_KEYS:
                preferred_artifacts.extend(extract(nested))
            if isinstance(nested, (dict, list, tuple, str)):
                visit(nested, depth + 1)

    visit(root if root is not None else value, 0)
    return _artifacts_as_attachments(
        linked + (preferred_assets + preferred_artifacts or artifacts),
        entity_id=entity_id,
    )


def workflow_result_files(value: Any, *, entity_id: str) -> list[dict[str, Any]]:
    """The files a final result delivered: one rule for Chat and the App's outputs."""
    return _workflow_final_output_attachments(value, entity_id=entity_id)


def workflow_project_result_view(value: Any) -> dict[str, Any] | None:
    """A WorkflowProject record read as the project it is, without its bookkeeping.

    A Flow that ends on a project step returns the project record (id,
    revision, key, schema version, folder). A person reads the project: its
    name and its own fields.
    """
    record = _workflow_result_record(value)
    if not record or not {"project_id", "project_type", "project_key"} <= set(record):
        return None
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    fields = {key: deepcopy(item) for key, item in state.items() if key != "project_root" and not str(key).startswith("_")}
    return {**fields, "title": str(state.get("title") or state.get("name") or record.get("project_key") or "")}


def workflow_result_headline(value: Any) -> tuple[str, str]:
    """What a final result is called and says, from the fields results use for that."""
    record = workflow_project_result_view(value) or _workflow_result_record(value)
    if record and not any(record.get(key) for key in ("title", "name", "topic_id", "summary", "message")):
        # A result that wraps its record ({decision, topic_brief}) is named by it.
        record = next((
            nested for nested in record.values()
            if isinstance(nested, dict) and (nested.get("title") or nested.get("name"))
        ), record)
    if record:
        title = _compact_result_text(
            record.get("title") or record.get("name") or record.get("topic_id") or "",
            limit=180,
        )
        summary = _compact_result_text(
            record.get("thesis")
            or record.get("summary")
            or record.get("objective")
            or record.get("message")
            or "",
            limit=_RESULT_MESSAGE_LIMIT,
        )
        return title, summary
    if isinstance(value, str):
        return "", _compact_result_text(value, limit=_RESULT_MESSAGE_LIMIT)
    if value is not None:
        return "", _compact_result_text(_display_output(value), limit=_RESULT_MESSAGE_LIMIT)
    return "", ""


def workflow_run_display_name(run: Any) -> str:
    """The name people started the run under: the Flow's name, not the definition slug."""
    trigger = run.trigger_data if isinstance(getattr(run, "trigger_data", None), dict) else {}
    snapshot = run.definition_snapshot if isinstance(getattr(run, "definition_snapshot", None), dict) else {}
    return str(
        trigger.get("_workflow_name") or snapshot.get("name") or run.workflow_id or "Workflow"
    ).strip()


def _reviewed_summaries(run: Any) -> set[str]:
    """Summaries the person already read on this run's review cards."""
    results = run.step_results if isinstance(getattr(run, "step_results", None), dict) else {}
    seen: set[str] = set()
    for result in results.values():
        review = result.get("review") if isinstance(result, dict) else None
        summary = review.get("summary") if isinstance(review, dict) else None
        if isinstance(summary, str) and summary.strip():
            seen.add(_compact_result_text(summary, limit=_RESULT_MESSAGE_LIMIT))
    return seen


def workflow_result_chat_projection(
    run: Any,
    value: Any,
) -> tuple[str, dict[str, Any]]:
    """Return concise Chat copy plus a durable link to the full run result."""
    workflow_name = workflow_run_display_name(run)
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

    result_title, result_summary = workflow_result_headline(value)
    if record:
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

    if publication_receipts:
        result_kind = "publication_receipt"
        if len(publication_receipts) == 1:
            platform = publication_platforms[0] if publication_platforms else "Platform"
            lead = f"Publication verified: {platform}."
        else:
            lead = f"{verified_publications} publications verified."
    elif result_kind == "topic_brief":
        lead = f"Topic brief ready: {result_title}" if result_title else "Topic brief ready."
    elif workflow_completion_message(run):
        lead = f"{workflow_name}: {workflow_completion_message(run)}"
    else:
        # Lead in the language the result is written in, so a Chinese
        # summary is not introduced by an English status line.
        locale = _text_locale(result_summary or result_title or workflow_name)
        if result_title:
            lead = t("workflow.result.completed_with_title", locale=locale, name=workflow_name, title=result_title)
        else:
            lead = t("workflow.result.completed", locale=locale, name=workflow_name)

    lines = [lead]
    # A summary the person just approved on the review card is not said again.
    if result_summary and result_summary != result_title and result_summary not in _reviewed_summaries(run):
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
        "url": _workflow_run_url(run),
        "status": str(getattr(run, "status", None) or "completed"),
        "kind": result_kind,
        "title": result_title or None,
        # The card names the result; the message itself carries all of it.
        "summary": _compact_result_text(result_summary) or None,
        "publication_summary": {
            "count": len(publication_receipts),
            "verified": verified_publications,
            "platforms": publication_platforms,
        } if publication_receipts else None,
    }
    return "\n\n".join(line for line in lines if line), reference


def workflow_completion_message(run: Any) -> str:
    """What the end a completed run reached says it means, if it says."""
    if str(getattr(run, "status", "") or "") != "completed":
        return ""
    results = getattr(run, "step_results", None)
    result = results.get(str(getattr(run, "current_step_id", "") or "")) if isinstance(results, dict) else None
    message = result.get("completion_message") if isinstance(result, dict) else None
    return " ".join(str(message or "").split())[:500]


def _workflow_business_state(run: Any) -> tuple[str, dict[str, Any]]:
    raw_variables = getattr(run, "variables", None)
    variables = raw_variables if isinstance(raw_variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    from packages.core.services.workflow_run_trace import workflow_result_outcome

    outcome = str(state.get("business_outcome") or workflow_result_outcome(variables) or "in_progress").strip().lower()
    retry_state = state.get("retry_state")
    if not isinstance(retry_state, dict):
        current_step_id = str(getattr(run, "current_step_id", "") or "")
        step_results = (
            run.step_results if isinstance(run.step_results, dict) else {}
        )
        current_result = step_results.get(current_step_id)
        if isinstance(current_result, dict):
            retry_state = current_result.get("retry_state")
    return outcome or "in_progress", retry_state if isinstance(retry_state, dict) else {}


def _workflow_error_payload(run: Any) -> Any:
    results = run.step_results if isinstance(run.step_results, dict) else {}
    step_names = {
        str(node.get("id")): {"id": str(node.get("id")), "name": node.get("name")}
        for node in ((run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}).get("nodes") or [])
        if isinstance(node, dict) and node.get("id")
    }

    def result_error(step_id: str) -> Any:
        result = results.get(step_id)
        if not isinstance(result, dict):
            return None
        value = user_facing_step_error(step_names.get(step_id) or {"id": step_id}, result)
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

    run_error = user_facing_run_error(run) if isinstance(getattr(run, "error", None), str) else None
    return run_error if isinstance(run_error, str) and run_error else None


def _failed_input_retry_schema(run: Any) -> dict[str, Any]:
    """Expose failed input bindings without turning runtime outputs into inputs."""
    empty = {"type": "object", "properties": {}}
    step_id = str(getattr(run, "current_step_id", "") or "")
    result = (getattr(run, "step_results", None) or {}).get(step_id) or {}
    if (getattr(run, "status", None) != "failed"
            or result.get("code") != "input_schema_validation_failed"):
        return empty
    snapshot = getattr(run, "execution_snapshot", None) or {}
    step = next((item for item in snapshot.get("steps", []) if item.get("id") == step_id), {})
    config = step.get("config") or {}
    schema = config.get("input_schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return empty
    declared = snapshot.get("variables") or {}
    editable, required = {}, []
    for binding in config.get("inputs") or []:
        if not isinstance(binding, dict) or not isinstance(binding.get("value"), str):
            continue
        match = re.fullmatch(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}", binding["value"])
        field = properties.get(binding.get("key"))
        if (not match or match[1] not in declared or not isinstance(field, dict)
                or field.get("readOnly") or field.get("writeOnly")):
            continue
        # Multiple validation fields targeting one variable can have conflicting
        # contracts. Keep those definitions on their explicit recovery path.
        if match[1] in editable and editable[match[1]] != field:
            return empty
        editable[match[1]] = deepcopy(field)
        if binding.get("key") in (schema.get("required") or []) and match[1] not in required:
            required.append(match[1])
    if not editable:
        return empty
    return {
        "type": "object", "properties": editable, "required": required,
        "additionalProperties": False,
        **{key: deepcopy(schema[key]) for key in ("$defs", "definitions") if key in schema},
    }


def _workflow_retry_input_schema(
    run: Any,
    retry_state: dict[str, Any],
) -> dict[str, Any]:
    raw_schema = retry_state.get("editable_input_schema")
    if not isinstance(raw_schema, dict):
        return _failed_input_retry_schema(run)
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
            # A scheduled Flow job may start its run without a binding (IS NULL).
            Message.meta["workflow_binding_id"].as_string() == (str(run.binding_id) if run.binding_id else None),
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


async def close_stale_workflow_cards(db: AsyncSession, *, run: Any) -> list[Message]:
    """Close the open cards an ended run leaves with nothing to decide.

    Its own questions and approvals can no longer be answered, and every card
    of an earlier ended run of the same Flow was replaced by this one (see
    ``ended_run_replaced``). The run's own Retry card is its activity's, which
    the status projection keeps. Cards being answered right now are skipped.
    """
    from packages.core.services.workflow_runtime_approvals import ENDED_RUN_STATUSES
    from packages.core.workspace_chat.pending_actions import ended_run_replaced

    if str(getattr(run, "status", "") or "") not in ENDED_RUN_STATUSES:
        return []
    owner = Message.pending_action["workflow_run_id"].as_string()
    kind = Message.pending_action["kind"].as_string()
    stale = [and_(owner == str(run.id), kind != PendingActionKind.WORKFLOW_RETRY.value)]
    if run.workspace_id and run.workflow_id:
        stale.append(owner.in_(select(WorkflowRun.id).where(
            WorkflowRun.entity_id == run.entity_id,
            WorkflowRun.workspace_id == run.workspace_id,
            WorkflowRun.workflow_id == run.workflow_id,
            WorkflowRun.binding_id.is_not_distinct_from(run.binding_id),
            WorkflowRun.id != str(run.id),
            ended_run_replaced(WorkflowRun),
        )))
    messages = list((await db.execute(
        select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Conversation.entity_id == str(run.entity_id),
            Message.resolved_at.is_(None),
            kind.isnot(None),
            or_(*stale),
        ).with_for_update(of=Message, skip_locked=True).execution_options(populate_existing=True)
    )).scalars().all())
    now = datetime.now(timezone.utc)
    for message in messages:
        choice = (
            "superseded" if (message.pending_action or {}).get("workflow_run_id") != str(run.id)
            else "stopped" if run.status == "cancelled" else "expired"
        )
        message.resolved_at = now
        message.resolution = {"choice": choice}
        # The Personal Chat mirror of the same card closes with it.
        requests = (message.meta or {}).get("hitl_requests")
        if isinstance(requests, list):
            message.meta = {**message.meta, "hitl_requests": [
                {**request, "resolved": True} if isinstance(request, dict) and request.get("id") == message.id
                else request for request in requests
            ]}
    return messages


def _workflow_progress_event(message: Message) -> dict[str, Any]:
    """Where a run's row stands, so an open Chat moves it without refetching.

    The row was posted once and its message is already loaded; each step only
    changes its statuses, which travel here as ids and statuses.
    """
    meta = message.meta if isinstance(message.meta, dict) else {}
    if message.message_kind != "workflow_activity" or not meta.get("workflow_run_id"):
        return {}
    return {"workflow_progress": {
        "workflow_run_id": str(meta["workflow_run_id"]),
        "workflow_status": meta.get("workflow_status"),
        "workflow_current_step_id": meta.get("workflow_current_step_id"),
        "workflow_steps": [
            {"id": step.get("id"), "status": step.get("status")}
            for step in meta.get("workflow_steps") or []
            if isinstance(step, dict) and step.get("id")
        ],
    }}


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
                # Workspace-scoped: an entity target would reach members who
                # cannot read the Workspace and miss its collaborators.
                "target": "workspace",
                "entity_id": conversation.entity_id,
                "workspace_id": conversation.workspace_id,
                "event": "workspace_chat_message",
                "data": {
                    "workspace_id": conversation.workspace_id,
                    "message_id": message.id,
                    "message_kind": message.message_kind,
                    "author_kind": message.author_kind,
                    "has_pending_action": bool(message.pending_action),
                    **_workflow_progress_event(message),
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
            {"type": "workflow_step", "id": step_id, "title": workflow_step_label(step) or step_id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_step_id": step_id,
            "workflow_step_status": "completed",
        },
    )


def workflow_step_recommendation(*sources: Any, options: list[str]) -> dict[str, Any] | None:
    """A paused step's configured recommended answer, when it names one.

    Read from the step's result (a step that worked one out at run time) and
    then its configuration: ``recommended`` (one of the step's options) and
    ``recommendation_reason`` (one sentence). Steps without one get the shared
    default for their options when the card is written.
    """
    from packages.core.services.decision_recommendation import recommendation

    for source in sources:
        if not isinstance(source, dict):
            continue
        option = source.get("recommended")
        if isinstance(option, str) and option in options:
            reason = " ".join(str(source.get("recommendation_reason") or "").split())
            return (
                recommendation(option, reason=reason[:280])
                if reason else recommendation(option, reason_code="most_likely")
            )
    return None


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
    from packages.core.services.workflow_chat_approvals import (
        workflow_chat_wait_config,
    )

    config = workflow_chat_wait_config(run, step)
    if config is None:
        return
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
            Message.resolved_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return
    from packages.core.services.conversation_messages import add_message

    message = str(result.get("output") or config.get("message") or workflow_step_label(step) or "Input required")
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
    options = wait_config.get("options", workflow_wait_options(config))
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
    step_recommendation = workflow_step_recommendation(
        result, wait_config, config, options=pending_action["options"],
    )
    if step_recommendation is not None:
        pending_action["recommendation"] = step_recommendation
    if isinstance(result.get("collection_fields"), list):
        pending_action["inputs"] = result["collection_fields"]
        pending_action["values"] = result.get("collection_values") or {}
        pending_action["title"] = workflow_step_label(step) or t("workflow.input.title_default")
        pending_action["collection_round"] = result.get("collection_round")
    elif kind == PendingActionKind.WORKFLOW_INPUT.value:
        input_schema = result.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = wait_config.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = config.get("input_schema")
        if isinstance(input_schema, dict):
            pending_action["input_schema"] = input_schema
    if context.get("nested") is True and context.get("host_run_id"):
        pending_action["workflow_chat_host_run_id"] = str(context["host_run_id"])
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
        # Notices and cards name the decision; the instructions stay in the review.
        pending_action.setdefault("title", pending_action["review_title"])
    from packages.core.services.workflow_chat_approvals import workflow_review_is_editable

    # A review the server cannot write back is read-only, whatever its shape.
    review_read_only = not workflow_review_is_editable(config)
    if review_read_only:
        pending_action["review_read_only"] = True
    if isinstance(config.get("review_editable_fields"), list):
        pending_action["review_editable_fields"] = config["review_editable_fields"]
    if isinstance(config.get("review_revision_field"), str):
        pending_action["review_revision_field"] = config["review_revision_field"]
    workflow_name = workflow_run_display_name(run)
    workflow_url = _workflow_run_url(run)
    node_name = workflow_step_label(step) or step_id
    hitl_message = await add_message(
        db,
        str(context["conversation_id"]),
        role="system",
        content=message,
        message_kind="hitl_request",
        refs=[
            {"type": "workflow_run", "id": run.id},
            {"type": "workflow_step", "id": step_id, "title": workflow_step_label(step) or step_id},
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
        "review_read_only": review_read_only,
        **(
            {"review_editable_fields": config["review_editable_fields"]}
            if isinstance(config.get("review_editable_fields"), list)
            else {}
        ),
        **(
            {"review_revision_field": config["review_revision_field"]}
            if isinstance(config.get("review_revision_field"), str)
            else {}
        ),
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
    if "input_schema" in pending_action:
        hitl_request["input_schema"] = pending_action["input_schema"]
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
    attachments = _workflow_final_output_attachments(
        raw_output,
        entity_id=str(getattr(run, "entity_id", "") or ""),
    )
    if not attachments:
        # A result that names no file (a project record) delivered what the run made.
        from packages.core.services.workflow_review_files import review_files_from_refs, workflow_run_made_files

        made = await review_files_from_refs(db, entity_id=str(getattr(run, "entity_id", "") or ""), refs=[
            item.get("document_id") or item.get("fs_path") for item in workflow_run_made_files(run)
        ])
        attachments = _workflow_final_output_attachments(
            {"files": made[:6]},
            entity_id=str(getattr(run, "entity_id", "") or ""),
        )
    if existing:
        # Runs completed before structured result cards were introduced stored
        # the full JSON payload directly in Chat. Upgrade that projection when
        # the same run is observed again without creating a duplicate message.
        existing_meta = dict(existing.meta or {})
        changed = False
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
            changed = True
        if attachments and existing.attachments != attachments:
            existing.attachments = attachments
            changed = True
        if changed:
            await _notify_update(db, existing)
        return
    subscription_id = str((activity.meta or {}).get("workflow_current_subscription_id") or "") or None
    from packages.core.services.conversation_messages import add_message

    await add_message(
        db,
        str(context["conversation_id"]),
        role="assistant" if subscription_id else "system",
        content=output,
        attachments=attachments or None,
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
    if context.get("nested") is True:
        if result is not None:
            await _project_wait_action(
                db,
                run=run,
                context=context,
                step=step,
                result=result,
            )
        return
    settings = workflow_projection_settings(context)
    activity = await _activity_message(db, run, context)
    if activity is None:
        return
    step_id = str(step.get("id") or "")
    if is_transient_retry_record(result):
        # A timed re-execution is still this step working, not a wait on a person.
        status = "running"
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
                "name": workflow_step_label(step) or step_id,
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
            from packages.core.services.workflow_runtime_approvals import (
                expire_ended_workflow_run_approvals,
                project_workflow_runtime_approval,
            )
            # An ended run's open approvals can never be decided; close them.
            # Readers already ignore them, so this cleanup is best-effort too.
            await expire_ended_workflow_run_approvals(db, run=run)
            await project_workflow_runtime_approval(
                db, run=run, step={"id": run.current_step_id},
                result=(run.step_results or {}).get(run.current_step_id),
            )
            await _project_workflow_run_status_chat(db, run=run)
    except Exception:
        logger.debug("Workflow Chat run projection skipped", exc_info=True)
    try:
        # After the activity points at this run, so only other runs' cards
        # and this run's own questions are left to close. Readers already
        # ignore them, so this cleanup is best-effort.
        async with db.begin_nested():
            closed = await close_stale_workflow_cards(db, run=run)
        if closed:
            from packages.core.workspace_chat.service import _publish_pending_action_resolution

            for message in closed:
                await _publish_pending_action_resolution(db, message=message)
    except Exception:
        logger.warning("Closing stale Workflow cards skipped", exc_info=True)


async def _project_workflow_run_status_chat(
    db: AsyncSession,
    *,
    run: Any,
) -> None:
    """Project run status into Chat inside the caller's UI savepoint."""
    context = _entrypoint_context(run)
    if context is None or context.get("nested") is True:
        return
    activity = await _activity_message(db, run, context)
    if activity is None:
        return
    settings = workflow_projection_settings(context)
    meta = dict(activity.meta or {})
    meta["workflow_run_id"] = str(run.id)
    meta["workflow_binding_id"] = str(run.binding_id) if run.binding_id else None
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
    # Every attempt of one run family shares this id: Chat keeps them on one row.
    meta["workflow_root_run_id"] = str(getattr(run, "lineage_root_run_id", None) or run.id)
    retry_from_step_id = workflow_retry_from_step_id(run, retry_state)
    if retry_from_step_id:
        meta["workflow_retry_from_step_id"] = retry_from_step_id
    else:
        meta.pop("workflow_retry_from_step_id", None)
    current_step_id = str(getattr(run, "current_step_id", "") or "").strip()
    current_result = (run.step_results or {}).get(current_step_id)
    if run.status == "paused" and is_transient_retry_record(current_result):
        meta["workflow_status"] = "running"
        meta["workflow_transient_retry"] = summarize_trace_value(current_result["transient_retry"])
    else:
        meta.pop("workflow_transient_retry", None)
    reconciliation_required = (
        isinstance(current_result, dict)
        and _failed_effect_requires_reconciliation(
            run, current_step_id, current_result,
        )
    )
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
    # A stalled run is retried by starting over on the current Flow, and only once it has changed.
    stalled = workflow_run_stalled(run)
    if stalled:
        from packages.core.services.workflow_service import stalled_run_restart

        restartable = await stalled_run_restart(db, run) is not None
    else:
        restartable = False
    if run.status == "cancelled":
        activity.content = t("workflow.run.cancelled", locale=_text_locale(title), title=title)
    elif business_outcome == "needs_input":
        activity.content = f"{title} needs input before it can continue."
    elif business_outcome == "revision_required":
        activity.content = f"{title} requires a revision."
    elif business_outcome == "ready_for_acceptance":
        activity.content = f"{title} is ready for playback and acceptance."
    elif business_outcome == "accepted":
        activity.content = f"{title} was accepted."
    elif run.status == "completed":
        completion_message = workflow_completion_message(run)
        activity.content = f"{title}: {completion_message}" if completion_message else f"{title} completed."
    elif run.status == "running":
        activity.content = f"{title} is starting."
    elif run.status == "failed":
        if reconciliation_required:
            activity.content = f"{title} requires reconciliation before it can be retried."
        elif stalled:
            # The step list behind this failure is for debugging; the person needs what to do.
            activity.content = f"{title} failed: " + (
                "its version of the Flow could not continue, so it was started again on the current version."
                if workflow_run_restarted_as(run)
                else f"{STALLED_PROBLEM} {STALLED_RESTART_CHANGE}" if restartable else STALLED_RETRY_ERROR
            )
        else:
            error_message, shortened = user_facing_error_text(summarize_trace_text(
                raw_workflow_error,
                fallback="Workflow failed",
            ))
            locale = _text_locale(error_message or title)
            activity.content = t("workflow.run.failed", locale=locale, title=title, error=error_message)
            if shortened:
                activity.content += " " + t("workflow.run.error_details", locale=locale)
    elif run.status == "paused" and is_transient_retry_record(current_result):
        # Parked on a timed re-execution: nobody needs to act.
        activity.content = (
            f"{title}: {transient_retry_status_message(current_result['transient_retry'])} "
            "It continues automatically."
        )
    elif run.status == "paused":
        activity.content = f"{title} is waiting for input."
    retryable = restartable if stalled else (run.status == "failed" or (
        run.status == "completed"
        and business_outcome == "needs_input"
    ))
    if restartable:
        retry_from_step_id = next((
            str(node.get("id")) for node in (run.definition_snapshot or {}).get("nodes") or []
            if isinstance(node, dict) and node.get("type") == "trigger"
        ), retry_from_step_id)
        retry_state = {**retry_state, "required_change": STALLED_RESTART_CHANGE}
    if retryable and retry_from_step_id:
        editable_input_schema = {} if stalled else _workflow_retry_input_schema(run, retry_state)
        observed_problem = STALLED_PROBLEM if stalled else retry_state.get("observed_problem")
        if observed_problem is None:
            observed_problem = workflow_error
        else:
            observed_problem = summarize_trace_value(observed_problem)
        required_change = summarize_trace_value(
            retry_state.get("required_change")
            or default_retry_change(observed_problem or title)
        )
        preserved_receipts = summarize_trace_value(
            retry_state.get("preserved_receipts") or []
        )
        step_id = retry_state.get("step_id") or run.current_step_id
        node_names = {
            node.get("id"): str(node.get("name") or "")
            for node in (run.definition_snapshot or {}).get("nodes") or []
            if isinstance(node, dict)
        }
        step_name = node_names.get(step_id, "")
        activity.pending_action = {
            "kind": PendingActionKind.WORKFLOW_RETRY.value,
            # Lists of open decisions name the card by its title; the observed
            # problem is raw tool output and reads as a file path or stack trace.
            "title": (
                f"{title} needs more input" if run.status != "failed"
                else f"{title} stopped at {step_name}" if step_name
                else f"{title} stopped"
            ),
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "business_outcome": business_outcome,
            "phase": retry_state.get("phase") or "execution",
            "step_id": step_id,
            "retry_from_step_id": retry_from_step_id,
            "retry_from_step_name": workflow_step_display_name(run, retry_from_step_id),
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
