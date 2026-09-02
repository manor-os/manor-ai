"""answer_task_blocker — the chat↔task HITL bridge's core service.

Guards the CLASS of lease-closeable kinds, not one instance: every kind in
LEASE_HITL_CLOSEABLE_KINDS must have an explicit branch here (answer,
confirm, or a deliberate refusal-to-auto-grant), so a kind added to the
mint side cannot silently fall through the chat bridge.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS
from packages.core.constants.approvals import (
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
)
from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.task_blockers import (
    ANSWERABLE_BLOCKER_KINDS,
    KIND_NEEDS_LOGIN,
    answer_task_blocker,
    list_open_task_blockers,
)

pytestmark = pytest.mark.asyncio


def _mk_ids():
    entity_id = generate_ulid()
    return {
        "entity_id": entity_id,
        "workspace_id": generate_ulid(),
        "user_id": entity_id,
    }


async def _seed_blocker(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    pending_kind: str,
    status: str = ApprovalStatus.PENDING.value,
    origin_kind: str = ApprovalOriginKind.LEASE.value,
):
    if await db.get(Workspace, workspace_id) is None:
        db.add(Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Task blocker workspace",
            operating_model={},
        ))
    user_id = entity_id
    if await db.get(User, user_id) is None:
        db.add(User(
            id=user_id,
            entity_id=entity_id,
            email=f"task-blocker-{entity_id}@example.com",
            password_hash="x",
            role="owner",
        ))
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        status=ExecutionPlanStatus.PAUSED.value,
        plan_dag={},
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="publish",
        kind="subagent",
        step_status=ExecutionStepStatus.WAITING_HUMAN.value,
        human_input_prompt="Which screenshot should I use?",
        params={"prompt": "publish"},
    )
    request = HitlRequest(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key="social_post.publish",
        risk_level="medium",
        origin_kind=origin_kind,
        origin_step_id=step.id,
        origin_plan_id=plan.id,
        origin_task_id=None,
        status=status,
        dedup_key=f"lease:{generate_ulid()}:{pending_kind}",
        reason="Which screenshot should I use?",
        hitl_type=HitlType.INPUT.value,
        context={"pending_kind": pending_kind},
        payload={"question": "Which screenshot?", "why": "publish needs one"},
    )
    db.add_all([plan, step, request])
    await db.flush()
    return plan, step, request


async def test_every_lease_closeable_kind_has_an_explicit_bridge_outcome(db_session):
    """The mint-side kind set and the bridge's handling must stay in
    lockstep: an answerable kind resumes the step; needs_login is refused
    with an explanation and its request stays PENDING (nothing was
    approved). A kind with neither behavior would strand its card."""
    ids = _mk_ids()
    for kind in sorted(LEASE_HITL_CLOSEABLE_KINDS):
        plan, step, request = await _seed_blocker(
            db_session, entity_id=ids["entity_id"],
            workspace_id=ids["workspace_id"], pending_kind=kind,
        )
        result = await answer_task_blocker(
            db_session,
            entity_id=ids["entity_id"],
            user_id=ids["user_id"],
            request_id=request.id,
            answer="use the landing page screenshot",
            answers={"screenshot": "landing page"},
            confirm=True,
        )
        await db_session.flush()
        refreshed = (await db_session.execute(
            select(HitlRequest).where(HitlRequest.id == request.id)
        )).scalar_one()
        fresh_step = (await db_session.execute(
            select(ExecutionStep).where(ExecutionStep.id == step.id)
        )).scalar_one()

        if kind in ANSWERABLE_BLOCKER_KINDS:
            assert result["resolved"] is True, (kind, result)
            assert refreshed.status == ApprovalStatus.CONSUMED.value, kind
            assert fresh_step.step_status == ExecutionStepStatus.PENDING.value, kind
        else:
            assert kind == KIND_NEEDS_LOGIN
            assert result["resolved"] is False
            assert result["reason"] == "needs_login_requires_card_flow"
            assert refreshed.status == ApprovalStatus.PENDING.value
            assert fresh_step.step_status == ExecutionStepStatus.WAITING_HUMAN.value


async def test_answer_writes_response_and_revives_plan(db_session):
    ids = _mk_ids()
    plan, step, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
    )
    result = await answer_task_blocker(
        db_session,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        user_display="Calvin",
        request_id=request.id,
        answer="use the landing page screenshot",
    )
    await db_session.flush()
    assert result["resolved"] is True
    assert result["_plan_to_run"] == plan.id
    fresh_step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step.id)
    )).scalar_one()
    assert fresh_step.human_input_response["note"] == "use the landing page screenshot"
    assert fresh_step.human_input_response["via"] == "chat_bridge"
    fresh_plan = (await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan.id)
    )).scalar_one()
    assert fresh_plan.status == ExecutionPlanStatus.RUNNING.value
    fresh_request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == request.id)
    )).scalar_one()
    assert fresh_request.decided_via == "chat_bridge"


async def test_needs_input_merges_answers_into_step_params(db_session):
    ids = _mk_ids()
    _, step, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="needs_input",
    )
    result = await answer_task_blocker(
        db_session,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        request_id=request.id,
        answers={"recipient": "revanth@example.com"},
    )
    await db_session.flush()
    assert result["resolved"] is True
    fresh_step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step.id)
    )).scalar_one()
    assert fresh_step.params["answers"] == {"recipient": "revanth@example.com"}
    # Pre-existing params survive the merge.
    assert fresh_step.params["prompt"] == "publish"


async def test_needs_confirmation_requires_explicit_confirm(db_session):
    ids = _mk_ids()
    _, step, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="needs_confirmation",
    )
    withheld = await answer_task_blocker(
        db_session,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        request_id=request.id,
        answer="hmm let me think",
    )
    assert withheld["resolved"] is False
    assert withheld["reason"] == "confirmation_required"
    fresh_request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == request.id)
    )).scalar_one()
    assert fresh_request.status == ApprovalStatus.PENDING.value


async def test_refuse_denies_request_and_fails_step(db_session):
    ids = _mk_ids()
    _, step, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
    )
    result = await answer_task_blocker(
        db_session,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        request_id=request.id,
        answer="don't publish this",
        refuse=True,
    )
    await db_session.flush()
    assert result == {
        "resolved": True,
        "decision": "refused",
        "request_id": request.id,
        "task_id": None,
        "_plan_to_run": step.plan_id,
    }
    fresh_request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == request.id)
    )).scalar_one()
    assert fresh_request.status == ApprovalStatus.DENIED.value
    fresh_step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step.id)
    )).scalar_one()
    assert fresh_step.step_status == ExecutionStepStatus.FAILED.value
    assert fresh_step.error["type"] == "UserSkipped"


async def test_second_answer_is_idempotent_not_double_spent(db_session):
    ids = _mk_ids()
    _, _, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
    )
    first = await answer_task_blocker(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"],
        request_id=request.id, answer="answer one",
    )
    assert first["resolved"] is True
    second = await answer_task_blocker(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"],
        request_id=request.id, answer="answer two",
    )
    assert second["resolved"] is False
    assert second["reason"] == f"already_{ApprovalStatus.CONSUMED.value}"


async def test_wrong_entity_and_non_lease_requests_are_rejected(db_session):
    ids = _mk_ids()
    _, _, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
    )
    cross_entity = await answer_task_blocker(
        db_session, entity_id=generate_ulid(), user_id=ids["user_id"],
        request_id=request.id, answer="hi",
    )
    assert cross_entity == {"resolved": False, "reason": "request_not_found"}

    _, _, step_origin = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
        origin_kind=ApprovalOriginKind.STEP.value,
    )
    governance = await answer_task_blocker(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"],
        request_id=step_origin.id, answer="approve it",
    )
    assert governance["resolved"] is False
    assert governance["reason"] == "not_a_task_blocker"


async def test_list_open_task_blockers_scopes_and_shapes(db_session):
    ids = _mk_ids()
    _, _, request = await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="needs_input",
    )
    # A consumed row and a foreign-workspace row must not appear.
    await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"], pending_kind="human_input",
        status=ApprovalStatus.CONSUMED.value,
    )
    await _seed_blocker(
        db_session, entity_id=ids["entity_id"],
        workspace_id=generate_ulid(), pending_kind="human_input",
    )
    blockers = await list_open_task_blockers(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
    )
    assert [b["request_id"] for b in blockers] == [request.id]
    entry = blockers[0]
    assert entry["pending_kind"] == "needs_input"
    assert entry["answerable_via_tool"] is True
    assert entry["reason"].startswith("Which screenshot")


async def test_runtime_blocker_dispatches_only_after_commit_and_recovers_failure(
    monkeypatch,
):
    from packages.core.ai.runtime.workspace_blocker_actions import (
        runtime_workspace_answer_task_blocker_action,
    )
    from packages.core.services import task_blockers, task_retry_service
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    events: list[str] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    async def fake_answer(_db, **_kwargs):
        events.append("resolve")
        return {
            "resolved": True,
            "decision": "answered",
            "_plan_to_run": "plan-1",
        }

    def fail_dispatch(plan_id: str):
        assert plan_id == "plan-1"
        events.append("dispatch")
        raise RuntimeError("broker unavailable")

    async def fake_mark(_db, **kwargs):
        assert kwargs == {
            "plan_id": "plan-1",
            "user_id": "user-1",
            "reason": "task_blocker_dispatch_failed",
        }
        events.append("recover")
        return True

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(task_blockers, "answer_task_blocker", fake_answer)
    monkeypatch.setattr(ai_tasks.run_plan, "delay", fail_dispatch)
    monkeypatch.setattr(
        task_retry_service,
        "mark_plan_continuation_dispatch_failed",
        fake_mark,
    )

    payload = json.loads(await runtime_workspace_answer_task_blocker_action(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        params={"request_id": "request-1", "answer": "continue"},
    ))

    assert payload == {
        "resolved": True,
        "decision": "answered",
        "dispatched": False,
    }
    assert events == ["resolve", "commit", "dispatch", "recover", "commit"]
