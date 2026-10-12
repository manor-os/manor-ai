"""Real owner-row projection: allocation, live claim, lineage and tenant scope."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.contracts.artifact_delivery import artifact_delivery_step_binding
from packages.core.ledger.event_types import EXECUTION_REQUESTED
from packages.core.ledger.service import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace
from packages.core.services.task_execution import (
    TASK_EXECUTION_OUTBOX_EVENT, task_execution_intent_dedupe_key, task_execution_intent_generation,
)
from packages.core.services.workspace_execution_evidence import ExecutionEvidenceScope, read_workspace_task_evidence

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def projection_session(db_session, monkeypatch):
    import packages.core.database as database
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))


async def seed(db):
    entity_id, user_id, workspace_id, conversation_id, message_id, task_id = [generate_ulid() for _ in range(6)]
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Execution proof", status="active")
    task = Task(id=task_id, entity_id=entity_id, workspace_id=workspace_id, title="A real task", status="in_progress",
                status_changed_at=datetime.now(timezone.utc))
    db.add_all([
        Entity(id=entity_id, name="Execution proof"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.invalid", password_hash="unused", role="owner", status="active"),
        UserMembership(entity_id=entity_id, user_id=user_id, role="owner", status="active"),
        workspace, task,
        Conversation(id=conversation_id, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, scope="workspace_main", status="active"),
        Message(id=message_id, conversation_id=conversation_id, role="user", content="Run the task"),
    ])
    await db.flush()
    scope = ExecutionEvidenceScope(entity_id, workspace_id, user_id, conversation_id, message_id)
    return NS(scope=scope, workspace=workspace, task=task)


async def read(seeded):
    return await read_workspace_task_evidence(seeded.scope, [{"id": seeded.task.id}])


async def add_plan(db, seeded):
    plan = ExecutionPlan(id=generate_ulid(), entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                         task_id=seeded.task.id, status="running", plan_dag={})
    step = ExecutionStep(id=generate_ulid(), entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                         plan_id=plan.id, step_key="work", kind="subagent", step_status="running", params={}, attempt_count=1)
    now = datetime.now(timezone.utc)
    lease = WorkLease(id=generate_ulid(), entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                      plan_id=plan.id, step_id=step.id, worker_id=generate_ulid(), status="active",
                      leased_at=now, lease_until=now+timedelta(minutes=2), execution_claim_id="preallocated")
    step.current_lease_id = lease.id
    run = TaskCoordinatorRun(task_id=seeded.task.id, entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                             root_subscription_id=generate_ulid(), root_agent_id=generate_ulid(), selection_fingerprint="fingerprint",
                             lifecycle_generation="generation", phase="waiting_plan", current_plan_id=plan.id)
    db.add_all([plan, step, lease, run])
    await db.flush()
    return plan, step, lease, run


async def test_real_outbox_allocation_claim_and_expiry_are_distinct(db_session):
    seeded = await seed(db_session)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "not_started"
    db_session.add(RuntimeOutboxEvent(
        event_type=TASK_EXECUTION_OUTBOX_EVENT, aggregate_id=seeded.task.id,
        dedupe_key=task_execution_intent_dedupe_key(seeded.task.id, task_execution_intent_generation(seeded.task)), payload={},
    ))
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "accepted"
    _, _, lease, _ = await add_plan(db_session, seeded)
    await db_session.commit()
    assigned = (await read(seeded))[0]
    assert assigned["state"] == "assigned"
    lease.execution_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()
    started = (await read(seeded))[0]
    assert started["state"] == "running" and started["revision"] != assigned["revision"]
    lease.lease_until = datetime.now(timezone.utc)-timedelta(seconds=1)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "accepted"


async def test_only_append_only_exact_turn_origin_is_creation_proof(db_session):
    seeded = await seed(db_session)
    origin = {
        "kind": "workspace_chat_create", "entity_id": seeded.scope.entity_id,
        "workspace_id": seeded.scope.workspace_id, "user_id": seeded.scope.user_id,
        "conversation_id": seeded.scope.conversation_id, "origin_user_message_id": seeded.scope.origin_user_message_id,
    }
    seeded.task.details = {"execution_origin": origin}
    await db_session.commit()
    assert (await read(seeded))[0]["origin"] == "existing_record"
    await record_event(db_session, entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                       event_type=EXECUTION_REQUESTED, source_kind="task", source_id=seeded.task.id,
                       actor_kind="user", actor_id=seeded.scope.user_id,
                       idempotency_key=f"chat-task-created:{seeded.task.id}", payload=origin)
    await db_session.commit()
    assert (await read(seeded))[0]["origin"] == "current_turn"
    another_message = Message(conversation_id=seeded.scope.conversation_id, role="user", content="Check progress")
    db_session.add(another_message)
    await db_session.commit()
    seeded.scope = replace(seeded.scope, origin_user_message_id=another_message.id)
    assert (await read(seeded))[0]["origin"] == "existing_record"


async def test_old_hitl_does_not_park_current_plan_and_changed_submission_invalidates_delivery(db_session):
    seeded = await seed(db_session)
    plan, step, lease, _ = await add_plan(db_session, seeded)
    step.step_status, lease.status = "waiting_human", "needs_human"
    request = HitlRequest(entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                          origin_kind="lease", origin_task_id=seeded.task.id, origin_plan_id=generate_ulid(), origin_step_id=step.id,
                          status="pending", dedup_key=f"lease:{lease.id}:needs_input", context={"pending_kind": "needs_input"})
    db_session.add(request)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] != "waiting_input"
    request.origin_plan_id = plan.id
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "waiting_input"
    plan.status, step.step_status, seeded.task.status = "completed", "done", "completed"
    step.result = {"text": "verified result"}
    plan.dispatcher_state = {"task_content_acceptance": {
        "verdict": "accepted", "snapshot_digest": "bound-digest", "artifact_manifest": {
            "plan_id": plan.id, "steps": [{**artifact_delivery_step_binding(step), "refs": []}],
        },
    }}
    seeded.task.actual_output = {"plan_id": plan.id, "supervisor_verdict": "completed"}
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "delivered"
    step.result = {"text": "unreviewed replacement"}
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "completed_unverified"


async def test_concurrent_real_workspace_scopes_and_lifecycle_are_isolated(db_session):
    workspaces = [await seed(db_session) for _ in range(6)]
    await db_session.commit()
    async def check(seeded):
        own, cross = await asyncio.gather(read(seeded), read_workspace_task_evidence(
            seeded.scope, [{"id": task.task.id} for task in workspaces],
        ))
        assert len(own) == len(cross) == 1
        assert own[0]["id"] == cross[0]["id"] == seeded.task.id
    await asyncio.gather(*(check(item) for item in workspaces))
    target = workspaces[0]
    target.workspace.status = "paused"
    await db_session.commit()
    assert len(await read(target)) == 1  # Paused autonomy still permits user-directed work.
    target.workspace.status = "archived"
    await db_session.commit()
    assert await read(target) == []
    target.workspace.status = "active"
    target.workspace.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert await read(target) == []


async def test_task_recovery_is_input_only_while_current_owner_points_to_exact_card(db_session):
    seeded = await seed(db_session)
    plan, step, lease, run = await add_plan(db_session, seeded)
    seeded.task.status, plan.status, run.phase = "waiting_on_customer", "failed", "waiting_human"
    step.step_status, lease.status = "done", "done"
    request = HitlRequest(id=generate_ulid(), entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
                          origin_kind="task", origin_task_id=seeded.task.id, origin_plan_id=plan.id,
                          action_key="task.recover", hitl_type="error", matched_rule="task.needs_recovery",
                          status="pending", dedup_key=f"task:{seeded.task.id}:task.recover")
    db_session.add(request)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "blocked"
    plan.dispatcher_state = {"task_recovery_request_id": request.id}
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "waiting_input"
    plan.dispatcher_state = {"task_recovery_request_id": generate_ulid()}
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "blocked"


async def test_ready_coordinator_claim_and_checkpoint_are_processing_not_unstarted(db_session):
    from packages.core.services.task_coordinator_wakeup import (
        checkpoint_task_coordinator_run, claim_task_coordinator_wake, stage_task_coordinator_wake,
    )
    seeded = await seed(db_session)
    plan, step, lease, run = await add_plan(db_session, seeded)
    step.step_status, lease.status, run.phase = "done", "completed", "ready"
    run.lifecycle_generation = task_execution_intent_generation(seeded.task)
    selection = NS(root=NS(subscription_id=run.root_subscription_id, agent_id=run.root_agent_id),
                   stable_fingerprint=lambda: run.selection_fingerprint)
    wake = await stage_task_coordinator_wake(db_session, task_id=seeded.task.id, reason="delivery_review_resume")
    await db_session.commit()
    waiting = (await read(seeded))[0]
    assert waiting["state"] == "awaiting_coordinator"
    claim, reason = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=wake.event_id, wake_generation=wake.wake_generation,
        claim_token="real-resume-claim", selection=selection, lease_seconds=120,
    )
    assert reason == "claimed"
    version = await checkpoint_task_coordinator_run(
        db_session, task_id=seeded.task.id, claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        expected_state_version=claim.state_version, checkpoint={"rounds": 1, "messages": []},
    )
    assert version is not None and run.phase == "ready"
    await db_session.commit()
    processing = (await read(seeded))[0]
    assert processing["state"] == "planning"
    assert processing["revision"] != waiting["revision"]
    run.claim_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "awaiting_coordinator"


@pytest.mark.parametrize("phase", ["ready", "running", "waiting_plan", "waiting_child"])
async def test_coordinator_requires_current_admitted_claim_not_phase_or_old_generation(db_session, phase):
    seeded = await seed(db_session)
    _, step, lease, run = await add_plan(db_session, seeded)
    lease.status, step.step_status, run.phase = "completed", "done", phase
    run.lifecycle_generation = task_execution_intent_generation(seeded.task)
    run.wake_generation = run.claim_generation = generate_ulid()
    run.claim_token, run.claim_expires_at = "claimed", datetime.now(timezone.utc)+timedelta(minutes=1)
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "planning"
    run.wake_generation = generate_ulid()
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "awaiting_coordinator"
    run.wake_generation = run.claim_generation
    run.lifecycle_generation = "obsolete-lifecycle"
    await db_session.commit()
    assert (await read(seeded))[0]["state"] == "awaiting_coordinator"


async def add_parked_agent_loop(db, seeded, phase, wait):
    run = TaskCoordinatorRun(
        task_id=seeded.task.id, entity_id=seeded.scope.entity_id, workspace_id=seeded.scope.workspace_id,
        root_subscription_id=generate_ulid(), root_agent_id=generate_ulid(), selection_fingerprint="fingerprint",
        lifecycle_generation=task_execution_intent_generation(seeded.task), engine="agent_loop", phase=phase,
        checkpoint={"messages": [{"role": "user", "content": "x" * 100}], "task_loop_wait": wait},
    )
    db.add(run)
    await db.commit()
    return run


@pytest.mark.parametrize(("phase", "wait", "label"), [
    ("waiting_timer", {"version": 1, "kind": "waiting_timer", "wake_at": "2026-10-12T09:00:00+00:00"},
     "Waiting until 2026-10-12T09:00:00+00:00"),
    ("waiting_job", {"version": 1, "kind": "waiting_job", "job_ids": ["job-a", "job-b"],
                     "deadline_at": "2026-10-04T13:00:00+00:00"},
     "Waiting for 2 media job(s)"),
])
async def test_parked_agent_loop_is_waiting_not_unstarted(db_session, phase, wait, label):
    seeded = await seed(db_session)
    run = await add_parked_agent_loop(db_session, seeded, phase, wait)

    fact = (await read(seeded))[0]

    assert fact["state"] == "waiting"
    assert fact["waiting"]["kind"] == phase and fact["waiting"]["label"] == label
    if phase == "waiting_timer":
        assert fact["waiting"]["wake_at"] == wait["wake_at"]
    else:
        assert fact["waiting"]["job_ids"] == ["job-a", "job-b"]

    # Woken and resumed: a live claim on the parked phase is running work.
    run.wake_generation = run.claim_generation = generate_ulid()
    run.claim_token, run.claim_expires_at = "claimed", datetime.now(timezone.utc) + timedelta(minutes=1)
    await db_session.commit()
    resumed = (await read(seeded))[0]
    assert resumed["state"] == "running" and "waiting" not in resumed


async def test_waiting_state_has_user_facing_labels():
    from packages.core.ai.runtime.workspace_response import _LABELS
    from packages.core.services.workspace_execution_evidence import ExecutionObservationState
    from packages.core.strategist.execution_presentation import _STATE_LABELS

    for state in ExecutionObservationState:
        assert state.value in _LABELS and state.value in _STATE_LABELS
