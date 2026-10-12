from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.api.routers.workers import _build_lease_dto_for_worker
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.dispatcher.action_params import executable_action_params
from packages.core.dispatcher.service import (
    CoordinatorCompletionRejected,
    Dispatcher,
    _expired_external_action_attempt_evidence,
    _resolve_subscription_for_step,
)
from packages.core.dispatcher.validation import SchemaError, validate_step_input
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import AgentSubscription
from packages.core.plans.coordinator import (
    coordinator_child_idempotency_key,
    coordinator_child_step_key,
)
from packages.core.workers.coordinator_tools import (
    _stored_child_response,
    acknowledge_coordinator_child_handoffs,
)
from packages.core.workers.transcript import build_worker_transcript_checkpoint
from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval


def _worker(entity_id: str, *, kind: str, supported_kind: str) -> Worker:
    return Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind=kind,
        display_name=f"{kind} worker",
        capabilities={
            "supported_kinds": [supported_kind],
            "supported_providers": None,
            "max_risk_level": "high",
        },
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("released", "expected_count"),
    [(None, 0), (False, 0), (True, 1)],
)
async def test_typed_child_checkout_requires_explicit_release(
    db_session,
    released,
    expected_count,
) -> None:
    entity_id = generate_ulid()
    await initialize_test_account_approval(db_session, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    plan_id = generate_ulid()
    worker = _worker(entity_id, kind="internal", supported_kind="subagent")
    params = {
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
        COORDINATOR_CHILD_ITEM_PARAM_KEY: "research",
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: "stable-child",
        "source_contract": {"kind": "subagent"},
        "prompt": "Research",
    }
    if released is not None:
        params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] = released
    child = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="coordinate__research",
        kind="subagent",
        params=params,
        depends_on=[],
        step_status=ExecutionStepStatus.PENDING.value,
        risk_level="low",
        attempt_count=0,
        max_attempts=3,
    )
    db_session.add_all([
        worker,
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            status="running",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ),
        child,
    ])
    await db_session.flush()

    leased = await Dispatcher().checkout_steps_for_worker(
        db_session,
        worker,
        max_n=1,
        plan_id=plan_id,
    )

    assert len(leased) == expected_count
    if released is not True:
        assert child.step_status == ExecutionStepStatus.PENDING.value
        assert child.attempt_count == 0
        assert child.current_lease_id is None
    else:
        assert leased[0][1].id == child.id


@pytest.mark.asyncio
async def test_coordinator_root_is_internal_worker_only(db_session) -> None:
    entity_id = generate_ulid()
    await initialize_test_account_approval(db_session, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    plan_id = generate_ulid()
    external = _worker(entity_id, kind="custom_http", supported_kind="subagent")
    internal = _worker(entity_id, kind="internal", supported_kind="subagent")
    root = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="coordinate",
        kind="subagent",
        params={COORDINATOR_PARAM_KEY: True, "checklist": []},
        depends_on=[],
        step_status=ExecutionStepStatus.PENDING.value,
        risk_level="low",
        attempt_count=0,
        max_attempts=3,
    )
    db_session.add_all([
        external,
        internal,
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            status="running",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ),
        root,
    ])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(
        db_session,
        external,
        max_n=1,
        plan_id=plan_id,
    ) == []
    assert root.step_status == ExecutionStepStatus.PENDING.value
    assert root.current_lease_id is None

    leased = await Dispatcher().checkout_steps_for_worker(
        db_session,
        internal,
        max_n=1,
        plan_id=plan_id,
    )
    assert len(leased) == 1
    assert leased[0][1].id == root.id


