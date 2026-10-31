"""Task.expected_output is a hard contract on the terminal agent step."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, ValidationError

from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    is_task_output_contract_schema,
    task_expected_output_json_schema,
    task_output_envelope_schema,
    task_output_payload_schema,
)
from packages.core.dispatcher.service import Dispatcher
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.validation import (
    output_schema_is_advisory,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker


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

        step = ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="research_and_shortlist_partner_leads",
            kind="subagent",
            step_status="pending",
            max_attempts=3,
            params={"prompt": "Research and return exactly ten qualified leads."},
            expected_output_schema=task_output_envelope_schema(_lead_schema()),
        )
        db.add(step)
        await db.flush()
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
