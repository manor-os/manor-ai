"""Durable delegation (W4a PR7): a delegated child run waits on a person.

A child that asks a question or hits an approval-gated call parks on its own
card ("Asked by {agent}") and only it waits. Its Task stays in progress while
anything in the run tree can move and waits on the person once nothing can.
The answer or decision reaches exactly the run that asked and wakes only it.

Real AgentLoop for the root run and every child run, real wake/claim/
checkpoint storage, real cards, real Task state machine; the approval tests
also run the real approval service and runtime guard on a real Workspace
(``test_task_agent_loop_hitl._scope``). Only the model rounds, the agent
contexts and the billing callbacks are faked (``build_harness``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import test_task_agent_loop as base
import test_task_agent_loop_hitl as loop_hitl_tests
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message, Task, TaskLog
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.services import task_agent_child_settlement as settlement
from packages.core.services import task_agent_loop_hitl as loop_hitl
from packages.core.services import task_requester_identity
from packages.core.services.task_agent_child_wakeup import task_agent_child_outbox_dedupe_key
from packages.core.services.task_blockers import answer_task_blocker
from packages.core.services.task_coordinator_wakeup import (
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY, apply_task_input_answer
from packages.core.tasks import runtime_tasks
from packages.core.workers import task_agent_child_loop as child_worker
from packages.core.workers import task_agent_loop as worker

from test_task_agent_durable_delegation import (
    PLAN_ARGS,
    _call,
    _complete,
    _tasks,
    _tool_result,
    build_harness,
    drop_harness_children,
    make_subs,
)


P = TaskCoordinatorPhase
TWEET = loop_hitl_tests.TWEET
JUSTIFICATION = loop_hitl_tests.JUSTIFICATION


@pytest.fixture
def subs():
    return make_subs()


@pytest.fixture
async def harness(db_session, monkeypatch, subs):
    h = await build_harness(db_session, monkeypatch, subs, await base._agent_loop_run(db_session, monkeypatch))
    yield h
    await drop_harness_children(db_session, h)


@pytest.fixture
def anyone_may_answer(monkeypatch):
    """The harness's seeded Tasks have no member rows; who may answer is not under test."""

    from packages.core.services import runtime_authorization

    monkeypatch.setattr(
        runtime_authorization, "authorize_hitl_resolution",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    )


def _with_request_input(h) -> None:
    async def request_input(args):
        # The registered tool's suspension in a parking host (chat's shape).
        from packages.core.ai.runtime.control import RuntimeToolSuspension

        return RuntimeToolSuspension(
            kind="waiting_human", reservation_id="", poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="awaiting_human_input", payload={"question": args.get("question")},
        )

    h.child_tools["request_input"] = request_input


def _asks(question: str, heard: dict, key: str) -> list:
    def finish(messages, _tools):
        heard[key] = json.loads(_tool_result(messages, "ask"))
        return f"{key.title()} used the answer.", None

    return [
        ("", [_call("ask", "request_input", {
            "question": question, "justification": JUSTIFICATION,
            "recommended": "Yes", "recommendation_reason": "It matches the brief.",
        })]),
        finish,
    ]


def _root_delegates(*keys: str) -> list:
    return [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks(*keys))]),
        _complete(),
    ]


async def _pending_wake(h, child_id: str) -> bool:
    event = await h.event(task_agent_child_outbox_dedupe_key(child_id))
    return event is not None and event.delivered_at is None


async def _root_pending_wake(h) -> bool:
    event = await h.event(task_coordinator_outbox_dedupe_key(h.task_id))
    return event is not None and event.delivered_at is None


async def _request(h, request_id: str) -> HitlRequest:
    return (await h.db.execute(
        select(HitlRequest).where(HitlRequest.id == request_id).execution_options(populate_existing=True)
    )).scalar_one()


async def _input_cards(h) -> list[Message]:
    rows = (await h.db.execute(
        select(Message).where(Message.pending_action.isnot(None)).execution_options(populate_existing=True)
    )).scalars().all()
    return [
        row for row in rows
        if (row.pending_action or {}).get("kind") == PendingActionKind.HUMAN_INPUT.value
        and (row.pending_action or {}).get("task_id") == h.task_id
    ]


def _card_of(cards: list[Message], child: TaskAgentChildRun) -> Message:
    return next(card for card in cards if card.pending_action.get("run_id") == child.id)


async def _answer(h, text: str, *, request_id: str | None = None) -> dict:
    applied = await apply_task_input_answer(
        h.db, task=await h.fresh_task(), user_id="user-1", user_label="Ana",
        response_text=text, payload={"response": text}, request_id=request_id,
    )
    await h.db.commit()
    return applied


