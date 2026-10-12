"""Planner-authored agent payload schemas must not promise a record count.

A subagent step on staging (task 01M2830V5C5T77DJ3KMW6TEAJD, 2026-09-11)
carried ``prospects: {minItems: 25, maxItems: 25}`` with a non-null
``prospect_name`` while its own prompt said "mark unknown fields as null
rather than inventing details". The sources held two placeholder rows, so the
only schema-valid answer was 23 invented prospects; the agent refused and the
task parked on a human. How many records exist is a fact the run discovers,
not a promise the Planner can make — it belongs in the envelope's
``progress`` and the prompt, never in the hard contract.
"""
from __future__ import annotations

from packages.core.plans.schema import PlanStep
from packages.core.plans.service import _expected_output_schema_for_plan_step


def _prospect_schema() -> dict:
    return {
        "type": "object",
        "required": ["source_summary", "prospects"],
        "properties": {
            "source_summary": {"type": "string"},
            "prospects": {
                "type": "array",
                "minItems": 25,
                "maxItems": 25,
                "items": {
                    "type": "object",
                    "required": ["prospect_name", "notes"],
                    "properties": {
                        "prospect_name": {"type": "string"},
                        "notes": {
                            "type": "array",
                            "minItems": 3,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def test_subagent_step_schema_drops_fixed_record_counts() -> None:
    step = PlanStep(
        key="extract_25_prospects",
        kind="subagent",
        service_key="lead_nurturing",
        expected_output_schema=_prospect_schema(),
        params={"prompt": "Find exactly 25 prospects."},
    )
    schema = _expected_output_schema_for_plan_step(step)
    assert schema is not None
    prospects = schema["properties"]["prospects"]
    assert "maxItems" not in prospects
    assert prospects["minItems"] == 1  # "at least one", never "at least 25"
    notes = prospects["items"]["properties"]["notes"]
    assert notes["minItems"] == 1
    # Everything the Planner legitimately declared survives: the fields,
    # their requiredness, and the hard-contract provenance marker.
    assert schema["required"] == ["source_summary", "prospects"]
    assert prospects["items"]["required"] == ["prospect_name", "notes"]
    assert schema["x-manor-contract-source"] == "plan.expected_output_schema"


def test_structured_step_schema_keeps_cardinality() -> None:
    """A tool/integration contract is a real external schema; leave it alone."""
    step = PlanStep(
        key="fetch",
        kind="action",
        service_key="lead_nurturing",
        provider="crm",
        action_key="crm.list",
        expected_output_schema=_prospect_schema(),
        params={},
    )
    schema = _expected_output_schema_for_plan_step(step)
    assert schema["properties"]["prospects"]["minItems"] == 25
