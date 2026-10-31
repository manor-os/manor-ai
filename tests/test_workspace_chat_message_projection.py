"""Workspace chat history omits run-envelope meta without changing persistence."""
from __future__ import annotations

from datetime import datetime, timezone

from apps.api.routers.workspace_chat import _message_refs_with_hydrated_task_ref, _to_message
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


def test_flow_cards_read_in_the_viewers_language_but_keep_what_the_run_produced():
    from apps.api.routers.workspace_chat import _in_viewer_language
    from packages.core.services.viewer_language import run_display_translations, run_names_for

    translations = run_display_translations(
        [{"id": "review", "name": "Review the cut", "config": {"review_title": "Approve the cut"},
          "i18n": {"zh": {"name": "审阅成片", "config": {"review_title": "确认成片"}}}}],
        name="Video Studio · Promos", name_translations={"zh": "Video Studio·商业宣传"},
        texts={"zh": {"Promos": "商业宣传"}},
    )
    activity = _message(meta={
        "workflow_run_id": "run-1", "workflow_title": "Promos",
        "workflow_steps": [{"id": "review", "name": "Review the cut", "status": "paused"}],
    }, refs=[{"type": "workflow", "id": "flow-1", "title": "Promos"}, {"type": "workflow_run", "id": "run-1"}])
    wait = _message(message_kind="hitl_request", meta={"workflow_run_id": "run-1", "hitl_requests": [{"id": "hitl-1",
        "review_title": "Approve the cut", "workflow": {"id": "flow-1", "name": "Video Studio · Promos"},
        "node": {"id": "review", "name": "Review the cut"},
    }]}, pending_action={"kind": "workflow_approval", "workflow_run_id": "run-1", "step_id": "review",
                         "title": "Approve the cut", "prompt": "Review the cut before rendering."})

    names = run_names_for(translations, "zh-CN")
    card = _in_viewer_language(_to_message(activity), names)
    assert card.meta["workflow_title"] == "商业宣传"
    assert card.meta["workflow_steps"][0]["name"] == "审阅成片"
    assert card.refs[0]["title"] == "商业宣传"
    shown = _in_viewer_language(_to_message(wait), names)
    assert shown.pending_action["title"] == "确认成片"
    # The wait's message is what the run wrote; it is not renamed.
    assert shown.pending_action["prompt"] == "Review the cut before rendering."
    assert shown.hitl_requests[0]["node"]["name"] == "审阅成片"
    assert shown.hitl_requests[0]["workflow"]["name"] == "Video Studio·商业宣传"
    assert _in_viewer_language(_to_message(activity), run_names_for(translations, "en")).meta["workflow_title"] == "Promos"


def test_task_reference_shows_the_live_status_over_the_saved_snapshot():
    message = _message(refs=[{"type": "task", "id": "task-1", "title": "Captions", "status": "in_progress"}])

    refs = _message_refs_with_hydrated_task_ref(
        message, {}, {}, {"task-1": {"title": "Renamed", "status": "completed", "priority": 2}},
    )

    assert refs == [{"type": "task", "id": "task-1", "title": "Captions", "status": "completed", "priority": 2}]
