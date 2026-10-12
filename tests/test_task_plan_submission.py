"""Workspace Plan APIs submit to V3 instead of creating new V2 Plans."""
from __future__ import annotations

import pytest
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from httpx import AsyncClient
from sqlalchemy import func, select

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.task_plan_submission import TaskPlanSubmission
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.workspace import Workspace
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.planner import CapabilityError, PlannerError, admit_manual_task_plan
from packages.core.plans.service import create_plan_from_dag
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake,
    ensure_task_coordinator_run,
    release_task_coordinator_claim,
    stage_task_coordinator_wake,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import (
    resolve_task_execution,
    task_execution_intent_dedupe_key,
    task_execution_intent_generation,
)
from packages.core.workers import task_coordinator as coordinator_worker
from packages.core.services.task_service import create_task
from packages.core.services.workflow_run_execution_claim import WorkflowRunExecutionClaim
from packages.core.services.task_plan_submission import (
    carry_pending_manual_submission_for_retry,
    latest_task_plan_submission,
    pending_task_plan_submission,
)
from tests.test_document_permissions import _auth
from tests.test_workspace_write_authz import (
    _add_workspace_plan_runtime,
    _make_workspace,
    _me,
)


async def _runnable_task(client: AsyncClient, suffix: str):
    headers = await _auth(client, f"v3_plan_submit_{suffix}")
    me = await _me(client, headers)
    entity_id = me["entity_id"]
    owner_id = me.get("user_id") or me.get("id")
    workspace_id = await _make_workspace(entity_id, f"V3 Plan submission {suffix}", owner_id)
    async with db_module.async_session() as db:
        subscription = await _add_workspace_plan_runtime(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )
        task = await create_task(
            db, entity_id, title=f"V3 Plan {suffix}", workspace_id=workspace_id,
            owner_service_key=subscription.service_key,
            owner_subscription_id=subscription.id,
        )
        task.status = "in_progress"
        await db.commit()
        return headers, task.id, workspace_id


async def _claim_manual_submission(db, task: Task):
    dispatch = await resolve_task_execution(db, task)
    assert dispatch.coordinator is not None
    await ensure_task_coordinator_run(db, task=task, selection=dispatch.coordinator)
    wake = await stage_task_coordinator_wake(db, task_id=task.id, reason="manual_gate_test")
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db,
        task_id=task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="manual-gate-test-owner",
        selection=dispatch.coordinator,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    return claim


def test_workspace_plan_api_advertises_202_receipts_in_openapi():
    from apps.api.main import app

    paths = app.openapi()["paths"]
    for path in ("/api/v1/plans", "/api/v1/plans/from-task/{task_id}"):
        responses = paths[path]["post"]["responses"]
        assert "201" in responses  # Taskless legacy Plan remains synchronous.
        assert "202" in responses
        schema = responses["202"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/TaskPlanAdmissionResponse")


@pytest.mark.asyncio
async def test_from_task_accepts_one_durable_v3_planner_request(client: AsyncClient):
    headers, task_id, _ = await _runnable_task(client, "planner")
    url = f"/api/v1/plans/from-task/{task_id}"
    first = await client.post(url, headers=headers, json={"execution_mode": "live"})
    assert first.status_code == 202, first.text
    receipt = first.json()
    assert receipt["task_id"] == task_id
    assert receipt["status"] == "pending"
    assert receipt["plan_id"] is None
    assert receipt["runtime"] == "task_coordinator_v3"

    duplicate = await client.post(url, headers=headers, json={"execution_mode": "live"})
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["submission_id"] == receipt["submission_id"]
    changed = await client.post(url, headers=headers, json={"execution_mode": "sandbox"})
    assert changed.status_code == 409

    async with db_module.async_session() as db:
        requests = list((await db.execute(select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task_id,
        ))).scalars())
        plans = list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars())
        assert len(requests) == 1
        assert requests[0].id == receipt["submission_id"]
        assert requests[0].request_kind == "planner"
        assert requests[0].plan_dag is None
        assert plans == []


@pytest.mark.asyncio
async def test_parallel_plan_requests_share_one_immutable_receipt(client: AsyncClient):
    headers, task_id, _ = await _runnable_task(client, "parallel")
    url = f"/api/v1/plans/from-task/{task_id}"
    responses = await asyncio.gather(*(
        client.post(url, headers=headers, json={"execution_mode": "live"})
        for _ in range(4)
    ))
    assert all(response.status_code == 202 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert len({response.json()["submission_id"] for response in responses}) == 1
    async with db_module.async_session() as db:
        submissions = list((await db.execute(select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task_id,
        ))).scalars())
        assert len(submissions) == 1
        assert submissions[0].id == responses[0].json()["submission_id"]