async def _seed_coordinator_completion_graph(
    db,
    *,
    child_count: int = 1,
    worker_kind: str = "internal",
) -> tuple[ExecutionStep, WorkLease, list[ExecutionStep]]:
    entity_id = generate_ulid()
    await initialize_test_account_approval(db, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    plan_id = generate_ulid()
    worker = _worker(entity_id, kind=worker_kind, supported_kind="subagent")
    lease_id = generate_ulid()
    now = datetime.now(timezone.utc)
    checklist: list[dict] = []
    children: list[ExecutionStep] = []
    for index in range(child_count):
        item_key = f"item_{index}"
        contract = {
            "key": item_key,
            "delegation_key": item_key,
            "kind": "subagent",
            "service_key": "",
            "description": f"Do item {index}",
            "params": {"prompt": f"Do item {index}"},
            "prompt": f"Do item {index}",
            "depends_on": [],
            "expects": [],
            "expected_input_schema": None,
            "expected_output_schema": None,
            "output_shape": None,
            "risk_level": "low",
            "requires_approval": False,
            "max_attempts": 3,
            "skill_refs": [],
            "provider": None,
            "action_key": None,
            "capability_id": None,
            "integration_id": None,
        }
        checklist.append(contract)
        children.append(ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key=coordinator_child_step_key("coordinate", item_key),
            kind="subagent",
            params={
                "source_contract": contract,
                COORDINATOR_CHILD_PARAM_KEY: True,
                COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
                COORDINATOR_CHILD_RELEASED_PARAM_KEY: True,
                COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
                COORDINATOR_CHILD_ITEM_PARAM_KEY: item_key,
                COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: (
                    coordinator_child_idempotency_key(
                        plan_id,
                        "coordinate",
                        item_key,
                    )
                ),
            },
            depends_on=[],
            step_status=ExecutionStepStatus.DONE.value,
            risk_level="low",
            attempt_count=1,
            max_attempts=3,
            result={"value": index},
            finished_at=now,
        ))
    root = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="coordinate",
        kind="subagent",
        params={
            COORDINATOR_PARAM_KEY: True,
            COORDINATOR_VERSION_PARAM_KEY: COORDINATOR_TYPED_VERSION,
            "checklist": checklist,
        },
        depends_on=[],
        step_status=ExecutionStepStatus.RUNNING.value,
        risk_level="low",
        attempt_count=1,
        max_attempts=3,
        current_lease_id=lease_id,
        started_at=now,
    )
    lease = WorkLease(
        id=lease_id,
        step_id=root.id,
        plan_id=plan_id,
        entity_id=entity_id,
        worker_id=worker.id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=now + timedelta(minutes=5),
        execution_claim_id=generate_ulid(),
    )
    db.add_all([
        worker,
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            status="running",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ),
        root,
        *children,
        lease,
    ])
    await db.flush()
    return root, lease, children


async def _ack_children(db, root: ExecutionStep, children: list[ExecutionStep]) -> None:
    calls = []
    messages: list[dict] = [{"role": "user", "content": "coordinate"}]
    for index, child in enumerate(children):
        item_key = child.params[COORDINATOR_CHILD_ITEM_PARAM_KEY]
        call_id = f"delegate-{index}"
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": DELEGATE_TO_SERVICE_TOOL_NAME,
                "arguments": json.dumps({"checklist_item_key": item_key}),
            },
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _stored_child_response(child),
        })
    messages.insert(1, {"role": "assistant", "content": "", "tool_calls": calls})
    root.runtime_checkpoint = build_worker_transcript_checkpoint(
        messages,
        rounds=1,
        usage={},
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME],
    )
    await db.flush()
    assert await acknowledge_coordinator_child_handoffs(db, root, messages) == len(children)
    await db.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["running", "failed", "missing_ack", "source_contract"])
async def test_coordinator_completion_rejects_invalid_child_without_writes(
    db_session,
    mutation,
) -> None:
    root, lease, children = await _seed_coordinator_completion_graph(db_session)
    await _ack_children(db_session, root, children)
    child = children[0]
    if mutation in {"running", "failed"}:
        child.step_status = mutation
    elif mutation == "missing_ack":
        params = dict(child.params)
        params.pop(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY, None)
        child.params = params
    else:
        params = dict(child.params)
        params.pop("source_contract", None)
        child.params = params
    await db_session.flush()

    with pytest.raises(CoordinatorCompletionRejected):
        await Dispatcher().complete_lease(
            db_session,
            lease.id,
            result={"status": "succeeded"},
            cost={"usd": 1},
            evidence_refs=[{"kind": "test"}],
        )

    assert root.step_status == ExecutionStepStatus.RUNNING.value
    assert root.current_lease_id == lease.id
    assert root.result is None
    assert root.evidence_refs in (None, [])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.result is None
    assert lease.evidence_refs in (None, [])


