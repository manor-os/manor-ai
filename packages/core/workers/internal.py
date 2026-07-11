"""Internal Worker — in-process execution behind the Dispatcher.

The InternalWorker is one of N executors that can take leases from
the Dispatcher. Three traits make it special:

  * **Always present**: ``ensure_internal_worker(entity_id)`` makes one
    per entity automatically. New entities get one before they need it.
  * **Trusted**: ``trust_level='high'`` — sees real credentials, can
    execute high-risk actions, no IP allowlist.
  * **In-process**: doesn't go through HTTP. The Celery task
    ``internal_worker_tick`` invokes its ``execute_lease`` directly,
    same as an external worker would over HTTP — but with no auth or
    network hop.

This module **only** contains the kind-specific execution logic
(action / llm / subagent / sandbox-simulate). Lease lifecycle is the
Dispatcher's job; the lease pull happens in a Celery beat task.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
from typing import Any, Optional

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.services.hitl_options import approval_options
from packages.core.dispatcher import Dispatcher
from packages.core.dispatcher.output_coercion import (
    coerce_step_output_for_schema,
    parse_json_from_text_for_schema,
)
from packages.core.ai.runtime import (
    runtime_execute_internal_worker_llm_step,
    runtime_execute_worker_subagent_loop,
    runtime_metadata_from_context,
    runtime_persist_internal_worker_runtime_events,
    runtime_prompt_with_output_schema,
)
from packages.core.models.document import Integration
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import Worker, WorkLease
from packages.core.workers.registry import (
    INTERNAL_WORKER_KIND,
    ensure_internal_worker,
)
from packages.core.contracts.shapes import coerce_to_shape, get_shape
from packages.core.contracts.envelope import Success, Failure, StepResult
from packages.core.contracts.workspace_paths import default_fs_path_into_workspace

logger = logging.getLogger(__name__)

_PROMPT_PARAM_KEYS = ("prompt", "user_prompt", "instructions", "instruction", "message", "task")
LEASE_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MANOR_INTERNAL_LEASE_HEARTBEAT_SECONDS", "60"))
LEASE_HEARTBEAT_EXTEND_SECONDS = float(os.getenv("MANOR_INTERNAL_LEASE_EXTEND_SECONDS", "300"))


def _step_prompt(params: dict[str, Any]) -> Any:
    """Return the first supported natural-language prompt field.

    Older planner runs sometimes emitted ``instruction`` (singular) while
    the worker expected ``instructions``. Keep accepting both so in-flight
    plans do not fail just because the synonym differs.
    """
    for key in _PROMPT_PARAM_KEYS:
        value = params.get(key)
        if value:
            return value
    return None


def _coerce_llm_text_result(content: str, schema: Optional[dict]) -> Any:
    """Shape LLM/subagent text to match the declared output schema.

    Agent loops return natural language, but planner steps often declare
    structured outputs such as a JSON array. If the model wrapped valid JSON
    in prose or a fenced block, extract it before validation. For document
    generation steps that declare ``{"result": "string"}``, keep the full
    text under that canonical key.
    """
    if not schema:
        return {"text": content}

    schema_type = schema.get("type")
    parsed = _parse_json_from_text(content, schema=schema)
    if parsed is not None:
        if _schema_accepts_type(schema, "string") and isinstance(parsed, str):
            return parsed
        if schema_type == "array" and isinstance(parsed, list):
            return parsed
        if schema_type == "array" and isinstance(parsed, dict):
            for key in ("result", "items", "data", "records", "rows"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
        if schema_type == "object" and isinstance(parsed, dict):
            return parsed
        if schema_type is None:
            return parsed

    if _schema_accepts_type(schema, "string"):
        return content

    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if schema_type == "object" and "result" in props:
        return {"result": content}
    single_text_key = _single_required_string_key(schema)
    if schema_type == "object" and single_text_key:
        return {single_text_key: content}

    return {"text": content}


def enforce_output_shape(
    shape_name: str,
    raw: Any,
    *,
    workspace_base_dir: str = "",
) -> StepResult:
    """Normalize raw onto a canonical shape, apply workspace path defaults,
    validate, and return a typed Success/Failure. No LLM repair here — that
    is layered in ``enforce_with_repair``."""
    from jsonschema import Draft202012Validator

    data = coerce_to_shape(shape_name, raw)
    data = default_fs_path_into_workspace(data, workspace_base_dir=workspace_base_dir)
    schema = get_shape(shape_name).json_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        def _fmt(err) -> str:
            path = "".join(f"[{p!r}]" for p in err.path)
            return f"${path}: {err.message}" if path else err.message

        return Failure(
            reason=f"output does not satisfy {shape_name}: {_fmt(errors[0])}",
            detail={"shape": shape_name, "errors": [_fmt(e) for e in errors[:5]]},
        )
    return Success(data)


async def enforce_with_repair(
    shape_name: str,
    raw: Any,
    *,
    reshaper,
    workspace_base_dir: str = "",
) -> StepResult:
    """``enforce_output_shape``, then one repair pass via ``reshaper`` on failure.

    ``reshaper(shape_name, raw, errors) -> dict`` is the LLM call (injected for
    testability). Production passes a thin wrapper over the worker's LLM client
    that feeds the shape JSON schema + error messages back to the model. Bounded
    to exactly one retry; after that the Failure stands.
    """
    first = enforce_output_shape(shape_name, raw, workspace_base_dir=workspace_base_dir)
    if isinstance(first, Success):
        return first
    errors = first.detail.get("errors") if first.detail else []
    repaired_raw = await reshaper(shape_name, raw, errors)
    return enforce_output_shape(shape_name, repaired_raw, workspace_base_dir=workspace_base_dir)


def _single_required_string_key(schema: dict) -> str | None:
    """Return the lone required string field when plain text is safe to wrap."""
    if schema.get("type") != "object":
        return None
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(key) for key in (schema.get("required") or [])]
    if len(required) != 1:
        return None
    key = required[0]
    prop_schema = props.get(key) if isinstance(props.get(key), dict) else {}
    if _schema_accepts_type(prop_schema, "string") and not _looks_like_url_field(key):
        return key
    return None


def _looks_like_url_field(key: str) -> bool:
    key_l = str(key or "").lower()
    return key_l == "url" or key_l.endswith("_url") or key_l.endswith("url")


def _schema_accepts_type(schema: dict, expected: str) -> bool:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type == expected
    if isinstance(schema_type, list):
        return expected in schema_type
    return False


_ARTIFACT_TOOL_KEYS = {
    "artifact_url",
    "artifact_path",
    "download_url",
    "file_url",
    "file_path",
    "document_url",
    "image_url",
    "video_url",
    "audio_url",
    "media_url",
    "output_url",
    "output_path",
    "public_url",
    "result_url",
    "url",
    "fs_path",
    "path",
    "local_path",
    "saved_to",
    "document_id",
}
_ARTIFACT_LIST_KEYS = ("files", "artifacts", "documents", "images", "image_urls")
_ARTIFACT_CREATION_FLAGS = {
    "created", "written", "edited", "saved", "generated", "uploaded",
    "downloaded", "exported",
}
_REFERENCE_ONLY_KEYS = {
    "context", "sources", "source_count", "scope", "groups", "knowledge_nets",
    "entries", "matches",
}


def _is_external_artifact_url(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("http://", "https://", "blob:", "data:"))


def _looks_like_relative_artifact_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text or _is_external_artifact_url(text):
        return False
    lowered = text.lstrip("/").lower()
    if lowered.startswith(("api/", "documents/", "viewer/", "editor/")):
        return False
    name = lowered.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    has_file_extension = bool(suffix) and suffix != name and len(suffix) <= 12
    return "/" in lowered or has_file_extension


def _artifact_value_is_path(source_key: str, value: Any) -> bool:
    return (
        "path" in source_key
        or source_key in {"fs_path", "saved_to", "local_path"}
        or (
            (source_key.endswith("_url") or source_key in {"url", "files", "documents", "artifacts"})
            and _looks_like_relative_artifact_path(value)
        )
    )


def _collect_artifact_refs_from_agent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        parsed = _parse_json_from_text(str(message.get("content") or ""))
        if not isinstance(parsed, dict):
            continue
        refs.extend(_artifact_refs_from_tool_payload(parsed))
    return refs


def _pending_action_from_agent_messages(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Promote tool-level HITL payloads from a subagent run to the lease.

    The chat runtime already renders ``workspace_operation`` review payloads.
    Worker subagents need the same contract; otherwise an approval request can
    be hidden inside a tool message while the step keeps running until schema
    validation fails.
    """
    for message in messages:
        if message.get("role") != "tool":
            continue
        parsed = _parse_json_from_text(str(message.get("content") or ""))
        if not isinstance(parsed, dict):
            continue
        pending = _pending_action_from_tool_payload(parsed)
        if pending:
            return pending
    return None


