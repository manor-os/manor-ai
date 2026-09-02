"""Task.expected_output is a hard contract on the terminal agent step."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select

from packages.core.contracts.task_output import (
    TaskOutputValueKind,
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
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.schema import Plan
from packages.core.plans.service import (
    _step_from_pydantic,
    bind_task_output_contract,
    create_plan_from_dag,
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
    assert envelope_schema["required"] == ["status", "summary"]
    validator = Draft202012Validator(envelope_schema)
    validator.validate({
        "status": "failed",
        "summary": "blocked",
        "failure": {
            "reason": "docs.upload is blocked",
            "retryable": False,
            "requires_human": True,
        },
    })
    with pytest.raises(ValidationError):
        validator.validate({"status": "failed", "summary": "blocked"})
    with pytest.raises(ValidationError):
        validator.validate({
            "status": "failed",
            "summary": "blocked",
            "failure": {"reason": "docs.upload is blocked"},
        })
    with pytest.raises(ValidationError):
        validator.validate({
            "status": "failed",
            "summary": "blocked",
            "failure": {
                "reason": "human input is required",
                "retryable": True,
                "requires_human": True,
            },
        })
    with pytest.raises(ValidationError):
        validator.validate({
            "status": "failed",
            "summary": "blocked",
            "failure": {"reason": "blocked", "retryable": False},
            "outputs": {"data": _valid_leads()},
        })
    with pytest.raises(ValidationError):
        validator.validate({"status": "succeeded", "summary": "done"})
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
    tool_validator = Draft202012Validator(params)

    assert params["required"] == ["summary"]
    assert result_schema["required"] == ["leads"]
    assert result_schema["properties"]["leads"]["minItems"] == 10
    assert result_schema["properties"]["leads"]["maxItems"] == 10
    tool_validator.validate({"summary": "done", "result": _valid_leads()})
    tool_validator.validate({
        "summary": "blocked",
        "status": "failed",
        "failure": {"reason": "docs.upload is blocked", "retryable": False},
    })
    with pytest.raises(ValidationError):
        tool_validator.validate({
            "summary": "contradictory success",
            "status": "succeeded",
            "result": _valid_leads(),
            "failure": {"reason": "also failed", "retryable": False},
        })
    with pytest.raises(ValidationError):
        tool_validator.validate({
            "summary": "contradictory failure",
            "status": "failed",
            "result": _valid_leads(),
            "failure": {"reason": "blocked", "retryable": False},
        })

    submitted = step_result_from_submit(
        {"summary": "qualified ten leads", "result": _valid_leads()},
        schema,
    )
    assert submitted == {
        "status": "succeeded",
        "summary": "qualified ten leads",
        "outputs": {"data": _valid_leads()},
    }

    failed = step_result_from_submit(
        {
            "summary": "cannot create the required file",
            "status": "failed",
            "result": {"leads": []},
            "failure": {
                "reason": "docs.upload is blocked",
                "blockers": ["docs.upload"],
                "retryable": False,
                "requires_human": True,
            },
        },
        schema,
    )
    assert failed["status"] == "failed"
    # A failed control result cannot smuggle a bogus partial deliverable into
    # the hard Task output validator; usable partial work must use status=partial.
    assert "outputs" not in failed
    Draft202012Validator(schema).validate(failed)


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


def test_task_payload_status_field_is_not_mistaken_for_control_failure() -> None:
    expected = {
        "type": "object",
        "required": ["status"],
        "additionalProperties": False,
        "properties": {"status": {"type": "string"}},
    }
    schema = task_output_envelope_schema(expected)
    payload = {"status": "failed"}

    coerced = coerce_step_output_for_schema(
        schema,
        payload,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert coerced == {
        "status": "succeeded",
        "summary": "structured task output submitted",
        "outputs": {"data": payload},
    }


def test_task_payload_status_and_summary_are_not_mistaken_for_envelope() -> None:
    expected = {
        "type": "object",
        "required": ["status", "summary"],
        "additionalProperties": False,
        "properties": {
            "status": {"const": "completed"},
            "summary": {"type": "string"},
        },
    }
    schema = task_output_envelope_schema(expected)
    payload = {"status": "completed", "summary": "report ready"}

    coerced = coerce_step_output_for_schema(
        schema,
        payload,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert coerced == {
        "status": "succeeded",
        "summary": "structured task output submitted",
        "outputs": {"data": payload},
    }
    Draft202012Validator(schema).validate(coerced)


def test_task_payload_failure_fields_are_not_mistaken_for_control_failure() -> None:
    expected = {
        "type": "object",
        "required": ["status", "failure"],
        "additionalProperties": False,
        "properties": {
            "status": {"const": "failed"},
            "failure": {
                "type": "object",
                "required": ["reason"],
                "properties": {"reason": {"type": "string"}},
            },
        },
    }
    schema = task_output_envelope_schema(expected)
    payload = {"status": "failed", "failure": {"reason": "provider declined"}}

    coerced = coerce_step_output_for_schema(
        schema,
        payload,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert coerced == {
        "status": "succeeded",
        "summary": "structured task output submitted",
        "outputs": {"data": payload},
    }


def test_task_payload_outputs_data_fields_are_not_mistaken_for_envelope() -> None:
    expected = {
        "type": "object",
        "required": ["outputs"],
        "additionalProperties": False,
        "properties": {
            "outputs": {
                "type": "object",
                "required": ["data"],
                "additionalProperties": False,
                "properties": {
                    "data": {
                        "type": "object",
                        "required": ["value"],
                        "additionalProperties": False,
                        "properties": {"value": {"type": "string"}},
                    }
                },
            }
        },
    }
    schema = task_output_envelope_schema(expected)
    payload = {"outputs": {"data": {"value": "business result"}}}

    coerced = coerce_step_output_for_schema(
        schema,
        payload,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert coerced == {
        "status": "succeeded",
        "summary": "structured task output submitted",
        "outputs": {"data": payload},
    }
    Draft202012Validator(schema).validate(coerced)


def test_broad_task_schema_preserves_declared_success_and_failure_envelopes() -> None:
    schema = task_output_envelope_schema({"type": "object"})
    success = step_result_from_submit(
        {"summary": "done", "result": {"value": "business result"}},
        schema,
    )
    failure = step_result_from_submit(
        {
            "summary": "blocked",
            "status": "failed",
            "failure": {"reason": "permission denied", "retryable": False},
        },
        schema,
    )

    coerced_success = coerce_step_output_for_schema(
        schema,
        success,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
    )
    coerced_failure = coerce_step_output_for_schema(
        schema,
        failure,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
    )

    assert coerced_success == success
    assert coerced_failure == failure
    Draft202012Validator(schema).validate(coerced_success)
    Draft202012Validator(schema).validate(coerced_failure)


def test_broad_task_schema_preserves_explicit_canonical_shaped_business_payload() -> None:
    schema = task_output_envelope_schema({"type": "object"})
    payload = {
        "status": "succeeded",
        "summary": "provider receipt",
        "outputs": {"data": {"provider_id": "job-42"}},
    }

    coerced = coerce_step_output_for_schema(
        schema,
        payload,
        step_kind="llm",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert coerced["outputs"]["data"] == payload
    Draft202012Validator(schema).validate(coerced)


def test_broad_task_schema_does_not_hide_declared_malformed_control_envelopes() -> None:
    schema = task_output_envelope_schema({"type": "object"})
    malformed_success = {"status": "succeeded", "summary": "claimed done"}
    malformed_failure = {
        "status": "failed",
        "summary": "blocked",
        "failure": {"reason": "permission denied"},
    }

    coerced_success = coerce_step_output_for_schema(
        schema,
        malformed_success,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
    )
    coerced_failure = coerce_step_output_for_schema(
        schema,
        malformed_failure,
        step_kind="subagent",
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
    )

    assert coerced_success == malformed_success
    assert coerced_failure == malformed_failure
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(coerced_success)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(coerced_failure)


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


def test_planning_rejects_noop_task_and_step_output_contracts() -> None:
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "deliver",
                    "kind": "subagent",
                    "service_key": "content_creation",
                    "expected_output_schema": {"properties": {}},
                    "params": {"prompt": "Return the deliverable."},
                }
            ]
        }
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output={"required": []},
        require_explicit_agent_outputs=True,
    )

    assert any(gap.kind == "invalid_task_output_contract" for gap in gaps)
    assert any(
        gap.kind == "empty_output_contract" and gap.step_key == "deliver"
        for gap in gaps
    )


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


def test_task_contract_is_bound_into_the_plan_before_materialization() -> None:
    """The persisted Plan and terminal ExecutionStep must name one contract."""
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "research",
                    "kind": "subagent",
                    "service_key": "apartment_partner_leads",
                    "output_shape": "TextResult",
                    "params": {"prompt": "Research candidate partners."},
                },
                {
                    "key": "deliver_shortlist",
                    "kind": "subagent",
                    "service_key": "apartment_partner_leads",
                    # This Planner guess conflicts with Task.expected_output.
                    "output_shape": "TextResult",
                    "params": {"prompt": "Return the final shortlist."},
                    "depends_on": ["research"],
                },
            ],
        }
    )

    bound = bind_task_output_contract(plan, _lead_schema())

    assert bound.steps[0].output_shape == "TextResult"
    terminal = bound.steps[1]
    assert terminal.output_shape is None
    assert is_task_output_contract_schema(terminal.expected_output_schema)
    assert task_output_payload_schema(terminal.expected_output_schema) == _lead_schema()


@pytest.mark.asyncio
async def test_persisted_plan_and_terminal_step_share_the_task_contract(db_session) -> None:
    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        title="Research apartment partner leads",
        description="Return exactly ten qualified partner leads.",
        expected_output=_lead_schema(),
    )
    db_session.add(task)
    await db_session.flush()
    planner_output = Plan.model_validate(
        {
            "steps": [{
                "key": "deliver_shortlist",
                "kind": "subagent",
                "service_key": "apartment_partner_leads",
                "output_shape": "TextResult",
                "params": {"prompt": "Return the final shortlist."},
            }],
        }
    )

    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=task.entity_id,
        workspace_id=None,
        task_id=task.id,
        agent_subscription_id=None,
        plan=planner_output,
        enforce_contract=True,
    )
    step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan_row.id)
    )).scalar_one()
    persisted_schema = plan_row.plan_dag["steps"][0]["expected_output_schema"]

    assert plan_row.plan_dag["steps"][0]["output_shape"] is None
    assert persisted_schema == step.expected_output_schema
    assert task_output_payload_schema(persisted_schema) == _lead_schema()


@pytest.mark.asyncio
async def test_format_only_task_contract_survives_materialization_and_supervisor_gate(
    db_session,
) -> None:
    from packages.core.plans.executor import _execution_output_contract_issue

    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        title="Return the canonical source URL",
        description="Return one URI.",
        expected_output={"format": "uri"},
    )
    db_session.add(task)
    await db_session.flush()
    planner_output = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "deliver_url",
                    "kind": "subagent",
                    "service_key": "research",
                    "params": {"prompt": "Return the source URL."},
                }
            ]
        }
    )

    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=task.entity_id,
        workspace_id=None,
        task_id=task.id,
        agent_subscription_id=None,
        plan=planner_output,
        enforce_contract=True,
    )
    step = (
        await db_session.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan_row.id)
        )
    ).scalar_one()
    step.step_status = "done"
    step.result = {
        "status": "succeeded",
        "summary": "source found",
        "outputs": {"data": "not a uri"},
    }

    issue = _execution_output_contract_issue(plan_row, [step], task)

    assert task_output_payload_schema(step.expected_output_schema) == {"format": "uri"}
    assert issue is not None
    assert "does not satisfy its output contract" in issue
    assert "uri" in issue


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
        await Dispatcher().complete_lease(
            db,
            first_lease.id,
            result=too_short,
            task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
        )
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
        await Dispatcher().complete_lease(
            db,
            second_lease.id,
            result=exact,
            task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
        )
        await db.flush()

        assert step.step_status == "done"
        assert step.error is None
        assert step.result["outputs"]["data"] == _valid_leads()


@pytest.mark.asyncio
async def test_terminal_task_contract_accepts_failed_control_without_fake_deliverable(
    client,
) -> None:
    """A failed StepResult is control data, not a malformed success payload."""
    import packages.core.database as dbmod

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    worker_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    failure = {
        "reason": "docs.upload is blocked",
        "blockers": ["docs.upload"],
        "retryable": False,
        "requires_human": True,
    }
    failed = {
        "status": "failed",
        "summary": failure["reason"],
        "failure": failure,
    }

    async with dbmod.async_session() as db:
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
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
        db.add(
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="create_artifact",
                kind="subagent",
                step_status="running",
                attempt_count=1,
                max_attempts=3,
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                current_lease_id=lease_id,
            )
        )
        db.add(
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                worker_id=worker_id,
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="active",
            )
        )
        await db.flush()

        await Dispatcher().complete_lease(
            db,
            lease_id,
            result=failed,
            task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
        )
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.step_status in {"failed", "waiting_human"}, (
            step.error,
            step.result,
        )
        assert step.step_status != "pending"
        assert step.error["type"] == "StepResultFailed"
        assert step.error["failure"] == failure