def _generations(task: Task, root, children) -> set[str]:
    return {task_execution_intent_generation(task), root.lifecycle_generation} | {
        child.lifecycle_generation for child in children if child.phase not in P.terminal()
    }


# ── questions ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_children_ask_at_once_and_each_answer_wakes_only_its_asker(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer", "analyst")
    h.scripts["writer"] = _asks("Which tone?", heard, "writer")
    h.scripts["analyst"] = _asks("Which audience?", heard, "analyst")

    assert (await h.root_wake())["status"] == "waiting_child"
    writer, analyst = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "waiting_human"
    # The analyst can still move: the Task stays in progress, the card is out.
    assert (await h.fresh_task()).status == "in_progress"
    assert (await h.child_wake(analyst.id))["status"] == "waiting_human"
    task = await h.fresh_task()
    assert task.status == "waiting_on_customer"

    # Two cards, each its own request, each saying who asked.
    cards = await _input_cards(h)
    assert len(cards) == 2
    writer_card, analyst_card = _card_of(cards, writer), _card_of(cards, analyst)
    assert writer_card.content.startswith("Asked by Writer, an agent working on")
    assert analyst_card.content.startswith("Asked by Analyst, an agent working on")
    assert analyst_card.pending_action["agent_label"] == "Analyst"
    writer_request = writer_card.pending_action["approval_request_id"]
    analyst_request = analyst_card.pending_action["approval_request_id"]
    assert writer_request != analyst_request
    assert (await _request(h, analyst_request)).context["run_id"] == analyst.id
    # The Task page shows the earliest question.
    assert task.details[TASK_INPUT_QUESTION_KEY]["question"] == "Which tone?"
    assert await loop_hitl.task_agent_loop_waiting_on_input(h.db, task)

    # Answering B (on its card) wakes only B.
    assert (await _answer(h, "Engineers.", request_id=analyst_request))["resumed"] is True
    writer, analyst = await h.children()
    assert (await h.fresh_task()).status == "in_progress"
    assert analyst.phase == P.READY.value and await _pending_wake(h, analyst.id)
    assert writer.phase == P.WAITING_HUMAN.value and not await _pending_wake(h, writer.id)
    assert not await _root_pending_wake(h)
    assert (await _request(h, analyst_request)).status == "consumed"
    assert (await _request(h, writer_request)).status == "pending"
    assert {card.id for card in await _input_cards(h) if card.resolved_at is None} == {writer_card.id}
    assert (await h.fresh_task()).details[TASK_INPUT_QUESTION_KEY]["question"] == "Which tone?"

    assert (await h.child_wake(analyst.id))["status"] == "completed"
    assert heard["analyst"]["answer"] == "Engineers."
    # The analyst ended; the writer still waits on the person: so does the Task.
    assert (await h.fresh_task()).status == "waiting_on_customer"

    # A comment (no request named) answers the earliest open question.
    assert (await _answer(h, "Formal."))["resumed"] is True
    (writer, _analyst) = await h.children()
    assert writer.phase == P.READY.value and await _pending_wake(h, writer.id)
    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert heard["writer"]["answer"] == "Formal."
    assert (await h.root_wake())["status"] == "completed"

    # Journal: each child's park and its wake (PR4 contract).
    for child in await h.children():
        kinds = [row.kind for row in await h.journal(child.id)]
        assert kinds.count("park") == 1 and kinds.count("wake") == 1, kinds
        assert kinds.index("park") < kinds.index("wake") < kinds.index("child_settled")
    logs = (await h.db.execute(select(TaskLog.content).where(TaskLog.task_id == h.task_id))).scalars().all()
    assert "Writer is waiting for an answer: Which tone?" in logs
    assert "Human input received for Analyst: Engineers." in logs


