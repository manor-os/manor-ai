"""Browser Chat owns input continuation independently of sandbox placement."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.api.routers import chat as chat_router
from packages.core.config import get_settings
from packages.core.models.runtime_run import RuntimeOutboxEvent, RuntimeRun
from packages.core.models.task import Message
from packages.core.services.runtime_event_stream import parse_sse_frame
from packages.core.services.runtime_run_service import persist_runtime_run_suspension
from test_chat import _auth
from test_runtime_human_input import _control

pytestmark = pytest.mark.asyncio
durable_runtime_event_stream = chat_router._durable_runtime_event_stream


@pytest.mark.parametrize("input_role", ["question", "answer"])
async def test_history_preserves_input_identity_without_internal_state(input_role):
    identity = {
        "runtime_run_id": "run",
        "runtime_input_request_id": "question",
        "runtime_input_role": input_role,
    }
    message = Message(
        id="message", conversation_id="conversation",
        role="assistant" if input_role == "question" else "user", content="Test input",
        meta={**identity, "checkpoint": {"private": "state"}, "execution_payload": {"internal": True}},
    )
    assert chat_router._to_chat_message_response(message).meta == identity


@pytest.fixture
def browser_runtime(monkeypatch):
    monkeypatch.setattr(
        "packages.core.services.chat_intent_routing.auto_chat_intent_routing_allowed",
        lambda **_: False,
    )

    async def events(run, **_):
        yield chat_router.format_sse("runtime_run", {
            "run_id": run.id, "conversation_id": run.conversation_id,
            "message_id": run.assistant_message_id, "status": run.status,
        })

    monkeypatch.setattr(chat_router, "_durable_runtime_event_stream", events)
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.dispatch_runtime_outbox_once", AsyncMock(return_value=0),
    )


async def _start(client, headers):
    response = await client.post(
        "/api/v1/chat/stream", headers=headers, data={"message": "Help me apply; ask for missing facts"},
    )
    assert response.status_code == 200, response.text
    frames = [parse_sse_frame(frame) for frame in response.text.split("\n\n") if frame.strip()]
    return json.loads(next(payload for event, payload in frames if event == "runtime_run"))


@pytest.mark.parametrize("execution,coordination", [
    ("inline", "local"), ("inline", "external-runner"),
    ("durable", "local"), ("durable", "external-runner"),
])
async def test_persisted_chat_always_commits_resumable_run(
    client, db_session, monkeypatch, browser_runtime, execution, coordination,
):
    monkeypatch.setattr(get_settings(), "MANOR_RUNTIME_EXECUTION_MODE", execution)
    monkeypatch.setattr(get_settings(), "SANDBOX_COORDINATION_MODE", coordination)
    headers = await _auth(client, "input_mode")
    event = await _start(client, headers)
    run = await db_session.get(RuntimeRun, event["run_id"])
    assert run is not None and run.status == "queued"
    assert run.conversation_id == event["conversation_id"]
    assert run.assistant_message_id == event["message_id"]
    assert run.execution_payload["message"] == "Help me apply; ask for missing facts"
    assert await db_session.get(Message, run.assistant_message_id) is not None
    outbox = list((await db_session.scalars(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == run.id,
        RuntimeOutboxEvent.event_type == "runtime.execute_run",
    ))).all())
    assert len(outbox) == 1


async def test_reload_answer_identity_and_duplicate_submission(client, db_session, browser_runtime):
    headers = await _auth(client, "input_owner")
    event = await _start(client, headers)
    run = await persist_runtime_run_suspension(db_session, run_id=event["run_id"], control=_control())
    await db_session.commit()
    run_id, conv_id, request_id = run.id, run.conversation_id, run.human_input_request_id

    active_url = f"/api/v1/chat/conversations/{conv_id}/active-run"
    active = await client.get(active_url, headers=headers)
    assert active.status_code == 200, active.text
    payload = active.json()
    assert payload["assistant_message_id"] == event["message_id"]
    assert payload["human_input"]["request_id"] == request_id
    assert payload["human_input"]["question"] == "Which unit?"
    assert "checkpoint" not in active.text

    # The initial SSE projection alone restores input even after Redis history
    # expires. No execution or cached runtime_waiting event is required.
    stream = durable_runtime_event_stream(run)
    try:
        event_type, data = parse_sse_frame(await anext(stream))
        assert event_type == "runtime_run"
        assert json.loads(data)["human_input"]["request_id"] == request_id
    finally:
        await stream.aclose()

    other = await _auth(client, "input_other")
    assert (await client.get(active_url, headers=other)).status_code == 404
    input_url = f"/api/v1/chat/runs/{run_id}/input"
    stale = await client.post(input_url, headers=headers, json={"request_id": "old-question", "answer": "wrong"})
    assert stale.status_code == 409
    assert (await client.post(input_url, headers=other, json={"request_id": request_id, "answer": "wrong"})).status_code == 404
    responses = await asyncio.gather(*(
        client.post(input_url, headers=headers, json={"request_id": request_id, "answer": text})
        for text in ["4C please", "different retransmitted answer"]
    ))
    for response in responses:
        assert response.status_code == 200, response.text
    await db_session.refresh(run)
    assert run.checkpoint["pending_tool_result"]["id"] == "call-ask"
    accepted = json.loads(run.checkpoint["pending_tool_result"]["content"])["answer"]
    assert accepted in {"4C please", "different retransmitted answer"}
    answers = list((await db_session.scalars(select(Message).where(
        Message.conversation_id == conv_id,
        Message.meta["runtime_input_role"].astext == "answer",
    ))).all())
    assert [message.content for message in answers] == [accepted]
    resumes = list((await db_session.scalars(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == run_id,
        RuntimeOutboxEvent.event_type == "runtime.resume_run",
    ))).all())
    assert len(resumes) == 1


async def test_worker_parks_visible_question_and_resumes_same_chat(client, db_session, browser_runtime, monkeypatch):
    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.services import chat_service
    from packages.core.tasks.runtime_tasks import execute_runtime_run_once
    from test_chat_stream_resume import _context_factory

    headers = await _auth(client, "input_worker")
    event = await _start(client, headers)
    calls = []

    async def model_loop(**kwargs):
        calls.append(kwargs)
        assert kwargs["human_input_suspension"] is True
        assert kwargs["runtime_run_id"] == event["run_id"]
        checkpoint = kwargs["runtime_checkpoint"]
        if checkpoint is None:
            return AgenticResult(
                content="", messages=[], usage={}, rounds=1, tool_calls_made=["request_input"],
                stop_reason="waiting_resource", control=_control(),
            )
        assert checkpoint["pending_tool_call"]["id"] == "call-ask"
        assert checkpoint["pending_tool_result"]["id"] == "call-ask"
        assert json.loads(checkpoint["pending_tool_result"]["content"])["answer"] == "4C"
        return AgenticResult(content="I will use unit 4C.", messages=[], usage={}, rounds=2, tool_calls_made=[])

    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", _context_factory(None))
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", model_loop)
    monkeypatch.setattr(chat_service, "record_chat_llm_usage", AsyncMock())
    first = await execute_runtime_run_once(run_id=event["run_id"], worker_id="initial")
    assert first["waiting"] is True
    question = await db_session.scalar(select(Message).where(
        Message.conversation_id == event["conversation_id"],
        Message.meta["runtime_input_role"].astext == "question",
    ))
    assert question is not None and "Which unit?" in question.content
    run = await db_session.get(RuntimeRun, event["run_id"])
    request_id = run.human_input_request_id
    # Release the read transaction before exercising the independent HTTP path.
    await db_session.commit()
    response = await client.post(f"/api/v1/chat/runs/{run.id}/input", headers=headers, json={
        "request_id": request_id, "answer": "4C",
    })
    assert response.status_code == 200, response.text
    final = await execute_runtime_run_once(run_id=run.id, worker_id="resume")
    assert final["status"] == "completed"
    await db_session.refresh(run)
    assistant = await db_session.get(Message, event["message_id"])
    assert assistant.content == "I will use unit 4C."
    assert run.status == "completed" and len(calls) == 2
