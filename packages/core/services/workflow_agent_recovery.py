"""Evidence carried between Agent attempts, separate from execution snapshots."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from copy import deepcopy

from packages.core.services.sensitive_data import sanitize_sensitive_payload


async def workflow_released_project_reviews(db: Any, run: Any, *, lock: bool = False) -> dict[str, dict]:
    """Offer a conflict-free rebase of a released, fenced project checkpoint.

    A completed maintenance run may add recovery metadata and release a lease.
    It cannot change any value the failed run observed. The current state must
    itself be proved by that maintenance run's native project output.
    """
    from sqlalchemy import select
    from packages.core.models.workflow import WorkflowProject, WorkflowRun
    from packages.core.services.workflow_project_service import _state_value, _set_state_value

    def projections(owner: Any):
        steps = {step["id"]: step for step in (owner.execution_snapshot or {}).get("steps", [])}
        for sid, result in (owner.step_results or {}).items():
            if (steps.get(sid, {}).get("type") != "workflow_project"
                    or not isinstance(result, dict) or result.get("status") != "completed"):
                continue
            output = result.get("output")
            if isinstance(output, dict):
                yield steps[sid].get("config") or {}, output.get("project", output)

    prior_outputs = list(projections(run))
    completed = {(output.get("project_id"), config.get("lease_path") or "lease")
                 for config, output in prior_outputs if isinstance(output, dict)
                 and config.get("operation") == "complete_lease"}
    reviews = {}
    for config, receipt in prior_outputs:
        if config.get("operation") != "lease_due" or not isinstance(receipt, dict):
            continue
        pid, lease_path = receipt.get("project_id"), config.get("lease_path") or "lease"
        if not pid or (pid, lease_path) in completed:
            continue
        expected = _state_value(receipt.get("state") or {}, lease_path)
        if not isinstance(expected, dict) or expected.get("run_id") != run.id:
            continue
        query = select(WorkflowProject).where(WorkflowProject.id == pid,
            WorkflowProject.entity_id == run.entity_id, WorkflowProject.workspace_id == run.workspace_id)
        if lock:
            query = query.with_for_update()
        project = await db.scalar(query.execution_options(populate_existing=True))
        if (project is None or project.current_stage != "verification_required"
                or _state_value(project.state or {}, lease_path) != {}):
            continue
        checkpoints = [output for _, output in prior_outputs if isinstance(output, dict)
            and output.get("project_id") == pid and output.get("last_run_id") == run.id
            and output.get("project_type") == project.project_type
            and output.get("schema_version") == project.schema_version
            and output.get("current_stage") == "leased"
            and isinstance(output.get("revision"), int)
            and _state_value(output.get("state") or {}, lease_path) == expected]
        if not checkpoints:
            continue
        checkpoint = max(checkpoints, key=lambda item: item["revision"])
        old_state = deepcopy(checkpoint["state"])
        _set_state_value(old_state, lease_path, {})
        if (project.revision <= checkpoint["revision"]
                or any(key not in project.state or project.state[key] != value for key, value in old_state.items())):
            continue
        owner = await db.get(WorkflowRun, project.last_run_id) if project.last_run_id else None
        if (owner is None or owner.status != "completed" or owner.entity_id != run.entity_id
                or owner.workspace_id != run.workspace_id):
            continue
        if not any(isinstance(output, dict) and output.get("project_id") == pid
                   and output.get("revision") == project.revision and output.get("state") == project.state
                   and output.get("last_run_id") == owner.id and output.get("current_stage") == project.current_stage
                   and output.get("project_type") == project.project_type
                   and output.get("schema_version") == project.schema_version
                   for _, output in projections(owner)):
            continue
        reviews[pid] = {"revision": project.revision, "checkpoint_revision": checkpoint["revision"],
                       "lease_path": lease_path}
    return reviews


async def workflow_effect_review_blocker(db: Any, run: Any, *, applied: bool = False) -> str | None:
    """Use durable operation evidence for both the review offer and submission."""
    return None


def workflow_effect_result_schema(run: Any) -> dict[str, Any] | None:
    """Only an explicit, frozen output contract can accept an operator result."""
    step_id = str(getattr(run, "current_step_id", "") or "")
    snapshot = getattr(run, "execution_snapshot", None) or {}
    step = next((item for item in snapshot.get("steps", []) if item.get("id") == step_id), {})
    schema = (step.get("config") or {}).get("output_schema")
    if (not isinstance(schema, dict) or schema.get("type") != "object"
            or not schema.get("properties") or "{{" in json.dumps(schema)):
        return None
    return schema


def workflow_cancelled_agent_requires_review(run: Any) -> bool:
    """Whether a cancelled Agent stopped without a durable outcome receipt."""
    if getattr(run, "status", None) != "cancelled":
        return False
    step_id = str(getattr(run, "current_step_id", "") or "")
    results = getattr(run, "step_results", None) or {}
    if results.get(step_id) is not None:
        return False
    snapshot = getattr(run, "execution_snapshot", None) or {}
    step = next((item for item in snapshot.get("steps", []) if item.get("id") == step_id), {})
    return step.get("type") == "agent"


def workflow_effect_review(run: Any) -> dict[str, Any] | None:
    """Describe an exact uncertain Agent outcome an operator may reconcile.

    This is an offer to review, never permission to replay. Confirmed effects
    and producer/output-contract failures must use their existing recovery path.
    """
    from packages.core.ai.runtime.outcome import attempt_forbids_replay

    step_id = str(getattr(run, "current_step_id", "") or "")
    results = getattr(run, "step_results", None) or {}
    current_result = results.get(step_id)
    result = current_result if isinstance(current_result, dict) else {}
    snapshot = getattr(run, "execution_snapshot", None) or {}
    step = next((item for item in snapshot.get("steps", []) if item.get("id") == step_id), {})
    # A cancellation can arrive after an Agent begins but before it
    # stores a result. Its external outcome is unknown, so it must use this
    # review contract rather than silently becoming a safe replay.
    cancelled_agent_without_result = workflow_cancelled_agent_requires_review(run)

    def has_confirmed_effect(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("effect_state") == "confirmed" or value.get("action_executed") is True or any(
                has_confirmed_effect(child) for child in value.values()
            )
        return isinstance(value, list) and any(has_confirmed_effect(child) for child in value)

    failed_unconfirmed_effect = (
        getattr(run, "status", None) == "failed"
        and step.get("type") == "agent"
        and result.get("status") == "failed"
        and result.get("effect_state") == "unconfirmed"
        and result.get("producer_completed") is not True
        and not isinstance(result.get("chrome_outcome"), dict)
        and not has_confirmed_effect(result)
    )
    if (
        not (failed_unconfirmed_effect or cancelled_agent_without_result)
        or any(
            sid != step_id and item.get("status") == "failed"
            and (attempt_forbids_replay(item) or item.get("retry_blocked") is True)
            for sid, item in results.items() if isinstance(item, dict)
        )
    ):
        return None
    fingerprint = hashlib.sha256(json.dumps({
        "run_id": run.id, "entity_id": run.entity_id,
        "workspace_id": run.workspace_id, "step_id": step_id,
        "snapshot": snapshot, "results": results,
    }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    runtime = (getattr(run, "trigger_data", None) or {}).get("_workflow_runtime_context") or {}
    return {
        "step_id": step_id,
        "effect_fingerprint": fingerprint,
        "deadline_at": runtime.get("workflow_effect_deadline_at"),
    }


def agent_retry_context(
    result: dict[str, Any], *, run_id: str, step_id: str, error: str | None,
    receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep complete, redacted evidence; never nest copies of recovery history."""
    previous = result.get("agent_recovery")
    previous = previous if isinstance(previous, dict) else {}
    attempts = [
        item for item in previous.get("attempts", [])
        if isinstance(item, dict) and item.get("run_id") != run_id
    ]
    attempts.append({
        "run_id": run_id,
        "step_id": step_id,
        "run_error": error,
        "result": {key: value for key, value in result.items() if key not in {
            "agent_recovery", "inputs", "runtime_trace", "usage",
        }},
        # Operations are scoped to the run, not necessarily this Agent step.
        "operation_receipts": receipts or [],
        "operation_receipts_scope": "workflow_run",
    })
    return sanitize_sensitive_payload({"attempts": attempts})


