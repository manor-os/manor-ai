"""DB-backed halves of request_input: the dispatcher keeps a parked step's
transcript, and a legacy task's answer flows through the shared service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.constants.execution import ExecutionStepStatus, WorkLeaseStatus
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task, TaskLog
from packages.core.models.user import User
from packages.core.models.worker import Worker, WorkLease
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.step_resume import resume_step_for_retry
from packages.core.services.task_blockers import answer_task_blocker, list_open_task_blockers
from packages.core.services.task_chat_hitl import (
    TASK_INPUT_ACTION_KEY,
    ensure_task_input_hitl,
)
from packages.core.services.task_input_resume import (
    TASK_INPUT_QUESTION_KEY,
    apply_task_input_answer,
)

pytestmark = pytest.mark.asyncio


async def _lease_fixture(db):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Lease WS", operating_model={}))
    worker_id = generate_ulid()
    db.add(Worker(
        id=worker_id, entity_id=entity_id, kind="internal", display_name="W",
        trust_level="trusted", status="active",
        capabilities={"supported_kinds": ["subagent"]}, consecutive_failures=0,
    ))
    plan_id, task_id, step_id, lease_id = (generate_ulid() for _ in range(4))
    db.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
        plan_dag={}, status="running",
    ))
    db.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        step_key="publish", kind="subagent", params={"prompt": "publish"},
        step_status="running", attempt_count=1, max_attempts=3, risk_level="medium",
        # The granted lease owns the running step (dispatcher grant state).
        current_lease_id=lease_id,
    ))
    db.add(WorkLease(
        id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    ))
    await db.flush()
    return {"entity_id": entity_id, "workspace_id": workspace_id, "plan_id": plan_id,
            "task_id": task_id, "step_id": step_id, "lease_id": lease_id}


def _checkpoint() -> dict:
    return {
        "schema_version": 4, "messages": [{"role": "system", "content": "s"}], "usage": {},
        "rounds": 1, "tool_calls_made": [],
        "pending_tool_call": {"id": "call-ask", "name": "request_input", "arguments": {}},
        "remaining_tool_calls": [], "pending_attempt": 1, "seen_tool_result_digests": {},
        "mcp_provider_error_rounds": {}, "disable_followup_tools": False,
        "tool_continuations": {}, "pending_tool_result": None,
    }


async def test_lease_pause_keeps_the_transcript_and_resume_preserves_it(db_session, monkeypatch) -> None:
    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr("packages.core.dispatcher.service._safe_chat_step_needs_human", quiet)
    monkeypatch.setattr("packages.core.dispatcher.service._safe_task_log", quiet)
    monkeypatch.setattr("packages.core.dispatcher.service._safe_task_event", quiet)
    fx = await _lease_fixture(db_session)

    await Dispatcher().lease_needs_human(
        db_session, fx["lease_id"], prompt="Which screenshot?", runtime_checkpoint=_checkpoint(),
    )
    await db_session.flush()
    step = await db_session.get(ExecutionStep, fx["step_id"])
    assert step.step_status == ExecutionStepStatus.WAITING_HUMAN.value
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"

    await resume_step_for_retry(
        db_session,
        entity_id=fx["entity_id"], user_id=fx["entity_id"], step_id=fx["step_id"],
        plan_id=fx["plan_id"], workspace_id=fx["workspace_id"],
        human_input_response={"choice": "respond", "note": "hero", "user": "Amy"},
        enqueue=False,
    )
    await db_session.flush()
    step = await db_session.get(ExecutionStep, fx["step_id"])
    assert step.step_status == ExecutionStepStatus.PENDING.value
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"
    assert step.human_input_response["note"] == "hero"

    # A cleared checkpoint is SQL NULL, not a JSON null (IS NULL must hold).
    from sqlalchemy import text

    step.runtime_checkpoint = None
    await db_session.flush()
    typeof = (await db_session.execute(
        text("select jsonb_typeof(runtime_checkpoint) from execution_steps where id = :id"),
        {"id": fx["step_id"]},
    )).scalar_one_or_none()
    assert typeof is None


async def test_step_resume_freeform_then_structured_replaces_answer_channel(
    db_session,
) -> None:
    fx = await _lease_fixture(db_session)
    step = await db_session.get(ExecutionStep, fx["step_id"])
    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
    step.current_lease_id = None
    step.params = {
        **step.params,
        "answers": {"stale": "structured"},
        "human_input_response": {"note": "stale params copy"},
    }
    await db_session.flush()

    await resume_step_for_retry(
        db_session,
        entity_id=fx["entity_id"],
        user_id=fx["entity_id"],
        step_id=fx["step_id"],
        plan_id=fx["plan_id"],
        workspace_id=fx["workspace_id"],
        human_input_response={"choice": "respond", "note": "new freeform"},
        enqueue=False,
    )
    await db_session.flush()
    assert "answers" not in step.params
    assert "human_input_response" not in step.params
    assert step.human_input_response == {
        "choice": "respond",
        "note": "new freeform",
    }

    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
    step.human_input_prompt = "Now provide fields"
    await db_session.flush()
    await resume_step_for_retry(
        db_session,
        entity_id=fx["entity_id"],
        user_id=fx["entity_id"],
        step_id=fx["step_id"],
        plan_id=fx["plan_id"],
        workspace_id=fx["workspace_id"],
        params_update={"answers": {"market": "US"}},
        enqueue=False,
    )
    await db_session.flush()

    db_session.expire_all()
    stored = await db_session.get(ExecutionStep, fx["step_id"])
    assert stored.params["answers"] == {"market": "US"}
    assert "human_input_response" not in stored.params
    assert stored.human_input_response is None


async def test_step_resume_structured_then_freeform_replaces_answer_channel(
    db_session,
) -> None:
    fx = await _lease_fixture(db_session)
    step = await db_session.get(ExecutionStep, fx["step_id"])
    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
    step.current_lease_id = None
    step.human_input_response = {"note": "stale freeform"}
    await db_session.flush()

    await resume_step_for_retry(
        db_session,
        entity_id=fx["entity_id"],
        user_id=fx["entity_id"],
        step_id=fx["step_id"],
        plan_id=fx["plan_id"],
        workspace_id=fx["workspace_id"],
        params_update={"answers": {"market": "EU"}},
        enqueue=False,
    )
    await db_session.flush()
    assert step.params["answers"] == {"market": "EU"}
    assert step.human_input_response is None

    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
    step.human_input_prompt = "Explain the change"
    await db_session.flush()
    await resume_step_for_retry(
        db_session,
        entity_id=fx["entity_id"],
        user_id=fx["entity_id"],
        step_id=fx["step_id"],
        plan_id=fx["plan_id"],
        workspace_id=fx["workspace_id"],
        human_input_response={"choice": "respond", "note": "use Canada"},
        enqueue=False,
    )
    await db_session.flush()

    db_session.expire_all()
    stored = await db_session.get(ExecutionStep, fx["step_id"])
    assert "answers" not in stored.params
    assert "human_input_response" not in stored.params
    assert stored.human_input_response == {
        "choice": "respond",
        "note": "use Canada",
    }


async def _legacy_task(db, *, with_workspace: bool = True):
    from packages.core.constants.agents import MANOR_AGENT_ID

    entity_id = generate_ulid()
    user_id = generate_ulid()
    db.add(User(id=user_id, entity_id=entity_id, email=f"u-{user_id}@example.com",
                password_hash="x", role="owner"))
    workspace_id = None
    if with_workspace:
        workspace_id = generate_ulid()
        db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Task WS", operating_model={}))
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        title="Find a unit", status=TaskStatus.WAITING_ON_CUSTOMER.value,
        agent_id=MANOR_AGENT_ID,
        details={TASK_INPUT_QUESTION_KEY: {"question": "Which unit?", "options": ["4C", "5A"]}},
    )
    db.add(task)
    await db.flush()
    return task, user_id


async def test_workspace_task_input_card_cannot_resume_through_legacy_loop(
    db_session,
    monkeypatch,
) -> None:
    posted: list[dict] = []

    async def fake_post(db, **kwargs):
        posted.append(kwargs)
        return None

    monkeypatch.setattr("packages.core.workspace_chat.service.post_message", fake_post)
    task, user_id = await _legacy_task(db_session)

    await ensure_task_input_hitl(db_session, task, question=task.details[TASK_INPUT_QUESTION_KEY])
    await ensure_task_input_hitl(db_session, task, question=task.details[TASK_INPUT_QUESTION_KEY])
    await db_session.flush()
    requests = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_task_id == task.id)
    )).scalars().all())
    assert len(requests) == 1
    assert requests[0].action_key == TASK_INPUT_ACTION_KEY
    assert requests[0].hitl_type == HitlType.INPUT.value
    assert requests[0].payload["question"] == "Which unit?"
    card = posted[0]["pending_action"]
    assert card["kind"] == "human_input" and card["task_id"] == task.id and "step_id" not in card
    assert card["options"] == ["4C", "5A"]

    # Chat prompt injection sees it as an answerable blocker.
    blockers = await list_open_task_blockers(
        db_session, entity_id=task.entity_id, workspace_id=task.workspace_id,
    )
    assert [b["request_id"] for b in blockers] == [requests[0].id]
    assert blockers[0]["answerable_via_tool"] is True

    applied = await apply_task_input_answer(
        db_session, task=task, user_id=user_id, user_label="Amy",
        response_text="4C", payload={"response": "4C", "source": "test"},
    )
    await db_session.flush()
    assert applied == {"resumed": False, "dispatch_agent_id": None}
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.status == TaskStatus.WAITING_ON_CUSTOMER.value
    assert "_hitl_response" not in refreshed.details
    assert refreshed.details[TASK_INPUT_QUESTION_KEY]["question"] == "Which unit?"
    request = await db_session.get(HitlRequest, requests[0].id)
    assert request.status == ApprovalStatus.PENDING.value
    logs = list((await db_session.execute(
        select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == TaskLogType.AI_HITL_RESUMED)
    )).scalars().all())
    assert logs == []

    # A second answer finds nothing waiting.
    again = await apply_task_input_answer(
        db_session, task=refreshed, user_id=user_id, user_label="Amy", response_text="5A",
    )
    assert again["resumed"] is False


async def test_chat_blocker_cannot_reenter_legacy_loop_for_workspace_task(
    db_session,
    monkeypatch,
) -> None:
    async def fake_post(db, **kwargs):
        return None

    monkeypatch.setattr("packages.core.workspace_chat.service.post_message", fake_post)
    task, user_id = await _legacy_task(db_session)
    await ensure_task_input_hitl(db_session, task, question=task.details[TASK_INPUT_QUESTION_KEY])
    await db_session.flush()
    request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_task_id == task.id)
    )).scalar_one()

    result = await answer_task_blocker(
        db_session, entity_id=task.entity_id, user_id=user_id, user_display="Amy",
        request_id=request.id, answer="4C",
    )
    await db_session.flush()
    assert result == {
        "resolved": False,
        "reason": "task_not_waiting",
        "request_id": request.id,
    }
    assert (await db_session.get(Task, task.id)).status == TaskStatus.WAITING_ON_CUSTOMER.value
    assert (await db_session.get(HitlRequest, request.id)).status == ApprovalStatus.PENDING.value




async def test_ownerless_workspace_team_task_does_not_take_legacy_resume(
    db_session,
) -> None:
    task, user_id = await _legacy_task(db_session, with_workspace=True)
    agent = Agent(
        id=generate_ulid(),
        entity_id=task.entity_id,
        name="Workspace service",
        status="active",
    )
    db_session.add_all([
        agent,
        AgentSubscription(
            id=generate_ulid(),
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            agent_id=agent.id,
            service_key="workspace_service",
            status="active",
        ),
    ])
    await db_session.flush()

    applied = await apply_task_input_answer(
        db_session,
        task=task,
        user_id=user_id,
        user_label="Amy",
        response_text="4C",
    )

    assert applied == {"resumed": False, "dispatch_agent_id": None}
    assert task.status == TaskStatus.WAITING_ON_CUSTOMER.value


async def test_refusing_workspace_task_question_does_not_dispatch_legacy_loop(
    db_session,
    monkeypatch,
) -> None:
    async def fake_post(db, **kwargs):
        return None

    monkeypatch.setattr("packages.core.workspace_chat.service.post_message", fake_post)
    task, user_id = await _legacy_task(db_session)
    await ensure_task_input_hitl(db_session, task, question=task.details[TASK_INPUT_QUESTION_KEY])
    await db_session.flush()
    request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_task_id == task.id)
    )).scalar_one()

    result = await answer_task_blocker(
        db_session, entity_id=task.entity_id, user_id=user_id, request_id=request.id, refuse=True,
    )
    await db_session.flush()
    assert result["outcome"] == "refused"
    assert "_task_to_run" not in result
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.status == TaskStatus.WAITING_ON_CUSTOMER.value
    assert "_hitl_response" not in refreshed.details
    assert (await db_session.get(HitlRequest, request.id)).status == ApprovalStatus.DENIED.value
