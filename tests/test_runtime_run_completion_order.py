from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from packages.core.models.runtime_run import RuntimeRunStatus
from packages.core.models.task import Message
from packages.core.services.runtime_event_stream import parse_sse_frame
from packages.core.tasks import runtime_tasks


@pytest.fixture
def execution(monkeypatch):
    run = SimpleNamespace(
        id="run-completion-order",
        conversation_id="conversation-completion-order",
        assistant_message_id="message-completion-order",
        entity_id="entity-completion-order",
        user_id="user-completion-order",
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "continue"},
        checkpoint=None,
        status=RuntimeRunStatus.RUNNING.value,
    )
    state = SimpleNamespace(
        run=run,
        committed_status=run.status,
        completion_calls=[],
        published=[],
        stream_count=0,
        frames=[],
        waiting=False,
        cancelled=False,
        publish_error=False,
        assistant_message=SimpleNamespace(
            id=run.assistant_message_id, conversation_id=run.conversation_id, meta={},
        ),
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            state.committed_status = run.status

        async def get(self, _model, _run_id):
            if _model is Message:
                return state.assistant_message
            return run

    async def claim(_db, **_kwargs):
        if state.committed_status in RuntimeRunStatus.terminal():
            return None
        return run

    async def complete(_db, **kwargs):
        state.completion_calls.append(kwargs)
        run.status = (
            RuntimeRunStatus.CANCELLED.value
            if state.cancelled
            else RuntimeRunStatus.FAILED.value
            if kwargs.get("error")
            else RuntimeRunStatus.COMPLETED.value
        )
        return run

    async def stream(*_args, **_kwargs):
        state.stream_count += 1
        for frame in state.frames:
            yield frame
        if state.waiting:
            run.status = RuntimeRunStatus.WAITING_RESOURCE.value

    async def append(_run_id, frame):
        if state.publish_error and parse_sse_frame(frame)[0] == "stream_end":
            raise RuntimeError("Redis unavailable")
        state.published.append((frame, state.committed_status))

    async def heartbeat(*_args):
        await asyncio.Future()

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: FakeSession,
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.claim_runtime_run_execution",
        claim,
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.complete_runtime_run_execution",
        complete,
    )
    monkeypatch.setattr("packages.core.ai.runtime.runtime_stream_chat_turn", stream)
    monkeypatch.setattr(runtime_tasks, "append_runtime_sse_event", append)
    monkeypatch.setattr(runtime_tasks, "_runtime_run_lease_heartbeat", heartbeat)
    return state


@pytest.mark.unit
@pytest.mark.parametrize(
    ("end_payload", "cancelled", "expected_status", "expected_error"),
    [
        ('{"stop_reason":"completed"}', False, "completed", None),
        (
            '{"stop_reason":"error","error":"Provider failed"}',
            False,
            "failed",
            {"code": "error", "message": "Provider failed"},
        ),
        ('{"stop_reason":"cancelled"}', True, "cancelled", None),
    ],
)
async def test_stream_end_waits_for_committed_terminal_status(
    execution, end_payload, cancelled, expected_status, expected_error,
) -> None:
    text_frame = 'event: text_delta\ndata: {"content":"Done"}\n\n'
    end_frame = f"event: stream_end\ndata: {end_payload}\n\n"
    execution.frames = [text_frame, end_frame]
    execution.cancelled = cancelled

    result = await runtime_tasks.execute_runtime_run_once(
        run_id=execution.run.id,
        worker_id="worker-completion-order",
    )

    assert execution.published == [
        (text_frame, RuntimeRunStatus.RUNNING.value),
        (end_frame, expected_status),
    ]
    assert len(execution.completion_calls) == 1
    assert execution.completion_calls[0]["error"] == expected_error
    assert result["status"] == expected_status


@pytest.mark.unit
async def test_waiting_resource_retains_active_run_without_completion(execution) -> None:
    waiting_frame = (
        'event: runtime_status\ndata: {"status":"waiting_resource"}\n\n'
    )
    execution.frames = [waiting_frame]
    execution.waiting = True

    result = await runtime_tasks.execute_runtime_run_once(
        run_id=execution.run.id,
        worker_id="worker-completion-order",
    )

    assert execution.completion_calls == []
    assert [frame for frame, _status in execution.published] == [waiting_frame]
    assert execution.committed_status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert result == {"claimed": True, "waiting": True, "run_id": execution.run.id}


@pytest.mark.unit
async def test_run_result_retains_validated_goal_and_capability_metadata(execution):
    outcome = {"status": "not_attempted", "evidence_refs": [], "limitations": ["Budget exhausted"]}
    hints = {"version": 1, "scope": {"conversation_id": execution.run.conversation_id},
             "hints": [{"name": "read_file", "source": "used"}]}
    execution.assistant_message.meta = {
        "outcome": outcome, "capability_hints": hints, "stop_reason": "max_rounds",
        "private_unrelated_field": "not copied",
    }
    execution.frames = ['event: stream_end\ndata: {"stop_reason":"max_rounds"}\n\n']
    await runtime_tasks.execute_runtime_run_once(run_id=execution.run.id, worker_id="worker")
    assert execution.completion_calls[0]["result"] == {
        "message_id": execution.run.assistant_message_id, "outcome": outcome,
        "capability_hints": hints, "stop_reason": "max_rounds",
    }


@pytest.mark.unit
async def test_end_publication_failure_preserves_completion_without_reexecution(execution) -> None:
    execution.frames = ['event: stream_end\ndata: {"stop_reason":"completed"}\n\n']
    execution.publish_error = True

    with pytest.raises(RuntimeError, match="Redis unavailable"):
        await runtime_tasks.execute_runtime_run_once(
            run_id=execution.run.id,
            worker_id="worker-completion-order",
        )

    assert execution.committed_status == RuntimeRunStatus.COMPLETED.value
    assert len(execution.completion_calls) == 1
    assert execution.completion_calls[0]["error"] is None

    result = await runtime_tasks.execute_runtime_run_once(
        run_id=execution.run.id,
        worker_id="worker-redelivery",
    )

    assert result == {"claimed": False, "run_id": execution.run.id}
    assert execution.stream_count == 1