def _pending_action_from_tool_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    pending_action = payload.get("_pending_action")
    if isinstance(pending_action, dict) and pending_action.get("kind"):
        return pending_action

    if payload.get("__hitl__") is not True:
        return None
    hitl = payload.get("hitl") if isinstance(payload.get("hitl"), dict) else {}
    operation = payload.get("operation")
    if not isinstance(operation, dict):
        operation = hitl.get("operation") if isinstance(hitl.get("operation"), dict) else {}

    if operation.get("kind") == "workspace_operation_review":
        draft_id = str(
            operation.get("draft_id")
            or hitl.get("id")
            or payload.get("approval_token")
            or ""
        ).strip()
        if not draft_id:
            return None
        return {
            "kind": "workspace_operation_review",
            "draft_id": draft_id,
            "approval_token": draft_id,
            "prompt": hitl.get("prompt") or "Apply this workspace operation draft?",
            "action": hitl.get("action") or "workspace.operation.apply",
            "tool": hitl.get("tool") or "workspace_operation",
            "content": hitl.get("content"),
            "args_preview": hitl.get("args_preview"),
            "operation": operation,
            "options": hitl.get("options") if isinstance(hitl.get("options"), list) else approval_options(),
        }

    action = hitl.get("action") or payload.get("approval_action") or "tool.approve"
    tool = hitl.get("tool") or payload.get("tool")
    approval_token = str(hitl.get("id") or payload.get("approval_token") or "").strip()
    return {
        "kind": "needs_confirmation",
        "approval_token": approval_token or None,
        "prompt": hitl.get("prompt") or payload.get("message") or "Approve this tool action?",
        "action": action,
        "tool": tool,
        "content": hitl.get("content"),
        "args_preview": hitl.get("args_preview"),
        "options": hitl.get("options") if isinstance(hitl.get("options"), list) else approval_options(),
    }


