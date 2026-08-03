"""Compact, display-safe snapshots and transition traces for Workflow runs."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

from packages.core.models.workflow import WorkflowRun
from packages.core.services.sensitive_data import sanitize_sensitive_payload


TRACE_SUMMARY_BYTES = 8 * 1024
MAX_ARTIFACT_REFS = 64
MAX_CHILD_RUN_IDS = 64
DEFINITION_CHANGED_ERROR = (
    "Workflow definition changed since this run started. Start a new run."
)

_ARTIFACT_KEYS = {
    "artifact_refs",
    "artifacts",
    "files",
    "documents",
    "images",
    "image_urls",
    "knowledge_artifacts",
}
_CHILD_RUN_ID_KEYS = {"child_run_id", "subrun_id"}
_CHILD_RUN_IDS_KEYS = {"child_run_ids", "subrun_ids"}
_ARTIFACT_FIELDS = (
    "id",
    "document_id",
    "fs_path",
    "path",
    "name",
    "mime_type",
    "status",
)
_HISTORY_SUMMARY_KEY = "_workflow_history_summary"


def workflow_definition_fingerprint(workflow: Any) -> str:
    """Return the graph identity required for checkpoint-compatible retries."""
    payload = {
        "id": getattr(workflow, "id", ""),
        "version": int(getattr(workflow, "version", 1) or 1),
        "steps": getattr(workflow, "steps", None) or [],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _targets(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item) for item in values if item not in (None, "")]


def workflow_chat_projection_visibility(step: dict[str, Any]) -> str:
    """Return the safe display visibility retained in run snapshots."""
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    value = str(
        config.get("chat_projection")
        or step.get("chat_projection")
        or "progress"
    ).lower()
    return value if value in {"hidden", "output"} else "progress"


def build_definition_snapshot(
    workflow: Any,
    *,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Retain only immutable display metadata and graph edges for one attempt."""
    nodes: list[dict[str, Any]] = []
    for order, step in enumerate(getattr(workflow, "steps", None) or []):
        if not isinstance(step, dict):
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        targets: list[str] = []
        for key in ("next", "true_next", "false_next"):
            targets.extend(_targets(step.get(key)))
        cases = config.get("cases", step.get("cases"))
        if isinstance(cases, list):
            for case in cases:
                if isinstance(case, dict):
                    targets.extend(_targets(case.get("next")))
        targets.extend(_targets(config.get("default_next", step.get("default_next"))))

        nodes.append({
            "id": str(step.get("id") or ""),
            "name": str(step.get("name") or step.get("id") or ""),
            "type": str(step.get("type") or ""),
            "order": order,
            "targets": list(dict.fromkeys(targets)),
            "chat_projection": workflow_chat_projection_visibility(step),
        })

    return {
        "workflow_id": str(getattr(workflow, "id", "")),
        "name": str(
            getattr(workflow, "name", None)
            or getattr(workflow, "id", "")
        ),
        "version": int(getattr(workflow, "version", 1) or 1),
        "fingerprint": fingerprint or workflow_definition_fingerprint(workflow),
        "nodes": nodes,
    }


