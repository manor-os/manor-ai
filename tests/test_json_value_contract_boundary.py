"""Worker/API boundaries preserve native JSON output values and presence."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError

from apps.api.routers.workers import CompleteLeaseRequest, HeartbeatCompletedLease
from packages.core.dispatcher import MISSING_RESULT, Dispatcher
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.service import _attach_knowledge_artifacts_for_contract
from packages.core.models.execution import ExecutionStep
from packages.core.models.worker import WorkLease
from packages.core.workers import internal
from packages.core.plans.executor import PlanExecutor
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.service import persisted_plan_contract_gaps
from packages.core.contracts.task_output import (
    TaskOutputProtocolError,
    TaskOutputValueKind,
    output_contract_for_schema,
    plan_output_contract_schema,
    task_output_envelope_schema,
)
from packages.core.services.artifact_knowledge import ArtifactKnowledgeProjection
from packages.worker_sdk.client import ManorClient
from packages.worker_sdk.types import (
    HeartbeatRequest,
    LeaseResult,
    LeaseResultFactory,
    TaskOutputValueKind as WorkerTaskOutputValueKind,
)


@pytest.mark.parametrize("value", [[1, 2], "ready", None])
async def test_action_worker_keeps_native_provider_payload(monkeypatch, value) -> None:
    """MCP action results must not be rewritten to the legacy value envelope."""
    class _Adapter:
        async def simulate_tool(self, _action_key, _params):
            return {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(value)}],
            }

    monkeypatch.setattr(
        "packages.core.workers.internal.importlib.import_module",
        lambda _name: _Adapter(),
    )
    out = await internal._exec_action({
        "provider": "fake_provider",
        "action_key": "return_value",
        "execution_mode": "dry_run",
        "params": {},
    })
    assert out["result"] == value


def test_python_worker_coercion_keeps_native_json_values_and_presence() -> None:
    omitted = LeaseResultFactory.from_handler_output(None)
    assert "result" not in omitted.model_fields_set

    for value in ([1, 2], "ready", 7):
        result = LeaseResultFactory.from_handler_output(value)
        assert "result" in result.model_fields_set
        assert result.result == value

    for value in (
        {"result": 42, "status": "ok"},
        {"cost": 12, "currency": "USD"},
        {"task_output_value_kind": "business_label", "data": [1, 2]},
    ):
        result = LeaseResultFactory.from_handler_output(value)
        assert result.result == value

    explicit_null = LeaseResultFactory.from_handler_output(LeaseResult(result=None))
    assert "result" in explicit_null.model_fields_set
    assert explicit_null.result is None


def test_api_models_accept_bare_json_values_and_track_null_presence() -> None:
    omitted_complete = CompleteLeaseRequest(
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert "result" not in omitted_complete.model_fields_set
    assert "result" in CompleteLeaseRequest(
        result=None,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    ).model_fields_set
    assert CompleteLeaseRequest(
        result=[1, 2],
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    ).result == [1, 2]
    assert omitted_complete.task_output_value_kind is TaskOutputValueKind.TASK_PAYLOAD

    omitted = HeartbeatCompletedLease(
        lease_id="lease",
        status="done",
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    explicit_null = HeartbeatCompletedLease(
        lease_id="lease",
        status="done",
        result=None,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert "result" not in omitted.model_fields_set
    assert "result" in explicit_null.model_fields_set
    assert omitted.task_output_value_kind is TaskOutputValueKind.TASK_PAYLOAD

    declared = HeartbeatCompletedLease(
        lease_id="lease",
        status="done",
        result={"status": "failed"},
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
    )
    assert declared.task_output_value_kind is TaskOutputValueKind.STEP_RESULT_FAILURE


def test_v2_completion_rejects_missing_discriminator_without_shape_inference() -> None:
    schema = task_output_envelope_schema({
        "type": "object",
        "required": ["value"],
        "additionalProperties": False,
        "properties": {"value": {"type": "string"}},
    })
    envelope = {
        "status": "succeeded",
        "summary": "done",
        "outputs": {"data": {"value": "ok"}},
    }
    with pytest.raises(PydanticValidationError):
        CompleteLeaseRequest(result=envelope)
    with pytest.raises(PydanticValidationError):
        HeartbeatCompletedLease(
            lease_id="lease",
            status="done",
            result=envelope,
        )
    with pytest.raises(TaskOutputProtocolError):
        coerce_step_output_for_schema(
            schema,
            envelope,
            step_kind="subagent",
        )


@pytest.mark.parametrize(
    ("value", "declared_kind"),
    [
        (
            {"status": "failed", "summary": "blocked"},
            TaskOutputValueKind.STEP_RESULT_ENVELOPE,
        ),
        (
            {"status": "succeeded", "summary": "done", "outputs": {"data": {}}},
            TaskOutputValueKind.STEP_RESULT_FAILURE,
        ),
    ],
)
def test_v2_discriminator_must_match_control_status(value, declared_kind) -> None:
    schema = task_output_envelope_schema({"type": "object"})

    with pytest.raises(TaskOutputProtocolError):
        coerce_step_output_for_schema(
            schema,
            value,
            task_output_value_kind=declared_kind,
        )


def test_result_columns_remain_nullable_for_unfinished_rows() -> None:
    """Native JSON typing must not turn historically nullable columns NOT NULL."""
    assert ExecutionStep.__table__.c.result.nullable is True
    assert WorkLease.__table__.c.result.nullable is True


def test_python_client_keeps_explicit_null_but_omits_unset_result(monkeypatch) -> None:
    client = ManorClient("http://example.test", worker_id="w", secret="s")
    sent: list[dict] = []

    assert client._auth_headers()["Manor-Protocol-Version"] == "2"

    async def fake_post(path, *, json, **kwargs):
        sent.append(json)
        return None

    monkeypatch.setattr(client, "_post", fake_post)

    asyncio.run(client.complete_lease("lease", LeaseResult()))
    assert "result" not in sent[-1]
    assert sent[-1]["task_output_value_kind"] == "task_payload"

    asyncio.run(client.complete_lease("lease", LeaseResult(result=None)))
    assert "result" in sent[-1] and sent[-1]["result"] is None

    asyncio.run(client.complete_lease("lease", LeaseResult(result=[1, 2])))
    assert sent[-1]["result"] == [1, 2]

    asyncio.run(client.complete_lease(
        "lease",
        LeaseResult(
            result={"status": "failed"},
            task_output_value_kind=WorkerTaskOutputValueKind.STEP_RESULT_FAILURE,
        ),
    ))
    assert sent[-1]["task_output_value_kind"] == "step_result_failure"


def test_python_client_restores_explicit_null_in_heartbeat_payload(monkeypatch) -> None:
    client = ManorClient("http://example.test", worker_id="w", secret="s")
    sent: list[dict] = []

    async def fake_post(path, *, json, **kwargs):
        sent.append(json)
        return {
            "server_time": "2026-08-18T00:00:00Z",
            "next_heartbeat_in_seconds": 2,
            "new_leases": [],
            "instructions": [],
        }

    monkeypatch.setattr(client, "_post", fake_post)
    req = HeartbeatRequest(
        completed_since_last=[
            # Explicit null is a valid bare result; omission is represented by
            # leaving the field out of the model constructor.
            {
                "lease_id": "null",
                "status": "done",
                "result": None,
                "task_output_value_kind": "task_payload",
            },
            {
                "lease_id": "omitted",
                "status": "done",
                "task_output_value_kind": "task_payload",
            },
        ]
    )
    asyncio.run(client.heartbeat(req))
    completions = sent[-1]["completed_since_last"]
    assert completions[0]["result"] is None
    assert "result" not in completions[1]
    assert all(item["task_output_value_kind"] == "task_payload" for item in completions)


class _EmptyDbResult:
    def scalar_one_or_none(self):
        return None

    def first(self):
        return None


class _NoopDb:
    async def execute(self, _statement):
        return _EmptyDbResult()

    def add(self, _row):
        return None

    async def commit(self):
        return None


def test_complete_endpoint_passes_task_output_kind_to_dispatcher(monkeypatch) -> None:
    from apps.api.routers import workers as workers_api
    from packages.core.plans import wakeup

    captured: list[dict] = []

    class _Dispatcher:
        async def complete_lease(self, _db, _lease_id, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(plan_id="plan", step_id="step", worker_id="worker")

    async def _allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workers_api, "Dispatcher", _Dispatcher)
    monkeypatch.setattr(workers_api, "_ensure_lease_belongs_to_worker", _allow)
    monkeypatch.setattr(wakeup, "wake_plan_cycle", lambda _plan_id: None)

    asyncio.run(workers_api.lease_complete(
        "lease",
        workers_api.CompleteLeaseRequest(
            result={"status": "failed"},
            task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
        ),
        worker=SimpleNamespace(id="worker"),
        db=_NoopDb(),
    ))

    assert captured[0]["task_output_value_kind"] is TaskOutputValueKind.STEP_RESULT_FAILURE

    asyncio.run(workers_api.lease_complete(
        "payload-lease",
        workers_api.CompleteLeaseRequest(
            result={"value": "business payload"},
            task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
        ),
        worker=SimpleNamespace(id="worker"),
        db=_NoopDb(),
    ))

    assert captured[1]["task_output_value_kind"] is TaskOutputValueKind.TASK_PAYLOAD


def test_heartbeat_passes_task_output_kind_to_dispatcher(monkeypatch) -> None:
    from apps.api.routers import workers as workers_api
    from packages.core.models.worker import Worker
    from packages.core.plans import wakeup

    captured: list[dict] = []

    class _Dispatcher:
        async def complete_lease(self, _db, _lease_id, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(plan_id="plan", step_id="step", worker_id="worker")

    monkeypatch.setattr(workers_api, "Dispatcher", _Dispatcher)
    monkeypatch.setattr(wakeup, "wake_plan_cycle", lambda _plan_id: None)
    worker = Worker(
        id="worker",
        entity_id="entity",
        kind="external",
        display_name="External worker",
        status="active",
        capabilities={},
    )
    request = workers_api.HeartbeatRequest(
        completed_since_last=[
            workers_api.HeartbeatCompletedLease(
                lease_id="lease",
                status="done",
                result={"status": "failed"},
                task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
            ),
            workers_api.HeartbeatCompletedLease(
                lease_id="payload-lease",
                status="done",
                result={"value": "business payload"},
                task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
            ),
        ],
        capacity=workers_api.HeartbeatCapacity(can_accept_leases=0),
    )

    asyncio.run(workers_api._heartbeat_inner(request, worker, _NoopDb()))

    assert captured[0]["task_output_value_kind"] is TaskOutputValueKind.STEP_RESULT_FAILURE
    assert captured[1]["task_output_value_kind"] is TaskOutputValueKind.TASK_PAYLOAD


def test_plan_executor_keeps_nullable_bare_null_for_downstream_refs() -> None:
    schema = plan_output_contract_schema({"type": "null"})
    step = SimpleNamespace(
        step_key="nullable",
        step_status="done",
        expected_output_schema=schema,
        result=None,
    )
    assert PlanExecutor._collect_prior_results([step]) == {"nullable": None}

    PlanExecutor._mark_done(step, None, None)
    assert step.result is None


def test_plan_executor_keeps_nullable_unmarked_action_null_for_downstream_refs() -> None:
    """Hydrated provider schemas can predate provenance markers.

    They are still native action payloads, so a valid JSON ``null`` must stay
    present in the prior-result map instead of disappearing as a legacy empty
    envelope.
    """
    schema = {"type": "null"}
    step = SimpleNamespace(
        step_key="action_nullable",
        kind="action",
        step_status="done",
        expected_output_schema=schema,
        result=None,
    )
    contract = output_contract_for_schema(schema)
    assert contract.preserves_native_payload is True
    assert PlanExecutor._collect_prior_results([step]) == {"action_nullable": None}

    PlanExecutor._mark_done(step, None, None)
    assert step.result is None


def test_native_contract_knowledge_projection_only_adds_declared_fields() -> None:
    """Knowledge indexing must not widen a strict bare payload after validation."""
    projection = ArtifactKnowledgeProjection(
        refs=[],
        knowledge_artifacts=[
            {
                "type": "document",
                "name": "packet.txt",
                "document_id": "doc_1",
                "viewer_url": "/documents/doc_1",
            }
        ],
        failures=[],
    )
    strict = output_contract_for_schema(
        plan_output_contract_schema(
            {
                "type": "object",
                "required": ["packet"],
                "additionalProperties": False,
                "properties": {"packet": {"type": "string"}},
            }
        )
    )
    assert _attach_knowledge_artifacts_for_contract(
        {"packet": "ok"}, projection, output_contract=strict,
    ) == {"packet": "ok"}

    declared = output_contract_for_schema(
        plan_output_contract_schema(
            {
                "type": "object",
                "required": ["packet"],
                "properties": {
                    "packet": {"type": "string"},
                    "knowledge_artifacts": {"type": "array"},
                },
            }
        )
    )
    enriched = _attach_knowledge_artifacts_for_contract(
        {"packet": "ok"}, projection, output_contract=declared,
    )
    assert enriched["packet"] == "ok"
    assert enriched["knowledge_artifacts"][0]["document_id"] == "doc_1"


def test_dispatcher_turns_hard_result_omission_into_controlled_schema_failure() -> None:
    dispatcher = Dispatcher()
    lease = SimpleNamespace(step_id="step", result=None)
    step = SimpleNamespace(
        step_key="deliver",
        kind="subagent",
        expected_output_schema=plan_output_contract_schema({"type": "null"}),
        result=None,
    )
    failed: dict = {}

    async def get_lease(_db, _lease_id):
        return lease

    async def get_step(_db, _step_id):
        return step

    async def fail_lease(_db, _lease_id, *, error, **_kwargs):
        failed.update(error)
        return lease

    dispatcher._get_active_lease = get_lease
    dispatcher._get_step = get_step
    dispatcher.fail_lease = fail_lease

    asyncio.run(
        dispatcher._complete_lease_inner(
            None,
            "lease",
            result=MISSING_RESULT,
            cost=None,
            evidence_refs=None,
            metadata=None,
        )
    )
    assert failed["type"] == "OutputSchemaError"
    assert "omitted" in failed["message"]


def test_persisted_legacy_plan_with_new_task_schema_is_replanned() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="deliver",
                kind="subagent",
                service_key="content_creation",
                params={"prompt": "Return the deliverable."},
            )
        ]
    )
    gaps = persisted_plan_contract_gaps(
        plan.model_dump(mode="json"),
        task_expected_output={
            "type": "object",
            "required": ["packet"],
            "properties": {"packet": {"type": "string"}},
        },
    )
    assert any(gap.kind == "task_output_contract" for gap in gaps)


def test_persisted_task_contract_must_bind_the_terminal_agent_step() -> None:
    """A marker on an upstream agent must not satisfy the task deliverable gate."""
    envelope = task_output_envelope_schema({"type": "string"})
    plan = Plan(
        steps=[
            PlanStep(
                key="research",
                kind="subagent",
                service_key="content_creation",
                expected_output_schema=envelope,
                params={"prompt": "Research."},
            ),
            PlanStep(
                key="deliver",
                kind="llm",
                service_key="content_creation",
                depends_on=["research"],
                params={"prompt": "Return the final deliverable."},
            ),
        ]
    )
    gaps = persisted_plan_contract_gaps(
        plan.model_dump(mode="json"),
        task_expected_output={"type": "string"},
    )
    assert any(gap.kind == "task_output_contract" for gap in gaps)