def _raise_if_agentic_loop_failed(result: Any) -> None:
    stop_reason = str(getattr(result, "stop_reason", "completed") or "completed")
    if stop_reason == "completed":
        return
    content = str(getattr(result, "content", "") or "").strip()
    if stop_reason == "max_rounds" and content:
        return

    detail_obj = getattr(result, "error_detail", None)
    detail = ""
    if isinstance(detail_obj, dict):
        detail = str(detail_obj.get("message") or detail_obj.get("error") or "").strip()
    if not detail:
        detail = str(getattr(result, "error", "") or "").strip()
    if detail in {"llm_call_failed", "provider_error", "error"}:
        if content:
            detail = content
    if not detail:
        detail = content
    if not detail:
        detail = "Agent loop stopped before producing a valid response."
    raise RuntimeError(f"subagent stopped with {stop_reason}: {detail}")


def _artifact_refs_from_tool_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if _is_reference_only_payload(payload):
        return []

    refs: list[dict[str, Any]] = []

    def add_ref(ref_type: str, value: Any, *, source_key: str) -> None:
        if not value:
            return
        ref: dict[str, Any] = {"type": ref_type, "source": source_key}
        if source_key == "document_id":
            ref["document_id"] = value
        elif _artifact_value_is_path(source_key, value):
            ref["fs_path"] = value
        else:
            ref["url"] = value
        refs.append(ref)

    for key in _ARTIFACT_TOOL_KEYS:
        if payload.get(key):
            add_ref(key.replace("_url", ""), payload[key], source_key=key)

    for key in _ARTIFACT_LIST_KEYS:
        value = payload.get(key)
        if key in {"documents", "files"} and not _has_artifact_creation_signal(payload):
            continue
        if key == "image_urls" and isinstance(value, list):
            for url in value:
                add_ref("image", url, source_key=key)
            continue
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                add_ref(key.rstrip("s") or "file", item, source_key=key)
                continue
            if not isinstance(item, dict):
                continue
            ref_type = str(item.get("type") or item.get("mime") or key.rstrip("s") or "file")
            for value_key in (
                "artifact_url", "download_url", "file_url", "document_url",
                "image_url", "video_url", "audio_url", "media_url",
                "result_url", "output_url", "public_url", "url",
                "fs_path", "artifact_path", "file_path", "output_path",
                "path", "local_path", "saved_to", "document_id",
            ):
                if item.get(value_key):
                    add_ref(ref_type, item[value_key], source_key=value_key)
                    break

    return refs


def _has_artifact_creation_signal(payload: dict[str, Any]) -> bool:
    if any(bool(payload.get(key)) for key in _ARTIFACT_CREATION_FLAGS):
        return True
    return any(bool(payload.get(key)) for key in _ARTIFACT_TOOL_KEYS - {"document_id"})


def _is_reference_only_payload(payload: dict[str, Any]) -> bool:
    """Tool search/list outputs can contain document paths used as sources.

    Those are context references, not newly produced artifacts. Only capture
    files/documents from payloads that clearly signal creation/export/generation.
    """
    if _has_artifact_creation_signal(payload):
        return False
    if any(key in payload for key in _REFERENCE_ONLY_KEYS):
        return True
    if "documents" in payload:
        return True
    return False


def _merge_artifact_refs(result: Any, refs: list[dict[str, Any]]) -> Any:
    if not refs:
        return result
    if not isinstance(result, dict):
        result = {"value": result}
    existing = result.get("files")
    if isinstance(existing, list):
        result["files"] = existing + refs
    else:
        result["files"] = refs
    for ref in refs:
        ref_type = str(ref.get("type") or "")
        if ref_type.startswith("image") and ref.get("url") and not result.get("image_url"):
            result["image_url"] = ref["url"]
        if ref_type.startswith("document") and ref.get("url") and not result.get("document_url"):
            result["document_url"] = ref["url"]
        if ref.get("fs_path") and not result.get("fs_path"):
            result["fs_path"] = ref["fs_path"]
        if ref.get("document_id") and not result.get("document_id"):
            result["document_id"] = ref["document_id"]
    return result


_MATERIALIZED_ARTIFACT_SCHEMA_FIELDS = {
    "fs_path",
    "path",
    "file_path",
    "file_url",
    "document_url",
}


def _schema_requires_fs_path(schema: Optional[dict]) -> bool:
    return _schema_requires_any_artifact_field(schema, {"fs_path"})


def _schema_requires_materialized_artifact(schema: Optional[dict]) -> bool:
    return _schema_requires_any_artifact_field(schema, _MATERIALIZED_ARTIFACT_SCHEMA_FIELDS)


def _schema_requires_any_artifact_field(schema: Optional[dict], field_names: set[str]) -> bool:
    if not isinstance(schema, dict):
        return False
    required = schema.get("required")
    if isinstance(required, list) and any(field in required for field in field_names):
        return True
    props = schema.get("properties")
    return isinstance(props, dict) and any(
        isinstance(props.get(field), dict) and props[field].get("required") is True
        for field in field_names
    )


