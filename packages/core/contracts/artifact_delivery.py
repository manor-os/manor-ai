"""Host-owned evidence for delivering, rather than creating, an existing file."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from packages.core.contracts.step_execution import (
    STEP_EXECUTION_CONTRACT_PARAM,
    StepDelivery,
    StepEffects,
    StepExecutionContract,
)


ARTIFACT_DELIVERY_SOURCE = "runtime_verified_artifact_delivery"
# JSON Schema annotation for an explicit final-file reference object (or an
# array of such objects). It describes delivery intent, never authorization or
# proof that the referenced Document exists.
ARTIFACT_DELIVERABLE_SCHEMA_KEY = "x-manor-deliverable"


def readonly_artifact_delivery(params: Any) -> bool:
    declaration = params.get(STEP_EXECUTION_CONTRACT_PARAM) if isinstance(params, dict) else None
    if declaration is None:
        return False
    contract = StepExecutionContract.model_validate(declaration)
    return contract.delivery is StepDelivery.ARTIFACT and contract.effects is StepEffects.READ_ONLY


def artifact_submission_sha256(result: Any) -> str:
    return hashlib.sha256(json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def artifact_delivery_step_binding(step: Any) -> dict[str, Any]:
    """Bind a projection to its existing owner; this is not execution state."""
    return {
        "step_id": str(step.id),
        "step_key": step.step_key,
        "attempt_count": step.attempt_count,
        "submission_sha256": artifact_submission_sha256(step.result),
        "contract_sha256": artifact_submission_sha256(getattr(step, "params", None)),
    }


def accepted_task_artifact_manifest(plan: Any, steps: list[Any]) -> dict[str, list[dict[str, Any]]] | None:
    """Read the terminal writer's projection only while its source still matches.

    It does not certify a Task or re-verify file access. Only the fenced
    acceptance writer creates it after verifying the submitted Documents.
    """
    acceptance = (getattr(plan, "dispatcher_state", None) or {}).get("task_content_acceptance")
    if not isinstance(acceptance, dict) or acceptance.get("verdict") != "accepted" or not acceptance.get("snapshot_digest"):
        return None
    manifest = acceptance.get("artifact_manifest")
    if not isinstance(manifest, dict) or manifest.get("plan_id") != str(plan.id):
        return None
    deliveries = manifest.get("steps")
    if not isinstance(deliveries, list) or len(deliveries) != len(steps):
        return None
    current = {str(step.id): step for step in steps}
    projected: dict[str, list[dict[str, Any]]] = {}
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            return None
        step_id = delivery.get("step_id")
        step = current.get(step_id)
        if step is None or step_id in projected or step.step_status != "done":
            return None
        if any(delivery.get(key) != value for key, value in artifact_delivery_step_binding(step).items()):
            return None
        refs = delivery.get("refs")
        if not isinstance(refs, list) or any(not isinstance(ref, dict) or not ref.get("document_id") or not ref.get("fs_path") for ref in refs):
            return None
        projected[step_id] = deepcopy(refs)
    return projected


def reused_artifact_evidence(
    step: Any, result: Any, snapshots: list[dict[str, Any]], *,
    lease_id: str, completed_at: datetime,
) -> list[dict[str, Any]]:
    """Called only by the fenced server completion path, never by a worker."""
    return [{
        "kind": "artifact",
        "type": "knowledge_document",
        "source": ARTIFACT_DELIVERY_SOURCE,
        "document_id": snapshot["document_id"],
        "fs_path": snapshot["fs_path"],
        "fields": {
            "disposition": "reused",
            "owner_plan_id": str(step.plan_id),
            "owner_step_id": str(step.id),
            "lease_id": lease_id,
            "owner_attempt_count": step.attempt_count,
            "owner_finished_at": completed_at.astimezone(timezone.utc).isoformat(),
            "submission_sha256": artifact_submission_sha256(result),
            "snapshot": {key: deepcopy(value) for key, value in snapshot.items() if key != "text"},
        },
    } for snapshot in snapshots]