@pytest.mark.asyncio
async def test_coordinator_completion_requires_internal_worker(db_session) -> None:
    root, lease, _children = await _seed_coordinator_completion_graph(
        db_session,
        worker_kind="custom_http",
    )

    with pytest.raises(CoordinatorCompletionRejected):
        await Dispatcher().complete_lease(
            db_session,
            lease.id,
            result={"status": "succeeded"},
        )
    assert root.step_status == ExecutionStepStatus.RUNNING.value
    assert lease.status == WorkLeaseStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_coordinator_completion_accepts_two_exact_handoffs(db_session) -> None:
    root, lease, children = await _seed_coordinator_completion_graph(
        db_session,
        child_count=2,
    )
    await _ack_children(db_session, root, children)

    completed = await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result={"status": "succeeded", "summary": "all done"},
    )

    assert completed.status == WorkLeaseStatus.COMPLETED.value
    assert root.step_status == ExecutionStepStatus.DONE.value
    assert all(
        child.step_status == ExecutionStepStatus.DONE.value for child in children
    )


def test_typed_action_uses_one_executable_params_projection() -> None:
    stored = {
        "message": "publish once",
        "payload": {"_nested": "provider-owned", "source_contract": "nested"},
        "source_contract": {"kind": "action"},
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
        COORDINATOR_CHILD_RELEASED_PARAM_KEY: True,
        ACTION_EFFECT_GENERATION_PARAM_KEY: 7,
        "_private": "runtime-only",
    }
    expected = {
        "message": "publish once",
        "payload": {"_nested": "provider-owned", "source_contract": "nested"},
        "human_input_response": {"choice": "approve"},
    }
    assert executable_action_params(
        stored,
        human_input_response={"choice": "approve"},
    ) == expected

    step = SimpleNamespace(
        id="step-1",
        step_key="publish",
        plan_id="plan-1",
        workspace_id=None,
        params=stored,
        human_input_response={"choice": "approve"},
        resolved_subscription_id=None,
        service_key=None,
        kind="action",
        provider="example",
        action_key="publish",
        capability_id="external.publish",
        integration_id=None,
        expected_input_schema=None,
        expected_output_schema=None,
        risk_level="low",
        attempt_count=7,
    )
    lease = SimpleNamespace(
        id="lease-1",
        subscription_id=None,
        execution_claim_id="claim-1",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit_usd=None,
    )
    dto = _build_lease_dto_for_worker(
        lease=lease,
        step=step,
        plan=SimpleNamespace(execution_mode="live"),
    )
    assert dto.params == expected

    evidence = _expired_external_action_attempt_evidence(lease, step)
    expected_hash = hashlib.sha256(json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    assert evidence["fields"]["arguments_hash"] == expected_hash

    schema_step = SimpleNamespace(
        step_key="publish",
        expected_input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "payload": {"type": "object"},
                "human_input_response": {"type": "object"},
            },
            "required": ["message", "payload", "human_input_response"],
            "additionalProperties": False,
        },
    )
    validate_step_input(schema_step, expected)
    with pytest.raises(SchemaError):
        validate_step_input(schema_step, stored)
    assert stored["source_contract"] == {"kind": "action"}
    assert stored[ACTION_EFFECT_GENERATION_PARAM_KEY] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["missing", "entity", "workspace", "service", "agent", "inactive"],
)
async def test_persisted_subscription_pin_never_falls_back(
    db_session,
    mismatch,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    pinned_id = generate_ulid()
    local_fallback = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=generate_ulid(),
        workspace_id=workspace_id,
        name="Fallback agent",
        service_key="writer",
        status="active",
    )
    db_session.add(local_fallback)
    if mismatch != "missing":
        db_session.add(AgentSubscription(
            id=pinned_id,
            entity_id=(generate_ulid() if mismatch == "entity" else entity_id),
            agent_id=(generate_ulid() if mismatch == "agent" else agent_id),
            workspace_id=(
                generate_ulid() if mismatch == "workspace" else workspace_id
            ),
            name="Pinned agent",
            service_key=("reviewer" if mismatch == "service" else "writer"),
            status=("paused" if mismatch == "inactive" else "active"),
        ))
    await db_session.flush()

    resolved = await _resolve_subscription_for_step(
        db_session,
        SimpleNamespace(
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="writer",
            resolved_subscription_id=pinned_id,
            resolved_agent_id=agent_id,
        ),
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_persisted_subscription_pin_requires_exact_agent(db_session) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    pinned = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        name="Pinned agent",
        service_key="writer",
        status="active",
    )
    db_session.add(pinned)
    await db_session.flush()

    resolved = await _resolve_subscription_for_step(
        db_session,
        SimpleNamespace(
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="writer",
            resolved_subscription_id=pinned.id,
            resolved_agent_id=agent_id,
        ),
    )

    assert resolved is not None
    assert resolved.id == pinned.id
