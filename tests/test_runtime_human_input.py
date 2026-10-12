"""A durable chat run parks on a request_input question and resumes on the answer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.control import (
    HUMAN_INPUT_ANSWERED_REASON,
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeAgentCheckpoint,
    RuntimeToolSuspension,
)
from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
)
from packages.core.services.runtime_human_input import (
    RuntimeHumanInputError,
    answer_runtime_human_input,
    is_awaiting_human_input,
)
from packages.core.services.runtime_run_service import (
    claim_runtime_run_execution,
    create_runtime_run,
    persist_runtime_run_suspension,
    project_runtime_run_status,
    request_runtime_run_cancel,
)

pytestmark = pytest.mark.asyncio


def _control(question: str = "Which unit?", *, call_id: str = "call-ask") -> dict:
    suspension = RuntimeToolSuspension(
        kind=HUMAN_INPUT_SUSPENSION_KIND,
        reservation_id="",
        poll_after_seconds=5,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=24),
        reason=HUMAN_INPUT_SUSPENSION_REASON,
        payload={"question": question, "options": ["4C", "5A"], "context": "Two units match."},
    )
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "find a unit"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "name": "request_input", "arguments": {"question": question}}
            ]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": call_id, "name": "request_input", "arguments": {"question": question}},
    )
    control = suspension.to_dict()
    control["checkpoint"] = checkpoint.to_dict()
    return control


async def _parked_run(db, *, entity="entity_hi", user="user_hi", conv="conv_hi") -> RuntimeRun:
    run = await create_runtime_run(
        db,
        conversation_id=conv,
        assistant_message_id="msg_hi",
        entity_id=entity,
        user_id=user,
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "find a unit"},
    )
    await db.flush()
    run = await persist_runtime_run_suspension(db, run_id=run.id, control=_control())
    await db.flush()
    return run


async def test_persist_parks_the_run_on_an_open_input_request(db_session) -> None:
    run = await _parked_run(db_session)

    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert run.status_reason == HUMAN_INPUT_SUSPENSION_REASON
    assert run.reservation_id is None
    assert run.human_input_request_id
    assert is_awaiting_human_input(run)
    request = await db_session.get(HitlRequest, run.human_input_request_id)
    assert request.status == ApprovalStatus.PENDING.value
    assert request.hitl_type == HitlType.INPUT.value
    assert request.origin_conversation_id == "conv_hi"
    assert request.context["question"]["question"] == "Which unit?"
    assert request.context["tool_call_id"] == "call-ask"
    assert request.payload["question"] == "Which unit?"

    projection = await project_runtime_run_status(db_session, run, poll_after_seconds=5)
    assert projection["queue"] is None
    assert projection["human_input"]["request_id"] == request.id
    assert projection["human_input"]["question"] == "Which unit?"
    assert projection["human_input"]["options"] == ["4C", "5A"]
    assert projection["human_input"]["status"] == ApprovalStatus.PENDING.value
    assert "checkpoint" not in json.dumps(projection)

    # Unanswered: not claimable by a resume worker.
    assert await claim_runtime_run_execution(db_session, run_id=run.id, worker_id="w1") is None


async def test_answer_writes_the_pending_result_and_queues_the_resume(db_session) -> None:
    run = await _parked_run(db_session)
    request_id = run.human_input_request_id

    answered = await answer_runtime_human_input(
        db_session,
        run_id=run.id,
        entity_id="entity_hi",
        user_id="user_hi",
        answer="4C please",
        fields={"budget": "1200"},
        answered_by="Amy",
    )
    await db_session.flush()

    assert answered.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert answered.status_reason == HUMAN_INPUT_ANSWERED_REASON
    checkpoint = RuntimeAgentCheckpoint.from_dict(answered.checkpoint)
    assert checkpoint.pending_tool_result["id"] == "call-ask"
    stored = json.loads(checkpoint.pending_tool_result["content"])
    assert stored == {
        "status": "answered",
        "answer": "4C please",
        "fields": {"budget": "1200"},
        "answered_by": "Amy",
        "instruction": stored["instruction"],
    }
    request = await db_session.get(HitlRequest, request_id)
    assert request.status == ApprovalStatus.CONSUMED.value
    assert request.decided_via == "chat_input"
    assert request.context["answer"]["answer"] == "4C please"

    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == run.id,
            RuntimeOutboxEvent.event_type == "runtime.resume_run",
        )
    )).scalars().all())
    assert len(events) == 1
    assert events[0].payload == {"run_id": run.id, "reservation_id": None, "reservation_version": None}

    projection = await project_runtime_run_status(db_session, answered, poll_after_seconds=5)
    assert projection["human_input"]["answer"]["answer"] == "4C please"

    # Answered: a resume worker can now claim it, with no reservation involved.
    claimed = await claim_runtime_run_execution(db_session, run_id=run.id, worker_id="w1")
    assert claimed is not None and claimed.status == RuntimeRunStatus.RESUMING.value


async def test_second_answer_is_a_no_op_not_a_second_resume(db_session) -> None:
    run = await _parked_run(db_session)
    await answer_runtime_human_input(
        db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="4C",
    )
    again = await answer_runtime_human_input(
        db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="5A",
    )
    await db_session.flush()
    assert json.loads(
        RuntimeAgentCheckpoint.from_dict(again.checkpoint).pending_tool_result["content"]
    )["answer"] == "4C"
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == run.id,
            RuntimeOutboxEvent.event_type == "runtime.resume_run",
        )
    )).scalars().all())
    assert len(events) == 1


async def test_answer_rejects_missing_or_foreign_runs_and_empty_answers(db_session) -> None:
    run = await _parked_run(db_session)
    with pytest.raises(RuntimeHumanInputError) as empty:
        await answer_runtime_human_input(
            db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="  ",
        )
    assert empty.value.code == "answer_required"
    with pytest.raises(RuntimeHumanInputError) as foreign:
        await answer_runtime_human_input(
            db_session, run_id=run.id, entity_id="someone_else", user_id="user_hi", answer="4C",
        )
    assert foreign.value.code == "run_not_found"


async def test_a_run_that_is_not_parked_cannot_be_answered(db_session) -> None:
    run = await create_runtime_run(
        db_session,
        conversation_id="conv_running",
        assistant_message_id="msg_running",
        entity_id="entity_hi",
        user_id="user_hi",
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "hi"},
    )
    await db_session.flush()
    with pytest.raises(RuntimeHumanInputError) as exc:
        await answer_runtime_human_input(
            db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="4C",
        )
    assert exc.value.code == "not_waiting"


async def test_cancel_denies_the_open_question(db_session) -> None:
    run = await _parked_run(db_session)
    request_id = run.human_input_request_id
    cancelled = await request_runtime_run_cancel(
        db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi",
    )
    await db_session.flush()
    assert cancelled.status == RuntimeRunStatus.CANCELLED.value
    request = await db_session.get(HitlRequest, request_id)
    assert request.status == ApprovalStatus.DENIED.value
    with pytest.raises(RuntimeHumanInputError):
        await answer_runtime_human_input(
            db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="4C",
        )


async def test_a_later_sandbox_wait_clears_the_question_pointer(db_session) -> None:
    """Persisting a sandbox suspension on a run that once asked a question must
    not leave the run looking parked on that (already answered) question."""
    from packages.core.models.runtime_run import SandboxReservation, SandboxReservationStatus

    run = await _parked_run(db_session)
    await answer_runtime_human_input(
        db_session, run_id=run.id, entity_id="entity_hi", user_id="user_hi", answer="4C",
    )
    now = datetime.now(timezone.utc)
    db_session.add(SandboxReservation(
        id="01RESERVATIONHUMANINPUT001",
        runtime_run_id=run.id,
        root_run_id=run.root_run_id,
        tool_call_id="call-skill",
        entity_id="entity_hi",
        user_id="user_hi",
        status=SandboxReservationStatus.PENDING_CHECKPOINT.value,
        priority=100,
        enqueued_at=now,
        deadline_at=now + timedelta(minutes=5),
    ))
    await db_session.flush()
    control = RuntimeToolSuspension(
        kind="waiting_resource",
        reservation_id="01RESERVATIONHUMANINPUT001",
        poll_after_seconds=5,
        deadline_at=now + timedelta(minutes=5),
    ).to_dict()
    control["checkpoint"] = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=2, tool_calls_made=[],
        pending_tool_call={"id": "call-skill", "name": "invoke_skill"},
    ).to_dict()
    run = await persist_runtime_run_suspension(db_session, run_id=run.id, control=control)
    assert run.human_input_request_id is None
    assert run.reservation_id == "01RESERVATIONHUMANINPUT001"
    assert not is_awaiting_human_input(run)


async def test_only_the_run_owner_can_answer(db_session) -> None:
    run = await _parked_run(db_session)
    with pytest.raises(RuntimeHumanInputError) as other_user:
        await answer_runtime_human_input(
            db_session, run_id=run.id, entity_id="entity_hi", user_id="someone_else", answer="4C",
        )
    assert other_user.value.code == "run_not_found"
    request = await db_session.get(HitlRequest, run.human_input_request_id)
    assert request.status == ApprovalStatus.PENDING.value


async def test_unanswered_questions_expire_and_fail_the_run(db_session) -> None:
    from packages.core.services.runtime_human_input import (
        expire_runtime_human_input_requests,
    )

    fresh = await _parked_run(db_session, conv="conv_fresh")
    stale = await _parked_run(db_session, conv="conv_stale")
    stale_request = await db_session.get(HitlRequest, stale.human_input_request_id)
    stale_request.context = {
        **stale_request.context,
        "deadline_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }
    await db_session.flush()

    expired = await expire_runtime_human_input_requests(db_session)
    await db_session.flush()
    assert expired == [stale.id]
    assert stale.status == RuntimeRunStatus.FAILED.value
    assert stale.status_reason == "human_input_timeout"
    assert (await db_session.get(HitlRequest, stale.human_input_request_id)).status == ApprovalStatus.DENIED.value
    assert fresh.status == RuntimeRunStatus.WAITING_RESOURCE.value
    with pytest.raises(RuntimeHumanInputError):
        await answer_runtime_human_input(
            db_session, run_id=stale.id, entity_id="entity_hi", user_id="user_hi", answer="late",
        )


async def test_question_and_answer_are_messages_ordered_before_the_reply(db_session) -> None:
    """After a reload the transcript must read prompt → question → answer → reply."""
    from packages.core.models.task import Conversation, Message
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.runtime_human_input import (
        record_human_input_answer_message,
        record_human_input_question_message,
    )

    db_session.add(Conversation(id="conv_order", entity_id="entity_hi", title="order"))
    await db_session.flush()
    prompt = await add_message(db_session, "conv_order", role="user", content="find a unit")
    prompt.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    placeholder = await add_message(db_session, "conv_order", role="assistant", content="")
    placeholder.created_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    run = await create_runtime_run(
        db_session,
        conversation_id="conv_order",
        assistant_message_id=placeholder.id,
        entity_id="entity_hi",
        user_id="user_hi",
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "find a unit"},
    )
    await db_session.flush()
    run = await persist_runtime_run_suspension(db_session, run_id=run.id, control=_control())

    question = await record_human_input_question_message(db_session, run=run)
    assert question.role == "assistant"
    assert question.content == "Which unit?\nOptions: 4C / 5A\nTwo units match."
    assert question.meta["runtime_input_role"] == "question"

    answer = await record_human_input_answer_message(db_session, run=run, answer_text="4C")
    placeholder.content = "Noted: unit 4C."
    await db_session.flush()

    rows = list((await db_session.execute(
        select(Message).where(Message.conversation_id == "conv_order")
        .order_by(Message.created_at.asc(), Message.id.asc())
    )).scalars().all())
    assert [(m.role, m.content) for m in rows] == [
        ("user", "find a unit"),
        ("assistant", "Which unit?\nOptions: 4C / 5A\nTwo units match."),
        ("user", "4C"),
        ("assistant", "Noted: unit 4C."),
    ]
    assert answer.meta["runtime_input_request_id"] == run.human_input_request_id
