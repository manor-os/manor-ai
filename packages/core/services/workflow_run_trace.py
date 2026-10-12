"""Compact, display-safe snapshots and transition traces for Workflow runs."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
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
_HISTORY_SUMMARY_SCHEMA_VERSION = 2
_PROCESSED_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
_KNOWN_STEP_STATUSES = _PROCESSED_STEP_STATUSES | {"pending", "queued", "running", "paused"}
WORKFLOW_IMPORT_SOURCE_TAG_PREFIX = "imported:"
WORKFLOW_IMPORT_PROOF_TAG_PREFIX = "manor-import-proof:"


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


def workflow_import_provenance_tags(workflow: Any, source: str) -> list[str]:
    """Return server-owned tags binding an importer source to this graph."""

    normalized_source = str(source or "").strip().lower()
    if not normalized_source:
        return []
    fingerprint = workflow_definition_fingerprint(workflow)
    return [
        f"{WORKFLOW_IMPORT_SOURCE_TAG_PREFIX}{normalized_source}",
        f"{WORKFLOW_IMPORT_PROOF_TAG_PREFIX}{normalized_source}:{fingerprint}",
    ]


def visible_workflow_tags(tags: Any) -> list[str]:
    """Hide server-owned provenance proofs from API and portable exports."""

    return [
        str(tag)
        for tag in (tags if isinstance(tags, list) else [])
        if not str(tag).strip().lower().startswith(
            WORKFLOW_IMPORT_PROOF_TAG_PREFIX
        )
    ]


def trusted_workflow_import_source(workflow: Any) -> str | None:
    """Return importer source only when its server proof matches this graph."""

    tags = {
        str(tag).strip().lower()
        for tag in (getattr(workflow, "tags", None) or [])
        if str(tag).strip()
    }
    fingerprint = workflow_definition_fingerprint(workflow)
    trusted_sources: list[str] = []
    for tag in sorted(tags):
        if not tag.startswith(WORKFLOW_IMPORT_SOURCE_TAG_PREFIX):
            continue
        source = tag.removeprefix(WORKFLOW_IMPORT_SOURCE_TAG_PREFIX)
        if not source:
            continue
        proof = f"{WORKFLOW_IMPORT_PROOF_TAG_PREFIX}{source}:{fingerprint}"
        if proof in tags:
            trusted_sources.append(source)
    return trusted_sources[0] if len(trusted_sources) == 1 else None


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


def build_execution_snapshot(
    workflow: Any,
    *,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Retain the private, immutable execution definition for one attempt."""
    import_source = trusted_workflow_import_source(workflow)
    return {
        "workflow_id": str(getattr(workflow, "id", "")),
        "version": int(getattr(workflow, "version", 1) or 1),
        "fingerprint": fingerprint or workflow_definition_fingerprint(workflow),
        "steps": deepcopy(getattr(workflow, "steps", None) or []),
        "variables": deepcopy(getattr(workflow, "variables", None) or {}),
        **({"import_source": import_source} if import_source else {}),
    }


def execution_snapshot_for_run(workflow: Any | None, run: WorkflowRun) -> dict[str, Any] | None:
    """Return a valid private execution snapshot, or ``None`` for legacy runs."""
    snapshot = getattr(run, "execution_snapshot", None)
    if not isinstance(snapshot, dict):
        return None
    expected_workflow_id = str(
        getattr(workflow, "id", None)
        or getattr(run, "workflow_id", "")
    )
    if str(snapshot.get("workflow_id") or "") != expected_workflow_id:
        return None
    steps = snapshot.get("steps")
    variables = snapshot.get("variables")
    version = snapshot.get("version")
    fingerprint = str(snapshot.get("fingerprint") or "")
    if (
        not isinstance(steps, list)
        or not all(isinstance(step, dict) for step in steps)
        or not isinstance(variables, dict)
        or not isinstance(version, int)
        or not fingerprint
    ):
        return None
    frozen_definition = SimpleNamespace(
        id=snapshot["workflow_id"],
        version=version,
        steps=steps,
    )
    if fingerprint != workflow_definition_fingerprint(frozen_definition):
        return None
    return snapshot


