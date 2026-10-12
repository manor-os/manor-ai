"""Completion requires persisted assessments, not model-authored receipts."""
from __future__ import annotations

import json

from sqlalchemy import select

from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseDecision
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.stats.assessment import observation_receipt


class StatAssessmentContract:
    response_format = None

    def __init__(self, *, entity_id: str, workspace_id: str, workflow_run_id: str, stat_keys: list[str]):
        self.entity_id = entity_id
        self.workspace_id = workspace_id
        self.workflow_run_id = workflow_run_id
        self.stat_keys = list(dict.fromkeys(stat_keys))
        self.instructions = (
            "This assessment completes only after record_stat_assessment persists one result "
            "for every required metric in this Workflow run. Read get_stat_status, inspect actual "
            "evidence, and submit measured or insufficient_evidence assessments with explicit gaps. "
            "A document/report or a number in your final answer is not a saved measurement. "
            f"Required metric keys: {json.dumps(self.stat_keys)}"
        )

    async def validate(self, content: str) -> FinalResponseDecision:
        from packages.core.database import async_session

        receipts = []
        missing = []
        async with async_session() as db:
            for key in self.stat_keys:
                row = (await db.execute(select(WorkspaceStatObservation).join(
                    WorkspaceStat, WorkspaceStat.id == WorkspaceStatObservation.stat_id,
                ).where(
                    WorkspaceStat.entity_id == self.entity_id,
                    WorkspaceStat.workspace_id == self.workspace_id,
                    WorkspaceStat.key == key,
                    WorkspaceStat.status == "active",
                    WorkspaceStatObservation.collector_revision == WorkspaceStat.revision,
                    WorkspaceStatObservation.source == "agent_assessment",
                    WorkspaceStatObservation.evidence["origin"]["workflow_run_id"].astext == self.workflow_run_id,
                ).order_by(WorkspaceStatObservation.observed_at.desc()).limit(1))).scalar_one_or_none()
                if row is None:
                    missing.append(key)
                else:
                    receipt = await observation_receipt(db, row)
                    receipts.append(receipt)
                    if receipt["projection_errors"]:
                        missing.append(key)
        if missing:
            return FinalResponseDecision(
                action=FinalResponseAction.CONTINUE_EXECUTION,
                diagnostic=tool_error_payload(
                    tool_name="stat_assessment", code="measurement_receipt_missing",
                    message="Required assessments have not been persisted for this run.",
                    details={"missing_stat_keys": missing, "persisted": receipts,
                             "next_read": "get_stat_status", "write_action": "record_stat_assessment"},
                ),
            )
        # Return only host-read receipts, never a model-invented completed list.
        return FinalResponseDecision(
            action=FinalResponseAction.ACCEPT,
            content=json.dumps({"status": "assessments_saved", "assessments": receipts,
                                "measured_count": sum(receipt["value"] is not None for receipt in receipts),
                                "insufficient_evidence_count": sum(receipt["value"] is None for receipt in receipts)}, ensure_ascii=False),
            evidence=tuple(receipts),
        )
