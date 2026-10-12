"""Task detail renders only exact, current ordinary-input Chat cards."""
from datetime import datetime, timedelta, timezone

import pytest

from apps.api.routers.tasks import _task_input_requests
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace


pytestmark = pytest.mark.asyncio


async def _task(db):
    workspace = Workspace(entity_id=generate_ulid(), name="Input projection")
    db.add(workspace)
    await db.flush()
    task = Task(entity_id=workspace.entity_id, workspace_id=workspace.id,
                title="Choose and deliver", status="in_progress", priority=2)
    db.add(task)
    await db.flush()
    plan = ExecutionPlan(entity_id=task.entity_id, workspace_id=workspace.id,
                         task_id=task.id, status="running", plan_dag={})
    db.add(plan)
    await db.flush()
    return task, plan


async def _card(db, task, plan):
    now = datetime.now(timezone.utc)
    step = ExecutionStep(entity_id=task.entity_id, workspace_id=task.workspace_id,
                         plan_id=plan.id, step_key=generate_ulid(), kind="subagent",
                         step_status="waiting_human", params={"prompt": "Ask for a color"},
                         human_input_prompt="Which color?", attempt_count=1)
    db.add(step)
    await db.flush()
    lease = WorkLease(entity_id=task.entity_id, workspace_id=task.workspace_id,
                      plan_id=plan.id, step_id=step.id, worker_id=generate_ulid(),
                      status="needs_human", leased_at=now, lease_until=now + timedelta(minutes=5))
    db.add(lease)
    await db.flush()
    request = HitlRequest(entity_id=task.entity_id, workspace_id=task.workspace_id,
                          origin_task_id=task.id, origin_plan_id=plan.id,
                          origin_step_id=step.id, origin_kind="lease", hitl_type="input",
                          status="pending", reason="Which color?", created_at=now,
                          dedup_key=f"lease:{lease.id}:needs_input", context={"pending_kind": "needs_input"},
                          payload={"question": "Which color?"})
    conversation = Conversation(entity_id=task.entity_id, workspace_id=task.workspace_id,
                                scope="workspace_thread", thread_ref_kind="plan", thread_ref_id=plan.id)
    db.add_all([request, conversation])
    await db.flush()
    message = Message(conversation_id=conversation.id, role="assistant", message_kind="hitl_request",
                      pending_action={
                          "kind": "needs_input", "approval_request_id": request.id,
                          "step_id": step.id, "plan_id": plan.id, "title": "Which color?",
                          "questions": [{"key": "color", "label": "Color", "type": "select",
                                         "required": True, "options": ["blue", "green"]}],
                          "approval_token": "must-not-be-public", "continuation": {"private": "hidden"},
                      })
    db.add(message)
    await db.flush()
    return step, lease, request, conversation, message


async def test_running_task_projects_two_exact_parallel_input_cards(db_session):
    task, plan = await _task(db_session)
    first = await _card(db_session, task, plan)
    second = await _card(db_session, task, plan)
    cards = await _task_input_requests(db_session, task)
    assert {card.step_id for card in cards} == {first[0].id, second[0].id}
    assert {card.message_id for card in cards} == {first[-1].id, second[-1].id}
    for card in cards:
        assert card.pending_action["hitl_type"] == "input"
        assert card.pending_action["questions"][0]["options"] == ["blue", "green"]
        assert card.pending_action["approval_request_id"] == card.request_id
        assert "approval_token" not in card.pending_action
        assert "continuation" not in card.pending_action
    assert task.status == "in_progress"


@pytest.mark.parametrize("change", [
    "authorize", "review", "error", "needs_login", "needs_confirmation",
    "resolved_message", "consumed_request", "missing_message", "done_step", "terminal_plan",
    "foreign_conversation", "foreign_request", "foreign_step", "foreign_plan",
    "wrong_card_step", "wrong_card_plan", "wrong_card_request", "expired_lease", "newer_lease",
    "wrong_message_origin", "wrong_conversation_origin", "private_conversation", "different_thread", "card_review_type",
])
async def test_untrusted_stale_or_non_input_card_is_not_projected(db_session, change):
    task, plan = await _task(db_session)
    step, lease, request, conversation, message = await _card(db_session, task, plan)
    if change in {"authorize", "review", "error"}:
        request.hitl_type = change
    elif change in {"needs_login", "needs_confirmation"}:
        message.pending_action = {**message.pending_action, "kind": change}
    elif change == "resolved_message":
        message.resolved_at = datetime.now(timezone.utc)
    elif change == "consumed_request":
        request.status = "consumed"
    elif change == "missing_message":
        await db_session.delete(message)
    elif change == "done_step":
        step.step_status = "done"
    elif change == "terminal_plan":
        plan.status = "completed"
    elif change == "foreign_conversation":
        conversation.workspace_id = generate_ulid()
    elif change == "foreign_request":
        request.entity_id = generate_ulid()
    elif change == "foreign_step":
        step.workspace_id = generate_ulid()
    elif change == "foreign_plan":
        plan.task_id = generate_ulid()
    elif change == "wrong_message_origin":
        request.origin_message_id = generate_ulid()
    elif change == "wrong_conversation_origin":
        request.origin_conversation_id = generate_ulid()
    elif change == "private_conversation":
        conversation.scope = "channel"
    elif change == "different_thread":
        conversation.thread_ref_id = generate_ulid()
    elif change == "card_review_type":
        message.pending_action = {**message.pending_action, "hitl_type": "review"}
    elif change.startswith("wrong_card_"):
        key = {"wrong_card_step": "step_id", "wrong_card_plan": "plan_id", "wrong_card_request": "approval_request_id"}[change]
        message.pending_action = {**message.pending_action, key: generate_ulid()}
    elif change == "expired_lease":
        lease.status = "expired"
    elif change == "newer_lease":
        db_session.add(WorkLease(entity_id=task.entity_id, workspace_id=task.workspace_id,
                                plan_id=plan.id, step_id=step.id, worker_id=generate_ulid(),
                                status="needs_human", leased_at=lease.leased_at + timedelta(seconds=1),
                                lease_until=lease.lease_until))
    await db_session.flush()
    assert await _task_input_requests(db_session, task) == []


async def test_newer_closed_request_cannot_reveal_older_pending_card(db_session):
    task, plan = await _task(db_session)
    step, _, request, _, _ = await _card(db_session, task, plan)
    db_session.add(HitlRequest(entity_id=task.entity_id, workspace_id=task.workspace_id,
                              origin_task_id=task.id, origin_plan_id=plan.id,
                              origin_step_id=step.id, origin_kind="lease", hitl_type="input",
                              status="consumed", dedup_key=generate_ulid(),
                              created_at=request.created_at + timedelta(seconds=1)))
    await db_session.flush()
    assert await _task_input_requests(db_session, task) == []


async def test_duplicate_message_uses_one_current_request_card(db_session):
    task, plan = await _task(db_session)
    _, _, _, conversation, message = await _card(db_session, task, plan)
    duplicate = Message(conversation_id=conversation.id, role="assistant", message_kind="hitl_request",
                        pending_action=message.pending_action,
                        created_at=message.created_at + timedelta(seconds=1))
    db_session.add(duplicate)
    await db_session.flush()
    cards = await _task_input_requests(db_session, task)
    assert len(cards) == 1
    assert cards[0].message_id == duplicate.id