def _schema_requests_field(schema: Optional[dict], field_name: str) -> bool:
    if not isinstance(schema, dict):
        return False
    required = schema.get("required")
    if isinstance(required, list) and field_name in required:
        return True
    props = schema.get("properties")
    return isinstance(props, dict) and field_name in props


def _target_artifact_path_from_prompt(prompt: Any, step_key: str | None = None) -> str:
    text = str(prompt or "")
    path_ext = r"(?:md|txt|json|csv|html|docx|pptx)"
    patterns = [
        rf"(?:保存至|保存到|写入|输出到|输出至|save(?: it)? to|write(?: it)? to|output to)\s*[:：]?\s*`([^`]+\.(?:{path_ext}))`",
        rf"(?:保存至|保存到|写入|输出到|输出至|save(?: it)? to|write(?: it)? to|output to)\s*[:：]?\s*([^\s，。；;]+?\.(?:{path_ext}))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _safe_artifact_rel_path(match.group(1))

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(step_key or "subagent-output")).strip("-").lower()
    return f"workspace/artifacts/{slug or 'subagent-output'}.md"


async def _workspace_scoped_artifact_path(s: dict, prompt: Any) -> str:
    rel_path = _target_artifact_path_from_prompt(prompt, s.get("step_key"))
    entity_id = str(s.get("entity_id") or "")
    workspace_id = str(s.get("workspace_id") or "").strip()
    if not entity_id or not workspace_id:
        return rel_path

    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
    )

    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if not workspace_base_dir:
        return rel_path

    target_path = _strip_legacy_workspace_prefix(rel_path)
    return _safe_artifact_rel_path(
        scope_workspace_artifact_path(
            target_path,
            workspace_base_dir,
            default_subdir="artifacts",
        )
    )


def _strip_legacy_workspace_prefix(path: str) -> str:
    rel_path = _safe_artifact_rel_path(path)
    if rel_path.lower().startswith("workspace/"):
        stripped = rel_path.split("/", 1)[1].strip("/")
        return stripped or rel_path
    return rel_path


def _safe_artifact_rel_path(path: str) -> str:
    cleaned = str(path or "").strip().strip("'\"").replace("\\", "/").lstrip("/")
    cleaned = re.sub(r"/+", "/", cleaned)
    norm = os.path.normpath(cleaned).replace("\\", "/")
    if norm.startswith("../") or norm == ".." or os.path.isabs(norm):
        raise ValueError(f"Unsafe artifact path: {path!r}")
    return norm


def _parse_json_from_text(text: str, *, schema: Optional[dict] = None) -> Any:
    """Best-effort JSON extraction from raw model text."""
    return parse_json_from_text_for_schema(text, schema=schema)


# ── One full tick ─────────────────────────────────────────────────────

async def tick_one_internal_worker(worker_id: str, *, max_n: int = 4) -> dict:
    """Single checkout pass + execute pass for one internal worker.

    Called by ``internal_worker_tick`` Celery beat job. Per-lease
    execution fans out to ``execute_lease`` Celery tasks so a slow
    LLM call doesn't block the next tick — keeps end-to-end latency
    bounded by the heartbeat interval, not by the slowest step.
    """
    leases_to_dispatch: list[str] = []

    async with async_session() as db:
        worker = (await db.execute(
            select(Worker).where(Worker.id == worker_id)
        )).scalar_one_or_none()
        if worker is None:
            return {"worker_id": worker_id, "error": "not_found"}
        if worker.status != "active":
            return {"worker_id": worker_id, "skipped": True, "status": worker.status}

        dispatcher = Dispatcher()
        leases = await dispatcher.checkout_steps_for_worker(db, worker, max_n=max_n)
        leases_to_dispatch = [lease.id for lease, _ in leases]
        await db.commit()

    # Fan out per-lease execution to its own Celery task. Imported here
    # to avoid celery_app circulars at module import.
    if leases_to_dispatch:
        from packages.core.tasks.ai_tasks import execute_lease
        for lid in leases_to_dispatch:
            execute_lease.delay(lid)

    return {
        "worker_id": worker_id,
        "leased": len(leases_to_dispatch),
    }


async def tick_all_internal_workers(*, max_n: int = 4) -> int:
    """Iterate every active internal worker and run one tick each.

    Run by Celery beat at the heartbeat cadence. Returns total leases
    issued across all workers (for logging / metrics).
    """
    async with async_session() as db:
        try:
            from packages.core.models.workspace import AgentSubscription

            entity_ids = list((await db.execute(
                select(AgentSubscription.entity_id)
                .where(AgentSubscription.status == "active")
                .distinct()
            )).scalars().all())
            for entity_id in entity_ids:
                await ensure_internal_worker(db, entity_id)
            if entity_ids:
                await db.commit()

            worker_ids = list((await db.execute(
                select(Worker.id).where(
                    Worker.kind == INTERNAL_WORKER_KIND,
                    Worker.status == "active",
                )
            )).scalars().all())
        except Exception as exc:  # noqa: BLE001
            msg = str(getattr(exc, "orig", exc))
            if 'relation "workers" does not exist' in msg:
                logger.warning(
                    "internal workers table missing (DB not migrated yet). "
                    "Run Alembic migrations or reset the dev DB volume."
                )
                return 0
            raise

    total = 0
    for wid in worker_ids:
        result = await tick_one_internal_worker(wid, max_n=max_n)
        total += result.get("leased", 0)
    return total