@pytest.mark.asyncio
async def test_manual_dag_is_frozen_and_rejects_runtime_marker(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "manual")
    url = "/api/v1/plans"
    request = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "review_brief", "kind": "human",
            "params": {"prompt": "Review the brief"},
        }]},
    }
    first = await client.post(url, headers=headers, json=request)
    assert first.status_code == 202, first.text
    receipt = first.json()
    duplicate = await client.post(url, headers=headers, json=request)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["submission_id"] == receipt["submission_id"]

    changed = await client.post(url, headers=headers, json={
        **request,
        "plan": {"steps": [{
            "key": "different_review", "kind": "human",
            "params": {"prompt": "Review a different brief"},
        }]},
    })
    assert changed.status_code == 409, changed.text
    marker = await client.post(url, headers=headers, json={
        **request,
        "plan": {"steps": [{
            "key": "review_brief", "kind": "human",
            "params": {"prompt": "Review the brief", "_coordinator_child": True},
        }]},
    })
    assert marker.status_code == 400, marker.text
    tool_grant = await client.post(url, headers=headers, json={
        **request,
        "plan": {"steps": [{
            "key": "review_brief", "kind": "human",
            "params": {"prompt": "Review the brief", "allowed_tools": ["publish"]},
        }]},
    })
    assert tool_grant.status_code == 400, tool_grant.text

    async with db_module.async_session() as db:
        requests = list((await db.execute(select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task_id,
        ))).scalars())
        assert len(requests) == 1
        assert requests[0].plan_dag["steps"][0]["key"] == "review_brief"
        assert requests[0].plan_dag["steps"][0]["params"] == {"prompt": "Review the brief"}
        assert list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars()) == []


@pytest.mark.asyncio
async def test_manual_consume_rechecks_required_steps_and_workspace_lifecycle(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "manual_gate")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "human_check", "kind": "human", "params": {"prompt": "Check"},
        }]},
    })
    assert response.status_code == 202, response.text
    submission_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        claim = await _claim_manual_submission(db, task)
        task.details = {**(task.details or {}), "required_plan_steps": [{"key": "missing_step"}]}
        await db.flush()
        with pytest.raises(PlannerError, match="missing required step"):
            await admit_manual_task_plan(
                db, task_id=task_id,
                submission_id=submission_id, task_coordinator_claim=claim,
            )
        assert not list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars())
        workspace = await db.get(Workspace, workspace_id)
        workspace.status = "archived"
        task.details = {}
        await db.flush()
        with pytest.raises(PlannerError, match="Workspace is unavailable"):
            await admit_manual_task_plan(
                db, task_id=task_id,
                submission_id=submission_id, task_coordinator_claim=claim,
            )
        assert not list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars())


@pytest.mark.asyncio
async def test_manual_consume_rejects_unbound_external_action(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "manual_action")
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        service_key = task.owner_service_key
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "publish", "kind": "action", "service_key": service_key,
            "provider": "instagram", "action_key": "publish_reel",
            "params": {"caption": "Test"},
        }]},
    })
    assert response.status_code == 202, response.text
    submission_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        claim = await _claim_manual_submission(db, task)
        with pytest.raises(CapabilityError, match="not available"):
            await admit_manual_task_plan(
                db, task_id=task_id,
                submission_id=submission_id, task_coordinator_claim=claim,
            )
        assert not list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars())


@pytest.mark.asyncio
async def test_explicit_retry_carries_only_unapplied_manual_dag(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "retry")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "human_check", "kind": "human",
            "params": {"prompt": "Check"},
        }]},
    })
    assert response.status_code == 202, response.text
    original_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        await carry_pending_manual_submission_for_retry(db, task=task)
        renewed = await pending_task_plan_submission(db, task=task)
        assert renewed is not None
        assert renewed.id != original_id
        assert renewed.source_submission_id == original_id
        assert renewed.plan_dag["steps"][0]["key"] == "human_check"
        renewed.state = "applied"
        renewed.plan_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        await carry_pending_manual_submission_for_retry(db, task=task)
        assert await pending_task_plan_submission(db, task=task) is None


@pytest.mark.asyncio
async def test_retry_never_resurrects_manual_dag_behind_newer_planner_receipt(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "newer_planner")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "human_check", "kind": "human", "params": {"prompt": "Check"},
        }]},
    })
    assert response.status_code == 202, response.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.add(TaskPlanSubmission(
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            task_id=task.id,
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            lifecycle_generation=task_execution_intent_generation(task),
            request_kind="planner",
            requested_by_user_id=response.json().get("requested_by_user_id") or "test-user",
            execution_mode="live",
            approval_required=False,
            plan_dag=None,
            request_fingerprint="newer-planner-request",
            state="pending",
        ))
        await db.flush()
        task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        await carry_pending_manual_submission_for_retry(db, task=task)
        assert await pending_task_plan_submission(db, task=task) is None


