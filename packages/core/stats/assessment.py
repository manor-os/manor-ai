"""Shared assessment input and durable receipt contract (no execution policy)."""
from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import select

from packages.core.models.goal import GoalMeasurement
from packages.core.models.document import Document
from packages.core.stats.service import StatError, _normalize_stat_number


async def readable_assessment_document(db, entity_id, workspace_id, user_id, document_id):
    from packages.core.services.document_access import user_can_read_document, document_workspace_ids
    document = (await db.execute(select(Document).where(
        Document.id == document_id, Document.entity_id == entity_id, Document.is_trashed.is_(False),
    ).with_for_update(read=True))).scalar_one_or_none()
    if document is None or not await user_can_read_document(
        db, document, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        actor_type="agent", allow_redacted=False,
    ):
        raise StatError("evidence document is unavailable to the current actor")
    if workspace_id not in await document_workspace_ids(db, document):
        raise StatError("evidence document does not belong to the active workspace; use Workspace evidence")
    return document


async def validate_assessment_sources(db, stat, user_id, evidence):
    from packages.core.services.workspace_access import user_can_write_workspace_id
    if not user_id or not await user_can_write_workspace_id(
        db, entity_id=stat.entity_id, workspace_id=stat.workspace_id, user_id=user_id,
    ):
        raise StatError("assessment requires an authorized actor; use record_stat_assessment")
    for ref in evidence["references"]:
        document = await readable_assessment_document(
            db, stat.entity_id, stat.workspace_id, user_id, ref["document_id"],
        )
        try:
            unchanged = datetime.fromisoformat(ref["updated_at"]) == (document.updated_at or document.created_at)
        except ValueError:
            unchanged = False
        if not unchanged:
            raise StatError(f"evidence changed: document {document.id}; read it again and reassess")


def validate_review_definition(collector_type: str, config: dict, cadence: str | None) -> None:
    if collector_type != "agent_review":
        return
    if cadence:
        raise StatError("agent_review cadence belongs to its Workflow Automation, not to each Stat")
    if not isinstance(config.get("workflow_slug"), str) or not config["workflow_slug"].strip():
        raise StatError("agent_review requires workflow_slug")
    assessment = config.get("assessment")
    if not isinstance(assessment, dict) or not str(assessment.get("rubric") or "").strip():
        raise StatError("agent_review requires assessment.rubric")
    if assessment.get("missing_evidence") != "unknown":
        raise StatError("assessment.missing_evidence must be unknown")
    if assessment.get("subject") != "workspace":
        raise StatError("Workspace metrics require assessment.subject=workspace; do not mix per-release scores")
    scale = assessment.get("scale")
    if not isinstance(scale, dict) or not {"minimum", "maximum"} <= scale.keys():
        raise StatError("assessment.scale requires minimum and maximum")
    if _normalize_stat_number(scale["minimum"]) >= _normalize_stat_number(scale["maximum"]):
        raise StatError("assessment.scale minimum must be less than maximum")


def validate_assessment(params: dict[str, Any], stat: Any) -> dict[str, Any]:
    status = params.get("status", "measured")
    if status not in {"measured", "insufficient_evidence"}:
        raise StatError("status must be measured or insufficient_evidence")
    if type(params.get("expected_revision")) is not int:
        raise StatError("expected_revision is required; read get_stat_status first")
    key = params.get("idempotency_key")
    if not isinstance(key, str) or not key.strip() or len(key) > 160:
        raise StatError("idempotency_key must be a nonempty string of at most 160 characters")
    evidence = params.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 32:
        raise StatError("evidence must be an array of at most 32 document references")
    for index, ref in enumerate(evidence):
        if not isinstance(ref, dict) or any(
            not isinstance(ref.get(field), str) or not ref[field].strip()
            for field in ("document_id", "updated_at", "locator", "reason")
        ):
            raise StatError(f"evidence[{index}] requires document_id, updated_at, locator and reason")
    gaps = params.get("gaps", [])
    if not isinstance(gaps, list) or any(not isinstance(gap, str) or not gap.strip() for gap in gaps):
        raise StatError("gaps must be an array of nonempty strings")
    reasoning = params.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise StatError("reasoning is required")
    value = params.get("value")
    if status == "insufficient_evidence":
        if value is not None or not gaps:
            raise StatError("insufficient_evidence requires value=null and explicit gaps, not zero")
    else:
        value = _normalize_stat_number(value)
        if not evidence:
            raise StatError("a measured assessment requires document evidence")
        scale = (stat.collector_config or {}).get("assessment", {}).get("scale", {})
        for bound, sign in (("minimum", -1), ("maximum", 1)):
            if bound in scale and (value - _normalize_stat_number(scale[bound])) * sign > 0:
                raise StatError(f"value violates {bound}={scale[bound]}")
    return {
        "value": value,
        "evidence": {"status": status, "references": evidence, "gaps": gaps, "reasoning": reasoning},
        "expected_revision": params["expected_revision"],
        "idempotency_key": key,
    }


async def observation_receipt(db, observation) -> dict[str, Any]:
    measurements = (await db.execute(select(GoalMeasurement).where(
        GoalMeasurement.meta["observation_id"].astext == observation.id,
    ))).scalars().all()
    return {
        "status": "persisted",
        "observation_id": observation.id,
        "stat_id": observation.stat_id,
        "workspace_id": observation.workspace_id,
        "assessment_status": "measured" if observation.value is not None else "insufficient_evidence",
        "value": float(observation.value) if observation.value is not None else None,
        "observed_at": observation.observed_at.isoformat(),
        "collector_revision": observation.collector_revision,
        "goal_measurements": [{"goal_id": row.goal_id, "measured_at": row.measured_at.isoformat()} for row in measurements],
        "projection_errors": (observation.evidence or {}).get("goal_projection_errors", []),
        "gaps": (observation.evidence or {}).get("gaps", []),
        "reasoning": (observation.evidence or {}).get("reasoning"),
    }