def workflow_execution_view(workflow: Any | None, run: WorkflowRun) -> Any | None:
    """Return the immutable definition used to execute this run when available."""
    snapshot = execution_snapshot_for_run(workflow, run)
    if snapshot is None:
        return workflow
    definition_snapshot = (
        run.definition_snapshot
        if isinstance(run.definition_snapshot, dict)
        else {}
    )
    return SimpleNamespace(
        id=getattr(workflow, "id", None) or snapshot["workflow_id"],
        entity_id=getattr(workflow, "entity_id", None) or run.entity_id,
        name=(
            getattr(workflow, "name", None)
            or definition_snapshot.get("name")
            or snapshot["workflow_id"]
        ),
        version=snapshot["version"],
        steps=deepcopy(snapshot["steps"]),
        variables=deepcopy(snapshot["variables"]),
    )


def workflow_definition_changed(workflow: Any, run: WorkflowRun) -> bool:
    """Return drift only for an unsnapshotted legacy run."""
    if execution_snapshot_for_run(workflow, run) is not None:
        return False
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


def _loaded_run_value(run: Any, key: str, default: Any) -> Any:
    """Read an already-loaded Run field without triggering an ORM lazy load."""
    state = getattr(run, "__dict__", None)
    if isinstance(state, dict) and key in state:
        return state[key]
    return default