@pytest.mark.asyncio
async def test_latest_receipt_uses_insert_time_after_task_lock_wait(client: AsyncClient):
    _headers, task_id, _workspace_id = await _runnable_task(client, "receipt_order")
    older_started = asyncio.Event()
    newer_has_task_lock = asyncio.Event()
    older_attempting_task_lock = asyncio.Event()
    ids = {"first": generate_ulid(), "second": generate_ulid()}

    async def older_transaction_writes_second():
        async with db_module.async_session() as db:
            # PostgreSQL now() is fixed at transaction start, before the
            # competing writer; this is the ordering trap under contention.
            await db.execute(select(func.now()))
            older_started.set()
            await newer_has_task_lock.wait()
            older_attempting_task_lock.set()
            task = (await db.execute(select(Task).where(Task.id == task_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalar_one()
            task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            db.add(TaskPlanSubmission(
                id=ids["second"], task_id=task.id, entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                lifecycle_generation=task_execution_intent_generation(task),
                request_kind="planner", requested_by_user_id=generate_ulid(),
                execution_mode="live", approval_required=False, plan_dag=None,
                request_fingerprint="later-insert", state="pending",
            ))
            await db.commit()

    async def newer_transaction_writes_first():
        await older_started.wait()
        async with db_module.async_session() as db:
            task = (await db.execute(select(Task).where(Task.id == task_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalar_one()
            db.add(TaskPlanSubmission(
                id=ids["first"], task_id=task.id, entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                lifecycle_generation=task_execution_intent_generation(task),
                request_kind="planner", requested_by_user_id=generate_ulid(),
                execution_mode="live", approval_required=False, plan_dag=None,
                request_fingerprint="earlier-insert", state="pending",
            ))
            await db.flush()
            newer_has_task_lock.set()
            await older_attempting_task_lock.wait()
            await asyncio.sleep(0.05)
            await db.commit()

    await asyncio.gather(older_transaction_writes_second(), newer_transaction_writes_first())
    async with db_module.async_session() as db:
        first = await db.get(TaskPlanSubmission, ids["first"])
        second = await db.get(TaskPlanSubmission, ids["second"])
        assert second.created_at > first.created_at
        latest = await latest_task_plan_submission(db, task_id=task_id)
        assert latest.id == ids["second"]


@pytest.mark.asyncio
async def test_rejected_manual_dag_replacement_is_atomic_and_competing_writes_conflict(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
):
    headers, task_id, workspace_id = await _runnable_task(client, "rejected_replacement")
    original_request = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "invalid_review", "kind": "human",
            "params": {"prompt": "Review invalid draft"},
        }]},
    }
    original = await client.post("/api/v1/plans", headers=headers, json=original_request)
    assert original.status_code == 202, original.text
    original_id = original.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        claim = await _claim_manual_submission(db, task)
        await db.commit()
    monkeypatch.setattr(coordinator_worker, "create_worker_session", lambda: db_module.async_session)
    blocked = await coordinator_worker._block_repair_required(
        claim, reason="Submitted manual Plan cannot execute: missing required step",
        rejected_submission_id=original_id,
    )
    assert blocked is True
    async with db_module.async_session() as db:
        assert await release_task_coordinator_claim(
            db, task_id=task_id, claim_token=claim.claim_token,
            claim_generation=claim.wake_generation,
        )
        await db.commit()
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        rejected = await db.get(TaskPlanSubmission, original_id)
        assert task.status == "blocked"
        assert rejected.state == "rejected"
        assert "missing required step" in rejected.rejection_reason
        rejected_generation = rejected.lifecycle_generation
        assert rejected_generation != task_execution_intent_generation(task)

    unchanged = await client.post("/api/v1/plans", headers=headers, json=original_request)
    assert unchanged.status_code == 409, unchanged.text
    corrected_requests = [
        {
            **original_request,
            "plan": {"steps": [{
                "key": f"corrected_review_{index}", "kind": "human",
                "params": {"prompt": f"Review corrected draft {index}"},
            }]},
        }
        for index in (1, 2)
    ]
    responses = await asyncio.gather(*(
        client.post("/api/v1/plans", headers=headers, json=request)
        for request in corrected_requests
    ))
    assert sorted(response.status_code for response in responses) == [202, 409], [
        (response.status_code, response.text) for response in responses
    ]
    winner = next(response for response in responses if response.status_code == 202)
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        rejected = await db.get(TaskPlanSubmission, original_id)
        receipts = list((await db.execute(select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task_id,
        ))).scalars())
        assert task.status == "in_progress"
        assert len(receipts) == 2
        assert rejected.state == "rejected"
        assert rejected.plan_dag["steps"][0]["key"] == "invalid_review"
        replacement = next(row for row in receipts if row.id != original_id)
        assert replacement.id == winner.json()["submission_id"]
        assert replacement.state == "pending"
        assert replacement.source_submission_id == original_id
        assert replacement.lifecycle_generation == task_execution_intent_generation(task)
        assert replacement.request_fingerprint != rejected.request_fingerprint
        wake_rows = list((await db.execute(select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_execution_intent_dedupe_key(
                task_id, task_execution_intent_generation(task),
            ),
        ))).scalars())
        assert len(wake_rows) == 1


