"""Workspace-scoped assessment tools shared by Chat, Tasks and Workflows."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.services.workspace_access import user_can_read_workspace_id, user_can_write_workspace_id
from packages.core.stats.assessment import observation_receipt, validate_assessment, readable_assessment_document
from packages.core.stats.service import StatError, list_observations, list_stats, record_observation


STAT_ASSESSMENT_GUIDANCE = (
    "Read get_stat_status before assessing metrics. To record_stat_assessment pass "
    "stat_id, expected_revision, stable idempotency_key, status=measured|insufficient_evidence, "
    "value (number or null), reasoning, evidence=[{document_id,updated_at,locator,reason}], gaps=[...]. "
    "get_stat_status accepts document_ids to obtain accessible source versions. "
    "Unknown is not zero. Only a returned persisted observation_id proves a saved assessment."
)


async def runtime_stat_action(
    *, action: str, entity_id: str, workspace_id: str | None, user_id: str | None,
    params: dict[str, Any],
    origin: dict[str, Any] | None = None,
) -> str:
    from packages.core.database import async_session

    try:
        if action not in {"get_stat_status", "record_stat_assessment"}:
            raise StatError("unsupported assessment action")
        if not workspace_id:
            raise StatError("an explicit authorized workspace is required")
        async with async_session() as db:
            access_check = user_can_read_workspace_id if action == "get_stat_status" else user_can_write_workspace_id
            if not user_id or not await access_check(
                db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
            ):
                raise StatError("the current actor cannot manage assessments in this workspace")
            if action == "get_stat_status":
                rows = await list_stats(db, entity_id=entity_id, workspace_id=workspace_id)
                items = []
                for row in rows:
                    history = await list_observations(db, stat_id=row.id, limit=1)
                    items.append({
                        "stat_id": row.id, "key": row.key, "name": row.name,
                        "description": row.description, "revision": row.revision,
                        "collector_type": row.collector_type, "collector_config": row.collector_config,
                        "current_value": str(row.current_value) if row.current_value is not None else None,
                        "last_collection_status": row.last_collection_status,
                        "last_assessment": ({**await observation_receipt(db, history[0]), "evidence": history[0].evidence} if history else None),
                    })
                versions = []
                ids = params.get("document_ids", [])
                if not isinstance(ids, list) or len(ids) > 32:
                    raise StatError("document_ids must be an array of at most 32 IDs")
                for document_id in ids:
                    document = await readable_assessment_document(db, entity_id, workspace_id, user_id, document_id)
                    versions.append({"document_id": document.id, "updated_at": (document.updated_at or document.created_at).isoformat(), "name": document.name})
                return json.dumps({"stats": items, "document_versions": versions, "guidance": STAT_ASSESSMENT_GUIDANCE}, default=str)
            stat = (await db.execute(select(WorkspaceStat).where(
                WorkspaceStat.id == params.get("stat_id"), WorkspaceStat.entity_id == entity_id,
                WorkspaceStat.workspace_id == workspace_id,
            ))).scalar_one_or_none()
            if stat is None:
                raise StatError("stat not found in the active workspace")
            assessment = validate_assessment(params, stat)
            assessment["evidence"]["origin"] = {**(origin or {}), "user_id": user_id}
            existing = (await db.execute(select(WorkspaceStatObservation).where(
                WorkspaceStatObservation.stat_id == stat.id,
                WorkspaceStatObservation.idempotency_key == assessment["idempotency_key"],
            ))).scalar_one_or_none()
            if existing is not None:
                if existing.value != assessment["value"] or existing.evidence != assessment["evidence"]:
                    raise StatError("idempotency key already records a different assessment")
                return json.dumps(await observation_receipt(db, existing))
            observation = await record_observation(db, stat, source="agent_assessment", assessment_actor_id=user_id, **assessment)
            result = await observation_receipt(db, observation)
            await db.commit()
            return json.dumps(result)
    except (StatError, ValueError, TypeError) as exc:
        return json.dumps(tool_error_payload(
            tool_name=action, code="stat_assessment_invalid", message=str(exc),
            details={"workspace_id": workspace_id, "stat_id": params.get("stat_id"),
                     "execution": "not_committed", "guidance": STAT_ASSESSMENT_GUIDANCE},
        ))
