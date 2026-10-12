"""Workspace chat history omits run-envelope meta without changing persistence."""
from __future__ import annotations

from datetime import datetime, timezone

from apps.api.routers.workspace_chat import _to_message
from packages.core.models.base import generate_ulid
from packages.core.models.task import Message


def _message(**kwargs) -> Message:
    payload = dict(
        id=generate_ulid(),
        conversation_id=generate_ulid(),
        role="assistant",
        content="Done",
        created_at=datetime.now(timezone.utc),
        message_kind="text",
        author_kind="agent",
    )
    payload.update(kwargs)
    return Message(**payload)


def test_workspace_chat_history_omits_runtime_envelope_and_duplicate_blocks():
    persisted_meta = {
        "stream_status": "complete",
        "client_message_id": "client-1",
        "workflow_run_id": "run-1",
        "workflow_title": "Publish video",
        "workflow_binding_id": "bind-1",
        "proposal": {"id": "proposal-1"},
        "limit_detail": {"code": "quota_exceeded", "message": "Try tomorrow"},
        "assistant_blocks_schema": "v1",
        "assistant_blocks": [{
            "id": "process-1",
            "type": "process",
            "steps": [{"id": "step-1", "name": "search", "status": "ok"}],
        }],
        "hitl_requests": [{"id": "hitl-1", "kind": "approval"}],
        "runtime": {
            "allowed_tool_names": ["mcp__youtube__upload", "sandbox"],
            "tool_bindings": [{"tool": "mcp__youtube__upload", "binding_id": "b1"}],
            "skill_descriptors": [{"name": "stickman"}],
            "principal": {"kind": "user", "id": "user-1"},
        },
    }
    message = _message(
        tool_calls=[{"name": "search", "result": "ok", "status": "ok"}],
        meta=persisted_meta,
    )

    response = _to_message(message)
    meta = response.meta or {}

    assert meta["stream_status"] == "complete"
    assert meta["client_message_id"] == "client-1"
    assert meta["workflow_run_id"] == "run-1"
    assert meta["workflow_title"] == "Publish video"
    assert meta["workflow_binding_id"] == "bind-1"
    assert meta["proposal"] == {"id": "proposal-1"}
    assert meta["limit_detail"]["code"] == "quota_exceeded"
    assert "runtime" not in meta
    assert "allowed_tool_names" not in meta
    assert "tool_bindings" not in meta
    assert "assistant_blocks_schema" not in meta
    assert "assistant_blocks" not in meta
    assert "hitl_requests" not in meta
    assert response.assistant_blocks[0]["id"] == "process-1"
    assert response.hitl_requests[0]["id"] == "hitl-1"
    assert response.tool_calls[0]["name"] == "search"
    assert message.meta == persisted_meta


def test_workspace_chat_history_keeps_sub_agent_events_outside_runtime():
    message = _message(meta={
        "sub_agent_events": [{"run_id": "child-1", "status": "completed", "tools": []}],
        "runtime": {"allowed_tool_names": ["sandbox"]},
    })

    response = _to_message(message)

    assert response.meta["sub_agent_events"][0]["run_id"] == "child-1"
    assert "runtime" not in response.meta