# ── Per-lease execution ───────────────────────────────────────────────

async def _heartbeat_active_lease(
    lease_id: str,
    *,
    interval_seconds: float | None = None,
    extend_seconds: float | None = None,
) -> None:
    """Keep a long-running in-process lease active until its handler exits."""
    interval = (
        LEASE_HEARTBEAT_INTERVAL_SECONDS
        if interval_seconds is None
        else float(interval_seconds)
    )
    extend_by = (
        LEASE_HEARTBEAT_EXTEND_SECONDS
        if extend_seconds is None
        else float(extend_seconds)
    )
    if interval <= 0:
        return

    dispatcher = Dispatcher()
    while True:
        await asyncio.sleep(interval)
        try:
            async with async_session() as db:
                await dispatcher.extend_lease(db, lease_id, extra_seconds=extend_by)
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "LeaseNotActive":
                logger.debug("execute_lease %s heartbeat stopped: lease no longer active", lease_id)
                return
            logger.warning("execute_lease %s heartbeat failed", lease_id, exc_info=True)


def _start_lease_heartbeat(lease_id: str) -> asyncio.Task | None:
    if LEASE_HEARTBEAT_INTERVAL_SECONDS <= 0:
        return None
    return asyncio.create_task(_heartbeat_active_lease(lease_id))