@pytest.mark.asyncio
async def test_the_last_runnable_child_parking_puts_the_task_on_the_person(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer", "analyst")
    h.scripts["writer"] = [("The draft is done.", None)]
    h.scripts["analyst"] = _asks("Which audience?", heard, "analyst")

    await h.root_wake()
    writer, analyst = await h.children()
    assert (await h.child_wake(analyst.id))["status"] == "waiting_human"
    task, root = await h.fresh_task(), await h.root()
    assert task.status == "in_progress"
    before = task_execution_intent_generation(task)

    # The writer, the last child that could move, ends: now everything waits.
    assert (await h.child_wake(writer.id))["status"] == "completed"
    task, root = await h.fresh_task(), await h.root()
    writer, analyst = await h.children()
    assert task.status == "waiting_on_customer"
    assert root.phase == P.WAITING_CHILD.value
    generations = _generations(task, root, [writer, analyst])
    assert len(generations) == 1 and before not in generations, "the whole tree was rebound"

    assert (await _answer(h, "Engineers."))["resumed"] is True
    task, root = await h.fresh_task(), await h.root()
    _writer, analyst = await h.children()
    assert task.status == "in_progress"
    assert task.details.get(TASK_INPUT_QUESTION_KEY) is None, "no question of the tree is open"
    assert len(_generations(task, root, [analyst])) == 1
    # Only the answered child wakes; its parent keeps waiting on it.
    assert analyst.phase == P.READY.value and await _pending_wake(h, analyst.id)
    assert root.phase == P.WAITING_CHILD.value and not await _root_pending_wake(h)

    assert (await h.child_wake(analyst.id))["status"] == "completed"
    assert (await h.root()).phase == P.READY.value
    assert (await h.root_wake())["status"] == "completed"
    assert heard["analyst"]["answer"] == "Engineers."


@pytest.mark.asyncio
async def test_an_answer_naming_another_request_reaches_no_child(harness, anyone_may_answer):
    h = harness
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer")
    h.scripts["writer"] = _asks("Which tone?", {}, "writer")
    await h.root_wake()
    (writer,) = await h.children()
    await h.child_wake(writer.id)
    assert (await _answer(h, "Formal.", request_id="not-this-card"))["resumed"] is False
    (writer,) = await h.children()
    assert writer.phase == P.WAITING_HUMAN.value


# ── approvals ──────────────────────────────────────────────────────────


@pytest.fixture
async def workspace_harness(db_session, monkeypatch, subs):
    """The harness on a real Workspace, user and approval policy (``social_post.publish``: ask)."""

    task, thread, envelope, selection = await loop_hitl_tests._scope(db_session)
    base._requester(monkeypatch)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=envelope.user_id),
    )
    task = await base._fresh(db_session, Task, task.id)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    h = await build_harness(
        db_session, monkeypatch, subs, (task, run, selection, wake.event_id, wake.wake_generation),
        user_id=envelope.user_id,
    )
    monkeypatch.setattr(worker, "resolve_task_run_user_id", AsyncMock(return_value=envelope.user_id))
    h.thread, h.envelope = thread, envelope
    h.executed = []

    async def tweet(args):
        # As the tool pipeline: the guard judges the call under the user's
        # own mode and rules, and spends the grant on the approved re-run.
        call_id = args.get("_runtime_tool_call_id_from_context")
        assert call_id
        blocked = await guard_runtime_tool_action(
            name=TWEET, arguments=args,
            operation_id=f"runtime:{h.task_id}:call:{call_id}",
            entity_id=envelope.entity_id, user_id=envelope.user_id,
            workspace_id=envelope.workspace_id, conversation_id=thread.id,
            task_id=h.task_id, runtime_envelope=envelope,
        )
        if blocked is not None:
            return blocked
        h.executed.append(args)
        return json.dumps({"ok": True, "tweet_id": "t-1", "text": args.get("text")})

    h.child_tools[TWEET] = tweet
    yield h
    await drop_harness_children(db_session, h)


async def _child_parks_on_tweet(h, heard: dict) -> tuple[TaskAgentChildRun, str]:
    def finish(messages, _tools):
        heard["post"] = json.loads(_tool_result(messages, "post"))
        return "Writer handled the post.", None

    h.scripts["root"] = _root_delegates("writer")
    h.scripts["writer"] = [("", [_call("post", TWEET, {"text": "We launch on 2026-10-10"})]), finish]
    await h.root_wake()
    (writer,) = await h.children()
    outcome = await h.child_wake(writer.id)
    assert outcome == {"status": "waiting_human", "child_id": writer.id, "kind": "waiting_approval"}, outcome
    (writer,) = await h.children()
    return writer, writer.checkpoint["task_loop_wait"]["hitl_request_id"]


async def _decide(h, hitl_id: str, action: str):
    return await loop_hitl_tests._decide(h.db, h, hitl_id, action)


