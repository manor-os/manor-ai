"""Coordinator delegation tool (Section W8).

A coordinator step runs one agent that delegates checklist items to workspace
services. Every delegation is materialised as a child ``ExecutionStep`` row
(so the run is inspectable and billable per service) and the delegate's JSON
is handed back to the model unchanged.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_INPUT_CONTEXT_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.ai.runtime.control import (
    RuntimeAgentCheckpoint,
    RuntimeToolSuspension,
)
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.coordinator import (
    coordinator_child_idempotency_key,
    coordinator_child_step_key,
)
from packages.core.workers import coordinator_tools, transcript
from packages.core.workers import internal as worker_internal

_DELEGATION_TARGET = "packages.core.ai.runtime.workspace_delegation_actions.runtime_workspace_delegate_service_action"


# ── tool schema ───────────────────────────────────────────────────────


def test_delegate_tool_schema_enumerates_the_allowed_services():
    tool = coordinator_tools.build_delegate_service_tool(["research", "writer"])

    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == DELEGATE_TO_SERVICE_TOOL_NAME
    props = fn["parameters"]["properties"]
    assert props["service_key"]["type"] == "string"
    assert props["service_key"]["enum"] == ["research", "writer"]
    assert props["checklist_item_key"]["type"] == "string"
    assert props["prompt"]["type"] == "string"
    assert props["max_rounds"]["type"] == "integer"
    assert "approval_token" not in props, "resume authority is server-owned"
    assert fn["parameters"]["required"] == ["checklist_item_key"]


def test_delegate_tool_schema_without_services_has_no_enum():
    props = coordinator_tools.build_delegate_service_tool([])["function"]["parameters"]["properties"]
    assert "enum" not in props["service_key"]


# ── handler ───────────────────────────────────────────────────────────


async def _seed_parent(db) -> dict:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    parent_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    execution_claim_id = generate_ulid()
    now = datetime.now(timezone.utc)
    checklist = [
        {
            "key": "research", "delegation_key": "research", "kind": "subagent",
            "service_key": "research", "description": "Research competitors",
            "params": {"prompt": "Find competitors"}, "prompt": "Find competitors",
            "depends_on": [], "expects": ["files"], "expected_input_schema": None,
            "expected_output_schema": {
                "type": "object", "required": ["competitors"],
                "properties": {"competitors": {"type": "integer"}},
            },
            "output_shape": None, "risk_level": "medium", "requires_approval": False,
            "max_attempts": 3, "skill_refs": ["web-research"], "provider": None,
            "action_key": None, "capability_id": None, "integration_id": None,
        },
        {
            "key": "send", "delegation_key": "send", "kind": "action",
            "service_key": "writer", "description": "Send the report",
            "params": {"to": "ops@example.com"}, "prompt": None,
            "depends_on": ["research"], "expects": ["publish"],
            "expected_input_schema": None, "expected_output_schema": None,
            "output_shape": None, "risk_level": "high", "requires_approval": True,
            "max_attempts": 2, "skill_refs": [], "provider": "gmail",
            "action_key": "send_email", "capability_id": "external.email",
            "integration_id": "gmail-1",
        },
    ]
    db.add_all([
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, workspace_id=workspace_id, status="running",
            execution_mode="live", approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=parent_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
            step_key="coordinate", kind="subagent",
            params={"prompt": "x", "checklist": checklist, COORDINATOR_PARAM_KEY: True},
            depends_on=[], step_status="running", risk_level="low", attempt_count=1, max_attempts=1,
            current_lease_id=lease_id,
        ),
        Worker(
            id=worker_id, entity_id=entity_id, kind="internal",
            display_name="Internal worker",
            capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True, status="active",
        ),
        WorkLease(
            id=lease_id, step_id=parent_id, plan_id=plan_id, entity_id=entity_id,
            workspace_id=workspace_id, worker_id=worker_id,
            status=WorkLeaseStatus.ACTIVE.value,
            lease_until=now + timedelta(minutes=5),
            execution_claim_id=execution_claim_id,
        ),
    ])
    await db.commit()
    return {
        "plan_id": plan_id, "entity_id": entity_id, "workspace_id": workspace_id,
        "step_id": parent_id, "step_key": "coordinate", "user_id": "user-1",
        "conversation_id": None, "task_id": "task-1",
        "lease_id": lease_id, "execution_claim_id": execution_claim_id,
        "params": {"prompt": "x", "checklist": checklist, COORDINATOR_PARAM_KEY: True},
    }


async def _children(db, plan_id: str) -> list[ExecutionStep]:
    db.expire_all()
    rows = (await db.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan_id).order_by(ExecutionStep.step_key)
    )).scalars().all()
    return [row for row in rows if (row.params or {}).get(COORDINATOR_CHILD_PARAM_KEY)]


async def _seed_typed_child(
    db,
    snapshot: dict,
    *,
    item_key: str = "research",
    status: str = ExecutionStepStatus.PAUSED.value,
    released: bool = False,
    result=None,
    evidence_refs=None,
    cost=None,
    error=None,
) -> ExecutionStep:
    parent = await db.get(ExecutionStep, snapshot["step_id"])
    contract = next(
        item
        for item in parent.params["checklist"]
        if item["delegation_key"] == item_key
    )
    child = ExecutionStep(
        id=generate_ulid(),
        plan_id=snapshot["plan_id"],
        entity_id=snapshot["entity_id"],
        workspace_id=snapshot["workspace_id"],
        step_key=coordinator_child_step_key(snapshot["step_key"], item_key),
        kind=contract["kind"],
        service_key=contract["service_key"],
        provider=contract["provider"],
        action_key=contract["action_key"],
        capability_id=contract["capability_id"],
        integration_id=contract["integration_id"],
        params={
            "source_contract": contract,
            COORDINATOR_CHILD_PARAM_KEY: True,
            COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
            COORDINATOR_CHILD_RELEASED_PARAM_KEY: released,
            COORDINATOR_PARENT_STEP_PARAM_KEY: snapshot["step_key"],
            COORDINATOR_CHILD_ITEM_PARAM_KEY: item_key,
            COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: (
                coordinator_child_idempotency_key(
                    snapshot["plan_id"], snapshot["step_key"], item_key,
                )
            ),
            ACTION_EFFECT_GENERATION_PARAM_KEY: 0,
        },
        expected_input_schema=contract["expected_input_schema"],
        expected_output_schema=contract["expected_output_schema"],
        depends_on=[],
        step_status=status,
        max_attempts=contract["max_attempts"],
        risk_level=contract["risk_level"],
        requires_approval=contract["requires_approval"],
        result=result,
        evidence_refs=evidence_refs or [],
        cost=cost or {},
        error=error,
        finished_at=(
            datetime.now(timezone.utc)
            if status in {
                ExecutionStepStatus.DONE.value,
                ExecutionStepStatus.FAILED.value,
                ExecutionStepStatus.SKIPPED.value,
                ExecutionStepStatus.CANCELLED.value,
            }
            else None
        ),
    )
    db.add(child)
    await db.commit()
    return child


@pytest.mark.asyncio
async def test_v2_missing_typed_child_fails_closed_without_legacy_fallback(db_session):
    snapshot = await _seed_parent(db_session)
    parent = await db_session.get(ExecutionStep, snapshot["step_id"])
    parent.params = {
        **parent.params,
        COORDINATOR_VERSION_PARAM_KEY: COORDINATOR_TYPED_VERSION,
    }
    snapshot["params"] = dict(parent.params)
    await db_session.commit()

    response = json.loads(
        await coordinator_tools.make_delegate_service_handler(snapshot)(
            {"checklist_item_key": "research"}
        )
    )

    assert response["error"] == "typed_child_release_failed"
    assert "is missing" in response["message"]
    assert await _children(db_session, snapshot["plan_id"]) == []


def _delegated(**overrides) -> str:
    payload = {
        "delegated": True, "run_id": "run-1", "status": "completed",
        "service": {"service_key": "research"},
        "content": '{"competitors":3}', "result": {"competitors": 3},
        "evidence_refs": [{"kind": "artifact", "document_id": "doc-1"}],
        "artifacts": [{"type": "knowledge_document", "document_id": "doc-1"}],
        "knowledge_refs": [{"type": "knowledge_document", "document_id": "doc-1"}],
        "rounds": 2, "tool_calls_made": ["web_search"],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "stop_reason": "completed", "error": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_v2_item_key_only_releases_typed_child_and_suspends_parent_turn(
    db_session,
):
    snapshot = await _seed_parent(db_session)
    child = await _seed_typed_child(db_session, snapshot)

    returned = await coordinator_tools.make_delegate_service_handler(snapshot)({
        "checklist_item_key": "research",
    })

    assert isinstance(returned, RuntimeToolSuspension)
    assert returned.kind == "waiting_child"
    assert returned.reservation_id == child.id
    assert returned.reason == "awaiting_coordinator_child"
    assert returned.payload == {
        "child_step_id": child.id,
        "child_step_key": child.step_key,
        "checklist_item_key": "research",
    }
    child_id = child.id
    db_session.expire_all()
    released = await db_session.get(ExecutionStep, child_id)
    parent = await db_session.get(ExecutionStep, snapshot["step_id"])
    assert released.step_status == ExecutionStepStatus.PENDING.value
    assert released.params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] is True
    assert released.params["prompt"] == "Find competitors"
    assert parent.params[COORDINATOR_WAITING_CHILD_PARAM_KEY] == {
        "version": 1,
        "child_step_id": child.id,
        "child_step_key": child.step_key,
        "checklist_item_key": "research",
    }


@pytest.mark.asyncio
async def test_v2_terminal_typed_child_replays_complete_durable_envelope(
    db_session,
):
    snapshot = await _seed_parent(db_session)
    child = await _seed_typed_child(
        db_session,
        snapshot,
        status=ExecutionStepStatus.DONE.value,
        released=True,
        result={"competitors": 3},
        evidence_refs=[{"kind": "artifact", "document_id": "doc-1"}],
        cost={"usd": "0.12"},
    )
    parent = await db_session.get(ExecutionStep, snapshot["step_id"])
    parent.params = {
        **parent.params,
        COORDINATOR_WAITING_CHILD_PARAM_KEY: {
            "version": 1,
            "child_step_id": child.id,
            "child_step_key": child.step_key,
            "checklist_item_key": "research",
        },
    }
    await db_session.commit()

    handler = coordinator_tools.make_delegate_service_handler(snapshot)
    first = await handler({"checklist_item_key": "research"})
    second = await handler({"checklist_item_key": "research"})

    assert first == second
    assert json.loads(first) == {
        "delegated": True,
        "status": "completed",
        "checklist_item_key": "research",
        "idempotency_key": coordinator_child_idempotency_key(
            snapshot["plan_id"], snapshot["step_key"], "research",
        ),
        "service_key": "research",
        "kind": "subagent",
        "result": {"competitors": 3},
        "evidence_refs": [{"kind": "artifact", "document_id": "doc-1"}],
        "cost": {"usd": "0.12"},
        "error": None,
    }
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, snapshot["step_id"])
    assert COORDINATOR_WAITING_CHILD_PARAM_KEY not in parent.params


@pytest.mark.asyncio
async def test_internal_parent_park_suspends_lease_and_refunds_attempt(
    db_session,
):
    snapshot = await _seed_parent(db_session)
    child = await _seed_typed_child(db_session, snapshot)
    suspension = await coordinator_tools.make_delegate_service_handler(snapshot)({
        "checklist_item_key": "research",
    })
    assert isinstance(suspension, RuntimeToolSuspension)
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "coordinate"}],
        usage={"prompt_tokens": 12},
        rounds=2,
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME],
        pending_tool_call={
            "id": "delegate-research",
            "name": DELEGATE_TO_SERVICE_TOOL_NAME,
            "arguments": {"checklist_item_key": "research"},
        },
    )
    loop_result = SimpleNamespace(
        stop_reason="waiting_resource",
        control={**suspension.to_dict(), "checkpoint": checkpoint.to_dict()},
    )
    wait = worker_internal._coordinator_child_pause_from_result(loop_result)
    assert wait is not None

    await worker_internal._park_coordinator_parent_for_child(
        db_session, snapshot, wait,
    )
    await db_session.flush()

    parent = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    assert parent.step_status == ExecutionStepStatus.PAUSED.value
    assert parent.current_lease_id is None
    assert parent.attempt_count == 0
    assert RuntimeAgentCheckpoint.from_dict(parent.runtime_checkpoint) == checkpoint
    assert lease.status == WorkLeaseStatus.SUSPENDED.value
    assert lease.execution_claim_id is None
    assert lease.execution_claimed_at is None
    assert lease.credential_leases == []
    assert (await db_session.get(ExecutionStep, child.id)).step_status == (
        ExecutionStepStatus.PENDING.value
    )
    wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == snapshot["plan_id"],
            RuntimeOutboxEvent.event_type == "runtime.dispatch_plan_cycle",
        )
    )).scalar_one()
    assert wake.delivered_at is None
    assert wake.payload["reason"] == "coordinator_parent_parked"


@pytest.mark.asyncio
async def test_done_child_is_replayed_until_parent_checkpoint_acks_handoff(
    db_session,
    monkeypatch,
):
    """Crash after child commit cannot let the parent submit unseen output."""

    from packages.core.ai.agentic_loop import _compact_tool_result_for_context

    s = await _seed_parent(db_session)
    parent = await db_session.get(ExecutionStep, s["step_id"])
    parent.params = {
        **parent.params,
        "checklist": [parent.params["checklist"][0]],
    }
    await db_session.commit()
    s["params"] = dict(parent.params)
    calls = 0

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        return _delegated(
            result={"competitors": 3, "notes": "x" * 9000},
            content="x" * 9000,
        )

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }
    returned = await handler(arguments)
    assert calls == 1
    assert "not yet been durably delivered" in (
        await coordinator_tools.coordinator_submission_error(s) or ""
    )

    forced = await coordinator_tools.coordinator_resume_forced_tool_calls(s)
    assert len(forced) == 1
    assert forced[0]["name"] == DELEGATE_TO_SERVICE_TOOL_NAME
    assert forced[0]["arguments"]["checklist_item_key"] == "research"
    assert forced[0]["arguments"]["service_key"] == "research"
    forced_arguments = forced[0]["arguments"]
    replayed = await handler(forced[0]["arguments"])
    assert json.loads(replayed) == json.loads(returned)
    assert calls == 1, "stored child replay must not re-enter the nested agent"

    compacted = _compact_tool_result_for_context(
        DELEGATE_TO_SERVICE_TOOL_NAME,
        replayed,
    )
    compacted_payload = json.loads(compacted)
    assert compacted_payload["_tool_result_truncated"]["sha256"]
    messages = [
        {"role": "user", "content": "coordinate"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "delegate-research",
                "type": "function",
                "function": {
                    "name": DELEGATE_TO_SERVICE_TOOL_NAME,
                    "arguments": json.dumps(forced_arguments),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "delegate-research",
            "content": compacted,
        },
    ]
    tampered = [dict(message) for message in messages]
    tampered_payload = dict(compacted_payload)
    tampered_payload["_tool_result_truncated"] = {
        **tampered_payload["_tool_result_truncated"],
        "sha256": "0" * 16,
    }
    tampered[-1] = {
        **tampered[-1],
        "content": json.dumps(tampered_payload),
    }
    await worker_internal._persist_worker_round_checkpoint(
        s,
        rounds=1,
        messages=tampered,
        usage={},
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME],
    )
    db_session.expire_all()
    (child,) = await _children(db_session, s["plan_id"])
    assert COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY not in child.params

    oversized = list(messages)
    for index in range(40):
        filler_id = f"filler-{index}"
        oversized.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": filler_id,
                    "type": "function",
                    "function": {"name": "inspect_filler", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": filler_id,
                "content": "z" * 8192,
            },
        ])
    await worker_internal._persist_worker_round_checkpoint(
        s,
        rounds=2,
        messages=oversized,
        usage={},
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME, "inspect_filler"],
    )
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    assert "delegate-research" in transcript.worker_transcript_dropped_tool_result_ids(
        parent.runtime_checkpoint
    )
    (child,) = await _children(db_session, s["plan_id"])
    assert COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY not in child.params

    await worker_internal._persist_worker_round_checkpoint(
        s,
        rounds=1,
        messages=messages,
        usage={},
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME],
    )

    db_session.expire_all()
    (child,) = await _children(db_session, s["plan_id"])
    assert child.params[COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY]["result_sha256"]
    assert await coordinator_tools.coordinator_submission_error(s) is None
    assert await coordinator_tools.coordinator_resume_forced_tool_calls(s) == []

    # A deadline/cancel/manual reset can intentionally discard the parent's
    # transcript while the durable child remains DONE.  The old ACK belongs to
    # the discarded checkpoint and must not let a fresh parent submit without
    # seeing that stored result again.
    parent = await db_session.get(ExecutionStep, s["step_id"])
    parent.runtime_checkpoint = None
    await db_session.commit()
    assert "not yet been durably delivered" in (
        await coordinator_tools.coordinator_submission_error(s) or ""
    )
    forced_after_reset = await coordinator_tools.coordinator_resume_forced_tool_calls(s)
    assert len(forced_after_reset) == 1
    replayed_after_reset = await handler(forced_after_reset[0]["arguments"])
    assert json.loads(replayed_after_reset) == json.loads(returned)
    assert calls == 1, "parent checkpoint reset must replay storage, not provider I/O"

    reset_messages = [
        {"role": "user", "content": "coordinate again"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "delegate-research-after-reset",
                "type": "function",
                "function": {
                    "name": DELEGATE_TO_SERVICE_TOOL_NAME,
                    "arguments": json.dumps(forced_after_reset[0]["arguments"]),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "delegate-research-after-reset",
            "content": _compact_tool_result_for_context(
                DELEGATE_TO_SERVICE_TOOL_NAME,
                replayed_after_reset,
            ),
        },
    ]
    await worker_internal._persist_worker_round_checkpoint(
        s,
        rounds=1,
        messages=reset_messages,
        usage={},
        tool_calls_made=[DELEGATE_TO_SERVICE_TOOL_NAME],
    )
    assert await coordinator_tools.coordinator_submission_error(s) is None


@pytest.mark.asyncio
async def test_legacy_numbered_child_blocks_new_stable_delegation(
    db_session,
    monkeypatch,
):
    """An upgrade must not run beside an unattributable pre-stable child."""

    s = await _seed_parent(db_session)
    db_session.add(ExecutionStep(
        id=generate_ulid(),
        plan_id=s["plan_id"],
        entity_id=s["entity_id"],
        workspace_id=s["workspace_id"],
        step_key="coordinate__research_1",
        kind="subagent",
        service_key="research",
        params={
            "prompt": "legacy",
            COORDINATOR_CHILD_PARAM_KEY: True,
            COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
        },
        depends_on=[],
        step_status=ExecutionStepStatus.DONE.value,
        result={"delegated": True, "status": "completed", "result": {"old": True}},
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        attempt_count=1,
        max_attempts=1,
        risk_level="low",
    ))
    await db_session.commit()
    calls = 0

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    response = json.loads(await coordinator_tools.make_delegate_service_handler(s)({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }))
    assert response["error"] == "delegation_claim_failed"
    assert "legacy coordinator child" in response["message"].lower()
    assert calls == 0
    assert len(await _children(db_session, s["plan_id"])) == 1


@pytest.mark.asyncio
async def test_delegation_creates_and_completes_a_child_step(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    calls: list[dict] = []

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    before = datetime.now(timezone.utc)
    returned = await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "list competitors",
        "max_rounds": 5,
    })

    returned_payload = json.loads(returned)
    expected_payload = json.loads(_delegated())
    assert {
        key: value for key, value in returned_payload.items() if key != "evidence_refs"
    } == {
        key: value for key, value in expected_payload.items() if key != "evidence_refs"
    }, "the typed delegate handoff reaches the model without semantic rewriting"
    assert len(calls) == 1
    call = calls[0]
    assert call["entity_id"] == s["entity_id"] and call["workspace_id"] == s["workspace_id"]
    assert call["user_id"] == "user-1" and call["conversation_id"] is None
    assert call["task_id"] == "task-1"
    assert call["params"]["service_key"] == "research"
    assert call["params"]["max_rounds"] is None
    assert call["params"]["delegation_idempotency_key"].startswith("coordinator-child-v1:")
    assert "Authoritative persisted checklist contract" in call["params"]["prompt"]
    assert call["output_schema"]["required"] == ["competitors"]
    assert call["initial_messages"] is None
    assert callable(call["on_round_end"])
    assert callable(call["on_service_resolved"])
    assert callable(call["on_human_input_suspension"])
    assert call["on_round_end_strict"] is True
    assert call["record_nested_usage"] is False

    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_key == "coordinate__research"
    assert child.kind == "subagent"
    assert child.service_key == "research"
    assert child.entity_id == s["entity_id"] and child.workspace_id == s["workspace_id"]
    assert "list competitors" in child.params["prompt"]
    assert child.params[COORDINATOR_CHILD_PARAM_KEY] is True
    assert child.params[COORDINATOR_PARENT_STEP_PARAM_KEY] == "coordinate"
    assert child.params["description"] == "Research competitors"
    assert child.params["source_contract"]["expects"] == ["files"]
    assert child.depends_on == []
    assert child.step_status == "done"
    assert child.attempt_count == 1 and child.max_attempts == 3
    assert child.risk_level == "medium" and child.requires_approval is False
    assert child.expected_output_schema["required"] == ["competitors"]
    assert child.started_at is not None and child.started_at >= before - __import__("datetime").timedelta(seconds=1)
    assert child.finished_at is not None
    assert child.result == returned_payload
    expected_evidence = [{
        "kind": "artifact",
        "document_id": "doc-1",
        "coordinator_child_step_id": child.id,
    }]
    assert returned_payload["evidence_refs"] == expected_evidence
    assert child.evidence_refs == expected_evidence
    assert child.cost == {
        "llm_tokens_input": 10,
        "llm_tokens_output": 5,
        "llm_rounds": 2,
        "tool_call_count": 1,
    }


@pytest.mark.asyncio
async def test_source_round_cap_cannot_be_raised_by_the_coordinator_model(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    s["params"]["checklist"][0]["params"]["max_rounds"] = 3
    calls: list[dict] = []

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "list competitors",
        "max_rounds": 99,
    })

    assert calls[0]["params"]["max_rounds"] == 3


@pytest.mark.asyncio
async def test_terminal_child_is_reused_by_stable_checklist_identity(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    calls = 0

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    first = await handler({
        "checklist_item_key": "research", "service_key": "research", "prompt": "one",
    })
    second = await handler({
        "checklist_item_key": "research", "service_key": "research", "prompt": "changed",
    })

    keys = sorted(child.step_key for child in await _children(db_session, s["plan_id"]))
    assert keys == ["coordinate__research"]
    assert calls == 1, "a terminal delegated child must never repeat its tools"
    assert json.loads(first) == json.loads(second)


@pytest.mark.asyncio
async def test_completed_child_missing_files_receipt_fails_and_can_retry(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    calls = 0
    invalid = _delegated(
        evidence_refs=[],
        # Typed/model output is not evidence by itself. Only receipts mined
        # from trusted child tool results may satisfy ``expects: files``.
        artifacts=[{"type": "file", "fs_path": "reports/claimed-only.pdf"}],
        knowledge_refs=[],
    )

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        return invalid if calls == 1 else _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "list competitors",
    }

    first = json.loads(await handler(arguments))

    assert first["status"] == "failed"
    assert first["error"] == "delegation_contract_unsatisfied"
    assert first["retryable"] is True
    assert "no saved artifact evidence" in first["message"]
    (failed_child,) = await _children(db_session, s["plan_id"])
    assert failed_child.step_status == "failed"
    assert failed_child.error["type"] == "DelegationContractUnsatisfied"

    second = json.loads(await handler(arguments))

    assert second["status"] == "completed"
    assert calls == 2
    (repaired_child,) = await _children(db_session, s["plan_id"])
    assert repaired_child.step_status == "done"
    assert repaired_child.attempt_count == 2


@pytest.mark.asyncio
async def test_completed_child_missing_publish_receipt_is_not_done(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)

    async def fake_delegate(**kwargs):
        if kwargs["params"]["service_key"] == "research":
            return _delegated()
        return _delegated(
            service={"service_key": "writer"},
            result={"sent": True},
            content='{"sent":true}',
            # An artifact is not a provider delivery receipt.
            evidence_refs=[{"kind": "artifact", "document_id": "draft-1"}],
        )

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "research first",
    })

    returned = json.loads(await handler({
        "checklist_item_key": "send",
        "service_key": "writer",
        "prompt": "send the report",
    }))

    assert returned["status"] == "failed"
    assert returned["error"] == "delegation_contract_unsatisfied"
    assert "no terminal provider publish/send receipt" in returned["message"]
    send_child = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    assert send_child.step_status == "failed"
    assert send_child.error["retryable"] is True


@pytest.mark.asyncio
async def test_invalid_legacy_done_child_is_revalidated_before_reuse(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    calls = 0
    invalid = _delegated(evidence_refs=[], artifacts=[], knowledge_refs=[])

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        return invalid if calls == 1 else _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "list competitors",
    }

    await handler(arguments)
    (child,) = await _children(db_session, s["plan_id"])
    # Simulate a row written by the earlier implementation, which accepted a
    # completed envelope without checking the source step's delivery contract.
    child.step_status = "done"
    child.result = json.loads(invalid)
    child.evidence_refs = []
    child.error = None
    child.finished_at = datetime.now(timezone.utc)
    await db_session.commit()

    repaired = json.loads(await handler(arguments))

    assert repaired["status"] == "completed"
    assert calls == 2, "an invalid DONE child must not be replayed as success"
    (repaired_child,) = await _children(db_session, s["plan_id"])
    assert repaired_child.step_status == "done"
    assert repaired_child.attempt_count == 2


@pytest.mark.asyncio
async def test_invalid_done_dependency_is_demoted_before_downstream_unlocks(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    calls: list[str] = []
    invalid = _delegated(evidence_refs=[], artifacts=[], knowledge_refs=[])

    async def fake_delegate(**kwargs):
        calls.append(kwargs["params"]["service_key"])
        return invalid

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "list competitors",
    })
    (child,) = await _children(db_session, s["plan_id"])
    child.step_status = "done"
    child.result = json.loads(invalid)
    child.evidence_refs = []
    child.error = None
    child.finished_at = datetime.now(timezone.utc)
    await db_session.commit()

    blocked = json.loads(await handler({
        "checklist_item_key": "send",
        "service_key": "writer",
        "prompt": "send the report",
    }))

    assert blocked["error"] == "delegation_claim_failed"
    assert "no longer satisfies its delivery contract" in blocked["message"]
    assert calls == ["research"], "downstream Runtime must not start"
    children = await _children(db_session, s["plan_id"])
    assert [row.step_key for row in children] == ["coordinate__research"]
    assert children[0].step_status == "failed"
    assert children[0].error["type"] == "DelegationContractUnsatisfied"


@pytest.mark.asyncio
async def test_interrupted_child_resumes_from_its_round_checkpoint(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    calls: list[dict] = []
    checkpoint_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "research"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "web_search", "arguments": '{"query":"ACME"}'},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": '{"results":["A"]}'},
    ]

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await kwargs["on_round_end"](
                1,
                checkpoint_messages,
                {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "cost_usd": 0.04,
                },
                ["web_search"],
            )
            raise asyncio.CancelledError
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research", "service_key": "research", "prompt": "go",
    }

    with pytest.raises(asyncio.CancelledError):
        await handler(arguments)
    (interrupted,) = await _children(db_session, s["plan_id"])
    assert interrupted.step_status == "running"
    assert interrupted.runtime_checkpoint["schema_version"] == "worker-transcript/1"
    assert interrupted.runtime_checkpoint["usage"]["prompt_tokens"] == 7
    assert interrupted.runtime_checkpoint["tool_calls_made"] == ["web_search"]
    assert interrupted.cost == {
        "llm_tokens_input": 7,
        "llm_tokens_output": 3,
        "llm_rounds": 1,
        "tool_call_count": 1,
        "usd": 0.04,
        "llm_usd": 0.04,
        "budget_usd": 0.0,
    }

    await handler(arguments)

    assert len(calls) == 2
    assert calls[1]["initial_messages"] == checkpoint_messages[1:]
    assert calls[1]["initial_rounds"] == 1
    assert calls[1]["initial_usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cost_usd": 0.04,
    }
    assert calls[1]["initial_tool_calls_made"] == ["web_search"]
    (resumed,) = await _children(db_session, s["plan_id"])
    assert resumed.step_status == "done" and resumed.attempt_count == 2
    assert resumed.runtime_checkpoint is None


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ["running", "failed"])
async def test_unconfirmed_child_failure_checkpoint_cannot_reopen_tools(
    db_session, monkeypatch, child_status,
):
    s = await _seed_parent(db_session)
    calls = 0

    async def interrupted(**kwargs):
        nonlocal calls
        calls += 1
        await kwargs["on_round_end"](
            2,
            [
                {"role": "user", "content": "research"},
                {"role": "assistant", "content": "The tool outcome is unknown."},
            ],
            {},
            ["ws_get_draft"],
            {"terminal_failure": "tool_outcome_unconfirmed"},
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(_DELEGATION_TARGET, interrupted)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research", "service_key": "research", "prompt": "go",
    }
    with pytest.raises(asyncio.CancelledError):
        await handler(arguments)

    (child,) = await _children(db_session, s["plan_id"])
    assert child.runtime_checkpoint["terminal_failure"] == "tool_outcome_unconfirmed"
    if child_status == "failed":
        child.step_status = "failed"
        await db_session.commit()

    blocked = json.loads(await handler(arguments))
    assert blocked["error"] == "delegated_tool_outcome_unconfirmed"
    assert blocked["stop_parent"] is True
    assert calls == 1
    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_status == "failed"


@pytest.mark.asyncio
async def test_child_request_input_parks_and_resumes_exact_pending_call(
    db_session,
    monkeypatch,
):
    from packages.core.ai.pending_action import PendingAction
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

    s = await _seed_parent(db_session)
    calls: list[dict] = []
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "research"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "input-1",
                    "type": "function",
                    "function": {
                        "name": "request_input",
                        "arguments": json.dumps({
                            "question": "Which market?",
                            "options": ["US", "EU"],
                        }),
                    },
                }],
            },
        ],
        usage={"prompt_tokens": 9, "completion_tokens": 2},
        rounds=1,
        tool_calls_made=["request_input"],
        pending_tool_call={
            "id": "input-1",
            "name": "request_input",
            "arguments": {
                "question": "Which market?",
                "options": ["US", "EU"],
            },
        },
    )
    pending_action = PendingAction.needs_input(
        questions=[{
            "name": "response",
            "label": "Which market?",
            "type": "select",
            "required": True,
            "options": ["US", "EU"],
        }],
        title="Which market?",
    ).to_dict()
    pause_payload = {
        "delegated": True,
        "status": "blocked",
        "blocker_kind": "request_input",
        "content": "Which market?",
        "result": {"question": "Which market?"},
        "human_input": {
            "question": "Which market?",
            "options": ["US", "EU"],
        },
        "_pending_action": pending_action,
        "usage": checkpoint.usage,
        "rounds": checkpoint.rounds,
        "tool_calls_made": checkpoint.tool_calls_made,
        "evidence_refs": [],
        "stop_parent": True,
        "error": None,
    }
    control = {
        "kind": "waiting_human",
        "reason": "awaiting_human_input",
        "payload": pause_payload["human_input"],
        "checkpoint": checkpoint.to_dict(),
    }

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        await kwargs["on_service_resolved"]("sub-1", "agent-1")
        if len(calls) == 1:
            await kwargs["on_human_input_suspension"](control, pause_payload)
            return json.dumps(pause_payload)
        assert kwargs["params"]["agent_subscription_id"] == "sub-1"
        assert kwargs["params"]["agent_id"] == "agent-1"
        resumed = RuntimeAgentCheckpoint.from_dict(kwargs["runtime_checkpoint"])
        assert resumed.pending_tool_call == checkpoint.pending_tool_call
        assert resumed.pending_tool_result["id"] == "input-1"
        answer = json.loads(resumed.pending_tool_result["content"])
        assert answer["answer"] == "US"
        return _delegated(
            usage={"prompt_tokens": 14, "completion_tokens": 5},
            rounds=2,
            tool_calls_made=["request_input", "web_search"],
        )

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }

    blocked = json.loads(await handler(arguments))
    assert blocked["_pending_action"]["kind"] == "needs_input"
    (parked,) = await _children(db_session, s["plan_id"])
    assert parked.step_status == "waiting_human"
    assert parked.runtime_checkpoint["schema_version"] == 4
    assert parked.runtime_checkpoint["pending_tool_call"]["id"] == "input-1"
    assert parked.resolved_subscription_id == "sub-1"
    assert parked.resolved_agent_id == "agent-1"
    assert parked.cost["llm_tokens_input"] == 9

    parked_id = parked.id
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    input_context = parent.params[COORDINATOR_INPUT_CONTEXT_PARAM_KEY]
    assert input_context == {
        "version": 1,
        "child_step_id": parked_id,
        "pending_tool_call_id": "input-1",
    }
    parent.params = {**parent.params, "answers": {"response": "US"}}
    await db_session.commit()
    s["params"] = dict(parent.params)
    forced = await coordinator_tools.coordinator_resume_forced_tool_call(s)
    assert forced["name"] == DELEGATE_TO_SERVICE_TOOL_NAME
    assert forced["arguments"]["checklist_item_key"] == "research"
    assert forced["arguments"]["service_key"] == "research"
    assert "Authoritative persisted checklist contract" in forced["arguments"]["prompt"]
    completed = json.loads(await handler(forced["arguments"]))

    assert completed["status"] == "completed"
    (resumed,) = await _children(db_session, s["plan_id"])
    assert resumed.step_status == "done"
    assert resumed.attempt_count == 1
    assert resumed.runtime_checkpoint is None
    assert resumed.cost == {
        "llm_tokens_input": 14,
        "llm_tokens_output": 5,
        "llm_rounds": 2,
        "tool_call_count": 2,
    }
    assert await coordinator_tools.coordinator_child_cost_totals(s) == {
        "llm_tokens_input": 14,
        "llm_tokens_output": 5,
        "llm_rounds": 2,
        "tool_call_count": 2,
        "llm_usd": 0.0,
        "unpriced_tokens_input": 14,
        "unpriced_tokens_output": 5,
    }
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    assert "answers" not in parent.params
    assert COORDINATOR_INPUT_CONTEXT_PARAM_KEY not in parent.params
    assert parent.human_input_response is None


@pytest.mark.asyncio
async def test_request_input_park_commit_replays_blocked_card_after_parent_crash(
    db_session,
    monkeypatch,
):
    """A crash after child park commit cannot lose the outer input card."""

    from packages.core.ai.pending_action import PendingAction
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

    s = await _seed_parent(db_session)
    old_snapshot = dict(s)
    calls = 0
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "research"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "input-crash-1",
                    "type": "function",
                    "function": {
                        "name": "request_input",
                        "arguments": json.dumps({"question": "Which market?"}),
                    },
                }],
            },
        ],
        usage={"prompt_tokens": 6, "completion_tokens": 2},
        rounds=1,
        tool_calls_made=["request_input"],
        pending_tool_call={
            "id": "input-crash-1",
            "name": "request_input",
            "arguments": {"question": "Which market?"},
        },
    )
    pending_action = PendingAction.needs_input(
        questions=[{
            "name": "response",
            "label": "Which market?",
            "type": "text",
            "required": True,
        }],
        title="Which market?",
    ).to_dict()
    pause_payload = {
        "delegated": True,
        "status": "blocked",
        "blocker_kind": "request_input",
        "content": "Which market?",
        "result": {"question": "Which market?"},
        "human_input": {"question": "Which market?"},
        "_pending_action": pending_action,
        "usage": checkpoint.usage,
        "rounds": checkpoint.rounds,
        "tool_calls_made": checkpoint.tool_calls_made,
        "evidence_refs": [],
        "stop_parent": True,
        "error": None,
    }
    control = {
        "kind": "waiting_human",
        "reason": "awaiting_human_input",
        "payload": pause_payload["human_input"],
        "checkpoint": checkpoint.to_dict(),
    }

    async def park_then_crash(**kwargs):
        nonlocal calls
        calls += 1
        await kwargs["on_service_resolved"]("sub-crash", "agent-crash")
        await kwargs["on_human_input_suspension"](control, pause_payload)
        raise asyncio.CancelledError

    monkeypatch.setattr(_DELEGATION_TARGET, park_then_crash)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }
    with pytest.raises(asyncio.CancelledError):
        await coordinator_tools.make_delegate_service_handler(s)(arguments)

    (parked,) = await _children(db_session, s["plan_id"])
    assert parked.step_status == "waiting_human"
    assert parked.runtime_checkpoint["pending_tool_call"]["id"] == "input-crash-1"
    parked_id = parked.id
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    old_lease = await db_session.get(WorkLease, s["lease_id"])
    assert parent.params[COORDINATOR_INPUT_CONTEXT_PARAM_KEY] == {
        "version": 1,
        "child_step_id": parked_id,
        "pending_tool_call_id": "input-crash-1",
    }
    assert "answers" not in parent.params
    assert parent.human_input_response is None

    # Simulate owner death and a fresh parent attempt.  The old execution
    # claim must not be able to force or replay the child.
    parent.max_attempts = 2
    old_lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    assert await Dispatcher().expire_leases(db_session) >= 1
    await db_session.commit()
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    old_lease = await db_session.get(WorkLease, s["lease_id"])
    assert parent.step_status == "pending"
    assert old_lease.status == WorkLeaseStatus.EXPIRED.value

    new_lease_id = generate_ulid()
    new_claim_id = generate_ulid()
    db_session.add(WorkLease(
        id=new_lease_id,
        step_id=parent.id,
        plan_id=parent.plan_id,
        entity_id=parent.entity_id,
        workspace_id=parent.workspace_id,
        worker_id=old_lease.worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        execution_claim_id=new_claim_id,
    ))
    parent.step_status = "running"
    parent.current_lease_id = new_lease_id
    parent.attempt_count = 2
    await db_session.commit()

    assert await coordinator_tools.coordinator_resume_forced_tool_call(
        old_snapshot
    ) is None
    db_session.expire_all()
    parent = await db_session.get(ExecutionStep, s["step_id"])
    s["lease_id"] = new_lease_id
    s["execution_claim_id"] = new_claim_id
    s["params"] = dict(parent.params)
    forced = await coordinator_tools.coordinator_resume_forced_tool_call(s)
    assert forced is not None
    assert forced["name"] == DELEGATE_TO_SERVICE_TOOL_NAME
    assert forced["arguments"]["checklist_item_key"] == "research"

    replayed = json.loads(
        await coordinator_tools.make_delegate_service_handler(s)(
            forced["arguments"]
        )
    )
    assert replayed == pause_payload
    assert replayed["_pending_action"]["kind"] == "needs_input"
    assert calls == 1, "card recovery must not re-enter the child Agent loop"
    (still_parked,) = await _children(db_session, s["plan_id"])
    assert still_parked.step_status == "waiting_human"
    assert still_parked.runtime_checkpoint["pending_tool_result"] is None


@pytest.mark.asyncio
async def test_checkpointed_child_write_is_reused_after_parent_lease_expiry(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    calls: list[dict] = []
    checkpoint_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "save the report"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-save",
                "type": "function",
                "function": {
                    "name": "save_sandbox_file",
                    "arguments": json.dumps({"path": "reports/market.md"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-save",
            "content": json.dumps({
                "ok": True,
                "status": "completed",
                "saved": True,
                "path": "reports/market.md",
            }),
        },
    ]

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await kwargs["on_round_end"](
                1,
                checkpoint_messages,
                {"prompt_tokens": 7, "completion_tokens": 3},
                ["save_sandbox_file"],
            )
            raise asyncio.CancelledError
        assert kwargs["initial_messages"] == checkpoint_messages[1:]
        assert kwargs["initial_tool_calls_made"] == ["save_sandbox_file"]
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }

    with pytest.raises(asyncio.CancelledError):
        await coordinator_tools.make_delegate_service_handler(s)(arguments)

    (checkpointed,) = await _children(db_session, s["plan_id"])
    child_id = checkpointed.id
    assert checkpointed.step_status == "running"
    assert checkpointed.runtime_checkpoint["rounds"] == 1
    assert any(
        ref.get("kind") == "tool_effect"
        and ref.get("effect") == "action"
        and ref.get("coordinator_child_step_id") == child_id
        for ref in checkpointed.evidence_refs
    )
    assert not any(ref.get("effect") == "pending" for ref in checkpointed.evidence_refs)

    parent = await db_session.get(ExecutionStep, s["step_id"])
    old_lease = await db_session.get(WorkLease, s["lease_id"])
    parent.max_attempts = 2
    old_lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    assert await Dispatcher().expire_leases(db_session) >= 1
    await db_session.commit()
    db_session.expire_all()
    old_lease = await db_session.get(WorkLease, s["lease_id"])
    parent = await db_session.get(ExecutionStep, s["step_id"])
    assert old_lease.status == WorkLeaseStatus.EXPIRED.value
    assert parent.step_status == "pending"
    assert parent.current_lease_id is None

    new_lease_id = generate_ulid()
    new_claim_id = generate_ulid()
    db_session.add(WorkLease(
        id=new_lease_id,
        step_id=parent.id,
        plan_id=parent.plan_id,
        entity_id=parent.entity_id,
        workspace_id=parent.workspace_id,
        worker_id=old_lease.worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        execution_claim_id=new_claim_id,
    ))
    parent.step_status = "running"
    parent.current_lease_id = new_lease_id
    parent.attempt_count = 2
    await db_session.commit()
    s["lease_id"] = new_lease_id
    s["execution_claim_id"] = new_claim_id

    await coordinator_tools.make_delegate_service_handler(s)(arguments)

    assert len(calls) == 2
    children = await _children(db_session, s["plan_id"])
    assert [child.id for child in children] == [child_id]
    assert children[0].step_status == "done"
    assert children[0].attempt_count == 2
    assert children[0].runtime_checkpoint is None


@pytest.mark.asyncio
async def test_checkpointed_child_effect_with_dropped_result_fails_closed(
    db_session,
    monkeypatch,
):
    """A receipt without its exact replay result must never reach a new model turn."""

    s = await _seed_parent(db_session)
    calls: list[dict] = []
    tool_result = json.dumps({
        "ok": True,
        "status": "completed",
        "saved": True,
        "path": "reports/market.md",
        "padding": "x" * 5_000,
    })
    checkpoint_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "save the report"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-save",
                "type": "function",
                "function": {
                    "name": "save_sandbox_file",
                    "arguments": json.dumps({"path": "reports/market.md"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-save",
            "content": tool_result,
        },
    ]

    async def interrupted_after_write(**kwargs):
        calls.append(kwargs)
        await kwargs["on_round_end"](
            1,
            checkpoint_messages,
            {"prompt_tokens": 7, "completion_tokens": 3},
            ["save_sandbox_file"],
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(
        transcript,
        "WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES",
        1_200,
    )
    monkeypatch.setattr(_DELEGATION_TARGET, interrupted_after_write)
    arguments = {
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    }
    handler = coordinator_tools.make_delegate_service_handler(s)

    with pytest.raises(asyncio.CancelledError):
        await handler(arguments)

    (checkpointed,) = await _children(db_session, s["plan_id"])
    assert transcript.worker_transcript_dropped_tool_result_ids(
        checkpointed.runtime_checkpoint
    ) == {"call-save"}
    assert any(
        ref.get("kind") == "tool_effect"
        and ref.get("effect") == "action"
        and (ref.get("fields") or {}).get("tool_call_id") == "call-save"
        for ref in checkpointed.evidence_refs
    )

    blocked = json.loads(await handler(arguments))

    assert blocked["error"] == "delegation_effect_requires_reconciliation"
    assert "tool result was dropped" in blocked["message"]
    assert blocked["stop_parent"] is True
    assert len(calls) == 1, "the settled write must not be replayed"
    (failed,) = await _children(db_session, s["plan_id"])
    assert failed.step_status == "failed"
    assert failed.error["type"] == "DelegatedEffectRequiresReconciliation"


@pytest.mark.asyncio
async def test_stale_parent_claim_cannot_checkpoint_or_settle_child(
    db_session,
    monkeypatch,
):
    s = await _seed_parent(db_session)
    checkpoint_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "research"},
        {"role": "assistant", "content": "round complete"},
    ]

    async def reclaimed_then_complete(**kwargs):
        lease = await db_session.get(WorkLease, s["lease_id"])
        lease.execution_claim_id = generate_ulid()
        await db_session.commit()
        await kwargs["on_round_end"](1, checkpoint_messages)
        return _delegated()

    monkeypatch.setattr(_DELEGATION_TARGET, reclaimed_then_complete)
    returned = await coordinator_tools.make_delegate_service_handler(s)({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "go",
    })

    assert json.loads(returned)["status"] == "failed"
    assert "execution fence changed" in json.loads(returned)["message"]
    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_status == "running"
    assert child.runtime_checkpoint is None
    assert child.result is None


@pytest.mark.asyncio
async def test_legacy_running_child_without_checkpoint_fails_closed(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    calls = 0

    async def interrupted(**kwargs):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(_DELEGATION_TARGET, interrupted)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "research", "service_key": "research", "prompt": "go",
    }

    with pytest.raises(asyncio.CancelledError):
        await handler(arguments)
    result = json.loads(await handler(arguments))

    assert result["error"] == "delegation_state_ambiguous"
    assert calls == 1, "ambiguous external effects must not be replayed"
    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_status == "failed"
    assert child.error["type"] == "AmbiguousDelegationState"


@pytest.mark.asyncio
async def test_hitl_envelope_marks_the_child_blocked_and_is_returned_verbatim(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    envelope = json.dumps({
        "__hitl__": True, "error": "approval_required", "approval_token": "tok-1",
        "hitl": {"prompt": "Approve sending the email?", "action": "tool.approve", "tool": "gmail_send"},
    })

    async def fake_delegate(**kwargs):
        if kwargs["params"]["service_key"] == "research":
            return _delegated()
        return envelope

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "research first",
    })

    returned = await handler({
        "checklist_item_key": "send", "service_key": "writer", "prompt": "send it",
    })

    assert returned == envelope
    child = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    assert child.step_status == "waiting_human"
    assert child.result["approval_token"] == "tok-1"
    assert child.finished_at is None
    assert child.requires_approval is True and child.risk_level == "high"
    assert child.provider == "gmail" and child.action_key == "send_email"


@pytest.mark.asyncio
async def test_hitl_resume_reuses_the_same_child_and_exact_nested_token(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    envelope = json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": "tok-1",
        "hitl": {"prompt": "Approve?", "action": "tool.approve", "tool": "gmail_send"},
    })
    calls: list[dict] = []

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        if kwargs["params"]["service_key"] == "research":
            return _delegated()
        if not kwargs["params"].get("approval_token"):
            return envelope
        return _delegated(
            service={"service_key": "writer"},
            result={"sent": True},
            content='{"sent":true}',
            evidence_refs=[{
                "kind": "tool_effect",
                "effect": "publish",
                "fields": {
                    "delivery_confirmed": True,
                    "message_id": "message-1",
                },
            }],
        )

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "send", "service_key": "writer", "prompt": "send it",
    }

    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "research first",
    })
    assert await handler(arguments) == envelope
    waiting_child = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    waiting_params = (
        waiting_child.params if isinstance(waiting_child.params, dict) else {}
    )
    db_session.add(HitlRequest(
        id="tok-1",
        entity_id=s["entity_id"],
        workspace_id=s["workspace_id"],
        action_key="gmail.send",
        risk_level="high",
        origin_kind="tool_call",
        origin_step_id=waiting_child.id,
        origin_task_id=s["task_id"],
        status=ApprovalStatus.GRANTED.value,
        dedup_key="coordinator-test:tok-1",
        reason="Approve?",
        context={
            "delegation_resume": {
                "version": 1,
                "approval_token": "tok-1",
                "task_id": s["task_id"],
                "step_id": waiting_child.id,
                "service_key": "writer",
                "delegation_idempotency_key": waiting_params[
                    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY
                ],
            },
        },
        payload={},
    ))
    await db_session.commit()

    completed = json.loads(await handler(arguments))

    assert completed["result"] == {"sent": True}
    send_calls = [
        call for call in calls if call["params"]["service_key"] == "writer"
    ]
    assert len(send_calls) == 2
    assert send_calls[1]["params"]["approval_token"] == "tok-1"
    children = await _children(db_session, s["plan_id"])
    assert len(children) == 2
    completed_send = next(
        row for row in children if row.step_key == "coordinate__send"
    )
    assert completed_send.step_status == "done" and completed_send.attempt_count == 1


@pytest.mark.asyncio
async def test_consumed_approved_effect_checkpoint_resumes_without_resending(
    db_session,
    monkeypatch,
):
    """A crash after the approved send receipt must resume after that tool call."""

    s = await _seed_parent(db_session)
    approval_token = "tok-approved-effect-crash"
    envelope = json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": approval_token,
        "hitl": {
            "prompt": "Approve?",
            "action": "tool.approve",
            "tool": "mcp__gmail__send_email",
        },
    })
    send_arguments = {
        "to": ["reader@example.com"],
        "subject": "Launch",
        "body": "Ready",
        "approval_token": approval_token,
    }
    approved_arguments = {
        key: value
        for key, value in send_arguments.items()
        if key != "approval_token"
    }
    checkpoint_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "send the approved report"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-approved-send",
                "type": "function",
                "function": {
                    "name": "mcp__gmail__send_email",
                    "arguments": json.dumps(send_arguments),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-approved-send",
            "content": json.dumps({
                "ok": True,
                "status": "completed",
                "message_id": "message-approved-1",
            }),
        },
    ]
    calls: list[dict] = []
    provider_sends = 0

    async def fake_delegate(**kwargs):
        nonlocal provider_sends
        calls.append(kwargs)
        if kwargs["params"]["service_key"] == "research":
            return _delegated()
        await kwargs["on_service_resolved"]("writer-subscription", "writer-agent")
        if len([call for call in calls if call["params"]["service_key"] == "writer"]) == 1:
            return envelope
        if kwargs["params"].get("approval_token"):
            provider_sends += 1
            request = await db_session.get(HitlRequest, approval_token)
            request.status = ApprovalStatus.CONSUMED.value
            request.consumed_at = datetime.now(timezone.utc)
            await db_session.commit()
            await kwargs["on_tool_effect_intent"](
                "mcp__gmail__send_email",
                send_arguments,
                "call-approved-send",
                None,
                approval_request_id=approval_token,
            )
            await kwargs["on_round_end"](
                1,
                checkpoint_messages,
                {"prompt_tokens": 7, "completion_tokens": 3},
                ["mcp__gmail__send_email"],
            )
            raise asyncio.CancelledError
        assert "approval_token" not in kwargs["params"]
        assert kwargs["initial_messages"] == checkpoint_messages[1:]
        return _delegated(
            service={"service_key": "writer"},
            result={"sent": True},
            content='{"sent":true}',
            evidence_refs=[],
        )

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    arguments = {
        "checklist_item_key": "send",
        "service_key": "writer",
        "prompt": "send it",
    }

    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "research first",
    })
    assert await handler(arguments) == envelope
    waiting_child = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    child_params = waiting_child.params if isinstance(waiting_child.params, dict) else {}
    child_idempotency_key = child_params[
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY
    ]
    db_session.add(HitlRequest(
        id=approval_token,
        entity_id=s["entity_id"],
        workspace_id=s["workspace_id"],
        action_key="gmail.send",
        risk_level="high",
        origin_kind="tool_call",
        origin_step_id=waiting_child.id,
        origin_task_id=s["task_id"],
        status=ApprovalStatus.GRANTED.value,
        dedup_key=f"coordinator-test:{approval_token}",
        reason="Approve?",
        context={
            "tool": "mcp__gmail__send_email",
            "continuation": {
                "version": 1,
                "tool": "mcp__gmail__send_email",
                "arguments": approved_arguments,
            },
            "delegation_resume": {
                "version": 1,
                "approval_token": approval_token,
                "task_id": s["task_id"],
                "step_id": waiting_child.id,
                "service_key": "writer",
                "agent_subscription_id": "writer-subscription",
                "delegation_idempotency_key": child_idempotency_key,
                "tool_name": "mcp__gmail__send_email",
            },
        },
        payload={},
    ))
    await db_session.commit()

    with pytest.raises(asyncio.CancelledError):
        await handler(arguments)

    (checkpointed_send,) = [
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    ]
    request = await db_session.get(HitlRequest, approval_token)
    assert request.status == ApprovalStatus.CONSUMED.value
    assert checkpointed_send.step_status == ExecutionStepStatus.RUNNING.value
    assert transcript.is_worker_transcript_checkpoint(
        checkpointed_send.runtime_checkpoint
    )
    assert any(
        ref.get("kind") == "tool_effect"
        and ref.get("effect") == "publish"
        and (ref.get("fields") or {}).get("approval_request_id")
        == approval_token
        and (ref.get("fields") or {}).get("tool_call_id")
        == "call-approved-send"
        for ref in checkpointed_send.evidence_refs
    )

    completed = json.loads(await handler(arguments))

    assert completed["result"] == {"sent": True}
    assert provider_sends == 1
    completed_send = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    assert completed_send.step_status == ExecutionStepStatus.DONE.value
    assert completed_send.runtime_checkpoint is None


@pytest.mark.asyncio
async def test_consumed_approved_effect_with_unbound_receipt_keeps_checkpoint(
    db_session,
    monkeypatch,
):
    """Incomplete receipt lineage must fail closed without erasing evidence."""

    from hashlib import sha256

    from packages.core.ai.runtime.workspace_delegation_actions import (
        _tool_arguments_hash,
    )

    s = await _seed_parent(db_session)
    approval_token = "tok-unbound-1"
    tool_name = "mcp__gmail__send_email"
    approved_arguments = {
        "to": ["reader@example.com"],
        "subject": "Launch",
        "body": "Ready",
    }
    envelope = json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": approval_token,
        "hitl": {
            "prompt": "Approve?",
            "action": "tool.approve",
            "tool": tool_name,
        },
    })
    calls = 0

    async def fake_delegate(**kwargs):
        nonlocal calls
        calls += 1
        if kwargs["params"]["service_key"] == "research":
            return _delegated()
        await kwargs["on_service_resolved"]("writer-subscription", "writer-agent")
        if calls == 2:
            return envelope
        raise AssertionError("an unbound consumed effect must not re-enter Runtime")

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)
    await handler({
        "checklist_item_key": "research",
        "service_key": "research",
        "prompt": "research first",
    })
    arguments = {
        "checklist_item_key": "send",
        "service_key": "writer",
        "prompt": "send it",
    }
    assert await handler(arguments) == envelope
    child = next(
        row
        for row in await _children(db_session, s["plan_id"])
        if row.step_key == "coordinate__send"
    )
    child_params = child.params if isinstance(child.params, dict) else {}
    idempotency_key = child_params[COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY]
    result = json.dumps({
        "ok": True,
        "status": "completed",
        "message_id": "message-1",
    })
    checkpoint = transcript.build_worker_transcript_checkpoint(
        [
            {"role": "user", "content": "send"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps({
                            **approved_arguments,
                            "approval_token": approval_token,
                        }),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": result},
        ],
        rounds=1,
        usage={},
        tool_calls_made=[tool_name],
    )
    assert checkpoint is not None
    child.step_status = ExecutionStepStatus.RUNNING.value
    child.runtime_checkpoint = checkpoint
    child.evidence_refs = [{
        "kind": "tool_effect",
        "effect": "publish",
        "coordinator_child_step_id": child.id,
        "fields": {
            "approval_request_id": approval_token,
            "owner_step_id": child.id,
            "owner_plan_id": child.plan_id,
            "owner_task_id": s["task_id"],
            "owner_service_key": child.service_key,
            "owner_subscription_id": child.resolved_subscription_id,
            "owner_idempotency_key": idempotency_key,
            "tool": tool_name,
            "arguments_hash": _tool_arguments_hash(approved_arguments),
            "tool_call_id": "call-1",
            # Deliberately does not bind the durable transcript result.
            "result_sha256": sha256(b"different result").hexdigest(),
            "intent_state": "returned",
        },
    }]
    db_session.add(HitlRequest(
        id=approval_token,
        entity_id=s["entity_id"],
        workspace_id=s["workspace_id"],
        action_key="gmail.send",
        risk_level="high",
        origin_kind="tool_call",
        origin_step_id=child.id,
        origin_task_id=s["task_id"],
        status=ApprovalStatus.CONSUMED.value,
        consumed_at=datetime.now(timezone.utc),
        dedup_key=f"coordinator-test:{approval_token}",
        reason="Approve?",
        context={
            "tool": tool_name,
            "continuation": {
                "version": 1,
                "tool": tool_name,
                "arguments": approved_arguments,
            },
            "delegation_resume": {
                "version": 1,
                "approval_token": approval_token,
                "task_id": s["task_id"],
                "step_id": child.id,
                "service_key": child.service_key,
                "agent_subscription_id": child.resolved_subscription_id,
                "delegation_idempotency_key": idempotency_key,
                "tool_name": tool_name,
            },
        },
        payload={},
    ))
    await db_session.commit()

    blocked = json.loads(await handler(arguments))

    assert blocked["error"] == "delegated_approval_result_unbound"
    assert blocked["stop_parent"] is True
    assert calls == 2
    failed = await db_session.get(ExecutionStep, child.id)
    await db_session.refresh(failed)
    assert failed.step_status == ExecutionStepStatus.FAILED.value
    assert failed.runtime_checkpoint == checkpoint


@pytest.mark.asyncio
async def test_delegation_error_marks_the_child_failed(db_session, monkeypatch):
    s = await _seed_parent(db_session)
    error = json.dumps({"error": "workspace_service_agent_not_found", "message": "no such service",
                        "available_services": ["research"]})

    async def fake_delegate(**kwargs):
        return error

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    returned = await handler({
        "checklist_item_key": "research", "service_key": "research", "prompt": "anything",
    })

    assert returned == error
    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_status == "failed"
    assert child.error["type"] == "DelegationFailed"
    assert "no such service" in child.error["message"]
    assert child.finished_at is not None


@pytest.mark.asyncio
async def test_delegate_exception_fails_the_child_and_returns_an_error_payload(db_session, monkeypatch):
    s = await _seed_parent(db_session)

    async def exploding(**kwargs):
        raise RuntimeError("delegate crashed")

    monkeypatch.setattr(_DELEGATION_TARGET, exploding)
    handler = coordinator_tools.make_delegate_service_handler(s)

    returned = json.loads(await handler({
        "checklist_item_key": "research", "service_key": "research", "prompt": "go",
    }))

    assert returned["error"] and "delegate crashed" in returned["message"]
    (child,) = await _children(db_session, s["plan_id"])
    assert child.step_status == "failed"
    assert child.error["type"] == "DelegationFailed"


@pytest.mark.asyncio
async def test_missing_arguments_are_rejected_without_a_child_row(db_session, monkeypatch):
    s = await _seed_parent(db_session)

    async def fake_delegate(**kwargs):
        raise AssertionError("must not delegate without a prompt and a service")

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    handler = coordinator_tools.make_delegate_service_handler(s)

    assert "error" in json.loads(await handler({
        "checklist_item_key": "research", "service_key": "research",
    }))
    assert "error" in json.loads(await handler({
        "checklist_item_key": "research", "prompt": "x",
    }))
    assert "error" in json.loads(await handler({"service_key": "research", "prompt": "x"}))
    assert await _children(db_session, s["plan_id"]) == []


@pytest.mark.asyncio
async def test_checklist_service_mismatch_is_rejected_before_child_or_runtime(db_session, monkeypatch):
    s = await _seed_parent(db_session)

    async def fake_delegate(**kwargs):
        raise AssertionError("a forged service must not reach Runtime")

    monkeypatch.setattr(_DELEGATION_TARGET, fake_delegate)
    result = json.loads(await coordinator_tools.make_delegate_service_handler(s)({
        "checklist_item_key": "research", "service_key": "writer", "prompt": "forge",
    }))

    assert result["error"] == "invalid_checklist_item"
    assert "belongs to service" in result["message"]
    assert await _children(db_session, s["plan_id"]) == []


def test_child_outcome_keeps_the_complete_typed_handoff():
    payload = json.loads(_delegated())
    outcome = coordinator_tools._child_outcome(json.dumps(payload))

    assert outcome.status == "done"
    assert outcome.result == payload
    assert outcome.evidence_refs == payload["evidence_refs"]
    assert outcome.error is None


def test_workspace_delegation_receipts_are_tool_derived_and_typed():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "mcp__twitter_x__post_tweet",
                    "arguments": json.dumps({"text": "launch"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps({
                "ok": True,
                "tweet_id": "tweet-1",
                "tweet_url": "https://x.example/tweet-1",
                "document_id": "doc-1",
            }),
        },
    ]

    evidence, artifacts, knowledge = actions._delegation_receipts(
        messages,
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    assert any(item.get("effect") == "publish" for item in evidence)
    assert any(item.get("document_id") == "doc-1" for item in artifacts)
    assert knowledge == [{
        "type": "knowledge_document",
        "source": "mcp__twitter_x__post_tweet",
        "document_id": "doc-1",
    }]
    assert actions._typed_delegation_result('{"summary":"done"}', None) == {
        "summary": "done",
    }
    schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
        "additionalProperties": False,
    }
    assert actions._delegation_schema_error({"summary": "done"}, schema) is None
    assert "$.summary" in actions._delegation_schema_error(
        {"summary": 3}, schema
    )


def test_workspace_task_creation_is_an_effect_not_a_file_receipt():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-task-call", "type": "function",
                "function": {
                    "name": "workspace_create_task",
                    "arguments": json.dumps({"title": "Create V3 Smoke 交付.md"}),
                },
            }],
        },
        {
            "role": "tool", "tool_call_id": "create-task-call",
            "content": json.dumps({
                "created": True,
                "task": {
                    "id": "child-task",
                    "title": "Create and register Knowledge document: V3 Smoke 交付.md",
                    "description": "Save /api/v1/fs/ent-1/Workspaces/Demo/source.md",
                },
            }),
        },
    ]

    evidence, artifacts, knowledge = actions._delegation_receipts(
        messages, entity_id="ent-1", workspace_id="ws-1", task_id="parent-task",
    )

    assert any(ref.get("kind") == "tool_effect" for ref in evidence)
    assert artifacts == []
    assert knowledge == []


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "mcp__facebook__create_live_video",
            {"page_id": "page-1", "status": "UNPUBLISHED"},
        ),
        (
            "mcp__facebook__end_live_video",
            {"page_id": "page-1", "live_video_id": "video-1"},
        ),
    ],
)
def test_workspace_delegation_receipts_do_not_promote_external_mutations_to_publish(
    tool_name,
    arguments,
):
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": json.dumps({
                    "ok": True,
                    "status": "completed",
                    "id": "effect-1",
                }),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    effects = [item for item in evidence if item.get("kind") == "tool_effect"]
    assert len(effects) == 1
    assert effects[0]["effect"] == "action"
    assert effects[0]["fields"]["operation"] in {"create", "modify"}


def test_workspace_delegation_receipts_treat_send_as_a_delivered_effect():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "mcp__gmail__send_email",
                        "arguments": json.dumps({
                            "to": ["reader@example.com"],
                            "subject": "Launch",
                            "body": "Ready",
                        }),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": json.dumps({"ok": True, "message_id": "message-1"}),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    effects = [item for item in evidence if item.get("kind") == "tool_effect"]
    assert len(effects) == 1
    assert effects[0]["effect"] == "publish"
    assert effects[0]["fields"]["operation"] == "send"


def _runtime_tool_effects(
    tool_name: str,
    arguments: dict,
    payload: dict,
) -> list[dict]:
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-status",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-status",
                "content": json.dumps(payload),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )
    return [item for item in evidence if item.get("kind") == "tool_effect"]


def _gmail_send_effects(payload: dict) -> list[dict]:
    return _runtime_tool_effects(
        "mcp__gmail__send_email",
        {
            "to": ["reader@example.com"],
            "subject": "Launch",
            "body": "Ready",
        },
        payload,
    )


@pytest.mark.parametrize(
    "status",
    ["denied", "rejected", "expired", "timeout", "unknown"],
)
def test_workspace_delegation_receipts_reject_non_success_terminal_statuses(status):
    effects = _gmail_send_effects({
        "ok": True,
        "status": status,
        "message_id": "must-not-authorize-delivery",
    })

    assert effects == []


def test_workspace_delegation_receipts_fail_closed_on_missing_status_and_generic_id():
    assert _gmail_send_effects({
        "ok": True,
        "id": "request-1",
    }) == []


@pytest.mark.parametrize("status", ["pending", "queued", "processing"])
def test_workspace_delegation_receipts_do_not_treat_async_generic_id_as_terminal(status):
    assert _gmail_send_effects({
        "ok": True,
        "status": status,
        "id": "async-job-1",
    }) == []


@pytest.mark.parametrize(
    "status",
    ["created_pending", "not_dispatched", "provider_new_state"],
)
def test_unsettled_tool_results_create_no_artifact_or_knowledge_receipts(status):
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    payload = {
        "ok": True,
        "status": status,
        "created": True,
        "path": "outputs/not-delivered.pdf",
        "url": "https://artifacts.example/not-delivered.pdf",
        "document_id": "not-delivered-document",
    }
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-unsettled",
                "type": "function",
                "function": {
                    "name": "save_sandbox_file",
                    "arguments": json.dumps({
                        "path": "outputs/not-delivered.pdf",
                    }),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-unsettled",
            "content": json.dumps(payload),
        },
    ]

    _, artifacts, knowledge = actions._delegation_receipts(
        messages,
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    assert artifacts == []
    assert knowledge == []
    assert worker_internal._artifact_refs_from_tool_payload(payload) == []


def test_workspace_delegation_receipts_do_not_use_generic_id_as_delivery_proof():
    assert _gmail_send_effects({
        "ok": True,
        "status": "completed",
        "id": "request-1",
    }) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"saved": True, "path": "outputs/report.pdf"},
        {"document_id": "document-1"},
    ],
    ids=["ok", "saved_path", "document_id"],
)
def test_workspace_delegation_receipts_accept_explicit_non_delivery_postconditions(
    payload,
):
    effects = _runtime_tool_effects(
        "save_sandbox_file",
        {"path": "outputs/report.pdf"},
        payload,
    )

    assert len(effects) == 1
    assert effects[0]["effect"] == "action"
    assert effects[0]["fields"]["operation"] not in {"publish", "send"}
    assert effects[0]["fields"]["delivery_confirmed"] is False


def test_workspace_delegation_receipts_reject_generic_id_only_for_other_actions():
    assert _runtime_tool_effects(
        "save_sandbox_file",
        {"path": "outputs/report.pdf"},
        {"id": "async-job-1"},
    ) == []


def test_workspace_delegation_receipts_accept_verified_typed_postcondition():
    effects = _gmail_send_effects({
        "ok": True,
        "id": "request-1",
        "postcondition": {
            "schema_version": "publication-receipt/v1",
            "platform": "gmail",
            "verification_status": "verified",
            "external_id": "message-typed-1",
            "published_at": "2026-09-19T00:00:00Z",
            "verified_at": "2026-09-19T00:00:01Z",
            "payload_hash": f"sha256:{'a' * 64}",
            "fallback_used": "none",
            "evidence": [{"kind": "message_id", "value": "message-typed-1"}],
            "attempts": [],
        },
    })

    assert len(effects) == 1
    assert effects[0]["effect"] == "publish"
    assert effects[0]["fields"]["delivery_confirmed"] is True
    assert effects[0]["fields"]["postcondition"]["verification_status"] == "verified"