async def _stop_lease_heartbeat(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def execute_lease_inproc(lease_id: str) -> dict:
    """Run one lease end-to-end and report the result back to Dispatcher.

    ``execute_lease`` Celery task wraps this. Each kind routes to its
    handler; failures inside a handler bubble to ``fail_lease`` so the
    DB is always the source of truth on lease state.
    """
    dispatcher = Dispatcher()

    async with async_session() as db:
        lease = (await db.execute(
            select(WorkLease).where(WorkLease.id == lease_id)
        )).scalar_one_or_none()
        if lease is None or lease.status != "active":
            logger.info("execute_lease %s: lease not active (state=%s)", lease_id,
                        lease.status if lease else "missing")
            return {"lease_id": lease_id, "skipped": True}

        step = (await db.execute(
            select(ExecutionStep).where(ExecutionStep.id == lease.step_id)
        )).scalar_one()
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == lease.plan_id)
        )).scalar_one()
        conversation_id = None
        user_id = None
        if plan.task_id:
            try:
                from packages.core.models.task import Task
                task_row = (await db.execute(
                    select(Task.conversation_id, Task.creator_id).where(Task.id == plan.task_id)
                )).first()
                if task_row:
                    conversation_id = task_row[0]
                    user_id = task_row[1]
            except Exception:
                conversation_id = None
                user_id = None

        params = dict(step.params or {})
        if step.human_input_response is not None:
            params["human_input_response"] = step.human_input_response

        # Snapshot everything the handlers need so we don't hold the
        # session across long-running LLM / HTTP calls.
        snapshot = {
            "lease_id": lease.id,
            "step_id": step.id,
            "step_key": step.step_key,
            "kind": step.kind,
            "provider": step.provider,
            "action_key": step.action_key,
            "capability_id": step.capability_id,
            "integration_id": step.integration_id,
            "resolved_subscription_id": step.resolved_subscription_id,
            "resolved_agent_id": step.resolved_agent_id,
            "params": params,
            "execution_mode": plan.execution_mode,
            "entity_id": step.entity_id,
            "workspace_id": step.workspace_id,
            "user_id": user_id,
            "task_id": plan.task_id,
            "conversation_id": conversation_id,
            "expected_output_schema": step.expected_output_schema,
        }

    # Execute outside the session. A background heartbeat keeps long-running
    # subagent/tool leases from expiring while the handler is awaiting models
    # or artifact generation.
    heartbeat_task = _start_lease_heartbeat(lease_id)
    try:
        result = await _execute_by_kind(snapshot)
    except _NeedsHumanInput as exc:
        async with async_session() as db:
            await dispatcher.lease_needs_human(
                db, lease_id,
                prompt=exc.prompt,
                pending_action=exc.pending_action,
            )
            await db.commit()
        return {"lease_id": lease_id, "outcome": "needs_human"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("execute_lease %s failed: %s", lease_id, exc)
        async with async_session() as db:
            await dispatcher.fail_lease(
                db, lease_id,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            await db.commit()
        return {"lease_id": lease_id, "outcome": "failed"}
    finally:
        await _stop_lease_heartbeat(heartbeat_task)

    # Success path.
    async with async_session() as db:
        completed_lease = await dispatcher.complete_lease(
            db, lease_id,
            result=result.get("result"),
            cost=result.get("cost"),
            evidence_refs=result.get("evidence_refs"),
            metadata=result.get("metadata"),
        )
        await db.commit()
    return {"lease_id": lease_id, "outcome": completed_lease.status}


# ── Kind dispatch ─────────────────────────────────────────────────────

class _NeedsHumanInput(Exception):
    """Worker signals that the step needs the user before it can finish.

    Two shapes are supported:

    * Plain text prompt — the historical "ask the user a question"
      path. Caller passes ``prompt="..."``.
    * Structured ``pending_action`` — the new generic contract from
      packages/core/ai/pending_action.py. When set, the chat notifier
      uses it verbatim (interactive button card etc.) instead of
      synthesizing a free-form text-input dialog.

    Both fields are optional, but at least one must be set; the chat
    layer falls back to a generic prompt if both are empty.
    """

    def __init__(
        self,
        prompt: str = "",
        *,
        pending_action: Optional[dict] = None,
    ) -> None:
        super().__init__(prompt or (pending_action or {}).get("title") or "")
        self.prompt = prompt
        self.pending_action = pending_action


async def _execute_by_kind(s: dict) -> dict:
    """Route a snapshot to the right handler. Returns
    ``{result, cost, evidence_refs}`` envelope."""
    kind = s["kind"]
    if kind == "human":
        return await _exec_human(s)
    if kind == "action":
        return await _exec_action(s)
    if kind == "llm":
        return await _exec_llm(s)
    if kind == "subagent":
        return await _exec_subagent(s)
    if kind == "code":
        # M5+ — not in scope yet.
        raise NotImplementedError("kind=code not yet supported")
    raise NotImplementedError(f"InternalWorker doesn't handle kind={kind!r}")


async def _exec_human(s: dict) -> dict:
    """Pause for operator input, or complete after a response is supplied."""
    params = s.get("params") if isinstance(s.get("params"), dict) else {}
    response = params.get("human_input_response")
    if response is not None:
        return {
            "result": response if isinstance(response, dict) else {"response": response},
            "cost": {"usd": 0},
        }

    pending_action = params.get("pending_action")
    if not (isinstance(pending_action, dict) and pending_action.get("kind")):
        pending_action = None
    prompt = str(
        params.get("prompt")
        or params.get("question")
        or params.get("title")
        or params.get("message")
        or (pending_action or {}).get("prompt")
        or (pending_action or {}).get("title")
        or "Please provide input to continue."
    )
    raise _NeedsHumanInput(prompt=prompt, pending_action=pending_action)


async def _exec_action(s: dict) -> dict:
    """Call a provider's MCP-style adapter. Sandbox / dry_run flips to
    the adapter's ``simulate_tool``."""
    if not s["provider"] or not s["action_key"]:
        raise ValueError("action step missing provider / action_key")

    try:
        module = importlib.import_module(f"packages.core.ai.mcp.{s['provider']}")
    except ImportError as exc:
        raise ValueError(f"no adapter for provider={s['provider']!r}") from exc

    if s["execution_mode"] in ("dry_run", "sandbox"):
        sim = getattr(module, "simulate_tool", None)
        if sim is None:
            envelope = {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"_simulated": True, "input": s["params"]}),
                }],
                "isError": False,
            }
        else:
            envelope = await sim(s["action_key"], s["params"])
    else:
        # Live mode — resolve credentials via Vault.
        from packages.core.credentials import Requester, get_credential_service

        async with async_session() as db:
            integration: Optional[Integration] = None
            if s["integration_id"]:
                integration = (await db.execute(
                    select(Integration).where(Integration.id == s["integration_id"])
                )).scalar_one_or_none()
            else:
                integration = (await db.execute(
                    select(Integration).where(
                        Integration.entity_id == s["entity_id"],
                        Integration.provider == s["provider"],
                        Integration.status == "active",
                    ).order_by(Integration.created_at.desc()).limit(1)
                )).scalar_one_or_none()

        if integration is None:
            raise ValueError(
                f"no active integration for provider={s['provider']!r}"
            )

        creds = get_credential_service().lease_integration(
            integration,
            requester=Requester(kind="step", id=s["step_id"], step_id=s["step_id"]),
            reason=f"action:{s['provider']}.{s['action_key']}",
        )
        token = (
            creds.get("access_token")
            or creds.get("bearer_token")
            or creds.get("token")
            or ""
        )
        envelope = await module.call_tool(s["action_key"], s["params"], token)

    if envelope.get("isError"):
        msg = "; ".join(c.get("text", "") for c in envelope.get("content", []))
        raise RuntimeError(f"adapter error: {msg}")

    _maybe_raise_needs_human(envelope)

    parsed = _extract_text(envelope)
    return {
        "result": parsed if isinstance(parsed, dict) else {"value": parsed},
        "cost": {"api_calls": 1, "usd": 0},
    }


async def _exec_llm(s: dict) -> dict:
    """Single LLM call — uses shared context builder for model resolution."""
    from packages.core.ai.context import build_agent_context
    from packages.core.database import async_session

    prompt = _step_prompt(s["params"])
    if not prompt:
        raise ValueError("llm step requires params.prompt (or instructions/instruction/user_prompt/message/task)")

    entity_id = s.get("entity_id")
    agent_id = s.get("resolved_agent_id")
    ctx = None
    model = None
    if entity_id:
        try:
            async with async_session() as db:
                ctx = await build_agent_context(
                    db,
                    entity_id=entity_id,
                    user_id=s.get("user_id"),
                    agent_id=agent_id,
                    workspace_id=s.get("workspace_id"),
                    conversation_id=s.get("conversation_id"),
                    model_role="worker",
                )
                model = ctx.model
        except Exception:
            pass

    completion = await runtime_execute_internal_worker_llm_step(
        prompt=prompt,
        expected_output_schema=s.get("expected_output_schema"),
        system_prompt=s["params"].get("system_prompt") or getattr(ctx, "system_prompt", None),
        entity_id=entity_id,
        user_id=s.get("user_id"),
        agent_id=agent_id,
        workspace_id=s.get("workspace_id"),
        model=model,
        byok=bool(getattr(ctx, "byok", False)),
        metadata=getattr(ctx, "llm_metadata", None),
    )

    usage = completion.usage or {}
    if ctx is not None:
        await runtime_persist_internal_worker_runtime_events(
            getattr(ctx, "runtime_envelope", None),
        )
    return {
        "result": _coerce_llm_text_result(completion.content, s.get("expected_output_schema")),
        "cost": {
            "llm_tokens_input": usage.get("prompt_tokens"),
            "llm_tokens_output": usage.get("completion_tokens"),
            "usd": 0,
        },
        "metadata": runtime_metadata_from_context(ctx),
    }


