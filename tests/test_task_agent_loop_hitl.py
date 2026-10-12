"""Task agent loop, Phase B: a person's answer or approval resumes the loop.

Real AgentLoop, real wake/claim/checkpoint storage, real Task state machine,
real HITL cards and the real approval service and runtime guard. Only the
model round, the agent context and the Workspace admission lookups are faked
(the host from ``test_task_agent_loop``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tests.approval_settings_test_utils import (
    action_rule, initialize_test_approval_owners, set_workspace_test_approval,
)
from sqlalchemy import select

import test_task_agent_loop as base
from packages.core.ai.runtime.approval_service import (
    guard_runtime_tool_action,
    resolve_runtime_approval_turn,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message, Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import (
    Agent, AgentSubscription, AgentToolBinding, ToolDefinition, Workspace,
)
from packages.core.services import task_requester_identity
from packages.core.services.task_agent_loop_hitl import (
    TASK_AGENT_LOOP_HITL_CONTEXT_KEY,
    TASK_AGENT_LOOP_INPUT_DISPATCH,
)
from packages.core.services.task_coordinator_wakeup import (
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
)
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
    task_execution_intent_generation,
)
from packages.core.services.task_input_resume import (
    apply_task_input_answer,
    dispatch_task_input_resume,
)
from packages.core.services.workspace_runtime import ensure_workspace_task_conversation
from packages.core.workers import task_agent_loop as worker


TWEET = "mcp__twitter_x__create_tweet"
TWEET_SCHEMA = {
    "type": "function",
    "function": {
        "name": TWEET,
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
}
JUSTIFICATION = {
    "kind": "human_decision",
    "why_human_required": "Only the user decides the launch date.",
    "attempted_resolution": [{"source": "conversation", "finding": "The brief names no date."}],
}
ASK = {"question": "Which launch date should the note use?", "justification": JUSTIFICATION}


async def _scope(db):
    entity_id, user_id, workspace_id = (generate_ulid() for _ in range(3))
    db.add(Entity(id=entity_id, name="Agent loop HITL", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
        password_hash="x", role="owner", status="active", display_name="Ana",
    ))
    db.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active", is_primary=True))
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Launch", status="active", operating_model={}))
    agent_id = generate_ulid()
    db.add(Agent(id=agent_id, entity_id=entity_id, name="Publisher", status="active"))
    sub = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent_id, name="Publisher", service_key="publisher", status="active",
    )
    db.add(sub)
    tool = (await db.execute(select(ToolDefinition).where(ToolDefinition.name == TWEET))).scalar_one_or_none()
    if tool is None:
        tool = ToolDefinition(id=generate_ulid(), name=TWEET, status="active")
        db.add(tool)
    db.add(AgentToolBinding(agent_id=agent_id, tool_id=tool.id))
    await db.flush()
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        creator_id=user_id, title="Write the launch note", agent_id=agent_id,
        description="Record a short note that names the launch date.",
        task_type="general", status="in_progress", details={},
    )
    db.add(task)
    await db.flush()
    thread = await ensure_workspace_task_conversation(
        db, entity_id=entity_id, workspace_id=workspace_id, task_id=task.id, title=task.title,
    )
    task.conversation_id = thread.id
    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db, workspace, user_id=user_id)
    await set_workspace_test_approval(
        db, entity_id=entity_id, workspace_id=workspace_id,
        actor_id=user_id, user_rules=[action_rule("social_post.publish", effect="ask")],
    )
    await db.commit()
    envelope = RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER,
            entity_id=entity_id, actor_user_id=user_id, execution_user_id=user_id,
            agent_id=agent_id, workspace_id=workspace_id,
        ),
        entity_id=entity_id, user_id=user_id, agent_id=agent_id,
        workspace_id=workspace_id, conversation_id=thread.id, task_id=task.id,
        allowed_tool_names=(TWEET,),
    )
    binding = TaskPlanServiceBinding(subscription_id=sub.id, service_key="publisher", agent_id=agent_id)
    selection = CoordinatorRuntimeSelection(root=binding, bindings=(binding,))
    return task, thread, envelope, selection


async def _start(db_session, monkeypatch):
    task, thread, envelope, selection = await _scope(db_session)
    base._flag(monkeypatch, True)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=envelope.user_id),
    )
    task = await base._fresh(db_session, Task, task.id)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    host = base._install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "resolve_task_execution_user_id", AsyncMock(return_value=envelope.user_id))
    host.envelope, host.thread, host.task_id, host.run_id = envelope, thread, task.id, run.id
    host.selection = selection
    return host, wake


def _with_request_input(host):
    from packages.core.ai.tools.request_input_tool import _request_input

    calls: list[dict] = []

    async def ask(args):
        calls.append(args)
        return await _request_input(**args)

    host.extra_tools["request_input"] = ask
    return calls


def _with_tweet(host):
    executed: list[dict] = []

    async def tweet(args):
        # As the tool pipeline: the guard spends and strips approval_token
        # from the very arguments the handler then runs with.
        call_id = args.get("_runtime_tool_call_id_from_context")
        assert call_id
        blocked = await guard_runtime_tool_action(
            name=TWEET, arguments=args,
            operation_id=f"runtime:{host.task_id}:call:{call_id}",
            entity_id=host.envelope.entity_id, user_id=host.envelope.user_id,
            workspace_id=host.envelope.workspace_id, conversation_id=host.thread.id,
            task_id=host.task_id, runtime_envelope=host.envelope,
        )
        if blocked is not None:
            return blocked
        executed.append(args)
        return json.dumps({"ok": True, "tweet_id": "t-1", "text": args.get("text")})

    host.context.return_value.tools = [base.NOTE_TOOL, TWEET_SCHEMA]
    host.context.return_value.allowed_tool_names = {"record_note", TWEET}
    host.extra_tools[TWEET] = tweet
    return executed


async def _run(db_session, task_id, wake=None):
    if wake is None:
        event = await base._event(db_session, task_id)
        event_id, generation = event.id, event.payload["wake_generation"]
    else:
        event_id, generation = wake.event_id, wake.wake_generation
    return await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task_id, wake_generation=generation,
    )


def _result_of(messages, call_id):
    return next(m["content"] for m in messages if m.get("tool_call_id") == call_id)


async def _answer(db_session, task_id, user_id, text, **payload):
    task = (await db_session.execute(
        select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    applied = await apply_task_input_answer(
        db_session, task=task, user_id=user_id, user_label="Ana",
        response_text=text, payload={"response": text, **payload},
    )
    await db_session.commit()
    return applied


async def _decide(db_session, host, hitl_id, action):
    result = await resolve_runtime_approval_turn(
        db_session, conversation_id=host.thread.id, entity_id=host.envelope.entity_id,
        user_id=host.envelope.user_id, hitl_id=hitl_id, action=action,
    )
    await db_session.commit()
    return result


# ── request_input ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_input_parks_on_the_task_card_and_the_answer_resumes_it(db_session, monkeypatch):
    from packages.core.tasks import ai_tasks

    host, wake = await _start(db_session, monkeypatch)
    asked = _with_request_input(host)

    def finish(messages, _tools):
        answer = json.loads(_result_of(messages, "ask"))
        assert answer["status"] == "answered" and answer["answer"] == "2026-10-10"
        assert answer["answered_by"] == "Ana"
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    seen = base._model(monkeypatch, [("", [base._call("ask", "request_input", ASK)]), finish])

    first = await _run(db_session, host.task_id, wake)

    assert first == {"status": "waiting_human", "task_id": host.task_id, "kind": "waiting_human"}
    task = await base._fresh(db_session, Task, host.task_id)
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert task.status == "waiting_on_customer"
    assert task.details["_hitl_question"]["question"] == ASK["question"]
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value and run.claim_token is None
    wait = run.checkpoint["task_loop_wait"]
    assert wait["kind"] == "waiting_human" and wait["tool_call_id"] == "ask"
    assert run.checkpoint["runtime_checkpoint"]["pending_tool_call"]["name"] == "request_input"
    request = await base._fresh(db_session, HitlRequest, wait["hitl_request_id"])
    assert request.status == "pending" and request.action_key == "task.request_input"
    card = (await db_session.execute(select(Message).where(
        Message.pending_action["approval_request_id"].astext == request.id,
    ))).scalar_one()
    assert card.pending_action["task_id"] == task.id and card.resolved_at is None
    assert (await base._event(db_session, task.id)).delivered_at is not None  # settled
    requested = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "ai_hitl_requested",
    ))).scalar_one()
    assert requested.meta["question"] == ASK["question"]

    applied = await _answer(db_session, task.id, host.envelope.user_id, "2026-10-10")

    assert applied == {"resumed": True, "dispatch_agent_id": TASK_AGENT_LOOP_INPUT_DISPATCH}
    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", AsyncMock(side_effect=AssertionError("no TaskRunner")))
    assert dispatch_task_input_resume(task.id, applied["dispatch_agent_id"]) is True
    task = await base._fresh(db_session, Task, task.id)
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert task.status == "in_progress"
    assert run.phase == TaskCoordinatorPhase.READY.value
    assert run.lifecycle_generation == task_execution_intent_generation(task)
    event = await base._event(db_session, task.id)
    assert event.delivered_at is None and event.payload["reason"] == "human_input_answered"
    assert (await base._fresh(db_session, HitlRequest, request.id)).status == "consumed"
    assert (await base._fresh(db_session, Message, card.id)).resolved_at is not None

    second = await _run(db_session, task.id)

    assert second == {"status": "completed", "task_id": task.id}
    assert len(asked) == 1  # the answer was replayed, the call never re-ran
    assert len(seen) == 2
    assert (await base._fresh(db_session, Task, task.id)).status == "completed"


@pytest.mark.asyncio
async def test_a_declined_question_resumes_with_a_declined_result(db_session, monkeypatch):
    from packages.core.services.task_blockers import answer_task_blocker

    host, wake = await _start(db_session, monkeypatch)
    _with_request_input(host)

    def finish(messages, _tools):
        declined = json.loads(_result_of(messages, "ask"))
        assert declined["status"] == "declined" and declined["choice"] == "refuse"
        assert "complete_task(outcome=blocked)" in declined["instruction"]
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    base._model(monkeypatch, [("", [base._call("ask", "request_input", ASK)]), finish])
    assert (await _run(db_session, host.task_id, wake))["status"] == "waiting_human"
    wait = (await base._fresh(db_session, TaskCoordinatorRun, host.run_id)).checkpoint["task_loop_wait"]

    await base._fresh(db_session, Task, host.task_id)  # this test session's copy is stale
    # The chat bridge's refusal: deny the request, then resume with it.
    result = await answer_task_blocker(
        db_session, entity_id=host.envelope.entity_id, user_id=host.envelope.user_id,
        request_id=wait["hitl_request_id"], refuse=True,
    )
    await db_session.commit()

    assert result["resolved"] is True and result["_task_to_run"][1] == TASK_AGENT_LOOP_INPUT_DISPATCH
    assert (await _run(db_session, host.task_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_pause_and_resume_waits_on_the_same_card_again(db_session, monkeypatch):
    host, wake = await _start(db_session, monkeypatch)
    asked = _with_request_input(host)
    seen = base._model(monkeypatch, [
        ("", [base._call("ask", "request_input", ASK)]),
        ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    ])
    assert (await _run(db_session, host.task_id, wake))["status"] == "waiting_human"
    parked = (await base._fresh(db_session, TaskCoordinatorRun, host.run_id)).checkpoint
    cards = (await db_session.execute(select(Message).where(Message.pending_action.isnot(None)))).scalars().all()

    task = await base._fresh(db_session, Task, host.task_id)
    task.status, task.status_changed_at = "on_hold", datetime.now(timezone.utc)
    await db_session.commit()
    task = await base._fresh(db_session, Task, host.task_id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=host.selection)
    await db_session.commit()

    assert run.phase == TaskCoordinatorPhase.READY.value
    assert run.checkpoint["runtime_checkpoint"] == parked["runtime_checkpoint"]
    assert run.checkpoint["task_loop_wait"] == parked["task_loop_wait"]

    again = await _run(db_session, host.task_id)

    assert again["status"] == "waiting_human" and len(seen) == 1 and len(asked) == 1
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert run.checkpoint["task_loop_wait"] == parked["task_loop_wait"]
    assert (await base._fresh(db_session, Task, host.task_id)).status == "waiting_on_customer"
    assert (await db_session.execute(
        select(Message).where(Message.pending_action.isnot(None))
    )).scalars().all().__len__() == len(cards)

    assert (await _answer(db_session, host.task_id, host.envelope.user_id, "2026-10-10"))["resumed"] is True
    assert (await _run(db_session, host.task_id))["status"] == "completed"
    assert len(asked) == 1


# ── tool approvals ──────────────────────────────────────────────────────


async def _park_on_tweet(db_session, monkeypatch, finish):
    host, wake = await _start(db_session, monkeypatch)
    executed = _with_tweet(host)
    seen = base._model(monkeypatch, [
        ("", [
            base._call("note", "record_note", {"text": "Launch on 2026-10-10"}),
            base._call("post", TWEET, {"text": "We launch on 2026-10-10"}),
        ]),
        finish,
    ])
    first = await _run(db_session, host.task_id, wake)
    assert first == {"status": "waiting_human", "task_id": host.task_id, "kind": "waiting_approval"}, first
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    hitl_id = run.checkpoint["task_loop_wait"]["hitl_request_id"]
    return host, executed, seen, hitl_id


@pytest.mark.asyncio
async def test_approved_action_runs_once_when_the_loop_resumes(db_session, monkeypatch):
    def finish(messages, _tools):
        assert json.loads(_result_of(messages, "post"))["tweet_id"] == "t-1"
        assert json.loads(_result_of(messages, "note"))["ok"] is True
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    host, executed, seen, hitl_id = await _park_on_tweet(db_session, monkeypatch, finish)

    assert executed == []
    task = await base._fresh(db_session, Task, host.task_id)
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert task.status == "waiting_on_customer"
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert run.checkpoint["runtime_checkpoint"]["pending_tool_call"]["id"] == "post"
    request = await base._fresh(db_session, HitlRequest, hitl_id)
    assert request.status == "pending" and request.origin_task_id == task.id
    tag = request.context[TASK_AGENT_LOOP_HITL_CONTEXT_KEY]
    assert tag["run_id"] == run.id and tag["tool_call_id"] == "post"
    # The card is the Task-thread runtime approval card TaskRunner shows.
    card = (await db_session.execute(select(Message).where(
        Message.response_surface_event_id == f"task-runtime-hitl:{hitl_id}",
    ))).scalar_one()
    assert card.conversation_id == host.thread.id and card.meta["task_runtime_hitl"] is True

    resolution = await _decide(db_session, host, hitl_id, "approve")

    assert resolution.runtime_metadata["approval_kind"] == "task_runtime"
    assert resolution.runtime_metadata["task_runtime_resume"]["choice"] == "approved"
    task = await base._fresh(db_session, Task, host.task_id)
    assert task.status == "in_progress"  # never failed
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "granted"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.READY.value
    assert run.checkpoint["runtime_checkpoint"].get("pending_tool_result") is None

    second = await _run(db_session, host.task_id)

    assert second == {"status": "completed", "task_id": host.task_id}
    assert len(executed) == 1 and executed[0]["text"] == "We launch on 2026-10-10"
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "consumed"
    assert len(seen) == 2

    # A second click on the decided card changes nothing and runs nothing.
    again = await _decide(db_session, host, hitl_id, "approve")
    assert again.runtime_metadata["approval_kind"] == "task_runtime"
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_rejected_action_never_runs_and_the_loop_hears_why(db_session, monkeypatch):
    def finish(messages, _tools):
        rejected = json.loads(_result_of(messages, "post"))
        assert rejected["status"] == "rejected" and rejected["action_executed"] is False
        assert "[Runtime approval rejected]" in rejected["message"]
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    host, executed, seen, hitl_id = await _park_on_tweet(db_session, monkeypatch, finish)

    resolution = await _decide(db_session, host, hitl_id, "reject")

    assert resolution.runtime_metadata["task_runtime_resume"]["choice"] == "rejected"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.checkpoint["runtime_checkpoint"]["pending_tool_result"]["gate"] is True

    second = await _run(db_session, host.task_id)

    # The host decided the call: it is no unconfirmed effect, so the Task can
    # still be completed with what did run.
    assert second == {"status": "completed", "task_id": host.task_id}, second
    assert executed == [] and len(seen) == 2
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "denied"


@pytest.mark.asyncio
async def test_a_stale_card_cannot_resume_a_newer_park(db_session, monkeypatch):
    host, executed, seen, first_id = await _park_on_tweet(
        db_session, monkeypatch,
        ("", [base._call("post2", TWEET, {"text": "Second post"})]),
    )
    await _decide(db_session, host, first_id, "approve")
    # Resumes, posts the first, then parks on the second (a new request).
    assert (await _run(db_session, host.task_id))["kind"] == "waiting_approval"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    second_id = run.checkpoint["task_loop_wait"]["hitl_request_id"]
    assert second_id != first_id and len(executed) == 1
    # An old card made live again (as if never decided) is not this park.
    old = await base._fresh(db_session, HitlRequest, first_id)
    old.status, old.consumed_at = "pending", None
    await db_session.commit()

    stale = await _decide(db_session, host, first_id, "approve")

    assert stale.runtime_metadata == {"approval_kind": "runtime_terminal", "approval_outcome": "expired"}
    assert (await base._fresh(db_session, HitlRequest, first_id)).status == "expired"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert run.checkpoint["task_loop_wait"]["hitl_request_id"] == second_id
    assert (await base._fresh(db_session, Task, host.task_id)).status == "waiting_on_customer"
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_a_tampered_park_tag_is_not_current(db_session, monkeypatch):
    host, executed, _seen, hitl_id = await _park_on_tweet(
        db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    )
    request = await base._fresh(db_session, HitlRequest, hitl_id)
    tag = dict(request.context[TASK_AGENT_LOOP_HITL_CONTEXT_KEY])
    tag["tool_call_id"] = "another-call"
    request.context = {**request.context, TASK_AGENT_LOOP_HITL_CONTEXT_KEY: tag}
    await db_session.commit()

    result = await _decide(db_session, host, hitl_id, "approve")

    assert result.runtime_metadata["approval_outcome"] == "expired"
    assert (await base._fresh(db_session, Task, host.task_id)).status == "waiting_on_customer"
    assert executed == []


@pytest.mark.asyncio
async def test_approving_an_agent_loop_request_never_fails_the_task(db_session, monkeypatch):
    """A Task-origin request the loop never parked on closes; the Task is untouched."""

    host, _wake = await _start(db_session, monkeypatch)
    task = await base._fresh(db_session, Task, host.task_id)
    orphan = HitlRequest(
        id=generate_ulid(), entity_id=task.entity_id, workspace_id=task.workspace_id,
        action_key="external.social.publish", resource_kind="workspace", risk_level="high",
        origin_kind="tool_call", origin_task_id=task.id, origin_conversation_id=host.thread.id,
        status="pending", dedup_key=f"tool:{generate_ulid()}", context={"tool": TWEET},
    )
    db_session.add(orphan)
    await db_session.commit()

    result = await _decide(db_session, host, orphan.id, "approve")

    assert result.runtime_metadata["approval_outcome"] == "expired"
    assert (await base._fresh(db_session, HitlRequest, orphan.id)).status == "expired"
    assert (await base._fresh(db_session, Task, task.id)).status == "in_progress"


# ── cancel while waiting ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("park", ["question", "approval"])
async def test_cancelling_a_task_waiting_on_a_person_closes_the_card(db_session, monkeypatch, park):
    from packages.core import database
    from packages.core.services.task_service import update_task
    from packages.core.tasks.runtime_tasks import reconcile_terminal_task_coordinator_runs_once

    if park == "question":
        host, wake = await _start(db_session, monkeypatch)
        _with_request_input(host)
        base._model(monkeypatch, [("", [base._call("ask", "request_input", ASK)])])
        assert (await _run(db_session, host.task_id, wake))["status"] == "waiting_human"
        hitl_id = (await base._fresh(db_session, TaskCoordinatorRun, host.run_id)).checkpoint["task_loop_wait"]["hitl_request_id"]
    else:
        host, _executed, _seen, hitl_id = await _park_on_tweet(
            db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
        )
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    task = await base._fresh(db_session, Task, host.task_id)

    await update_task(db_session, task.id, task.entity_id, status="cancelled")
    await db_session.commit()
    assert await reconcile_terminal_task_coordinator_runs_once() >= 1

    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.CANCELLED.value
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "expired"
    if park == "question":
        card = (await db_session.execute(select(Message).where(
            Message.pending_action["approval_request_id"].astext == hitl_id,
        ))).scalar_one()
        assert card.resolved_at is not None
        # An answer to the closed card resumes nothing.
        assert (await _answer(db_session, host.task_id, host.envelope.user_id, "late"))["resumed"] is False


# ── delegated agents ────────────────────────────────────────────────────


def _child_pause(question: str) -> str:
    return json.dumps({
        "delegated": True, "run_id": "child-run", "status": "blocked",
        "service": {"agent_name": "Writer", "service_key": "writer"},
        "content": question, "result": {"question": question},
        "stop_reason": "waiting_resource", "blocker_kind": "request_input",
        "human_input": {"question": question, "options": ["2026-10-10", "2026-10-17"]},
        "_pending_action": {"kind": "needs_input"}, "stop_parent": True, "error": None,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [True, False])
async def test_a_delegated_agents_question_parks_and_the_answer_reaches_the_parent(
    db_session, monkeypatch, published,
):
    from packages.core.ai.runtime.tool_results import RuntimeDelegatedPauseToolResult

    host, wake = await _start(db_session, monkeypatch)
    delegations: list[dict] = []

    async def delegate(args):
        delegations.append(args)
        if len(delegations) == 1:
            pause = _child_pause("Which launch date?")
            # The host carrier exists only when the child's ledger reached the
            # parent; otherwise the pause is plain JSON.
            return RuntimeDelegatedPauseToolResult(pause) if published else pause
        return json.dumps({"delegated": True, "status": "completed", "content": "Note drafted for 2026-10-10."})

    host.extra_tools["delegate_to_agent"] = delegate
    ask_child = {"tasks": [{"agent": "Writer", "task": "Draft the launch note."}]}

    def redelegate(messages, _tools):
        note = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
        assert "The user answered: 2026-10-10" in note
        assert "continue_interrupted=true" in note
        return "", [base._call("again", "delegate_to_agent", {
            "tasks": [{"agent": "Writer", "task": "Use 2026-10-10.", "continue_interrupted": True}],
        })]

    seen = base._model(monkeypatch, [
        ("", [base._call("child", "delegate_to_agent", ask_child)]),
        redelegate,
        ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
        ("", [base._call("done-again", "complete_task", base.COMPLETE_ARGS)]),
    ])

    first = await _run(db_session, host.task_id, wake)

    assert first == {"status": "waiting_human", "task_id": host.task_id, "kind": "waiting_delegated_input"}
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    wait = run.checkpoint["task_loop_wait"]
    assert wait["tool_name"] == "Writer" and "runtime_checkpoint" not in run.checkpoint
    request = await base._fresh(db_session, HitlRequest, wait["hitl_request_id"])
    assert request.action_key == "task.request_input" and request.status == "pending"
    assert request.context["question"]["options"] == ["2026-10-10", "2026-10-17"]
    assert "Asked by Writer" in request.context["question"]["context"]

    assert (await _answer(db_session, host.task_id, host.envelope.user_id, "2026-10-10"))["resumed"] is True
    done = await _run(db_session, host.task_id)

    assert len(delegations) == 2
    assert delegations[1]["tasks"][0]["continue_interrupted"] is True
    if published:
        # The child's own effects came through its merged ledger; the pause
        # itself is no effect, so the Task completes.
        assert done == {"status": "completed", "task_id": host.task_id}, done
        assert len(seen) == 3
    else:
        # Without the host's proof the paused delegation stays unconfirmed:
        # the first completed claim is refused, an insisted one is no success.
        refusal = json.loads(_result_of(seen[3], "done"))
        assert refusal["error"] == "unconfirmed_effects"
        assert done == {"status": "blocked", "task_id": host.task_id, "code": "agent_loop_completed_non_success"}, done


@pytest.mark.asyncio
async def test_a_delegated_agents_approval_blocks_honestly_and_withdraws_its_card(db_session, monkeypatch):
    host, wake = await _start(db_session, monkeypatch)
    task = await base._fresh(db_session, Task, host.task_id)
    child_request = HitlRequest(
        id=generate_ulid(), entity_id=task.entity_id, workspace_id=task.workspace_id,
        action_key="external.social.publish", resource_kind="workspace", risk_level="high",
        origin_kind="tool_call", origin_task_id=task.id, origin_conversation_id=host.thread.id,
        status="pending", dedup_key=f"tool:{generate_ulid()}", context={"tool": TWEET},
    )
    db_session.add(child_request)
    await db_session.commit()
    envelope = json.dumps({
        "__hitl__": True, "error": "approval_required", "approval_token": child_request.id,
        "hitl": {"id": child_request.id, "type": "approval", "prompt": "Post the launch tweet?", "tool": TWEET},
    })

    async def delegate(_args):
        return envelope

    host.extra_tools["delegate_to_agent"] = delegate
    base._model(monkeypatch, [("", [base._call("child", "delegate_to_agent", {
        "tasks": [{"agent": "Writer", "task": "Post the launch tweet."}],
    })])])

    outcome = await _run(db_session, host.task_id, wake)

    assert outcome == {"status": "blocked", "task_id": host.task_id, "code": "agent_loop_delegated_hitl_unsupported"}
    assert (await base._fresh(db_session, HitlRequest, child_request.id)).status == "expired"
    failure = (await base._fresh(db_session, TaskCoordinatorRun, host.run_id)).checkpoint["coordinator_outcome"]
    assert "requested by a delegated agent" in failure["message"]
    assert failure["details"]["approval_withdrawn"] is True


# ── review fixes ────────────────────────────────────────────────────────


async def _park_on_question(db_session, monkeypatch, script_after=None):
    host, wake = await _start(db_session, monkeypatch)
    asked = _with_request_input(host)
    seen = base._model(monkeypatch, [
        ("", [base._call("ask", "request_input", ASK)]),
        *(script_after or []),
    ])
    assert (await _run(db_session, host.task_id, wake))["status"] == "waiting_human"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    return host, asked, seen, run.checkpoint["task_loop_wait"]["hitl_request_id"]


@pytest.mark.asyncio
async def test_an_answer_never_resurrects_a_task_cancelled_under_it(db_session, monkeypatch):
    from packages.core.services.task_state_machine import apply_task_status_transition

    host, _asked, _seen, hitl_id = await _park_on_question(db_session, monkeypatch)
    # The answering surface loaded the Task before a cancel committed.
    stale = await base._fresh(db_session, Task, host.task_id)
    assert stale.status == "waiting_on_customer"
    async with host.session_factory() as other:
        row = (await other.execute(select(Task).where(Task.id == host.task_id).with_for_update())).scalar_one()
        await apply_task_status_transition(row, "cancelled", db=other)
        await other.commit()

    applied = await apply_task_input_answer(
        db_session, task=stale, user_id=host.envelope.user_id, user_label="Ana",
        response_text="2026-10-10", payload={"response": "2026-10-10"},
    )
    await db_session.commit()

    assert applied["resumed"] is False
    assert (await base._fresh(db_session, Task, host.task_id)).status == "cancelled"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert (await base._event(db_session, host.task_id)).delivered_at is not None  # no wake


@pytest.mark.asyncio
async def test_an_answer_while_the_task_is_paused_is_kept_and_replayed_on_resume(db_session, monkeypatch):
    host, asked, seen, hitl_id = await _park_on_question(
        db_session, monkeypatch, [("", [base._call("done", "complete_task", base.COMPLETE_ARGS)])],
    )
    task = await base._fresh(db_session, Task, host.task_id)
    task.status, task.status_changed_at = "on_hold", datetime.now(timezone.utc)
    await db_session.commit()

    applied = await _answer(db_session, host.task_id, host.envelope.user_id, "2026-10-10")

    assert applied == {"resumed": True, "dispatch_agent_id": TASK_AGENT_LOOP_INPUT_DISPATCH}
    assert (await base._fresh(db_session, Task, host.task_id)).status == "on_hold"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert run.checkpoint["task_loop_wait"]["resolution"] == "answered"
    assert (await base._event(db_session, host.task_id)).delivered_at is not None  # no wake yet
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "consumed"

    task = await base._fresh(db_session, Task, host.task_id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await ensure_task_coordinator_run(db_session, task=task, selection=host.selection)
    await db_session.commit()

    assert (await _run(db_session, host.task_id))["status"] == "completed"
    assert len(asked) == 1 and len(seen) == 2
    assert json.loads(_result_of(seen[1], "ask"))["answer"] == "2026-10-10"


@pytest.mark.asyncio
async def test_a_reviewed_approval_runs_its_finalized_arguments_once(db_session, monkeypatch):
    from packages.core.ai.runtime.approval_messages import approval_args_hash, runtime_approval_continuation

    host, executed, _seen, hitl_id = await _park_on_tweet(
        db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    )
    final = {"text": "Reviewed: we launch on 2026-10-10"}
    request = await base._fresh(db_session, HitlRequest, hitl_id)
    request.context = {
        **request.context,
        "continuation": runtime_approval_continuation(TWEET, final),
        "args_hash": approval_args_hash(final, tool_name=TWEET),
    }
    await db_session.commit()

    await _decide(db_session, host, hitl_id, "approve")
    done = await _run(db_session, host.task_id)

    assert done == {"status": "completed", "task_id": host.task_id}, done
    assert [args["text"] for args in executed] == [final["text"]]
    assert all("approval_token" not in args for args in executed)
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "consumed"


@pytest.mark.asyncio
async def test_retired_always_approval_cannot_grant_and_approve_once_is_consumed(db_session, monkeypatch):
    host, executed, _seen, hitl_id = await _park_on_tweet(
        db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    )

    with pytest.raises(ValueError, match="versioned approval-settings command"):
        await _decide(db_session, host, hitl_id, "always_approve")
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "pending"
    assert executed == []

    await _decide(db_session, host, hitl_id, "approve")
    assert (await _run(db_session, host.task_id))["status"] == "completed"

    assert len(executed) == 1
    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "consumed"
    live = (await db_session.execute(select(HitlRequest).where(
        HitlRequest.origin_task_id == host.task_id, HitlRequest.status == "granted",
    ))).scalars().all()
    assert live == []


@pytest.mark.asyncio
async def test_the_approved_token_cannot_run_a_different_call(db_session, monkeypatch):
    host, executed, _seen, hitl_id = await _park_on_tweet(
        db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    )
    await _decide(db_session, host, hitl_id, "approve")

    blocked = await guard_runtime_tool_action(
        name=TWEET, arguments={"text": "Something else entirely", "approval_token": hitl_id},
        entity_id=host.envelope.entity_id, user_id=host.envelope.user_id,
        workspace_id=host.envelope.workspace_id, conversation_id=host.thread.id,
        task_id=host.task_id, runtime_envelope=host.envelope,
    )

    assert blocked is not None
    assert (await base._fresh(db_session, Task, host.task_id)).status != "failed"
    assert executed == []


@pytest.mark.asyncio
async def test_a_terminal_transition_closes_the_input_card_at_once(db_session, monkeypatch):
    from packages.core.services.task_service import update_task

    host, _asked, _seen, hitl_id = await _park_on_question(db_session, monkeypatch)
    task = await base._fresh(db_session, Task, host.task_id)

    await update_task(db_session, task.id, task.entity_id, status="cancelled")
    await db_session.commit()

    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "expired"
    card = (await db_session.execute(select(Message).where(
        Message.pending_action["approval_request_id"].astext == hitl_id,
    ))).scalar_one()
    assert card.resolved_at is not None


@pytest.mark.asyncio
async def test_the_sweep_retries_an_input_card_a_terminal_task_left_open(db_session, monkeypatch):
    from packages.core import database
    from packages.core.tasks.runtime_tasks import reconcile_terminal_task_coordinator_runs_once

    host, _asked, _seen, hitl_id = await _park_on_question(db_session, monkeypatch)
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    # A terminal write that bypassed the close, after the run already stopped.
    task = await base._fresh(db_session, Task, host.task_id)
    task.status = "cancelled"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    run.phase = TaskCoordinatorPhase.CANCELLED.value
    await db_session.commit()

    await reconcile_terminal_task_coordinator_runs_once()

    assert (await base._fresh(db_session, HitlRequest, hitl_id)).status == "expired"


# ── answer surfaces serialize with the terminal path ────────────────────


def _record_locks(db_session):
    from sqlalchemy import event

    locked: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        if "pg_advisory_xact_lock" in statement:
            locked.append("approval_boundary")
        elif "FOR UPDATE" in statement and "FROM" in statement:
            locked.append(statement.split("FROM", 1)[1].split()[0])

    engine = db_session.bind.sync_engine if hasattr(db_session.bind, "sync_engine") else db_session.bind
    event.listen(engine, "before_cursor_execute", record)
    return locked, lambda: event.remove(engine, "before_cursor_execute", record)


def _assert_boundary_first(locked):
    # The terminal path takes boundary -> input HitlRequest -> Task; an answer
    # that took the Task (or its card's Message) first could deadlock with it.
    assert "approval_boundary" in locked, locked
    first_row_lock = next(i for i, name in enumerate(locked) if name != "approval_boundary")
    assert locked.index("approval_boundary") < first_row_lock, locked


@pytest.mark.asyncio
async def test_the_workspace_card_answer_takes_the_task_boundary_before_any_row(db_session, monkeypatch):
    from packages.core.models.user import User
    from packages.core.services.workspace_input_resolution import resolve_workspace_input_message

    host, _asked, _seen, hitl_id = await _park_on_question(db_session, monkeypatch)
    card = (await db_session.execute(select(Message).where(
        Message.pending_action["approval_request_id"].astext == hitl_id,
    ))).scalar_one()
    user = await db_session.get(User, host.envelope.user_id)
    locked, stop = _record_locks(db_session)
    try:
        result = await resolve_workspace_input_message(
            db_session, workspace_id=host.envelope.workspace_id, conversation_id=card.conversation_id,
            message_id=card.id, user=user, response="2026-10-10",
        )
    finally:
        stop()
    await db_session.commit()

    assert result.changed
    _assert_boundary_first(locked)
    assert (await base._fresh(db_session, Task, host.task_id)).status == "in_progress"


@pytest.mark.asyncio
async def test_the_task_page_answer_takes_the_task_boundary_before_the_task(db_session, monkeypatch):
    from apps.api.routers.tasks import _resume_hitl
    from packages.core.models.user import User

    host, _asked, _seen, _hitl_id = await _park_on_question(db_session, monkeypatch)
    user = await db_session.get(User, host.envelope.user_id)
    task = await base._fresh(db_session, Task, host.task_id)
    locked, stop = _record_locks(db_session)
    try:
        result = await _resume_hitl(
            db_session, task, user, step_id=None, response_text="2026-10-10",
            payload={"response": "2026-10-10", "choice": None, "fields": {}},
        )
    finally:
        stop()

    assert result["resumed"] is True and result["mode"] == "agent_loop"
    _assert_boundary_first(locked)
