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
from packages.core.services.viewer_language import run_display_translations, without_i18n


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
# v3 adds the person-facing outcome: stages, the current stage, delivered
# files and publication receipts, so surfaces never re-derive them from the
# full run JSON.
HISTORY_SUMMARY_SCHEMA_VERSION = 4
_HISTORY_SUMMARY_SCHEMA_VERSION = HISTORY_SUMMARY_SCHEMA_VERSION
_PROCESSED_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
_KNOWN_STEP_STATUSES = _PROCESSED_STEP_STATUSES | {"pending", "queued", "running", "paused"}
# Routing and data-shaping nodes move the run along but are not work a person
# would recognise as a stage.
_CONTROL_STEP_TYPES = frozenset({
    "trigger", "webhook", "end", "condition", "switch", "merge", "stop", "split", "parallel", "loop",
    "filter", "limit", "sort", "dedupe", "aggregate", "transform", "note", "datetime",
    "workflow_action_grant", "unsupported",
})
_SUMMARY_LIST_LIMIT = 12
_SUMMARY_TEXT_LIMIT = 200
# Outcomes a run's own final result may declare when no Workflow Project state
# tracks it: the run finished, but the work still needs the person.
RESULT_DECLARED_OUTCOMES = frozenset({"needs_input", "revision_required"})


def workflow_result_outcome(variables: Any) -> str:
    """The business outcome a finished run's final result declares, if any."""
    result = variables.get("__result") if isinstance(variables, dict) else None
    status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else ""
    return status if status in RESULT_DECLARED_OUTCOMES else ""


# The run's own Flow cannot advance: no step can start and none is waiting.
STALLED_ERROR_PREFIX = "No runnable steps and workflow not complete"
STALLED_RETRY_ERROR = (
    "This run stopped because its version of the Flow cannot continue past this step, and the Flow "
    "has not changed since. Retrying would stop at the same place."
)
STALLED_PROBLEM = "This version of the Flow cannot continue past this step."
# What Retry does for such a run once the Flow has changed: start again on it.
STALLED_RESTART_CHANGE = (
    "Retry starts again from the beginning on the Flow's current version, with the same inputs."
)
STALLED_RESTARTED_KEY = "_restarted_as_run_id"


def workflow_run_stalled(run: Any) -> bool:
    """A failure that retrying the same frozen Flow repeats exactly, whatever is changed."""
    return (
        str(getattr(run, "status", "") or "") == "failed"
        and str(getattr(run, "error", "") or "").startswith(STALLED_ERROR_PREFIX)
    )


def workflow_run_restarted_as(run: Any) -> str:
    """The run that started this stalled run's inputs again on the current Flow, if any."""
    trigger = getattr(run, "trigger_data", None)
    return str(trigger.get(STALLED_RESTARTED_KEY) or "") if isinstance(trigger, dict) else ""


WORKFLOW_IMPORT_SOURCE_TAG_PREFIX = "imported:"
WORKFLOW_IMPORT_PROOF_TAG_PREFIX = "manor-import-proof:"