async def _exec_subagent(s: dict) -> dict:
    """Multi-turn agent with tools through the Runtime Harness adapter."""
    from packages.core.ai.context import build_agent_context
    from packages.core.database import async_session

    original_prompt = _step_prompt(s["params"])
    if not original_prompt:
        raise ValueError("subagent step requires params.prompt (or instructions/instruction/user_prompt/message/task)")
    prompt = runtime_prompt_with_output_schema(original_prompt, s.get("expected_output_schema"))

    entity_id = s.get("entity_id")
    agent_id = s.get("resolved_agent_id")

    async with async_session() as db:
        ctx = await build_agent_context(
            db, entity_id=entity_id or "", agent_id=agent_id,
            user_id=s.get("user_id"),
            workspace_id=s.get("workspace_id"),
            active_user_message=prompt,
            model_role="primary",
        )

    system_prompt = s["params"].get("system_prompt") or ctx.system_prompt
    params = s.get("params") if isinstance(s.get("params"), dict) else {}

    loop_result = await runtime_execute_worker_subagent_loop(
        runtime_envelope=ctx.runtime_envelope,
        system_prompt=system_prompt,
        user_message=prompt,
        tools=ctx.tools,
        entity_id=entity_id or "",
        agent_id=agent_id,
        workspace_id=s.get("workspace_id"),
        conversation_id=s.get("conversation_id"),
        task_id=s.get("task_id"),
        active_user_message=prompt,
        legacy_tool_profile=ctx.legacy_runtime_profile,
        allowed_tool_names=ctx.allowed_tool_names,
        model=ctx.model,
        metadata=getattr(ctx, "llm_metadata", None),
        requested_name=params.get("subagent") or params.get("subagent_name"),
        requested_max_rounds=params.get("max_rounds"),
    )
    result = loop_result.result

    usage = result.usage or {}
    _raise_if_agentic_loop_failed(result)
    pending_action = _pending_action_from_agent_messages(result.messages or [])
    if pending_action:
        raise _NeedsHumanInput(
            prompt=str(pending_action.get("prompt") or pending_action.get("title") or ""),
            pending_action=pending_action,
        )

    step_result = _coerce_llm_text_result(result.content, s.get("expected_output_schema"))
    step_result = _merge_artifact_refs(
        step_result,
        _collect_artifact_refs_from_agent_messages(result.messages or []),
    )
    step_result = _infer_prompt_backed_fields(
        step_result,
        prompt=original_prompt,
        schema=s.get("expected_output_schema"),
    )
    step_result = _merge_tool_backed_fields_for_schema(
        step_result,
        result.messages or [],
        schema=s.get("expected_output_schema"),
    )
    if (
        _schema_requires_materialized_artifact(s.get("expected_output_schema"))
        and isinstance(step_result, dict)
        and not any(step_result.get(field) for field in _MATERIALIZED_ARTIFACT_SCHEMA_FIELDS)
    ):
        step_result = await _persist_subagent_text_artifact(
            s,
            prompt=original_prompt,
            content=result.content,
            result=step_result,
        )
    await runtime_persist_internal_worker_runtime_events(
        getattr(ctx, "runtime_envelope", None),
    )
    return {
        "result": step_result,
        "cost": {
            "llm_tokens_input": usage.get("prompt_tokens"),
            "llm_tokens_output": usage.get("completion_tokens"),
            "llm_rounds": result.rounds,
            "tool_call_count": len(result.tool_calls_made or []),
            "usd": 0,
        },
        "metadata": runtime_metadata_from_context(ctx),
    }


def _infer_prompt_backed_fields(result: Any, *, prompt: Any, schema: Optional[dict]) -> Any:
    """Fill fields that are explicitly present in the step prompt.

    This is intentionally conservative: it copies the publish text the plan
    already supplied, but it does not invent externally confirmed values such
    as live URLs.
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return result
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if "post_text" not in props:
        return result
    if not isinstance(result, dict):
        result = {"text": str(result)}
    if isinstance(result.get("post_text"), str) and result["post_text"].strip():
        return result

    post_text = _extract_delimited_post_text(prompt)
    if post_text:
        result = dict(result)
        result["post_text"] = post_text
    return result


def _merge_tool_backed_fields_for_schema(
    result: Any,
    messages: list[dict[str, Any]],
    *,
    schema: Optional[dict],
) -> Any:
    """Fill schema fields that are proven by successful tool results.

    This keeps publish steps from relying on the model to copy IDs/timestamps
    from an integration response into its final JSON-only answer.
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return result
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    wanted = {
        "tweet_id",
        "published_at",
        "status",
        "post_url",
        "tweet_url",
        "post_text",
        "tweet_text",
    }
    if not any(name in props or name in required for name in wanted):
        return result

    merged = dict(result) if isinstance(result, dict) else {"text": str(result)}
    for payload in _publish_tool_payloads_from_agent_messages(messages):
        candidate = coerce_step_output_for_schema(schema, payload)
        if not isinstance(candidate, dict):
            continue
        for key in (set(props) | required):
            if _has_output_value(merged.get(key)) or not _has_output_value(candidate.get(key)):
                continue
            merged[key] = candidate[key]
    return merged


