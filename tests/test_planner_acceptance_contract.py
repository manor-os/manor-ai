from types import SimpleNamespace

import pytest

from packages.core.plans.planner import (
    PlannerError,
    _enforce_planner_acceptance_contract,
    _normalize_planner_acceptance_contract,
)
from packages.core.plans.schema import Plan, PlanStep


def _task(*deliverable_names: str):
    return SimpleNamespace(
        title="Repair assets in place",
        description="Repair existing assets in place.",
        expected_output={
            "deliverables": [
                {"name": name, "acceptance": f"{name} exists."}
                for name in deliverable_names
            ]
        },
    )


def _plan(*criterion_names: str) -> Plan:
    steps = [
        PlanStep(
            key="inspect_current",
            kind="subagent",
            service_key="campaign_and_content",
            params={"prompt": "Inspect current campaign_draft_pack and campaign_files."},
        ),
        PlanStep(
            key="repair_existing",
            kind="subagent",
            service_key="campaign_and_content",
            depends_on=["inspect_current"],
            params={"prompt": "Repair campaign_draft_pack and campaign_files in place."},
        ),
    ]
    criteria = [
        {
            "key": f"{name}_{index}",
            "deliverable_name": name,
            "description": f"{name} criterion {index}",
            "evidence_step_keys": ["repair_existing"],
        }
        for index, name in enumerate(criterion_names)
    ]
    return Plan(
        steps=steps,
        metadata={
            "acceptance_contract": {
                "expected_result": "Named deliverables exist.",
                "criteria": criteria,
            }
        },
    )


def test_normalize_keeps_first_duplicate_deliverable_criterion() -> None:
    task = _task("campaign_draft_pack", "campaign_files")
    plan = _plan(
        "campaign_draft_pack",
        "campaign_draft_pack",
        "campaign_files",
    )

    normalized = _normalize_planner_acceptance_contract(plan, task)
    names = [
        criterion.deliverable_name
        for criterion in normalized.metadata.acceptance_contract.criteria
    ]
    descriptions = [
        criterion.description
        for criterion in normalized.metadata.acceptance_contract.criteria
    ]

    assert names == ["campaign_draft_pack", "campaign_files"]
    assert descriptions == [
        "campaign_draft_pack criterion 0",
        "campaign_files criterion 2",
    ]
    _enforce_planner_acceptance_contract(normalized, task)


def test_normalize_does_not_invent_missing_deliverable_criteria() -> None:
    task = _task("campaign_draft_pack", "campaign_files")
    plan = _plan("campaign_draft_pack", "campaign_draft_pack")

    normalized = _normalize_planner_acceptance_contract(plan, task)
    names = [
        criterion.deliverable_name
        for criterion in normalized.metadata.acceptance_contract.criteria
    ]
    assert names == ["campaign_draft_pack"]
    with pytest.raises(PlannerError, match="missing required criteria for: campaign_files"):
        _enforce_planner_acceptance_contract(normalized, task)


def test_unique_acceptance_criteria_are_unchanged() -> None:
    task = _task("campaign_draft_pack", "campaign_files")
    plan = _plan("campaign_draft_pack", "campaign_files")

    normalized = _normalize_planner_acceptance_contract(plan, task)
    names = [
        criterion.deliverable_name
        for criterion in normalized.metadata.acceptance_contract.criteria
    ]
    assert names == ["campaign_draft_pack", "campaign_files"]
    _enforce_planner_acceptance_contract(normalized, task)
