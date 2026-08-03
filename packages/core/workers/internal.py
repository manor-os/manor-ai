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
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import create_engine, select
from sqlalchemy import text as sa_text
from sqlalchemy.engine import Engine

from packages.core.constants.execution import (
    WorkLeaseStatus,
    WorkerStatus,
)
from packages.core.config import get_settings
from packages.core.constants.pending_actions import PendingActionKind
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
    runtime_tool_call_error,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import Worker, WorkLease
from packages.core.workers.execution_claim import (
    claim_lease_for_execution,
    release_execution_claim,
)
from packages.core.ai.terminal_stops import is_terminal_tool_success
from packages.core.models.media_job import MediaJobStatus
from packages.core.workers.submit_result import (
    SUBMIT_RESULT_PROMPT_SUFFIX,
    SUBMIT_RESULT_TERMINAL_POLICY,
    SUBMIT_RESULT_TOOL_NAME,
    build_submit_result_tool,
    step_result_from_submit,
    submit_result_capture,
    submit_result_followup_message,
)
from packages.core.workers.registry import (
    INTERNAL_WORKER_KIND,
    ensure_internal_worker,
)
from packages.core.services.step_deadline import (
    DEFAULT_MAX_RUNTIME_SECONDS,
    resolve_step_deadline,
    step_deadline_error,
)
from packages.core.contracts.shapes import coerce_to_shape, get_shape
from packages.core.services.workspace_layout import WorkspaceArtifactDir
from packages.core.contracts.envelope import Success, Failure, StepResult
from packages.core.contracts.workspace_paths import default_fs_path_into_workspace

logger = logging.getLogger(__name__)

_PROMPT_PARAM_KEYS = ("prompt", "user_prompt", "instructions", "instruction", "message", "task")
LEASE_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MANOR_INTERNAL_LEASE_HEARTBEAT_SECONDS", "60"))
LEASE_HEARTBEAT_EXTEND_SECONDS = float(os.getenv("MANOR_INTERNAL_LEASE_EXTEND_SECONDS", "300"))
# Slack when deciding whether a TimeoutError came from our own deadline or
# from inside the step body (a socket/client timeout is an ordinary failure).
_DEADLINE_TOLERANCE_SECONDS = 0.05


