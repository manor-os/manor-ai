"""A run that ends outside its agent loop still ends for the person waiting.

2026-10-07 staging: a Chat run parked on a Sandbox ticket whose allocation
failed four times was set to ``failed`` by the scheduler and nothing else. Its
assistant Message stayed ``streaming`` with a running "Processing" block, no
event reached the browser, and the UI showed "Waiting to start · queue
position 1" forever. Every out-of-loop terminal path must settle the Message,
stage the client notice, and drop the queue from the status projection.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.models.task import Message
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.services.runtime_event_stream import parse_sse_frame
from packages.core.services.runtime_run_service import project_runtime_run_status
from packages.core.services.runtime_run_terminal import (
    RUNTIME_RUN_TERMINAL_NOTICE_EVENT,
    finish_runtime_run_out_of_loop,
    runtime_run_failure_message,
)
from packages.core.services.sandbox_queue_service import (
    enqueue_sandbox_reservation,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    recover_stale_sandbox_allocations,
)

PARTIAL_REPLY = "I'll build the deck. First I need a work environment"
RUNNING_BLOCKS = [{
    "id": "blk_process",
    "type": "process",
    "lifecycle_status": "running",
    "status": "running",
    "started_at_ms": 1000,
    "steps": [{"id": "step-1", "name": "run_skill", "status": "running"}],
}]


async def _parked_run(db, *, now: datetime, wait_seconds: int = 600):
    """A Chat run waiting on its Sandbox ticket, with a streaming placeholder."""

    conversation_id = generate_ulid()
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content=PARTIAL_REPLY,
        meta={
            "stream_status": "streaming",
            "origin_user_message_id": "user-question",
            "assistant_blocks_schema": 1,
            "assistant_blocks": RUNNING_BLOCKS,
        },
        created_at=now,
    )
    run_id = generate_ulid()
    run = RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=conversation_id,
        assistant_message_id=message.id,
        entity_id="entity_terminal_projection",
        user_id="user_terminal_projection",
        status=RuntimeRunStatus.RUNNING.value,
        execution_payload={"message": "make a deck", "surface": "global_owner_chat"},
        checkpoint={"pending_tool_call": {"id": "call-1", "name": "run_skill"}},
    )
    db.add_all([message, run])
    await db.flush()
    reservation = await enqueue_sandbox_reservation(
        db,
        run=run,
        tool_call_id=f"call-{run_id}",
        request_payload={"skill_name": "pptx"},
        now=now,
        max_wait_seconds=wait_seconds,
    )
    await db.flush()
    return run, message, reservation


async def _allocating(db, reservation, *, attempts: int) -> SandboxRunner:
    runner = SandboxRunner(
        id=f"runner-{reservation.id}"[:80],
        base_url=f"http://10.9.0.1:8000/{reservation.id}",
        # Bookkeeping only: some tests here commit it, and a committed healthy
        # runner with a free slot would take other tests' tickets.
        status=SandboxRunnerStatus.DRAINING.value,
        active_limit=2,
        executing_limit=1,
        active_count=1,
    )
    db.add(runner)
    reservation.status = SandboxReservationStatus.ALLOCATING.value
    reservation.runner_id = runner.id
    reservation.attempt_count = attempts
    await db.flush()
    return runner


async def _notices(db, run_id: str) -> list[RuntimeOutboxEvent]:
    return list((await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == run_id,
            RuntimeOutboxEvent.event_type == RUNTIME_RUN_TERMINAL_NOTICE_EVENT,
        )
    )).scalars().all())


async def _assert_failed_and_settled(db, run, message, *, reason: str, version_before: int):
    await db.refresh(message)
    assert run.status == RuntimeRunStatus.FAILED.value
    assert run.status_reason == reason
    assert run.completed_at is not None
    assert run.version > version_before
    assert run.lease_owner is None
    assert run.error == {"code": reason, "message": runtime_run_failure_message(reason)}

    # The reader sees a finished, failed reply rather than a running one.
    meta = message.meta
    assert meta["stream_status"] == "error"
    assert meta["stream_error"] is True
    assert meta["stop_reason"] == "error"
    assert meta["runtime_failure"] == {"run_id": run.id, "status": "failed", "reason": reason}
    assert meta["origin_user_message_id"] == "user-question"
    assert message.content == runtime_run_failure_message(reason)
    assert "try again" in message.content.lower()
    assert reason not in message.content
    process, summary = meta["assistant_blocks"]
    assert process["lifecycle_status"] == "failed"
    assert process["steps"] == RUNNING_BLOCKS[0]["steps"]
    assert summary == {
        "id": "blk_failure_summary", "type": "text", "phase": "final",
        "text": message.content,
    }

    notices = await _notices(db, run.id)
    assert [event.payload for event in notices] == [{"run_id": run.id}]
    assert notices[0].dedupe_key == f"{RUNTIME_RUN_TERMINAL_NOTICE_EVENT}:{run.id}:{run.version}"

    projection = await project_runtime_run_status(db, run, poll_after_seconds=5)
    assert projection["status"] == "failed"
    assert projection["status_reason"] == reason
    assert projection["queue"] is None
    assert projection["completed_at"] is not None


@pytest.mark.integration
async def test_last_failed_allocation_attempt_settles_the_parked_chat(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    runner = await _allocating(db_session, reservation, attempts=5)
    version_before = run.version

    await fail_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        error="SandboxAPIError",
        now=now,
        max_attempts=5,
    )
    await db_session.flush()

    assert reservation.status == SandboxReservationStatus.FAILED.value
    assert runner.active_count == 0
    await _assert_failed_and_settled(
        db_session, run, message,
        reason="sandbox_allocation_failed", version_before=version_before,
    )


@pytest.mark.integration
async def test_retryable_allocation_failure_leaves_the_chat_waiting(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    await _allocating(db_session, reservation, attempts=1)

    await fail_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        error="SandboxAPIError",
        now=now,
        max_attempts=5,
    )
    await db_session.flush()
    await db_session.refresh(message)

    assert reservation.status == SandboxReservationStatus.REQUEUED.value
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert message.meta["stream_status"] == "streaming"
    assert message.content == PARTIAL_REPLY
    assert await _notices(db_session, run.id) == []
    projection = await project_runtime_run_status(db_session, run, poll_after_seconds=5)
    assert projection["queue"]["ticket"] == reservation.id


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["allocation", "lease", "queue", "uncommitted_checkpoint", "capacity", "unavailable", "rejected"])
async def test_exhausted_sandbox_resumes_original_checkpoint_for_summary(db_session, failure):
    from packages.core.services.runtime_run_service import claim_runtime_run_execution

    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    version_before = run.version
    run.checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "Make the PDF using my previous answers."}],
        usage={"input_tokens": 42}, rounds=3, tool_calls_made=["request_input"],
        pending_tool_call={"id": "pdf-call", "name": "generate_file", "arguments": {}},
    ).to_dict()
    if failure in {"queue", "uncommitted_checkpoint"}:
        if failure == "uncommitted_checkpoint":
            # The live worker still owns an older checkpoint; a ticket that
            # never parked must not turn that stale call into a summary.
            reservation.status = SandboxReservationStatus.PENDING_CHECKPOINT.value
            run.lease_owner = "original-worker"
            run.lease_expires_at = now + timedelta(seconds=60)
        reservation.deadline_at = now - timedelta(seconds=1)
        await expire_sandbox_reservations(db_session, now=now)
        reason = "sandbox_queue_timeout"
    else:
        runner = await _allocating(db_session, reservation, attempts=5)
        reason = "sandbox_allocation_failed"
        if failure == "lease":
            reservation.next_attempt_at = now - timedelta(seconds=1)
            await recover_stale_sandbox_allocations(db_session, now=now, max_attempts=5)
        elif failure in {"capacity", "unavailable", "rejected"}:
            reservation.deadline_at = now - timedelta(seconds=1)
            reason = {"capacity": "sandbox_capacity_timeout", "unavailable": "sandbox_runner_unavailable",
                      "rejected": "sandbox_allocation_rejected"}[failure]
            await fail_sandbox_allocation(
                db_session, reservation_id=reservation.id,
                error="dependency_install_failed: private.example.test/token", now=now,
                outcome={"rejected": "fatal"}.get(failure, failure),
            )
        else:
            for _ in range(2):  # duplicate allocator delivery must not enqueue another summary
                await fail_sandbox_allocation(db_session, reservation_id=reservation.id,
                                             error="sandbox_dependency_install_failed", now=now, max_attempts=5)
        assert runner.active_count == 0
    await db_session.flush()
    if failure == "uncommitted_checkpoint":
        await _assert_failed_and_settled(db_session, run, message, reason=reason, version_before=version_before)
        return
    assert run.status == "queued"
    assert run.status_reason == "sandbox_failure_summary"
    assert run.reservation_id is None
    checkpoint = RuntimeAgentCheckpoint.from_dict(run.checkpoint)
    assert checkpoint.messages[0]["content"] == "Make the PDF using my previous answers."
    assert checkpoint.usage == {"input_tokens": 42}
    assert checkpoint.pending_tool_result["id"] == "pdf-call"
    assert checkpoint.pending_tool_result["terminal_failure"] == reason
    assert json.loads(checkpoint.pending_tool_result["content"])["error"] == reason
    assert "private.example.test" not in checkpoint.pending_tool_result["content"]
    assert (await project_runtime_run_status(db_session, run, poll_after_seconds=5))["queue"] is None
    assert message.content == PARTIAL_REPLY  # the model, not the scheduler, owns the final reply
    events = list((await db_session.scalars(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == run.id,
    ))).all())
    assert [event.event_type for event in events] == ["runtime.execute_run"]
    assert await claim_runtime_run_execution(db_session, run_id=run.id, worker_id="summary-worker", now=now) is run
    assert await claim_runtime_run_execution(db_session, run_id=run.id, worker_id="duplicate-worker", now=now) is None


@pytest.mark.integration
async def test_allocation_lease_expiry_on_last_attempt_settles_the_chat(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    await _allocating(db_session, reservation, attempts=5)
    reservation.next_attempt_at = now - timedelta(seconds=1)
    await db_session.flush()
    version_before = run.version

    await recover_stale_sandbox_allocations(db_session, now=now, max_attempts=5)
    await db_session.flush()

    assert reservation.status == SandboxReservationStatus.FAILED.value
    assert reservation.last_error == "sandbox_allocation_lease_expired"
    await _assert_failed_and_settled(
        db_session, run, message,
        reason="sandbox_allocation_failed", version_before=version_before,
    )


@pytest.mark.integration
async def test_ticket_deadline_expiry_settles_the_chat(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(
        db_session, now=now - timedelta(seconds=120), wait_seconds=60,
    )
    version_before = run.version

    await expire_sandbox_reservations(db_session, now=now)
    await db_session.flush()

    assert reservation.status == SandboxReservationStatus.EXPIRED.value
    await _assert_failed_and_settled(
        db_session, run, message,
        reason="sandbox_queue_timeout", version_before=version_before,
    )
    assert "in time" in message.content


@pytest.mark.integration
async def test_transition_is_idempotent_and_never_rewrites_a_finished_reply(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, message, _reservation = await _parked_run(db_session, now=now)
    assert await finish_runtime_run_out_of_loop(
        db_session, run, status=RuntimeRunStatus.FAILED, reason="sandbox_allocation_failed", now=now,
    )
    version = run.version
    assert not await finish_runtime_run_out_of_loop(
        db_session, run, status=RuntimeRunStatus.CANCELLED, reason="user_cancelled", now=now,
    )
    assert run.status == RuntimeRunStatus.FAILED.value
    assert run.version == version
    assert len(await _notices(db_session, run.id)) == 1

    # A reply the loop already finished is never replaced by a failure note.
    finished_run, finished, _ = await _parked_run(db_session, now=now)
    finished.meta = {"stream_status": "completed"}
    finished.content = "Here is your deck."
    await db_session.flush()
    await finish_runtime_run_out_of_loop(
        db_session, finished_run, status=RuntimeRunStatus.FAILED, reason="sandbox_queue_timeout", now=now,
    )
    await db_session.refresh(finished)
    assert finished.content == "Here is your deck."
    assert finished.meta == {"stream_status": "completed"}

    with pytest.raises(ValueError):
        await finish_runtime_run_out_of_loop(
            db_session, finished_run, status=RuntimeRunStatus.COMPLETED, reason="x", now=now,
        )


@pytest.mark.integration
async def test_outbox_dispatch_announces_failure_to_stream_and_realtime(
    db_session, monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.services import realtime, runtime_event_stream
    from packages.core.tasks import runtime_tasks

    # This global publisher also recovers expired runs. Earlier tests may have
    # committed both runs and notices; clear them before creating this target.
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.execute(delete(RuntimeRun))
    await db_session.commit()
    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    await _allocating(db_session, reservation, attempts=5)
    await fail_sandbox_allocation(
        db_session, reservation_id=reservation.id, error="SandboxAPIError", now=now,
    )
    await db_session.commit()

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(runtime_tasks.celery_app, "send_task", lambda *args, **kwargs: None)
    frames: list[tuple[str, str]] = []

    async def capture_frame(run_id, frame, **_kwargs):
        frames.append((run_id, frame))
        return "1-0"

    pushed: list[tuple[str, dict]] = []

    async def capture_snapshot(user_id, snapshot, *, entity_id):
        pushed.append(("chat_stream_snapshot", snapshot))

    async def capture_message(user_id, data, *, entity_id):
        pushed.append(("conversation_message", data))

    monkeypatch.setattr(runtime_event_stream, "append_runtime_sse_event", capture_frame)
    monkeypatch.setattr(realtime, "push_chat_stream_snapshot", capture_snapshot)
    monkeypatch.setattr(realtime, "push_conversation_message", capture_message)

    await runtime_tasks.dispatch_runtime_outbox_once(batch_size=50)

    events = [(parse_sse_frame(frame)[0], json.loads(parse_sse_frame(frame)[1]))
              for run_id, frame in frames if run_id == run.id]
    assert [name for name, _ in events] == ["runtime_status", "error", "stream_end"]
    status, error, end = (payload for _, payload in events)
    assert status == {"run_id": run.id, "status": "failed", "status_reason": "sandbox_allocation_failed"}
    assert error["message"] == runtime_run_failure_message("sandbox_allocation_failed")
    assert error["persisted"] is True
    assert error["message_id"] == message.id
    assert error["error_code"] == "sandbox_allocation_failed"
    assert end["status"] == "error" and end["persisted"] is True
    assert ("conversation_message", {
        "conversation_id": run.conversation_id,
        "message_id": message.id,
        "message_kind": "text",
        "author_kind": message.author_kind,
        "has_pending_action": False,
    }) in pushed
    snapshot = next(data for kind, data in pushed if kind == "chat_stream_snapshot")
    assert snapshot["status"] == "error" and snapshot["message_id"] == message.id

    notice = (await _notices(db_session, run.id))[0]
    await db_session.refresh(notice)
    assert notice.delivered_at is not None

    # Delivered once: the next sweep does not announce it again.
    frames.clear()
    await runtime_tasks.dispatch_runtime_outbox_once(batch_size=50)
    assert [frame for run_id, frame in frames if run_id == run.id] == []


@pytest.mark.integration
async def test_event_stream_fallback_rebuilds_the_failure_when_the_notice_is_late(
    db_session, monkeypatch,
) -> None:
    from apps.api.routers import chat as chat_router
    from packages.core import database
    from packages.core.services import runtime_event_stream

    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)
    await expire_sandbox_reservations(db_session, now=now + timedelta(seconds=601))
    await db_session.commit()
    await db_session.refresh(run)
    assert run.status == RuntimeRunStatus.FAILED.value

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def no_frames(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runtime_event_stream, "read_runtime_sse_events", no_frames)
    stale = RuntimeRun(**{
        column.key: getattr(run, column.key) for column in RuntimeRun.__table__.columns
    })
    stale.status = RuntimeRunStatus.WAITING_RESOURCE.value  # what the client last saw
    frames = [frame async for frame in chat_router._durable_runtime_event_stream(stale)]
    names = [parse_sse_frame(frame)[0] for frame in frames]
    assert names == ["runtime_run", "runtime_status", "error", "stream_end"]
    error = json.loads(parse_sse_frame(frames[2])[1])
    assert error["message"] == runtime_run_failure_message("sandbox_queue_timeout")
    assert error["persisted"] is True


@pytest.mark.integration
async def test_human_input_timeout_settles_the_parked_chat(db_session) -> None:
    from packages.core.ai.runtime.control import HUMAN_INPUT_SUSPENSION_REASON
    from packages.core.services.runtime_human_input import expire_runtime_human_input_requests

    now = datetime.now(timezone.utc)
    run, message, _reservation = await _parked_run(db_session, now=now)
    run.reservation_id = None
    run.status = RuntimeRunStatus.WAITING_RESOURCE.value
    run.status_reason = HUMAN_INPUT_SUSPENSION_REASON
    run.human_input_request_id = generate_ulid()  # the request row is gone
    await db_session.flush()

    expired = await expire_runtime_human_input_requests(db_session, now=now, limit=500)
    await db_session.flush()
    await db_session.refresh(message)

    assert run.id in expired
    assert run.status == RuntimeRunStatus.FAILED.value
    assert message.meta["stream_status"] == "error"
    assert "not answered in time" in message.content
    assert len(await _notices(db_session, run.id)) == 1


@pytest.mark.integration
async def test_cancel_of_parked_run_uses_the_same_transition(db_session) -> None:
    from packages.core.services.runtime_run_service import request_runtime_run_cancel

    now = datetime.now(timezone.utc)
    run, message, reservation = await _parked_run(db_session, now=now)

    await request_runtime_run_cancel(
        db_session, run_id=run.id, entity_id=run.entity_id, user_id=run.user_id,
    )
    await db_session.flush()
    await db_session.refresh(message)

    assert run.status == RuntimeRunStatus.CANCELLED.value
    assert run.status_reason == "user_cancelled"
    assert reservation.status == SandboxReservationStatus.CANCELLED.value
    assert message.meta["stream_status"] == "interrupted"
    assert message.meta["stop_reason"] == "cancelled"
    assert len(await _notices(db_session, run.id)) == 1
    projection = await project_runtime_run_status(db_session, run, poll_after_seconds=5)
    assert projection["queue"] is None


@pytest.mark.integration
async def test_crashed_loop_failure_settles_its_still_streaming_message(db_session) -> None:
    from packages.core.services.runtime_run_service import complete_runtime_run_execution

    now = datetime.now(timezone.utc)
    run, message, _reservation = await _parked_run(db_session, now=now)
    run.status = RuntimeRunStatus.RUNNING.value
    run.lease_owner = "worker-1"
    await db_session.flush()

    await complete_runtime_run_execution(
        db_session, run_id=run.id, worker_id="worker-1",
        error={"code": "runtime_execution_failed", "message": "KeyError: 'x'"},
    )
    await db_session.flush()
    await db_session.refresh(message)

    assert run.status == RuntimeRunStatus.FAILED.value
    assert message.meta["stream_status"] == "error"
    assert "KeyError" not in message.content
    assert len(await _notices(db_session, run.id)) == 1

    # A loop that saved its own failure first is left exactly as it wrote it.
    saved_run, saved, _ = await _parked_run(db_session, now=now)
    saved_run.status = RuntimeRunStatus.RUNNING.value
    saved_run.lease_owner = "worker-2"
    saved.meta = {"stream_status": "error", "stream_error": True}
    saved.content = "The model provider rejected the request."
    await db_session.flush()
    await complete_runtime_run_execution(
        db_session, run_id=saved_run.id, worker_id="worker-2",
        error={"code": "llm_call_failed", "message": "rejected"},
    )
    await db_session.flush()
    await db_session.refresh(saved)
    assert saved.content == "The model provider rejected the request."
    assert await _notices(db_session, saved_run.id) == []