@pytest.mark.asyncio
async def test_v3_plan_approval_wakes_task_coordinator_not_only_plan(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "approval")
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        dispatch = await resolve_task_execution(db, task)
        assert dispatch.coordinator is not None
        run = await ensure_task_coordinator_run(
            db, task=task, selection=dispatch.coordinator,
        )
        wake = await stage_task_coordinator_wake(db, task_id=task_id, reason="test_plan_approval")
        assert wake is not None
        claim, reason = await claim_task_coordinator_wake(
            db,
            task_id=task_id,
            event_id=wake.event_id,
            wake_generation=wake.wake_generation,
            claim_token="approval-test-owner",
            selection=dispatch.coordinator,
            lease_seconds=60,
        )
        assert reason == "claimed" and claim is not None
        plan = await create_plan_from_dag(
            db,
            entity_id=task.entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_subscription_id=dispatch.coordinator.root.subscription_id,
            plan=Plan(steps=[PlanStep(
                key="human_review", kind="human", params={"prompt": "Approve"},
            )]),
            execution_mode="live",
            approval_required=True,
            task_coordinator_claim=claim,
        )
        await db.commit()
        plan_id = plan.id
        old_generation = run.lifecycle_generation

    approval = await client.post(f"/api/v1/plans/{plan_id}/approve", headers=headers)
    assert approval.status_code == 200, approval.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        run = (await db.execute(select(TaskCoordinatorRun).where(
            TaskCoordinatorRun.task_id == task_id,
        ))).scalar_one()
        plan = await db.get(ExecutionPlan, plan_id)
        assert task.status == "in_progress"
        assert plan.status == "draft"
        assert run.lifecycle_generation == task_execution_intent_generation(task)
        assert run.lifecycle_generation != old_generation
        assert run.wake_generation is not None
        wake_event = (await db.execute(select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task_id),
        ))).scalar_one()
        assert wake_event.payload["wake_generation"] == run.wake_generation


@pytest.mark.asyncio
async def test_task_run_consumes_manual_submission_as_direct_source_dag(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
):
    headers, task_id, workspace_id = await _runnable_task(client, "consume")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "review_output", "kind": "human",
            "params": {"prompt": "Review output"},
        }]},
    })
    assert response.status_code == 202, response.text
    submission_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        dispatch = await resolve_task_execution(db, task)
        run = await ensure_task_coordinator_run(db, task=task, selection=dispatch.coordinator)
        run.checkpoint = {
            "messages": [{"role": "assistant", "content": "Prior checkpoint"}],
            "rounds": 1,
        }
        wake = await stage_task_coordinator_wake(db, task_id=task_id, reason="consume_manual_submission")
        await db.commit()
        run_id = run.id

    @asynccontextmanager
    async def fake_plan_claim(_task_id):
        async def fence_for_commit(_db):
            return None

        yield SimpleNamespace(fence_for_commit=fence_for_commit, mark_terminal_committed=lambda: None)

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    async def fake_loop(**kwargs):
        assert kwargs["forced_tool_calls"] == [{"name": "plan_task", "arguments": {}}]
        assert kwargs["initial_messages"] == [{"role": "assistant", "content": "Prior checkpoint"}]
        planned = json.loads(await kwargs["tool_executor"]("plan_task", {}))
        assert planned["status"] == "planned"
        assert planned["plan_status"] == "draft"
        return SimpleNamespace(
            stop_reason="round_limit", rounds=coordinator_worker._MAX_ROUNDS,
            messages=[], usage={},
            tool_calls_made=["plan_task"],
        )

    monkeypatch.setattr(coordinator_worker, "create_worker_session", lambda: db_module.async_session)
    monkeypatch.setattr(coordinator_worker, "task_plan_execution_claim", fake_plan_claim)
    monkeypatch.setattr(coordinator_worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(coordinator_worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None, model=None,
        llm_metadata={},
    )))
    monkeypatch.setattr(coordinator_worker, "runtime_execute_agentic_loop", fake_loop)
    outcome = await coordinator_worker.run_task_coordinator_once(
        event_id=wake.event_id, task_id=task_id, wake_generation=wake.wake_generation,
    )
    assert outcome["status"] == "blocked"  # Fake loop deliberately omits submit_task.
    async with db_module.async_session() as db:
        submission = await db.get(TaskPlanSubmission, submission_id)
        plan = await db.get(ExecutionPlan, submission.plan_id)
        assert submission.state == "applied"
        assert plan.dispatcher_state["task_coordinator_run_id"] == run_id
        assert [step["key"] for step in plan.plan_dag["steps"]] == ["review_output"]