class _StepDeadlineExceeded(Exception):
    """Internal signal: the step body outlived its resolved runtime budget."""

    def __init__(self, *, elapsed_seconds: float) -> None:
        super().__init__("step deadline exceeded")
        self.elapsed_seconds = elapsed_seconds


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
_ARTIFACT_LIST_KEYS = ("files", "artifacts", "documents", "images", "image_urls", "jobs")
_ARTIFACT_CREATION_FLAGS = {
    "created", "written", "edited", "saved", "generated", "uploaded",
    "downloaded", "exported",
}
_REFERENCE_ONLY_KEYS = {
    "context", "sources", "source_count", "scope", "groups", "knowledge_nets",
    "entries", "matches", "evidence_mode", "content_evidence_available",
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


def _collect_step_evidence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Structured evidence of externally-verified effects (envelope part ③).

    Sourced ONLY from successful tool results in the transcript — model
    claims never count. Flows worker → complete_lease(evidence_refs=) →
    step.evidence_refs, where the plan supervisor checks it against the
    step's declared ``expects``.
    """
    evidence: list[dict[str, Any]] = []
    for payload in _publish_tool_payloads_from_agent_messages(messages):
        if payload.get("error") or payload.get("ok") is False:
            continue
        fields = {
            key: payload[key]
            for key in (
                "tweet_id", "tweet_url", "post_url", "share_url", "urn",
                "post_urn", "id", "published_at", "platform",
            )
            if _has_output_value(payload.get(key))
        }
        evidence.append({"kind": "tool_effect", "effect": "publish", "fields": fields})
    for ref in _collect_artifact_refs_from_agent_messages(messages):
        if isinstance(ref, dict):
            evidence.append({"kind": "artifact", **ref})
    return evidence


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

    if operation.get("kind") == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        # One producer for this card, not two. This branch used to rebuild the
        # blob field by field alongside the identical builder in
        # hitl_requests — so the typed review payload (the part that says a
        # hard block is being removed) reached the durable card on one path
        # and was silently dropped on the other.
        from packages.core.services.hitl_requests import (
            workspace_operation_pending_action_from_data,
        )

        return workspace_operation_pending_action_from_data(payload)

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
    if stop_reason in {"completed", "submit_result"}:
        return
    # A terminal-tool policy ending the loop is a deliberate SUCCESS, not an
    # error: submit_result, skill terminals, media generation and friends all
    # report their own stop_reason. Generalized via the shared registry so a
    # new terminal policy never re-introduces this bug (a compliant subagent
    # ending with stop_reason="submit_result" used to fail its step outright).
    if is_terminal_tool_success(result):
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
        if key == "jobs":
            # Async media pipeline: the finished file's fs_path / document_id
            # lives on each job, not at the payload root. Without this the
            # evidence channel could not see a single generated video, so a
            # step that produced 12 of them recorded no artifact at all.
            # Only completed jobs — a pending one has no file yet, a failed
            # one never will.
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                if not MediaJobStatus.is_completed(item.get("status")):
                    continue
                for value_key in ("fs_path", "document_id", "result_url"):
                    if item.get(value_key):
                        add_ref(
                            str(item.get("kind") or "media"),
                            item[value_key],
                            source_key=value_key,
                        )
                        break
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


def _artifact_ref_identity(ref: Any) -> str | None:
    """What makes two artifact refs the same file.

    The location, not the whole dict: the same PNG discovered through a tool
    payload, a step result and the evidence sweep arrives with different
    ``type``/``source`` labels but one path. Comparing whole dicts left the
    run output listing six images as eighteen.
    """
    if not isinstance(ref, dict):
        return None
    for key in ("fs_path", "document_id", "url"):
        value = str(ref.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return None


def _dedupe_artifact_refs(refs: list[Any]) -> list[Any]:
    """Keep first occurrence of each artifact, order preserved."""
    seen: set[str] = set()
    out: list[Any] = []
    for ref in refs:
        identity = _artifact_ref_identity(ref)
        if identity is None:
            out.append(ref)
            continue
        if identity in seen:
            continue
        seen.add(identity)
        out.append(ref)
    return out


def _merge_artifact_refs(result: Any, refs: list[dict[str, Any]]) -> Any:
    if not refs:
        return result
    if not isinstance(result, dict):
        result = {"value": result}
    existing = result.get("files")
    if isinstance(existing, list):
        result["files"] = _dedupe_artifact_refs(existing + refs)
    else:
        result["files"] = _dedupe_artifact_refs(refs)
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
            default_subdir=WorkspaceArtifactDir.ARTIFACTS.value,
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
        if worker.status != WorkerStatus.ACTIVE:
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
                    Worker.status == WorkerStatus.ACTIVE,
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

# The heartbeat runs in its OWN OS thread, not as an asyncio task.
#
# It used to be ``asyncio.create_task(...)`` in the same event loop as the step
# body. That works only while the body cooperates: any step that blocks the
# loop — CPU-bound work, a synchronous vendor SDK, blocking IO — starves the
# heartbeat's ``await asyncio.sleep`` for exactly as long as it blocks. The
# lease then sails past its 300s TTL, ``cleanup_expired_leases`` reclaims it,
# and a step that is very much alive gets retried underneath a still-running
# worker. The longer the legitimate budget (now up to 6h), the likelier that is.
#
# A thread cannot be starved by the loop, so liveness no longer depends on
# every tool author remembering ``asyncio.to_thread``.
#
# The thread must NOT touch ``async_session``: the async engine's asyncpg
# connections are bound to the main loop and are not safe to use from another
# thread. It gets its own small SYNC engine instead (same pattern as
# ``packages/core/credentials/audit.py``), and its one statement is a single
# conditional UPDATE — the sync twin of ``Dispatcher.extend_lease``.

_LEASE_HEARTBEAT_ENGINE: Engine | None = None
_LEASE_HEARTBEAT_ENGINE_LOCK = threading.Lock()

# How often the async side refreshes its liveness marker, and how many missed
# refreshes it takes before the heartbeat thread calls the loop blocked.
_LOOP_MARKER_REFRESH_SECONDS = 5.0
_LOOP_STALL_INTERVALS = 3

_EXTEND_ACTIVE_LEASE_SQL = sa_text(
    """
    UPDATE work_leases
       SET lease_until = :lease_until,
           last_heartbeat_at = :now,
           heartbeat_count = COALESCE(heartbeat_count, 0) + 1,
           extended_count = COALESCE(extended_count, 0) + 1
     WHERE id = :lease_id
       AND status = 'active'
    """
)


def _lease_heartbeat_database_url() -> str:
    """Sync DSN for the heartbeat thread.

    Read from the environment at call time (falling back to ``Settings``,
    which reads the same variable) so a process that configures its database
    after import — the test suite does exactly that — still heartbeats against
    the database the rest of the run is using.
    """
    return os.getenv("DATABASE_URL_SYNC") or get_settings().DATABASE_URL_SYNC


def _lease_heartbeat_engine() -> Engine:
    """Lazily built, process-wide sync engine.

    Small pool on purpose: every concurrent lease in this process shares it,
    but each one borrows a connection for a single UPDATE once per interval.
    Built lazily so importing this module never opens a connection (and so a
    prefork Celery child builds its own rather than inheriting the parent's).
    """
    global _LEASE_HEARTBEAT_ENGINE
    with _LEASE_HEARTBEAT_ENGINE_LOCK:
        if _LEASE_HEARTBEAT_ENGINE is None:
            _LEASE_HEARTBEAT_ENGINE = create_engine(
                _lease_heartbeat_database_url(),
                pool_size=4,
                max_overflow=4,
                pool_pre_ping=True,
                pool_recycle=1800,
                future=True,
            )
        return _LEASE_HEARTBEAT_ENGINE


def _reset_lease_heartbeat_engine() -> None:
    """Drop the cached engine (tests re-point the database between runs)."""
    global _LEASE_HEARTBEAT_ENGINE
    with _LEASE_HEARTBEAT_ENGINE_LOCK:
        engine, _LEASE_HEARTBEAT_ENGINE = _LEASE_HEARTBEAT_ENGINE, None
    if engine is not None:
        with contextlib.suppress(Exception):
            engine.dispose()


def extend_active_lease_sync(lease_id: str, *, extra_seconds: float) -> bool:
    """Push ``lease_until`` out by ``extra_seconds`` — only while active.

    Returns False when the row is gone or no longer ``active``; that is the
    signal to stop heartbeating (the async twin raises ``LeaseNotActive``).
    """
    now = datetime.now(timezone.utc)
    with _lease_heartbeat_engine().begin() as conn:
        result = conn.execute(
            _EXTEND_ACTIVE_LEASE_SQL,
            {
                "lease_id": lease_id,
                "now": now,
                "lease_until": now + timedelta(seconds=float(extra_seconds)),
            },
        )
    return bool(result.rowcount)


class _EventLoopLivenessMarker:
    """Last time the main event loop was demonstrably making progress.

    The async side bumps ``touched_at`` between awaits; the heartbeat thread
    reads it. A marker that stops moving means the loop is blocked — which is
    precisely the condition that used to kill the heartbeat silently.
    """

    __slots__ = ("touched_at",)

    def __init__(self) -> None:
        self.touched_at = time.monotonic()

    def touch(self) -> None:
        self.touched_at = time.monotonic()

    def stalled_for(self) -> float:
        return time.monotonic() - self.touched_at


class LeaseHeartbeat:
    """A lease heartbeat running on a dedicated daemon thread."""

    def __init__(
        self,
        lease_id: str,
        *,
        interval_seconds: float,
        extend_seconds: float,
        loop_marker: _EventLoopLivenessMarker | None = None,
    ) -> None:
        self.lease_id = lease_id
        self.interval_seconds = float(interval_seconds)
        self.extend_seconds = float(extend_seconds)
        self._stop = threading.Event()
        self._marker = loop_marker or _EventLoopLivenessMarker()
        self._marker_task: asyncio.Task | None = None
        self._warned_blocked = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{lease_id}",
            daemon=True,
        )

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()
        # The marker refresher IS an asyncio task, on purpose: it is the probe.
        # When the step body blocks the loop this task cannot run, the marker
        # goes stale, and the thread reports the stall.
        with contextlib.suppress(RuntimeError):
            self._marker_task = asyncio.create_task(self._refresh_loop_marker())

    def stop(self, *, join_timeout: float = 10.0) -> None:
        """Signal + join. Called from ``finally`` — must never raise."""
        self._stop.set()
        if self._marker_task is not None:
            self._marker_task.cancel()
            self._marker_task = None
        if self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                logger.warning(
                    "execute_lease %s heartbeat thread did not stop within %.0fs",
                    self.lease_id, join_timeout,
                )

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    @property
    def marker_task(self) -> asyncio.Task | None:
        return self._marker_task

    # ── the two loops ─────────────────────────────────────────────────

    def _marker_refresh_interval(self) -> float:
        return min(_LOOP_MARKER_REFRESH_SECONDS, max(self.interval_seconds, 0.01))

    async def _refresh_loop_marker(self) -> None:
        interval = self._marker_refresh_interval()
        while True:
            self._marker.touch()
            await asyncio.sleep(interval)

    def _run(self) -> None:
        # ``Event.wait`` returns True the moment stop() fires, so a finished
        # step never waits out a full interval before the thread exits.
        while not self._stop.wait(self.interval_seconds):
            try:
                still_active = extend_active_lease_sync(
                    self.lease_id, extra_seconds=self.extend_seconds,
                )
            except Exception:  # noqa: BLE001 — a heartbeat must never fail a step
                logger.warning(
                    "execute_lease %s heartbeat failed", self.lease_id, exc_info=True,
                )
                continue
            if not still_active:
                logger.debug(
                    "execute_lease %s heartbeat stopped: lease no longer active",
                    self.lease_id,
                )
                return
            self._report_loop_stall()

    def _report_loop_stall(self) -> None:
        stalled = self._marker.stalled_for()
        # Scales with whichever cadence is slower, so a compressed test
        # interval still fires and a 60s production interval doesn't cry wolf
        # over one slow await.
        threshold = _LOOP_STALL_INTERVALS * max(
            self.interval_seconds, self._marker_refresh_interval()
        )
        if stalled >= threshold:
            self._warned_blocked = True
            logger.warning(
                "lease %s: event loop appears blocked for %.0fs (heartbeat still extending)",
                self.lease_id, stalled,
            )
        elif self._warned_blocked:
            self._warned_blocked = False
            logger.info("lease %s: event loop is responsive again", self.lease_id)


def _start_lease_heartbeat(lease_id: str) -> LeaseHeartbeat | None:
    if LEASE_HEARTBEAT_INTERVAL_SECONDS <= 0:
        return None
    heartbeat = LeaseHeartbeat(
        lease_id,
        interval_seconds=LEASE_HEARTBEAT_INTERVAL_SECONDS,
        extend_seconds=LEASE_HEARTBEAT_EXTEND_SECONDS,
    )
    heartbeat.start()
    return heartbeat


async def _stop_lease_heartbeat(heartbeat: LeaseHeartbeat | None) -> None:
    """Always called from ``finally`` — the thread must not outlive the step."""
    if heartbeat is None:
        return
    marker_task = heartbeat.marker_task
    heartbeat.stop()
    if marker_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await marker_task


async def execute_lease_inproc(lease_id: str) -> dict:
    """Run one lease end-to-end and report the result back to Dispatcher.

    ``execute_lease`` Celery task wraps this. Each kind routes to its
    handler; failures inside a handler bubble to ``fail_lease`` so the
    DB is always the source of truth on lease state.

    Re-delivery safety lives here, not in the handlers. Celery may deliver the
    same ``execute_lease`` message twice — the broker's visibility timeout
    lapsing, or ``task_reject_on_worker_lost`` requeueing after a worker dies
    mid-step. Before anything else, this takes a database-level execution claim
    on the lease (``packages/core/workers/execution_claim.py``); of N
    simultaneous deliveries exactly one gets it and the rest return
    ``{"skipped": True}`` without touching the step. The claim is released on
    every exit path so a retry can re-take it.
    """
    claim_id = generate_ulid()
    async with async_session() as db:
        claim = await claim_lease_for_execution(db, lease_id, claim_id=claim_id)
        await db.commit()
    if not claim:
        return {"lease_id": lease_id, "skipped": True, "reason": claim.reason}

    try:
        return await _execute_claimed_lease(lease_id)
    finally:
        async with async_session() as db:
            await release_execution_claim(db, lease_id, claim_id=claim_id)
            await db.commit()


async def _execute_claimed_lease(lease_id: str) -> dict:
    """The body of ``execute_lease_inproc``, run under a held execution claim."""
    dispatcher = Dispatcher()

    async with async_session() as db:
        lease = (await db.execute(
            select(WorkLease).where(WorkLease.id == lease_id)
        )).scalar_one_or_none()
        if lease is None or lease.status != WorkLeaseStatus.ACTIVE:
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
        task_binding_constraints: list[str] = []
        if plan.task_id:
            try:
                from packages.core.models.task import Task
                task_row = (await db.execute(
                    select(
                        Task.conversation_id, Task.creator_id, Task.details,
                    ).where(Task.id == plan.task_id)
                )).first()
                if task_row:
                    conversation_id = task_row[0]
                    user_id = task_row[1]
                    # The user's verbatim task constraints, carried to the
                    # executing subagent so a prohibition like "no essay" is
                    # not lost between planning and execution.
                    from packages.core.plans.task_constraints import (
                        extract_binding_constraints,
                    )
                    task_binding_constraints = extract_binding_constraints(task_row[2])
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
            "task_binding_constraints": task_binding_constraints,
            "expected_output_schema": step.expected_output_schema,
            "attempt_count": step.attempt_count,
            "prior_error": step.error if isinstance(step.error, dict) else None,
        }

        # Explicit runtime budget for THIS attempt, resolved from the same
        # step/plan/workspace layering the retry policy uses. Carried on the
        # work item so the worker enforces the configured policy, not a
        # process-level timeout.
        deadline = await resolve_step_deadline(db, step, plan=plan)
        snapshot["max_runtime_seconds"] = deadline.max_runtime_seconds
        snapshot["max_runtime_source"] = deadline.source

    # Execute outside the session. A background heartbeat THREAD keeps
    # long-running subagent/tool leases from expiring while the handler is
    # awaiting models or artifact generation — liveness is proven by the
    # heartbeat, and the only thing that ends a live step is its explicit
    # deadline below. It is a thread, not a task, so a step body that blocks
    # this event loop cannot starve it (see LeaseHeartbeat).
    heartbeat = _start_lease_heartbeat(lease_id)
    max_runtime_seconds = float(snapshot.get("max_runtime_seconds") or DEFAULT_MAX_RUNTIME_SECONDS)
    started_at = time.monotonic()
    try:
        try:
            # asyncio.wait_for cancels the body cleanly on expiry (the handler
            # sees CancelledError and unwinds); the heartbeat thread keeps the
            # lease alive until the body settles, and is stopped in `finally`.
            result = await asyncio.wait_for(
                _execute_by_kind(snapshot),
                timeout=max_runtime_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            elapsed = time.monotonic() - started_at
            if elapsed + _DEADLINE_TOLERANCE_SECONDS < max_runtime_seconds:
                # A timeout raised INSIDE the step body (socket / client
                # timeout) — an ordinary failure, not our deadline.
                raise
            raise _StepDeadlineExceeded(elapsed_seconds=elapsed) from exc
    except _StepDeadlineExceeded as deadline_exc:
        elapsed = deadline_exc.elapsed_seconds
        error = step_deadline_error(
            max_runtime_seconds=max_runtime_seconds,
            elapsed_seconds=elapsed,
            source=str(snapshot.get("max_runtime_source") or "default"),
        )
        logger.warning(
            "execute_lease %s exceeded its %ss runtime budget (elapsed %.1fs)",
            lease_id, max_runtime_seconds, elapsed,
        )
        async with async_session() as db:
            # A normal step failure — flows through the retry policy exactly
            # like any other error, instead of a SIGKILL'd process.
            await dispatcher.fail_lease(db, lease_id, error=error)
            await db.commit()
        return {"lease_id": lease_id, "outcome": "failed", "error": error}
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
        await _stop_lease_heartbeat(heartbeat)

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

class EmptyModelOutput(Exception):
    """The model finished normally but produced nothing to record.

    Raised instead of returning an empty result: an empty completion is a step
    FAILURE, not a success holding ``{"text": ""}``. Empty completions are
    frequently transient, so this deliberately bubbles to ``fail_lease`` and
    lets the dispatcher's retry policy own the retry — the worker never retries
    silently on its own.
    """


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
    handler = _KIND_HANDLERS.get(kind)
    if handler is not None:
        return await handler(s)
    if kind == "code":
        # M5+ — not implemented, and deliberately not advertised to the
        # dispatcher, so a lease for it should never reach this worker.
        raise NotImplementedError("kind=code not yet supported")
    raise NotImplementedError(f"InternalWorker doesn't handle kind={kind!r}")


async def _exec_sleep(s: dict) -> dict:
    """Complete a timer step.

    ``PlanExecutor`` normally resolves sleep inline, but the kind is
    advertised to the dispatcher, so the worker must be able to honour a
    lease it is legally handed.
    """
    params = s.get("params") if isinstance(s.get("params"), dict) else {}
    seconds = params.get("seconds") or params.get("duration_seconds") or 0
    try:
        seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        seconds = 0
    return {"result": {"slept": seconds}, "cost": {"usd": 0}}


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


def _prior_attempt_feedback(prior_error: Any) -> str | None:
    """Turn the previous attempt's stored error into a prompt note.

    Dispatcher retries re-lease the step with identical params, so without
    this the model re-runs the exact prompt that just failed — attempts 2
    and 3 are indistinguishable from attempt 1. Surfacing the validation
    errors is the only signal that lets a retry do better."""
    if not isinstance(prior_error, dict) or not prior_error:
        return None
    err_type = str(prior_error.get("type") or "").strip()
    message = str(prior_error.get("message") or "").strip()[:300]
    headline = ": ".join(part for part in (err_type, message) if part) or "unknown error"
    lines = [f"[Retry context] The previous attempt of this step failed — {headline}"]
    errors = prior_error.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("Validation errors:")
        for item in errors[:6]:
            if isinstance(item, dict):
                path = str(item.get("path") or "$")
                msg = str(item.get("message") or "")[:160]
                lines.append(f"- {path}: {msg}")
            else:
                lines.append(f"- {str(item)[:160]}")
    lines.append(
        "Fix this in your output now: satisfy the required output contract "
        "exactly, and do not repeat the same mistake."
    )
    return "\n".join(lines)


def _with_binding_constraints(prompt: str, s: dict) -> str:
    """Prepend the user's verbatim task constraints so the executing model
    obeys them directly — the transmission the planner paraphrase used to drop.
    Bounded (a short list, not chat history), so it can't blow the context."""
    constraints = s.get("task_binding_constraints") or []
    if not constraints:
        return prompt
    from packages.core.plans.task_constraints import render_constraints_block

    block = render_constraints_block(constraints)
    return f"{block}\n\n{prompt}" if block else prompt


async def _exec_llm(s: dict) -> dict:
    """Single LLM call — uses shared context builder for model resolution."""
    from packages.core.ai.context import build_agent_context
    from packages.core.database import async_session

    prompt = _step_prompt(s["params"])
    if not prompt:
        raise ValueError("llm step requires params.prompt (or instructions/instruction/user_prompt/message/task)")
    prompt = _with_binding_constraints(prompt, s)
    feedback = _prior_attempt_feedback(s.get("prior_error"))
    if feedback:
        prompt = f"{feedback}\n\n{prompt}"

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
                    # Without these the step's RuntimeEventLog rows land with
                    # task_id/conversation_id NULL and can only be matched
                    # back to the step by timestamp proximity.
                    task_id=s.get("task_id"),
                    model_role="worker",
                )
                model = ctx.model
        except Exception:
            pass

    # Evidence is written in `finally` so it survives every exit path —
    # including the provider raising and the lease deadline cancelling us.
    # See docs/EXECUTION_OBSERVABILITY_DESIGN_ZH.md §3 principle 3.
    try:
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
        # An empty completion produced nothing — never coerce it into a
        # `{"text": ""}` "success". Fail so the retry policy gets a turn.
        if not str(completion.content or "").strip():
            raise EmptyModelOutput("model returned no content")
        return {
            "result": _coerce_llm_text_result(completion.content, s.get("expected_output_schema")),
            "cost": {
                "llm_tokens_input": usage.get("prompt_tokens"),
                "llm_tokens_output": usage.get("completion_tokens"),
                "usd": 0,
            },
            "metadata": runtime_metadata_from_context(ctx),
        }
    finally:
        await runtime_persist_internal_worker_runtime_events(
            getattr(ctx, "runtime_envelope", None),
        )


WORKER_SUBAGENT_TOOL_CALL_SOURCE = "worker_subagent"
"""``tool_call_logs.source`` for rows written from a plan step's subagent."""


def _worker_tool_call_log_callbacks(s: dict) -> tuple[Any, Any]:
    """Return ``(on_tool_start, on_tool_end)`` that log a step's tool calls.

    The chat path has always written ``tool_call_logs`` rows; the worker
    path passed no ``on_tool_end`` at all, so a plan step's tool calls left
    zero rows — precisely the rows an operator needs when the step fails
    (design doc §2.2 defect B). Same writer, same fire-and-forget
    discipline: a logging failure must never fail the step.
    """
    round_counter = [0]

    def on_tool_start(name: str, args: dict[str, Any]) -> None:
        round_counter[0] += 1

    def on_tool_end(
        name: str,
        result: str,
        duration_ms: float = 0,
        args: dict[str, Any] | None = None,
    ) -> None:
        try:
            from packages.core.ai import chat_logger

            text = result if isinstance(result, str) else str(result)
            error = runtime_tool_call_error(text)
            chat_logger.schedule_tool_call_log(
                entity_id=s.get("entity_id") or "",
                workspace_id=s.get("workspace_id"),
                agent_id=s.get("resolved_agent_id"),
                user_id=s.get("user_id"),
                conversation_id=s.get("conversation_id"),
                tool_name=name,
                round_num=round_counter[0],
                duration_ms=int(duration_ms or 0),
                result_chars=len(text),
                success=error is None,
                error=error,
                tool_args=chat_logger.safe_tool_args(args),
                source=WORKER_SUBAGENT_TOOL_CALL_SOURCE,
            )
        except Exception:
            logger.debug("worker tool-call log failed (best-effort)", exc_info=True)

    return on_tool_start, on_tool_end


async def _exec_subagent(s: dict) -> dict:
    """Multi-turn agent with tools through the Runtime Harness adapter."""
    from packages.core.ai.context import build_agent_context
    from packages.core.database import async_session

    original_prompt = _step_prompt(s["params"])
    if not original_prompt:
        raise ValueError("subagent step requires params.prompt (or instructions/instruction/user_prompt/message/task)")
    original_prompt = _with_binding_constraints(original_prompt, s)
    feedback = _prior_attempt_feedback(s.get("prior_error"))
    if feedback:
        original_prompt = f"{feedback}\n\n{original_prompt}"
    prompt = runtime_prompt_with_output_schema(original_prompt, s.get("expected_output_schema"))
    prompt = f"{prompt}{SUBMIT_RESULT_PROMPT_SUFFIX}"

    entity_id = s.get("entity_id")
    agent_id = s.get("resolved_agent_id")

    async with async_session() as db:
        ctx = await build_agent_context(
            db, entity_id=entity_id or "", agent_id=agent_id,
            user_id=s.get("user_id"),
            workspace_id=s.get("workspace_id"),
            # Without these the step's RuntimeEventLog rows land with
            # task_id/conversation_id NULL and can only be matched back to
            # the step by timestamp proximity (design doc §2.2 defect D).
            conversation_id=s.get("conversation_id"),
            task_id=s.get("task_id"),
            active_user_message=prompt,
            model_role="primary",
        )

    system_prompt = s["params"].get("system_prompt") or ctx.system_prompt
    params = s.get("params") if isinstance(s.get("params"), dict) else {}

    # ── forced submit_result finalization (StepResult envelope part ②) ──
    # The loop carries a submit_result tool and terminates on its call; the
    # captured payload IS the step result. Text-coercion heuristics below
    # remain as fallback only.
    submit_tool = build_submit_result_tool(s.get("expected_output_schema"))
    submit_handler, get_submit_payload = submit_result_capture()
    allowed_tool_names = ctx.allowed_tool_names
    if allowed_tool_names is not None:
        allowed_tool_names = [*allowed_tool_names, SUBMIT_RESULT_TOOL_NAME]
    on_tool_start, on_tool_end = _worker_tool_call_log_callbacks(s)

    # Evidence is written in `finally` so it survives every exit path —
    # a failed subagent step used to discard its whole runtime event list
    # with the process, because the persist call sat after every raise
    # (design doc §2.2 defect A, §3 principle 3). The persist itself is
    # idempotent per event, so this cannot double-write a run.
    try:
        loop_result = await runtime_execute_worker_subagent_loop(
            runtime_envelope=ctx.runtime_envelope,
            system_prompt=system_prompt,
            user_message=prompt,
            tools=[*ctx.tools, submit_tool],
            entity_id=entity_id or "",
            agent_id=agent_id,
            workspace_id=s.get("workspace_id"),
            conversation_id=s.get("conversation_id"),
            task_id=s.get("task_id"),
            active_user_message=prompt,
            tool_profile=ctx.tool_profile,
            allowed_tool_names=allowed_tool_names,
            model=ctx.model,
            metadata=getattr(ctx, "llm_metadata", None),
            requested_name=params.get("subagent") or params.get("subagent_name"),
            requested_max_rounds=params.get("max_rounds"),
            dynamic_tool_handlers={SUBMIT_RESULT_TOOL_NAME: submit_handler},
            terminal_tool_result_policy=SUBMIT_RESULT_TERMINAL_POLICY,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
        )
        result = loop_result.result

        usage = result.usage or {}
        # Capture the submitted deliverable BEFORE judging the loop's stop reason.
        # The deliverable is the contract; a stop reason is bookkeeping. Raising
        # first threw away a result the model had already handed over — a captured
        # payload must never be lost to an exception about how the loop ended.
        submit_payload = get_submit_payload()
        if submit_payload is None:
            _raise_if_agentic_loop_failed(result)
        pending_action = _pending_action_from_agent_messages(result.messages or [])
        if pending_action:
            raise _NeedsHumanInput(
                prompt=str(pending_action.get("prompt") or pending_action.get("title") or ""),
                pending_action=pending_action,
            )

        fallback_usage: dict[str, Any] = {}
        if submit_payload is None:
            # The model finished without submitting. One cheap follow-up round —
            # transcript continued, submit_result the ONLY tool — turns the
            # trailing prose into a deliberate submission.
            submit_payload, fallback_usage = await _force_submit_result_round(
                s, ctx=ctx, prompt=prompt, system_prompt=system_prompt,
                prior_messages=result.messages or [],
                submit_tool=submit_tool, submit_handler=submit_handler,
                get_submit_payload=get_submit_payload,
            )

        artifact_refs = _collect_artifact_refs_from_agent_messages(result.messages or [])
        evidence_refs = _collect_step_evidence(result.messages or [])
        if (
            submit_payload is None
            and not str(result.content or "").strip()
            and not artifact_refs
            and not evidence_refs
        ):
            # No submitted payload, no prose, no artifact, no tool effect — the
            # loop produced nothing. Same rule as _exec_llm: fail, don't dress an
            # empty string up as a result.
            raise EmptyModelOutput("model returned no content")

        if submit_payload is not None:
            step_result = step_result_from_submit(submit_payload)
        else:
            step_result = _coerce_llm_text_result(result.content, s.get("expected_output_schema"))
        step_result = _merge_artifact_refs(step_result, artifact_refs)
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
        return {
            "result": step_result,
            "evidence_refs": evidence_refs,
            "cost": {
                "llm_tokens_input": (usage.get("prompt_tokens") or 0)
                + (fallback_usage.get("prompt_tokens") or 0),
                "llm_tokens_output": (usage.get("completion_tokens") or 0)
                + (fallback_usage.get("completion_tokens") or 0),
                "llm_rounds": (result.rounds or 0) + (1 if fallback_usage else 0),
                "tool_call_count": len(result.tool_calls_made or []),
                "usd": 0,
            },
            "metadata": runtime_metadata_from_context(ctx),
        }
    finally:
        await runtime_persist_internal_worker_runtime_events(
            getattr(ctx, "runtime_envelope", None),
        )


_KIND_HANDLERS: dict[str, Any] = {
    "human": _exec_human,
    "action": _exec_action,
    "llm": _exec_llm,
    "subagent": _exec_subagent,
    "sleep": _exec_sleep,
}
"""Single source of truth for what this worker can actually run.

``registry.DEFAULT_INTERNAL_CAPABILITIES['supported_kinds']`` derives from
this map, because the dispatcher leases a step to any worker advertising
its kind. Advertising a kind with no handler is what produced the
``InternalWorker doesn't handle kind='human'`` failures: the registry
listed ``human`` from 2026-04-26 but the handler only landed 2026-06-11,
so every human step leased in between burned all 3 attempts and skipped
its dependents. Add the handler and the advertisement in the same change.
"""

INTERNAL_WORKER_SUPPORTED_KINDS: tuple[str, ...] = tuple(_KIND_HANDLERS)


async def _force_submit_result_round(
    s: dict,
    *,
    ctx: Any,
    prompt: str,
    system_prompt: str,
    prior_messages: list[dict[str, Any]],
    submit_tool: dict[str, Any],
    submit_handler: Any,
    get_submit_payload: Any,
) -> tuple[Optional[dict], dict[str, Any]]:
    """One follow-up round whose ONLY tool is submit_result.

    Runs when the main loop ended without a submission: the transcript is
    continued with a direct instruction to submit. Best-effort — any failure
    returns (None, usage) and the caller falls back to text coercion, so this
    can only improve on the legacy behavior, never regress it.
    """
    from packages.core.ai.runtime import runtime_execute_worker_subagent_followup

    try:
        followup = await runtime_execute_worker_subagent_followup(
            runtime_envelope=getattr(ctx, "runtime_envelope", None),
            system_prompt=system_prompt,
            user_message=prompt,
            initial_messages=[
                *prior_messages,
                {
                    "role": "user",
                    "content": submit_result_followup_message(
                        s.get("expected_output_schema")
                    ),
                },
            ],
            tools=[submit_tool],
            entity_id=s.get("entity_id") or "",
            agent_id=s.get("resolved_agent_id"),
            workspace_id=s.get("workspace_id"),
            conversation_id=s.get("conversation_id"),
            task_id=s.get("task_id"),
            active_user_message=prompt,
            allowed_tool_names=[SUBMIT_RESULT_TOOL_NAME],
            model=ctx.model,
            metadata=getattr(ctx, "llm_metadata", None),
            max_rounds=1,
            dynamic_tool_handlers={SUBMIT_RESULT_TOOL_NAME: submit_handler},
            terminal_tool_result_policy=SUBMIT_RESULT_TERMINAL_POLICY,
        )
        return get_submit_payload(), dict(getattr(followup, "usage", None) or {})
    except Exception:
        logger.warning("forced submit_result round failed", exc_info=True)
        return None, {}


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
        "platform",
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
    if "linkedin" in name and any(verb in name for verb in ("create_post", "create_share", "publish", "share_post")):
        return True
    if payload.get("tweet_id") or payload.get("tweet_url"):
        return True
    if payload.get("post_url") or payload.get("share_url"):
        return True
    urn = payload.get("urn") or payload.get("post_urn") or payload.get("id")
    if isinstance(urn, str) and urn.startswith("urn:li:"):
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