def workflow_definition_fingerprint(workflow: Any) -> str:
    """Return the graph identity required for checkpoint-compatible retries.

    Translations of display text are not part of the graph: adding one never
    makes a paused run incompatible.
    """
    payload = {
        "id": getattr(workflow, "id", ""),
        "version": int(getattr(workflow, "version", 1) or 1),
        "steps": without_i18n(getattr(workflow, "steps", None) or []),
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

        node = {
            "id": str(step.get("id") or ""),
            "name": str(step.get("name") or step.get("id") or ""),
            "type": str(step.get("type") or ""),
            "order": order,
            "targets": list(dict.fromkeys(targets)),
            "chat_projection": workflow_chat_projection_visibility(step),
        }
        # The step's name in other languages, for each viewer's progress.
        names = {
            code: {"name": entry["node_names"][node["id"]]}
            for code, entry in run_display_translations([step]).items()
        }
        if names:
            node["i18n"] = names
        nodes.append(node)

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


#: Pauses that continue on their own: an automatic retry after a temporary
#: problem, a rendering media job, a timer (and a child flow, by its subrun).
BACKGROUND_WAIT_TYPES = frozenset({"transient_retry", "media_job", "timer"})


def step_waits_in_background(result: Any) -> bool:
    """Whether a paused step is still working, so nobody needs to act on it."""
    if not isinstance(result, dict) or str(result.get("status") or "").strip().lower() != "paused":
        return False
    wait_type = str(result.get("wait_type") or "").strip().lower()
    return wait_type in BACKGROUND_WAIT_TYPES or (not wait_type and bool(result.get("subrun_id")))


def _step_result_status(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("skipped") is True:
        return "skipped"
    if step_waits_in_background(result):
        return "running"  # Still working; only a wait for a person shows as paused.
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
        # A running run's current step with only a failed result is being run
        # again (a retry from it); its last attempt's failure is not its state.
        retried = result_status == "failed" and run_status in {"pending", "running"}
        if node_id == current_id and (result_status is None or retried):
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
        # A check runs to completion whichever way it decides; its answer is what the person needs.
        if isinstance(result, dict) and isinstance(result.get("condition_result"), bool):
            row["answer"] = "yes" if result["condition_result"] else "no"
        elif status == "completed" and isinstance(result, dict) and isinstance(result.get("output"), dict):
            # A step can finish running yet report it could not do its job, or
            # that it needs the person: it reads as that, as the run does.
            from packages.core.contracts.envelope import StepResultStatus, normalize_step_result_status

            declared = str(result["output"].get("status") or "").strip().lower()
            if declared in RESULT_DECLARED_OUTCOMES:
                row["outcome"] = declared
            elif normalize_step_result_status(declared) is StepResultStatus.FAILED:
                row["outcome"] = "blocked"
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
    """Count the stages a run does: the steps that do work, not routing or data shaping.

    The total is the stage count of the run's graph, so it stays the same while the
    run moves; a stage that finishes, fails or is cancelled counts as processed.
    """
    stages = [
        step for step in steps
        if str(step.get("type") or "").lower() not in _CONTROL_STEP_TYPES
    ]
    return {
        "processed_count": sum(
            1
            for step in stages
            if str(step.get("status") or "").lower() in _PROCESSED_STEP_STATUSES
        ),
        "total_count": len(stages),
    }


def workflow_longest_path_stages(nodes: list[dict[str, Any]]) -> int:
    """Stages on the longest route from the Flow's start to its last step.

    Loop-back edges are cut first, so a loop body counts once and the route is
    a finite path through the forward edges of the graph.
    """
    by_id = {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }

    def is_stage(node_id: str) -> bool:
        return str(by_id[node_id].get("type") or "").lower() not in _CONTROL_STEP_TYPES

    def children(node_id: str) -> list[str]:
        return [str(target) for target in by_id[node_id].get("targets") or [] if str(target) in by_id]

    starts = [
        node_id for node_id in by_id
        if str(by_id[node_id].get("type") or "").lower() == "trigger"
    ] or [
        node_id for node_id in by_id
        if not any(node_id in children(other) for other in by_id)
    ]
    state: dict[str, str] = {}
    longest: dict[str, int] = {}
    loop_edges: set[tuple[str, str]] = set()
    # Iterative depth-first walk: an edge into a node still on the walk closes a loop.
    for start in starts:
        if start in state:
            continue
        state[start] = "open"
        walk = [(start, iter(children(start)))]
        while walk:
            node_id, pending = walk[-1]
            child = next(pending, None)
            if child is None:
                walk.pop()
                state[node_id] = "done"
                longest[node_id] = (1 if is_stage(node_id) else 0) + max(
                    (
                        longest[target]
                        for target in children(node_id)
                        if (node_id, target) not in loop_edges and state.get(target) == "done"
                    ),
                    default=0,
                )
            elif child not in state:
                state[child] = "open"
                walk.append((child, iter(children(child))))
            elif state[child] == "open":
                loop_edges.add((node_id, child))
    return max((longest[start] for start in starts if start in longest), default=0)


#: Runs created before the longest-route total existed keep the whole-Flow stage
#: count they were shown with. The total applies to runs created from this point on.
LONGEST_ROUTE_TOTAL_SINCE = datetime(2026, 10, 29, 16, 39, tzinfo=timezone.utc)


def workflow_run_stage_counts(run: Any, workflow_steps: list[dict[str, Any]]) -> dict[str, int]:
    """A run's stages done, against the longest route through its Flow.

    The route is fixed by the run's definition snapshot, so the total does not
    move as the run takes one branch or another. Runs created earlier keep the
    whole-Flow stage count, so their history figures do not change.
    """
    progress = workflow_execution_progress(workflow_steps)
    created_at = getattr(run, "created_at", None)
    if not isinstance(created_at, datetime) or created_at < LONGEST_ROUTE_TOTAL_SINCE:
        return progress
    snapshot = _loaded_run_value(run, "definition_snapshot", {})
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
    return {
        "processed_count": progress["processed_count"],
        "total_count": workflow_longest_path_stages(nodes if isinstance(nodes, list) else []),
    }


def _short_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:_SUMMARY_TEXT_LIMIT] if text else None


def workflow_run_stages(workflow_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The visible steps a person would call stages, without control flow."""
    return [
        {"id": step["id"], "name": step["name"], "type": step.get("type") or "", "status": step["status"]}
        for step in workflow_steps
        if str(step.get("type") or "").lower() not in _CONTROL_STEP_TYPES
    ]


def workflow_run_current_stage(run_status: str, stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Where an unfinished run is now, and whether it is waiting on a person."""
    if run_status in {"completed", "cancelled", "skipped"}:
        return None
    for index, stage in enumerate(stages):
        if stage["status"] in {"running", "paused", "queued", "failed"}:
            waiting = stage["status"] == "paused" or (stage["type"] == "wait" and stage["status"] != "failed")
            return {"id": stage["id"], "name": stage["name"], "index": index, "status": stage["status"], "waiting": waiting}
    if run_status == "paused":
        return {"id": None, "name": None, "index": None, "status": "paused", "waiting": True}
    return None


def workflow_run_deliverables(variables: Any, *, entity_id: str = "") -> list[dict[str, Any]]:
    """The files the run's final result delivered, as bounded references.

    Found the way Chat finds them (``workflow_result_files``), so a result that
    nests its files (a delivery package, a project) lists them in the App too.
    """
    from packages.core.services.workflow_chat_projection import workflow_result_files

    result = variables.get("__result") if isinstance(variables, Mapping) else None
    delivered: list[dict[str, Any]] = []
    declared = result.get("files") if isinstance(result, Mapping) else None
    # Files the result lists itself keep their role and group.
    for ref in declared if isinstance(declared, list) else []:
        if not isinstance(ref, Mapping) or not str(ref.get("document_id") or "").strip():
            continue
        item = {"document_id": str(ref["document_id"]).strip()[:64]}
        for key in ("name", "role", "group", "mime_type", "file_type"):
            text = _short_text(ref.get(key))
            if text:
                item[key] = text
        delivered.append(item)
        if len(delivered) >= _SUMMARY_LIST_LIMIT * 2:
            return delivered
    if delivered:
        return delivered
    for ref in workflow_result_files(result, entity_id=entity_id) if result is not None else []:
        document_id = str(ref.get("document_id") or "").strip()[:64]
        fs_path = str(ref.get("fsPath") or "").strip()[:512]
        if not document_id and not fs_path:
            continue
        item: dict[str, Any] = {"document_id": document_id} if document_id else {"fs_path": fs_path}
        for key, source in (("name", "name"), ("file_type", "fileType")):
            text = _short_text(ref.get(source))
            if text:
                item[key] = text
        delivered.append(item)
        if len(delivered) >= _SUMMARY_LIST_LIMIT * 2:
            break
    return delivered


def workflow_run_result_headline(variables: Any) -> dict[str, str | None]:
    """What the final result is called and says, for a result card."""
    from packages.core.services.workflow_chat_projection import workflow_result_headline

    result = variables.get("__result") if isinstance(variables, Mapping) else None
    title, summary = workflow_result_headline(result) if isinstance(result, (Mapping, str)) else ("", "")
    if isinstance(result, str):
        # A plain-text result is the whole output, not a summary line for a card.
        summary = ""
    return {"result_title": _short_text(title), "result_summary": _short_text(summary)}


def workflow_run_publications(step_results: Any) -> list[dict[str, Any]]:
    """Where the run published, from its persisted publication receipts."""
    from packages.core.services.workflow_publication_receipts import publication_receipts_from_step_results

    publications = []
    for receipt in publication_receipts_from_step_results(step_results)[:_SUMMARY_LIST_LIMIT]:
        publications.append({
            key: _short_text(receipt.get(key))
            for key in ("platform", "title", "published_url", "verification_status", "published_at", "account")
            if _short_text(receipt.get(key))
        })
    return publications


def update_workflow_history_summary(run: WorkflowRun) -> None:
    """Refresh the redacted, bounded summary used by Workflow History and the App."""
    workflow_steps = workflow_execution_steps(run)
    progress = workflow_run_stage_counts(run, workflow_steps)
    stages = workflow_run_stages(workflow_steps)
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
    state = {**state, "business_outcome": state.get("business_outcome") or workflow_result_outcome(variables)}
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
        "stages": stages,
        "current_stage": workflow_run_current_stage(str(run.status or "").lower(), stages),
        "deliverables": workflow_run_deliverables(variables, entity_id=str(run.entity_id or "")),
        "publications": workflow_run_publications(step_results),
        # What the result is called and says.
        **workflow_run_result_headline(variables),
    }
    trigger_data = dict(run.trigger_data or {})
    trigger_data[_HISTORY_SUMMARY_KEY] = summary
    run.trigger_data = trigger_data


def execution_trace_capacity(run: WorkflowRun) -> int:
    """Entries a run's trace keeps before further transitions are dropped."""

    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    return max(2000, len(nodes) * 8)
# Fields whose change can move a run's stage, result or publications.
_SUMMARY_SOURCE_FIELDS = ("status", "current_step_id", "variables", "step_results", "execution_trace", "error")


def refresh_history_summaries_before_flush(session: Any) -> None:
    """Keep every changed run's summary current, whichever code path changed it.

    Dozens of paths set a terminal status or final result directly; refreshing
    before each flush means none of them can leave the App, History or Chat
    projection stale. Explicit refreshes elsewhere stay valid: the summary is
    a pure derivation. Registered by ``packages.core.models.workflow`` so it
    is active wherever runs can be saved.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import InvalidRequestError

    # Reading an unloaded column must not start a nested flush.
    with session.no_autoflush:
        for obj in list(session.dirty):
            if not isinstance(obj, WorkflowRun):
                continue
            state = sa_inspect(obj)
            if not any(state.attrs[field].history.has_changes() for field in _SUMMARY_SOURCE_FIELDS):
                continue
            try:
                update_workflow_history_summary(obj)
            except InvalidRequestError as exc:
                # A listing loaded this row with load_only + raiseload; its
                # writer refreshes the summary explicitly where it matters.
                if "lazy='raise'" not in str(exc):
                    raise


def append_execution_trace(
    run: WorkflowRun,
    *,
    node: dict[str, Any],
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Append one bounded, ordered node transition to a run."""
    current = list(run.execution_trace or [])
    if len(current) >= execution_trace_capacity(run):
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