def _step_result_status(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("skipped") is True:
        return "skipped"
    status = str(result.get("status") or "").strip().lower()
    if status in _KNOWN_STEP_STATUSES:
        return status
    if result.get("error") not in (None, ""):
        return "failed"
    return "completed"


def workflow_execution_steps(
    run: Any,
    *,
    activity_status: str | None = None,
    existing_steps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return reached nodes first, followed by visible nodes not yet reached.

    The immutable definition snapshot remains the complete graph. This compact
    projection uses trace sequence for execution order, folds node transitions
    to their latest state, falls back to result receipts when trace detail is
    intentionally deferred, and appends the remaining snapshot nodes in their
    frozen definition order.
    """
    snapshot = _loaded_run_value(run, "definition_snapshot", {})
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    raw_nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    if not raw_nodes and isinstance(existing_steps, list):
        raw_nodes = existing_steps

    nodes: list[dict[str, Any]] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict) or workflow_chat_projection_visibility(node) == "hidden":
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        nodes.append({**node, "id": node_id, "_fallback_order": index})
    node_by_id = {node["id"]: node for node in nodes}
    has_node_contract = bool(raw_nodes)

    raw_trace = _loaded_run_value(run, "execution_trace", [])
    trace = raw_trace if isinstance(raw_trace, list) else []
    ordered_trace = sorted(
        (
            (entry, index)
            for index, entry in enumerate(trace)
            if isinstance(entry, dict)
        ),
        key=lambda item: (
            item[0].get("sequence")
            if isinstance(item[0].get("sequence"), (int, float))
            else item[1] + 1,
            item[1],
        ),
    )
    trace_order: list[str] = []
    latest_trace_by_id: dict[str, dict[str, Any]] = {}
    trace_child_ids: dict[str, list[str]] = {}
    for entry, _index in ordered_trace:
        node_id = str(entry.get("node_id") or "").strip()
        if not node_id or (has_node_contract and node_id not in node_by_id):
            continue
        if node_id not in latest_trace_by_id:
            trace_order.append(node_id)
        latest_trace_by_id[node_id] = entry
        children = trace_child_ids.setdefault(node_id, [])
        for child_id in entry.get("child_run_ids") or []:
            _append_bounded_child_id(children, child_id)

    raw_results = _loaded_run_value(run, "step_results", {})
    results = raw_results if isinstance(raw_results, dict) else {}
    result_ids = [
        str(node_id)
        for node_id, result in results.items()
        if isinstance(result, dict)
        and str(node_id).strip()
        and (not has_node_contract or str(node_id) in node_by_id)
    ]
    existing_order = [
        str(step.get("id") or "")
        for step in (existing_steps or [])
        if isinstance(step, dict)
        and str(step.get("id") or "") in result_ids
    ]
    missing_result_ids = [node_id for node_id in result_ids if node_id not in latest_trace_by_id]
    if not trace_order and existing_order:
        ordered_ids = [
            *existing_order,
            *(node_id for node_id in missing_result_ids if node_id not in existing_order),
        ]
    elif str(_loaded_run_value(run, "retry_of_run_id", "") or "").strip():
        snapshot_order = {
            node["id"]: (
                node.get("order")
                if isinstance(node.get("order"), (int, float))
                else node["_fallback_order"]
            )
            for node in nodes
        }
        missing_result_ids.sort(key=lambda node_id: snapshot_order.get(node_id, len(nodes)))
        ordered_ids = [*missing_result_ids, *trace_order]
    elif not trace_order and nodes:
        result_id_set = set(missing_result_ids)
        ordered_ids = [node["id"] for node in nodes if node["id"] in result_id_set]
    else:
        ordered_ids = [*trace_order, *missing_result_ids]

    run_status = str(_loaded_run_value(run, "status", "") or "").strip().lower()
    current_id = str(_loaded_run_value(run, "current_step_id", "") or "").strip()
    if (
        current_id
        and current_id not in ordered_ids
        and run_status != "completed"
        and (not has_node_contract or current_id in node_by_id)
    ):
        ordered_ids.append(current_id)
    ordered_id_set = set(ordered_ids)
    ordered_ids.extend(
        node["id"] for node in nodes if node["id"] not in ordered_id_set
    )

    existing_by_id = {
        str(step.get("id") or ""): step
        for step in (existing_steps or [])
        if isinstance(step, dict) and step.get("id")
    }
    active_status = str(activity_status or run_status or "running").strip().lower()
    projected: list[dict[str, Any]] = []
    for node_id in ordered_ids:
        node = node_by_id.get(node_id, {})
        trace_entry = latest_trace_by_id.get(node_id, {})
        result = results.get(node_id)
        status = str(trace_entry.get("status") or "").strip().lower()
        result_status = _step_result_status(result)
        if result_status:
            status = result_status
        if node_id == current_id and result_status is None:
            if active_status in _KNOWN_STEP_STATUSES:
                status = active_status
            elif run_status in _KNOWN_STEP_STATUSES:
                status = run_status
        if status not in _KNOWN_STEP_STATUSES:
            status = "pending"

        row = {
            "id": node_id,
            "name": str(
                node.get("name")
                or trace_entry.get("node_name")
                or existing_by_id.get(node_id, {}).get("name")
                or node_id
            ),
            "type": str(
                node.get("type")
                or trace_entry.get("node_type")
                or existing_by_id.get(node_id, {}).get("type")
                or ""
            ),
            "status": status,
        }
        child_run_ids = list(trace_child_ids.get(node_id, []))
        if isinstance(result, dict):
            _artifacts, result_child_ids = _discovered_refs(result)
            for child_id in result_child_ids:
                _append_bounded_child_id(child_run_ids, child_id)
        if child_run_ids:
            row["child_run_ids"] = child_run_ids
        existing = existing_by_id.get(node_id)
        if isinstance(existing, dict) and existing.get("subscription_id") is not None:
            row["subscription_id"] = existing["subscription_id"]
        projected.append(row)
    return projected


def workflow_execution_progress(steps: list[dict[str, Any]]) -> dict[str, int]:
    """Derive processed and total counts from one complete visible projection."""
    return {
        "processed_count": sum(
            1
            for step in steps
            if str(step.get("status") or "").lower() in _PROCESSED_STEP_STATUSES
        ),
        "total_count": len(steps),
    }


def update_workflow_history_summary(run: WorkflowRun) -> None:
    """Refresh the redacted, bounded summary used by Workflow History."""
    workflow_steps = workflow_execution_steps(run)
    progress = workflow_execution_progress(workflow_steps)
    step_results = run.step_results if isinstance(run.step_results, dict) else {}

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
        "schema_version": _HISTORY_SUMMARY_SCHEMA_VERSION,
        "business_outcome": str(state.get("business_outcome") or ""),
        **progress,
        "workflow_steps": workflow_steps,
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