def recovery_prompt_evidence(value: Any, *, max_chars: int = 32_000) -> Any:
    """Bound the prompt without replacing an entire result with a JSON prefix.

    Durable recovery evidence stays complete. Oversized fields are explicit
    omissions, never plausible but shortened IDs. Divide the budget between
    siblings so a large description cannot hide a later identity or blocker.
    """
    value = sanitize_sensitive_payload(value)

    def compact(item: Any, budget: int) -> Any:
        if len(json.dumps(item, ensure_ascii=False)) <= budget:
            return item
        omitted = {"truncated": True}
        if isinstance(item, dict) and item:
            overhead = len(json.dumps(dict.fromkeys(item), ensure_ascii=False))
            available = budget - overhead
            if available >= len(item) * 64:
                share = available // len(item)
                return {key: compact(child, share) for key, child in item.items()}
        elif isinstance(item, list) and item:
            # Keep the first attempt's effect evidence and the latest attempts.
            count = min(len(item), max(0, (budget - 64) // 128))
            if count:
                if count == len(item):
                    selected = item
                elif count > 1:
                    selected = [item[0], *item[-(count - 1):]]
                else:
                    selected = [item[-1]]
                share = (budget - 64) // count - 2
                result = [compact(child, share) for child in selected]
                if count < len(item):
                    result.append({"truncated": True, "omitted_items": len(item) - count})
                return result
        elif isinstance(item, str) and budget >= 128:
            # JSON escaping can expand a preview; measure the serialized form.
            preview = item[: max(0, (budget - 64) // 6)]
            return {"truncated": True, "preview": preview}
        return omitted

    return compact(value, max_chars)