@pytest.mark.asyncio
async def test_an_approved_child_action_runs_exactly_once(workspace_harness):
    h = workspace_harness
    heard: dict = {}
    writer, hitl_id = await _child_parks_on_tweet(h, heard)

    assert h.executed == []
    assert writer.phase == P.WAITING_HUMAN.value
    assert (await h.fresh_task()).status == "waiting_on_customer"
    request = await _request(h, hitl_id)
    tag = request.context[loop_hitl.TASK_AGENT_LOOP_HITL_CONTEXT_KEY]
    assert (tag["version"], tag["run_kind"], tag["run_id"], tag["root_run_id"]) == (
        loop_hitl.CHILD_APPROVAL_TAG_VERSION, "child", writer.id, h.run_id,
    )
    assert loop_hitl.verified_request_tag(request) == tag
    # The Task-thread card says which agent asked.
    card = (await h.db.execute(select(Message).where(
        Message.response_surface_event_id == f"task-runtime-hitl:{hitl_id}",
    ))).scalar_one()
    assert card.content.startswith("Asked by Writer, an agent working on ")
    assert card.meta["requested_by_agent"] == "Writer" and card.meta["run_id"] == writer.id
    assert card.author_subscription_id == writer.agent_subscription_id

    resolution = await _decide(h, hitl_id, "approve")
    assert resolution.runtime_metadata["task_runtime_resume"]["choice"] == "approved"
    (writer,) = await h.children()
    assert (await h.fresh_task()).status == "in_progress"
    assert writer.phase == P.READY.value and await _pending_wake(h, writer.id)
    assert not await _root_pending_wake(h)

    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert len(h.executed) == 1 and h.executed[0]["text"] == "We launch on 2026-10-10"
    assert heard["post"]["tweet_id"] == "t-1"
    assert (await _request(h, hitl_id)).status == "consumed"
    assert (await h.root_wake())["status"] == "completed"
    # A second click on the decided card runs nothing.
    await _decide(h, hitl_id, "approve")
    assert len(h.executed) == 1
    logs = (await h.db.execute(select(TaskLog.content).where(TaskLog.task_id == h.task_id))).scalars().all()
    assert "Approval granted for mcp__twitter_x__create_tweet; Writer continues and runs it." in logs


@pytest.mark.asyncio
async def test_a_rejected_child_action_writes_the_gate_result_and_the_child_continues(workspace_harness):
    h = workspace_harness
    heard: dict = {}
    writer, hitl_id = await _child_parks_on_tweet(h, heard)

    resolution = await _decide(h, hitl_id, "reject")
    assert resolution.runtime_metadata["task_runtime_resume"]["choice"] == "rejected"
    (writer,) = await h.children()
    assert writer.checkpoint["runtime_checkpoint"]["pending_tool_result"]["gate"] is True

    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert h.executed == []
    assert heard["post"]["status"] == "rejected" and heard["post"]["action_executed"] is False
    (writer,) = await h.children()
    # The host decided the call: no unconfirmed effect, a plain completion.
    assert writer.interrupted is False and writer.result["content"] == "Writer handled the post."
    assert (await _request(h, hitl_id)).status == "denied"
    assert (await h.root_wake())["status"] == "completed"


@pytest.mark.asyncio
async def test_an_approval_racing_a_re_park_is_current_and_runs_once(workspace_harness):
    """The child was woken (a stray wake) and holds its claim, about to re-park
    on the same card, when the person approves: the approval resumes it."""

    from packages.core.services import durable_run
    from packages.core.services.task_agent_child_wakeup import (
        TASK_AGENT_CHILD_RUN_SPEC,
        stage_task_agent_child_wake,
    )

    h = workspace_harness
    heard: dict = {}
    writer, hitl_id = await _child_parks_on_tweet(h, heard)
    wake = await stage_task_agent_child_wake(h.db, child_id=writer.id, reason="stray")
    claim, reason = await durable_run.claim_run_wake(
        h.db, TASK_AGENT_CHILD_RUN_SPEC, key=writer.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="racing-owner", lease_seconds=60,
    )
    assert reason == "claimed"
    await h.db.commit()
    request = await _request(h, hitl_id)
    assert await loop_hitl.task_agent_loop_approval_is_current(h.db, request=request)
    await h.db.commit()

    resolution = await _decide(h, hitl_id, "approve")

    assert resolution.runtime_metadata["task_runtime_resume"]["choice"] == "approved"
    assert (await _request(h, hitl_id)).status == "granted"
    (writer,) = await h.children()
    # The racing owner lost its claim; the approved call runs on the new wake.
    assert writer.phase == P.READY.value and writer.claim_token is None
    assert await _pending_wake(h, writer.id)
    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert len(h.executed) == 1


