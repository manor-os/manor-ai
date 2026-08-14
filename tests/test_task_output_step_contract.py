"""Task.expected_output is a hard contract on the terminal agent step."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.contracts.task_output import (
    is_plan_output_contract_schema,
    is_task_output_contract_schema,
    task_expected_output_json_schema,
    task_output_envelope_schema,
    task_output_payload_schema,
)
from packages.core.dispatcher.service import Dispatcher
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.validation import (
    SchemaError,
    output_schema_is_advisory,
    validate_step_output,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.schema import Plan
from packages.core.plans.service import (
    _step_from_pydantic,
    materialize_plan_steps,
    plan_contract_gaps,
)
from packages.core.workers.submit_result import (
    build_submit_result_tool,
    step_result_from_submit,
)


def _lead_schema() -> dict:
    return {
        "type": "object",
        "required": ["leads"],
        "additionalProperties": False,
        "properties": {
            "leads": {
                "type": "array",
                "minItems": 10,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "required": ["name", "source_url"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "source_url": {"type": "string"},
                    },
                },
            },
        },
    }


def _valid_leads() -> dict:
    return {
        "leads": [{"name": f"Partner {index}", "source_url": f"https://example.com/{index}"} for index in range(10)]
    }


def test_task_schema_is_nested_in_step_result_without_acceptance_metadata() -> None:
    expected = {
        **_lead_schema(),
        "deliverables": [{"name": "shortlist", "kind": "value"}],
    }

    payload_schema = task_expected_output_json_schema(expected)
    envelope_schema = task_output_envelope_schema(expected)

    assert payload_schema is not None
    assert "deliverables" not in payload_schema
    assert envelope_schema is not None
    assert is_task_output_contract_schema(envelope_schema)
    assert envelope_schema["required"] == ["status", "summary", "outputs"]
    assert envelope_schema["properties"]["outputs"]["required"] == ["data"]
    assert task_output_payload_schema(envelope_schema) == _lead_schema()


def test_strategist_is_told_to_encode_exact_counts_in_expected_output() -> None:
    from packages.core.ai.runtime.strategist import RUNTIME_STRATEGIST_DEFAULT_PREAMBLE

    assert "minItems" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "maxItems" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "exactly N records" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE


def test_planner_is_told_to_emit_one_terminal_structured_step() -> None:
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    prompt = runtime_planner_system_prompt(
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=[],
    )
    assert "exactly one terminal llm/subagent deliverable step" in prompt
    assert "hard-validate it before completion" in prompt
    assert "it never guesses missing fields or shapes" in prompt


def test_submit_result_tool_preserves_hard_fields_and_exact_record_count() -> None:
    schema = task_output_envelope_schema(_lead_schema())
    tool = build_submit_result_tool(schema)
    params = tool["function"]["parameters"]
    result_schema = params["properties"]["result"]

    assert params["required"] == ["summary", "result"]
    assert result_schema["required"] == ["leads"]
    assert result_schema["properties"]["leads"]["minItems"] == 10
    assert result_schema["properties"]["leads"]["maxItems"] == 10

    submitted = step_result_from_submit(
        {"summary": "qualified ten leads", "result": _valid_leads()},
        schema,
    )
    assert submitted == {
        "status": "succeeded",
        "summary": "qualified ten leads",
        "outputs": {"data": _valid_leads()},
    }


def test_top_level_array_task_contract_is_supported() -> None:
    expected = {
        "type": "array",
        "minItems": 10,
        "maxItems": 10,
        "items": _lead_schema()["properties"]["leads"]["items"],
    }
    schema = task_output_envelope_schema(expected)
    tool = build_submit_result_tool(schema)
    result_schema = tool["function"]["parameters"]["properties"]["result"]
    records = _valid_leads()["leads"]

    assert result_schema["type"] == "array"
    assert result_schema["minItems"] == result_schema["maxItems"] == 10
    assert (
        step_result_from_submit(
            {"summary": "ten records", "result": records},
            schema,
        )["outputs"]["data"]
        == records
    )


def test_task_contract_is_hard_while_unmarked_legacy_schema_stays_advisory() -> None:
    legacy_unmarked_schema = {
        "type": "object",
        "required": ["leads"],
        "properties": {"leads": {"type": "array"}},
    }
    task_contract = task_output_envelope_schema(_lead_schema())

    assert output_schema_is_advisory("subagent", legacy_unmarked_schema) is True
    assert output_schema_is_advisory("subagent", task_contract) is False


def test_new_plan_requires_explicit_output_contract_for_every_agent_step() -> None:
    missing = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "research",
                    "kind": "subagent",
                    "service_key": "apartment_partner_leads",
                    "params": {"prompt": "Research partners."},
                },
                {
                    "key": "summarize",
                    "kind": "llm",
                    "service_key": "apartment_partner_leads",
                    "output_shape": "TextResult",
                    "params": {"prompt": "Summarize ${{ steps.research.result.items }}"},
                    "depends_on": ["research"],
                },
            ]
        }
    )
    gaps = plan_contract_gaps(
        missing.topo_order(),
        require_explicit_agent_outputs=True,
    )
    assert any(gap.kind == "missing_explicit_output_contract" and gap.step_key == "research" for gap in gaps)

    explicit = missing.model_copy(
        update={
            "steps": [
                missing.steps[0].model_copy(
                    update={
                        "expected_output_schema": {
                            "type": "object",
                            "required": ["candidates"],
                            "properties": {
                                "candidates": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["name", "source_url"],
                                        "properties": {
                                            "name": {"type": "string"},
                                            "source_url": {"type": "string"},
                                        },
                                    },
                                }
                            },
                        }
                    }
                ),
                missing.steps[1],
            ]
        }
    )
    gaps = plan_contract_gaps(
        explicit.topo_order(),
        require_explicit_agent_outputs=True,
    )
    assert not any(gap.kind in {"missing_explicit_output_contract", "generic_output_contract"} for gap in gaps)


def test_explicit_plan_output_shape_is_hard_but_legacy_schema_stays_advisory() -> None:
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "draft",
                    "kind": "llm",
                    "service_key": "content_creation",
                    "output_shape": "TextResult",
                    "params": {"prompt": "Draft the response."},
                }
            ]
        }
    )
    plan_row = type(
        "_PlanRow",
        (),
        {"id": "plan", "entity_id": "entity", "workspace_id": None},
    )()
    step = _step_from_pydantic(
        plan_row,
        plan.steps[0],
        max_attempts=3,
        output_shape="TextResult",
    )

    assert is_plan_output_contract_schema(step.expected_output_schema)
    assert output_schema_is_advisory("llm", step.expected_output_schema) is False
    tool = build_submit_result_tool(step.expected_output_schema)
    assert tool["function"]["parameters"]["required"] == ["summary", "result"]
    assert tool["function"]["parameters"]["properties"]["result"]["required"] == ["text"]

    legacy_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    assert output_schema_is_advisory("llm", legacy_schema) is True

    summary_only = {"summary": "drafted the response"}
    coerced = coerce_step_output_for_schema(step.expected_output_schema, summary_only)
    assert coerced == summary_only
    with pytest.raises(SchemaError):
        validate_step_output(step, coerced)

    custom_plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "research",
                    "kind": "subagent",
                    "service_key": "content_creation",
                    "expected_output_schema": _lead_schema(),
                    "params": {"prompt": "Return exactly ten lead records."},
                }
            ]
        }
    )
    custom_step = _step_from_pydantic(
        plan_row,
        custom_plan.steps[0],
        max_attempts=3,
        # Mirrors materialization: the linker may infer the generic legacy
        # shape, but it must not override this explicitly authored schema.
        output_shape="StepResult",
    )
    assert is_plan_output_contract_schema(custom_step.expected_output_schema)
    assert output_schema_is_advisory("subagent", custom_step.expected_output_schema) is False
    custom_tool = build_submit_result_tool(custom_step.expected_output_schema)
    leads_schema = custom_tool["function"]["parameters"]["properties"]["result"]["properties"]["leads"]
    assert leads_schema["minItems"] == leads_schema["maxItems"] == 10
    exact_custom = step_result_from_submit(
        {"summary": "ten leads", "status": "succeeded", "result": _valid_leads()},
        custom_step.expected_output_schema,
    )
    assert exact_custom == _valid_leads()
    assert "summary" not in exact_custom
    assert "status" not in exact_custom


def test_planning_rejects_ambiguous_terminal_structured_producers() -> None:
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "research_east",
                    "kind": "subagent",
                    "service_key": "apartment_partner_leads",
                    "params": {"prompt": "Research eastern partners."},
                },
                {
                    "key": "research_west",
                    "kind": "subagent",
                    "service_key": "apartment_partner_leads",
                    "params": {"prompt": "Research western partners."},
                },
            ],
        }
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=_lead_schema(),
    )
    assert any(gap.kind == "task_output_contract" for gap in gaps)


@pytest.mark.asyncio
async def test_terminal_step_retries_bad_task_shape_then_accepts_exact_shape(client) -> None:
    """Execution-level proof: summary-only/short output cannot complete a step."""
    import packages.core.database as dbmod

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    worker_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Research apartment partner leads",
                description="Return exactly ten qualified partner leads.",
                expected_output=_lead_schema(),
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        db.add(
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Internal worker",
                capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
                status="active",
            )
        )
        await db.flush()

        plan = Plan.model_validate(
            {
                "steps": [
                    {
                        "key": "research_and_shortlist_partner_leads",
                        "kind": "subagent",
                        "service_key": "apartment_partner_leads",
                        "params": {"prompt": "Research and return exactly ten qualified leads."},
                    }
                ],
            }
        )
        steps = await materialize_plan_steps(db, plan_id, plan)
        step = steps[0]
        assert is_task_output_contract_schema(step.expected_output_schema)
        data_schema = task_output_payload_schema(step.expected_output_schema)
        assert data_schema["properties"]["leads"]["minItems"] == 10
        assert data_schema["properties"]["leads"]["maxItems"] == 10

        first_lease = WorkLease(
            id=generate_ulid(),
            step_id=step.id,
            plan_id=plan_id,
            entity_id=entity_id,
            worker_id=worker_id,
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="active",
        )
        db.add(first_lease)
        step.step_status = "running"
        step.attempt_count = 1
        step.current_lease_id = first_lease.id
        await db.flush()

        too_short = {
            "status": "succeeded",
            "summary": "researched leads and source URLs",
            "outputs": {
                "data": {
                    "leads": [{"name": "Only one", "source_url": "https://example.com/1"}],
                },
            },
        }
        await Dispatcher().complete_lease(db, first_lease.id, result=too_short)
        await db.flush()

        assert step.step_status == "pending"
        assert step.error["type"] == "OutputSchemaError"
        assert any("too short" in item["message"] for item in step.error["errors"])

        second_lease = WorkLease(
            id=generate_ulid(),
            step_id=step.id,
            plan_id=plan_id,
            entity_id=entity_id,
            worker_id=worker_id,
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="active",
        )
        db.add(second_lease)
        step.step_status = "running"
        step.attempt_count = 2
        step.current_lease_id = second_lease.id
        await db.flush()

        exact = {
            "status": "succeeded",
            "summary": "qualified ten leads",
            "outputs": {"data": _valid_leads()},
        }
        await Dispatcher().complete_lease(db, second_lease.id, result=exact)
        await db.flush()

        assert step.step_status == "done"
        assert step.error is None
        assert step.result["outputs"]["data"] == _valid_leads()