def _publish_tool_payloads_from_agent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_names_by_id: dict[str, str] = {}
    payloads: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                call_id = str(tool_call.get("id") or "")
                if call_id:
                    tool_names_by_id[call_id] = _tool_call_name(tool_call)
            continue
        if message.get("role") != "tool":
            continue
        parsed = _parse_json_from_text(str(message.get("content") or ""))
        if not isinstance(parsed, dict):
            continue
        tool_name = tool_names_by_id.get(str(message.get("tool_call_id") or ""), "")
        if _looks_like_publish_tool_payload(tool_name, parsed):
            payloads.append(parsed)
    return payloads


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(tool_call.get("name") or "")


def _looks_like_publish_tool_payload(tool_name: str, payload: dict[str, Any]) -> bool:
    name = tool_name.lower()
    if "twitter_x" in name and ("create_tweet" in name or "post_tweet" in name):
        return True
    if payload.get("tweet_id") or payload.get("tweet_url"):
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return bool(data.get("edit_history_tweet_ids"))


def _has_output_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _extract_delimited_post_text(prompt: Any) -> str | None:
    text = str(prompt or "")
    candidates = [
        match.strip()
        for match in re.findall(r"(?:^|\n)\s*---\s*\n([\s\S]+?)\n\s*---(?:\n|$)", text)
    ]
    candidates = [
        candidate
        for candidate in candidates
        if len(candidate) >= 20 and "${{" not in candidate and "}}" not in candidate
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


async def _persist_subagent_text_artifact(
    s: dict,
    *,
    prompt: Any,
    content: str,
    result: dict,
) -> dict:
    """Persist a text-only subagent answer when the plan requires a file.

    This keeps the planner/subagent handoff robust: models sometimes produce a
    correct artifact as final text instead of calling write_file. If the
    expected schema requires ``fs_path``, the worker materialises that text and
    returns the file reference for downstream steps.
    """
    entity_id = str(s.get("entity_id") or "")
    if not entity_id:
        return result

    rel_path = await _workspace_scoped_artifact_path(s, prompt)
    payload = content or json.dumps(result, ensure_ascii=False, indent=2, default=str)
    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    entity_root = get_entity_root(entity_id)
    abs_path = write_entity_file_atomic(
        entity_id,
        rel_path,
        payload.encode("utf-8"),
        allow_empty=False,
    )
    sync = await sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source="agent",
        created_by=s.get("resolved_agent_id") or "worker-subagent",
        force=True,
        workspace_id=s.get("workspace_id"),
        task_id=s.get("task_id"),
        agent_id=s.get("resolved_agent_id"),
        conversation_id=s.get("conversation_id"),
        tool_name="subagent_text_artifact",
    )
    file_ref = {
        "type": "file",
        "fs_path": rel_path,
        "name": os.path.basename(rel_path),
    }
    if sync.document_id:
        file_ref["document_id"] = sync.document_id

    result = dict(result)
    result["fs_path"] = rel_path
    result.setdefault("path", rel_path)
    for alias in ("file_path", "file_url", "document_url"):
        if _schema_requests_field(s.get("expected_output_schema"), alias) and not result.get(alias):
            result[alias] = rel_path
    result["files"] = [*list(result.get("files") or []), file_ref]
    if sync.document_id and not result.get("document_id"):
        result["document_id"] = sync.document_id
    result.setdefault("summary", str(result.get("text") or content)[:500])
    result["artifact_materialized"] = True
    return result


def _extract_text(envelope: dict) -> Any:
    for block in envelope.get("content", []):
        text = block.get("text", "")
        if not text:
            continue
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text
    return None


def _maybe_raise_needs_human(envelope: dict) -> None:
    """Lift the generic ``_pending_action`` contract from an MCP
    envelope. If present, raise ``_NeedsHumanInput`` carrying it so
    the worker's existing ``except _NeedsHumanInput`` path routes the
    lease through ``dispatcher.lease_needs_human``.

    See packages/core/ai/pending_action.py for the contract. Any tool
    wrapper can opt in by attaching ``_pending_action`` to its result
    envelope. Tools that don't set it just don't (no behavior change).
    """
    pending_action = envelope.get("_pending_action")
    if not isinstance(pending_action, dict):
        return
    if not pending_action.get("kind"):
        return
    # Title is human-readable; use it as the prompt fallback so older
    # UIs reading step.human_input_prompt still show something useful.
    prompt = (pending_action.get("title") or "").strip()
    raise _NeedsHumanInput(prompt=prompt, pending_action=pending_action)


# Re-export the registry helper since some callers import it from here.
__all__ = [
    "ensure_internal_worker",
    "tick_one_internal_worker",
    "tick_all_internal_workers",
    "execute_lease_inproc",
]