@pytest.mark.asyncio
async def test_an_approval_closed_before_the_child_could_wait_fails_it_plainly(workspace_harness):
    h = workspace_harness
    guarded = h.child_tools[TWEET]

    async def decided_at_once(args):
        result = await guarded(args)
        # The card closes before the agent can park on it.
        hitl_id = json.loads(result)["hitl"]["id"] if '"hitl"' in result else json.loads(result)["approval_token"]
        request = await _request(h, hitl_id)
        request.status, request.resolved_reason = "expired", "closed_at_once"
        await h.db.commit()
        return result

    h.child_tools[TWEET] = decided_at_once
    h.scripts["root"] = _root_delegates("writer")
    h.scripts["writer"] = [("", [_call("post", TWEET, {"text": "We launch on 2026-10-10"})])]
    await h.root_wake()
    (writer,) = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "failed"
    (writer,) = await h.children()
    assert writer.result["error"] == child_worker.CHILD_APPROVAL_CLOSED_BEFORE_PARK
    assert "decided or closed before it could wait on it" in writer.result["content"]
    assert writer.interrupted is False and h.executed == [], {k: v for k, v in writer.checkpoint.get("tool_evidence").items() if k in ("effect_state", "unconfirmed_calls", "effect_floor", "effect_floor_reason")}


@pytest.mark.asyncio
async def test_approving_after_the_child_was_cancelled_expires_the_card(workspace_harness):
    h = workspace_harness
    writer, hitl_id = await _child_parks_on_tweet(h, {})
    writer = (await h.children())[0]
    await settlement.cancel_call_children(h.db, [writer], error_code="test_cancelled")
    await h.db.commit()

    stale = await _decide(h, hitl_id, "approve")

    assert stale.runtime_metadata == {"approval_kind": "runtime_terminal", "approval_outcome": "expired"}
    assert (await _request(h, hitl_id)).status == "expired"
    assert h.executed == []


@pytest.mark.asyncio
async def test_the_sweep_closes_the_cards_of_ended_children(workspace_harness):
    h = workspace_harness
    writer, hitl_id = await _child_parks_on_tweet(h, {})
    await settlement.cancel_call_children(h.db, list(await h.children()), error_code="test_cancelled")
    await h.db.commit()

    assert await runtime_tasks.recover_ended_child_cards_once() == 1
    assert (await _request(h, hitl_id)).status == "expired"
    assert await runtime_tasks.recover_ended_child_cards_once() == 0


@pytest.mark.asyncio
async def test_an_ended_childs_question_card_is_closed(harness, anyone_may_answer):
    h = harness
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer")
    h.scripts["writer"] = _asks("Which tone?", {}, "writer")
    await h.root_wake()
    (writer,) = await h.children()
    await h.child_wake(writer.id)
    (card,) = await _input_cards(h)
    request_id = card.pending_action["approval_request_id"]
    await settlement.cancel_call_children(h.db, list(await h.children()), error_code="test_cancelled")
    await h.db.commit()

    async with h.host.session_factory() as db:
        assert await loop_hitl.close_ended_child_cards(db, task_id=h.task_id) == 1
        await db.commit()
    assert (await _request(h, request_id)).status == "expired"
    (card,) = await _input_cards(h)
    assert card.resolved_at is not None
    # Answering it afterwards resumes nothing.
    assert (await _answer(h, "Formal.", request_id=request_id))["resumed"] is False


async def _comment(h, text: str) -> dict:
    """A Task comment as the comment endpoint resumes with it (no request named)."""

    from apps.api.routers.tasks import _resume_hitl

    task = await h.fresh_task()
    user = SimpleNamespace(id="user-1", entity_id=task.entity_id, display_name="Ana", email="ana@example.test")
    result = await _resume_hitl(
        h.db, await h.fresh_task(), user, response_text=text,
        payload={"response": text, "fields": {}, "source": "comment"},
    )
    await h.db.commit()
    return result