@pytest.mark.asyncio
async def test_task_run_does_not_consume_manual_dag_when_billable_claim_is_held(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
):
    headers, task_id, workspace_id = await _runnable_task(client, "held_plan_claim")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "manual_review", "kind": "human",
            "params": {"prompt": "Review before execution"},
        }]},
    })
    assert response.status_code == 202, response.text
    submission_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        selection = (await resolve_task_execution(db, task)).coordinator
        assert selection is not None
        await ensure_task_coordinator_run(db, task=task, selection=selection)
        wake = await stage_task_coordinator_wake(
            db, task_id=task_id, reason="held_plan_claim_test",
        )
        await db.commit()

    @asynccontextmanager
    async def held_plan_claim(_task_id):
        yield WorkflowRunExecutionClaim(
            run_id=task_id, token="", granted=False, reason="claim_held_by_live_execution",
            lost=asyncio.Event(), terminal_committed=asyncio.Event(),
        )

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    async def fake_loop(**kwargs):
        assert kwargs["forced_tool_calls"] == [{"name": "plan_task", "arguments": {}}]
        await kwargs["tool_executor"]("plan_task", {})
        raise AssertionError("held planning claim unexpectedly returned a Plan")

    manual_persist = AsyncMock(side_effect=AssertionError("manual DAG persisted without claim"))
    provider = AsyncMock(side_effect=AssertionError("Planner ran without claim"))
    monkeypatch.setattr(coordinator_worker, "create_worker_session", lambda: db_module.async_session)
    monkeypatch.setattr(coordinator_worker, "task_plan_execution_claim", held_plan_claim)
    monkeypatch.setattr(coordinator_worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(coordinator_worker, "admit_manual_task_plan", manual_persist)
    monkeypatch.setattr(coordinator_worker, "plan_task", provider)
    monkeypatch.setattr(coordinator_worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None, model=None,
        llm_metadata={},
    )))
    monkeypatch.setattr(coordinator_worker, "runtime_execute_agentic_loop", fake_loop)
    with pytest.raises(RuntimeError, match="Task Planner claim is held"):
        await coordinator_worker.run_task_coordinator_once(
            event_id=wake.event_id, task_id=task_id,
            wake_generation=wake.wake_generation,
        )
    manual_persist.assert_not_awaited()
    provider.assert_not_awaited()
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        submission = await db.get(TaskPlanSubmission, submission_id)
        plans = list((await db.execute(select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_id,
        ))).scalars())
        assert task.status == "in_progress"
        assert submission.state == "pending" and submission.plan_id is None
        assert plans == []


@pytest.mark.asyncio
async def test_public_task_retry_carries_pending_manual_submission(client: AsyncClient):
    headers, task_id, workspace_id = await _runnable_task(client, "public_retry")
    response = await client.post("/api/v1/plans", headers=headers, json={
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {"steps": [{
            "key": "review_again", "kind": "human",
            "params": {"prompt": "Review again"},
        }]},
    })
    assert response.status_code == 202, response.text
    old_submission_id = response.json()["submission_id"]
    async with db_module.async_session() as db:
        from packages.core.services.task_state_machine import apply_task_status_transition

        task = await db.get(Task, task_id)
        await apply_task_status_transition(task, "blocked", db=db, actor_kind="system")
        await db.commit()

    retry = await client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers, json={})
    assert retry.status_code == 200, retry.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        current = await pending_task_plan_submission(db, task=task)
        assert task.status == "in_progress"
        assert current is not None
        assert current.id != old_submission_id
        assert current.source_submission_id == old_submission_id
        assert current.plan_dag["steps"][0]["key"] == "review_again"