def workflow_definition_changed(workflow: Any, run: WorkflowRun) -> bool:
    """Return whether a run's immutable graph differs from the live definition."""
    snapshot = (
        run.definition_snapshot
        if isinstance(run.definition_snapshot, dict)
        else {}
    )
    snapshot_fingerprint = str(snapshot.get("fingerprint") or "")
    return bool(
        snapshot_fingerprint
        and snapshot_fingerprint != workflow_definition_fingerprint(workflow)
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(child_key): _json_safe(child)
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(child) for child in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def redact_sensitive_value(value: Any, key: str = "") -> Any:
    """Apply the shared key and free-text sanitizer with the trace marker."""
    wrapped = _json_safe({key: value} if key else value)
    sanitized = sanitize_sensitive_payload(
        wrapped,
        replacement="[REDACTED]",
    )
    if key and isinstance(sanitized, Mapping):
        sanitized = sanitized.get(key)
    return _json_safe(sanitized)


def _serialized(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def summarize_trace_value(value: Any) -> Any:
    """Redact a value and cap its serialized representation at 8 KiB."""
    redacted = redact_sensitive_value(value)
    encoded = _serialized(redacted)
    if len(encoded) <= TRACE_SUMMARY_BYTES:
        return redacted

    source = encoded.decode("utf-8")
    low, high = 0, len(source)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {"truncated": True, "preview": source[:middle]}
        if len(_serialized(candidate)) <= TRACE_SUMMARY_BYTES:
            low = middle
        else:
            high = middle - 1
    return {"truncated": True, "preview": source[:low]}


def summarize_trace_text(value: Any, *, fallback: str = "Workflow failed") -> str:
    """Return one redacted, JSON-safe, 8 KiB-bounded text value."""
    summary = summarize_trace_value(
        fallback if value is None or value == "" else value
    )
    if isinstance(summary, str):
        return summary
    return _serialized(summary).decode("utf-8")


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _compact_artifact_ref(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        path = redact_sensitive_value(value)
        return {"path": path} if path else {}
    if not isinstance(value, Mapping):
        return {}

    compact: dict[str, Any] = {}
    artifact_id = value.get("id") or value.get("artifact_id")
    if artifact_id not in (None, ""):
        compact["id"] = redact_sensitive_value(artifact_id)
    for field in _ARTIFACT_FIELDS[1:]:
        item = value.get(field)
        if item in (None, "") or isinstance(item, (Mapping, list, tuple, set)):
            continue
        compact[field] = redact_sensitive_value(item)
    return compact


def _append_bounded_artifact(items: list[dict[str, Any]], value: Any) -> None:
    if len(items) >= MAX_ARTIFACT_REFS:
        return
    compact = _compact_artifact_ref(value)
    if not compact:
        return

    fitted: dict[str, Any] = {}
    for field in _ARTIFACT_FIELDS:
        if field not in compact:
            continue
        candidate_ref = {**fitted, field: compact[field]}
        if len(_serialized([*items, candidate_ref])) <= TRACE_SUMMARY_BYTES:
            fitted = candidate_ref
    if not fitted or fitted in items:
        return
    items.append(fitted)


def _append_bounded_child_id(items: list[str], value: Any) -> None:
    if len(items) >= MAX_CHILD_RUN_IDS or value in (None, ""):
        return
    if isinstance(value, (Mapping, list, tuple, set)):
        return
    child_run_id = str(redact_sensitive_value(value))
    if child_run_id in items:
        return
    if len(_serialized([*items, child_run_id])) <= TRACE_SUMMARY_BYTES:
        items.append(child_run_id)


def _discovered_refs(result: dict[str, Any]) -> tuple[list[Any], list[str]]:
    artifacts: list[Any] = []
    child_run_ids: list[str] = []
    seen_containers: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key).lower()
                if key in _ARTIFACT_KEYS:
                    for artifact in _values(child):
                        _append_bounded_artifact(artifacts, artifact)
                if key in _CHILD_RUN_ID_KEYS:
                    for item in _values(child):
                        _append_bounded_child_id(child_run_ids, item)
                elif key in _CHILD_RUN_IDS_KEYS:
                    for item in _values(child):
                        _append_bounded_child_id(child_run_ids, item)
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(result)

    return artifacts, child_run_ids


def _timestamp(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value in (None, ""):
        return None
    return str(value)


def update_workflow_history_summary(run: WorkflowRun) -> None:
    """Refresh the redacted, bounded summary used by Workflow History."""
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    visible_node_ids = [
        str(node.get("id") or "")
        for node in nodes
        if isinstance(node, dict)
        and str(node.get("type") or "").lower() != "end"
        and str(node.get("chat_projection") or "progress").lower() != "hidden"
        and node.get("id")
    ]
    step_results = run.step_results if isinstance(run.step_results, dict) else {}
    processed_count = 0
    for node_id in visible_node_ids:
        result = step_results.get(node_id)
        if not isinstance(result, dict) or result.get("skipped"):
            continue
        if str(result.get("status") or "").lower() in {
            "completed", "failed", "cancelled"
        }:
            processed_count += 1

    artifact_identities: set[bytes] = set()
    for entry in run.execution_trace or []:
        if not isinstance(entry, dict):
            continue
        for ref in entry.get("artifact_refs") or []:
            artifact_identities.add(_serialized(ref))
    for result in step_results.values():
        if not isinstance(result, dict):
            continue
        artifacts, _child_run_ids = _discovered_refs(result)
        for ref in artifacts:
            artifact_identities.add(_serialized(ref))

    variables = run.variables if isinstance(run.variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    retry_state = state.get("retry_state") if isinstance(state.get("retry_state"), dict) else {}
    blocker = (
        retry_state.get("observed_problem")
        or retry_state.get("required_change")
        or run.error
    )
    summary = {
        "business_outcome": str(state.get("business_outcome") or ""),
        "processed_count": processed_count,
        "total_count": len(visible_node_ids),
        "artifact_count": len(artifact_identities),
        "blocker": summarize_trace_value(blocker) if blocker not in (None, "") else None,
    }
    trigger_data = dict(run.trigger_data or {})
    trigger_data[_HISTORY_SUMMARY_KEY] = summary
    run.trigger_data = trigger_data


def append_execution_trace(
    run: WorkflowRun,
    *,
    node: dict[str, Any],
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Append one bounded, ordered node transition to a run."""
    current = list(run.execution_trace or [])
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    limit = max(2000, len(nodes) * 8)
    if len(current) >= limit:
        update_workflow_history_summary(run)
        return

    payload = result if isinstance(result, dict) else {}
    artifacts, child_run_ids = _discovered_refs(payload)
    attempt_number = run.effective_attempt_number
    node_id = str(node.get("id") or "")
    normalized_status = str(status or "unknown")
    now = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "sequence": len(current) + 1,
        "attempt_number": attempt_number,
        "node_id": node_id,
        "node_name": str(node.get("name") or node_id),
        "node_type": str(node.get("type") or ""),
        "status": normalized_status,
        "input_summary": summarize_trace_value(payload.get("inputs")),
        "output_summary": summarize_trace_value(payload.get("output")),
        "error": summarize_trace_value(payload.get("error")),
        "artifact_refs": artifacts,
        "child_run_ids": child_run_ids,
    }

    started_at = _timestamp(payload.get("started_at"))
    completed_at = _timestamp(payload.get("completed_at"))
    if normalized_status == "running":
        entry["started_at"] = started_at or now
    else:
        if not started_at:
            started_at = next((
                str(prior.get("started_at"))
                for prior in reversed(current)
                if prior.get("node_id") == node_id
                and prior.get("attempt_number") == attempt_number
                and prior.get("status") == "running"
                and prior.get("started_at")
            ), None)
        if started_at:
            entry["started_at"] = started_at
        entry["completed_at"] = completed_at or now
    if payload.get("duration_ms") is not None:
        entry["duration_ms"] = payload["duration_ms"]

    current.append(entry)
    run.execution_trace = current
    update_workflow_history_summary(run)