@pytest.mark.asyncio
async def test_a_comment_answers_only_a_task_that_waits_on_its_person(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer", "analyst")
    h.scripts["writer"] = _asks("Which tone?", heard, "writer")
    h.scripts["analyst"] = _asks("Which audience?", heard, "analyst")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    assert (await h.fresh_task()).status == "in_progress"

    # In progress (the analyst still works): a comment is just a comment.
    assert (await _comment(h, "Just a note."))["resumed"] is False
    writer, _analyst = await h.children()
    assert writer.phase == P.WAITING_HUMAN.value and not await _pending_wake(h, writer.id)

    # Waiting on the person: the comment answers the earliest question.
    await h.child_wake(analyst.id)
    assert (await h.fresh_task()).status == "waiting_on_customer"
    assert (await _comment(h, "Formal."))["resumed"] is True
    writer, analyst = await h.children()
    assert writer.phase == P.READY.value and analyst.phase == P.WAITING_HUMAN.value
    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert heard["writer"]["answer"] == "Formal."


# ── a card that closes undecided never strands its child ───────────────


async def _expire(h, request_id: str, reason: str = "approval_ttl") -> None:
    """A close path that is not a decision (here: expiry), as its writers do it."""

    from packages.core.services.task_runtime_approvals import expire_task_runtime_approval_request

    request = (await h.db.execute(
        select(HitlRequest).where(HitlRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    assert await expire_task_runtime_approval_request(h.db, request=request, reason=reason)
    await h.db.commit()


@pytest.mark.asyncio
async def test_an_expired_question_wakes_exactly_its_child_and_the_run_continues(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer", "analyst")
    h.scripts["writer"] = _asks("Which tone?", heard, "writer")
    h.scripts["analyst"] = _asks("Which audience?", heard, "analyst")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    await h.child_wake(analyst.id)
    assert (await h.fresh_task()).status == "waiting_on_customer"
    writer_request = _card_of(await _input_cards(h), writer).pending_action["approval_request_id"]

    await _expire(h, writer_request)

    writer, analyst = await h.children()
    assert writer.phase == P.READY.value and await _pending_wake(h, writer.id)
    assert writer.checkpoint["task_loop_wait"]["resolution"] == loop_hitl.CLOSED_RESOLUTION
    assert analyst.phase == P.WAITING_HUMAN.value and not await _pending_wake(h, analyst.id)
    assert not await _root_pending_wake(h)
    assert (await h.fresh_task()).status == "in_progress"
    assert [row.kind for row in await h.journal(writer.id)][-1] == "wake"

    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert heard["writer"]["status"] == "unanswered" and heard["writer"]["reason"] == "approval_ttl"
    # The analyst still waits on its own, open card; so does the Task again.
    assert (await h.fresh_task()).status == "waiting_on_customer"
    assert (await _answer(h, "Engineers."))["resumed"] is True
    _writer, analyst = await h.children()
    assert (await h.child_wake(analyst.id))["status"] == "completed"
    assert (await h.root_wake())["status"] == "completed"


@pytest.mark.asyncio
async def test_an_expired_child_approval_becomes_its_gate_result(workspace_harness):
    h = workspace_harness
    heard: dict = {}
    writer, hitl_id = await _child_parks_on_tweet(h, heard)

    await _expire(h, hitl_id, reason="approval_ttl")

    (writer,) = await h.children()
    assert writer.phase == P.READY.value and await _pending_wake(h, writer.id)
    stored = writer.checkpoint["runtime_checkpoint"]["pending_tool_result"]
    assert stored["gate"] is True
    # The child continues from the gate result and ends its own loop. Its
    # only action did not run, so chat's outcome rule reports it as not
    # done (failed), honestly and without an unknown effect.
    outcome = await h.child_wake(writer.id)
    (writer,) = await h.children()
    assert outcome["status"] == "failed" and writer.result["error"] is None
    assert writer.result["content"] == "Writer handled the post." and writer.interrupted is False
    assert h.executed == []
    details = heard["post"]["details"]
    assert details["action_executed"] is False and details["status"] == "not_approved"
    assert "closed without approval (approval_ttl)" in json.dumps(heard["post"])
    assert (await h.root_wake())["status"] == "completed"


async def _lost_close_recovers(h) -> bool:
    """Close the writer's card without its close-time wake; does the sweep alone recover it?"""

    (writer,) = await h.children()
    request_id = writer.checkpoint["task_loop_wait"]["hitl_request_id"]
    request = await _request(h, request_id)
    # A close path that never called the wake (the backstop's case).
    request.status, request.resolved_reason = "expired", "lost_close"
    await h.db.commit()
    await runtime_tasks.recover_runs_on_closed_cards_once()
    (writer,) = await h.children()
    return writer.phase == P.READY.value and await _pending_wake(h, writer.id)


async def _writer_asks(h, heard: dict) -> None:
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer")
    h.scripts["writer"] = _asks("Which tone?", heard, "writer")
    await h.root_wake()
    (writer,) = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "waiting_human"


@pytest.mark.asyncio
async def test_the_sweep_alone_wakes_a_child_whose_close_time_wake_was_lost(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    await _writer_asks(h, heard)

    assert await _lost_close_recovers(h)
    assert (await h.fresh_task()).status == "in_progress"
    (writer,) = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "completed"
    assert heard["writer"]["status"] == "unanswered"
    assert (await h.root_wake())["status"] == "completed"
    # Nothing left for the sweep.
    assert await runtime_tasks.recover_runs_on_closed_cards_once() == 0


@pytest.mark.asyncio
async def test_the_closed_card_sweep_branch_is_load_bearing(harness, anyone_may_answer, monkeypatch):
    h = harness
    await _writer_asks(h, {})
    # Mutation: the sweep no longer considers the person phase it owns.
    monkeypatch.setattr(runtime_tasks, "TASK_AGENT_CHILD_CLOSED_CARD_PHASES", frozenset())
    assert not await _lost_close_recovers(h)


@pytest.mark.asyncio
async def test_the_sweep_never_wakes_a_child_whose_card_is_still_open(harness, anyone_may_answer):
    h = harness
    await _writer_asks(h, {})
    assert await runtime_tasks.recover_runs_on_closed_cards_once() == 0
    (writer,) = await h.children()
    assert writer.phase == P.WAITING_HUMAN.value and not await _pending_wake(h, writer.id)


# ── the root run's card that closes undecided ──────────────────────────


async def _root_expire(db, request_id: str, reason: str = "approval_ttl") -> None:
    from packages.core.services.task_runtime_approvals import expire_task_runtime_approval_request

    request = (await db.execute(
        select(HitlRequest).where(HitlRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    assert await expire_task_runtime_approval_request(db, request=request, reason=reason)
    await db.commit()


@pytest.mark.asyncio
async def test_an_expired_root_approval_wakes_the_task_and_the_run_continues(db_session, monkeypatch):
    def finish(messages, _tools):
        heard = json.loads(loop_hitl_tests._result_of(messages, "post"))
        assert "closed without approval (approval_ttl)" in json.dumps(heard)
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    host, executed, _seen, hitl_id = await loop_hitl_tests._park_on_tweet(db_session, monkeypatch, finish)
    assert (await base._fresh(db_session, Task, host.task_id)).status == "waiting_on_customer"

    await _root_expire(db_session, hitl_id)

    task = await base._fresh(db_session, Task, host.task_id)
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert task.status == "in_progress"
    assert run.phase == P.READY.value
    assert run.lifecycle_generation == task_execution_intent_generation(task)
    assert run.checkpoint["task_loop_wait"]["resolution"] == loop_hitl.CLOSED_RESOLUTION
    assert run.checkpoint["runtime_checkpoint"]["pending_tool_result"]["gate"] is True
    assert (await base._event(db_session, host.task_id)).delivered_at is None
    assert await loop_hitl_tests._run(db_session, host.task_id) == {"status": "completed", "task_id": host.task_id}
    assert executed == []


async def _root_question_closed_without_wake(db_session, monkeypatch, *, script_after=None):
    """The root parks on its question; the card then closes on a path that never woke it."""

    from packages.core import database

    host, _asked, _seen, request_id = await loop_hitl_tests._park_on_question(
        db_session, monkeypatch, script_after=script_after,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    request = await base._fresh(db_session, HitlRequest, request_id)
    request.status, request.resolved_reason = "expired", "lost_close"
    await db_session.commit()
    return host


@pytest.mark.asyncio
async def test_the_sweep_alone_recovers_a_root_whose_close_time_wake_was_lost(db_session, monkeypatch):
    def finish(messages, _tools):
        heard = json.loads(loop_hitl_tests._result_of(messages, "ask"))
        assert heard["status"] == "unanswered" and heard["reason"] == "lost_close"
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    host = await _root_question_closed_without_wake(db_session, monkeypatch, script_after=[finish])

    assert await runtime_tasks.recover_runs_on_closed_cards_once() == 1

    task = await base._fresh(db_session, Task, host.task_id)
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert task.status == "in_progress" and run.phase == P.READY.value
    assert run.lifecycle_generation == task_execution_intent_generation(task)
    assert await loop_hitl_tests._run(db_session, host.task_id) == {"status": "completed", "task_id": host.task_id}
    assert await runtime_tasks.recover_runs_on_closed_cards_once() == 0


@pytest.mark.asyncio
async def test_the_root_closed_card_sweep_branch_is_load_bearing(db_session, monkeypatch):
    host = await _root_question_closed_without_wake(db_session, monkeypatch)
    # Mutation: the sweep no longer considers the root's person phase.
    monkeypatch.setattr(runtime_tasks, "TASK_AGENT_ROOT_CLOSED_CARD_PHASES", frozenset())
    assert await runtime_tasks.recover_runs_on_closed_cards_once() == 0
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert run.phase == P.WAITING_HUMAN.value


# ── relays (the user's own judgement; #1035 adds the guard-level cases) ──


@pytest.mark.asyncio
async def test_answer_task_blocker_relays_a_child_question_as_the_users_input(harness, anyone_may_answer):
    h = harness
    heard: dict = {}
    _with_request_input(h)
    h.scripts["root"] = _root_delegates("writer", "analyst")
    h.scripts["writer"] = _asks("Which tone?", heard, "writer")
    h.scripts["analyst"] = _asks("Which audience?", heard, "analyst")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    await h.child_wake(analyst.id)
    cards = await _input_cards(h)
    analyst_request = _card_of(cards, analyst).pending_action["approval_request_id"]
    task = await h.fresh_task()

    relayed = await answer_task_blocker(
        h.db, entity_id=task.entity_id, user_id="user-1", user_display="Ana",
        request_id=analyst_request, answer="Engineers.",
    )
    await h.db.commit()

    assert relayed["resolved"] is True and relayed["outcome"] == "answered"
    writer, analyst = await h.children()
    assert analyst.phase == P.READY.value and writer.phase == P.WAITING_HUMAN.value
    assert (await h.child_wake(analyst.id))["status"] == "completed"
    assert heard["analyst"]["answer"] == "Engineers."


@pytest.mark.asyncio
async def test_answer_task_blocker_never_decides_a_child_approval(workspace_harness):
    h = workspace_harness
    writer, hitl_id = await _child_parks_on_tweet(h, {})

    relayed = await answer_task_blocker(
        h.db, entity_id=h.envelope.entity_id, user_id=h.envelope.user_id,
        request_id=hitl_id, confirm=True,
    )
    await h.db.commit()

    assert relayed["resolved"] is False and relayed["reason"] == "not_a_task_blocker"
    assert (await _request(h, hitl_id)).status == "pending"
    (writer,) = await h.children()
    assert writer.phase == P.WAITING_HUMAN.value and h.executed == []


# ── tags ───────────────────────────────────────────────────────────────


def test_a_child_tag_is_signed_with_its_root_and_cannot_be_retargeted():
    tag = loop_hitl.approval_tag(
        run_id="child-1", task_id="task-1", tool_call_id="post", hitl_request_id="req-1",
        checkpoint_digest="d", run_kind=loop_hitl.RUN_KIND_CHILD, root_run_id="root-1",
    )
    request = SimpleNamespace(
        id="req-1", origin_task_id="task-1", context={loop_hitl.TASK_AGENT_LOOP_HITL_CONTEXT_KEY: tag},
    )
    assert loop_hitl.verified_request_tag(request) == tag
    assert loop_hitl.tag_run_kind(tag) == loop_hitl.RUN_KIND_CHILD
    for field, value in (("root_run_id", "root-2"), ("run_id", "child-2"), ("version", 1)):
        forged = {**tag, field: value}
        request.context = {loop_hitl.TASK_AGENT_LOOP_HITL_CONTEXT_KEY: forged}
        assert loop_hitl.verified_request_tag(request) is None, field
    root_tag = loop_hitl.approval_tag(
        run_id="root-1", task_id="task-1", tool_call_id="post", hitl_request_id="req-1", checkpoint_digest="d",
    )
    request.context = {loop_hitl.TASK_AGENT_LOOP_HITL_CONTEXT_KEY: root_tag}
    assert loop_hitl.verified_request_tag(request) == root_tag
    assert loop_hitl.tag_run_kind(root_tag) == loop_hitl.RUN_KIND_ROOT


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type,details,owner,expected", [
    ("general", {}, "Nova", "Nova is working on “Three Instagram captions” and needs your approval to continue."),
    ("general", {}, None, "The agent working on “Three Instagram captions” needs your approval to continue."),
    ("scheduled", {}, "Nova", "The scheduled run of “Three Instagram captions” needs your approval before it can continue."),
    ("general", {"scheduled_job_id": "job"}, "Nova", "The scheduled run of “Three Instagram captions” needs your approval"),
    ("general", {"workflow_run_id": "run"}, "Nova", "The workflow working on “Three Instagram captions” needs your approval"),
])
async def test_an_approval_card_names_where_the_work_came_from(task_type, details, owner, expected):
    """Only work that was scheduled calls itself scheduled."""
    from packages.core.services.task_runtime_approvals import _task_approval_lead

    task = SimpleNamespace(
        title="Three Instagram captions", task_type=task_type, details=details,
        owner_subscription_id="sub" if owner else None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(name=owner)))
    lead = await _task_approval_lead(db, task, delegated_agent="")
    assert lead.startswith(expected)
    assert "scheduled agent" not in lead
