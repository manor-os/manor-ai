from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.services.chat_approvals import (
    chat_hitl_action_is_pending,
    parse_hitl_action,
    resolve_chat_approval_turn,
)
from packages.core.services.workflow_chat_projection import (
    _notify_update,
    _workflow_run_url,
    project_workflow_step,
)
from packages.core.services.workflow_chat_approvals import resolve_chat_workflow_wait
from packages.core.services.workflow_run_trace import build_execution_snapshot
from packages.core.services.workflow_service import create_workflow, start_workflow


@pytest.mark.parametrize("workspace_id", [None, "workspace/one"])
def test_workflow_result_url_uses_the_flow_run_route(workspace_id) -> None:
    run = SimpleNamespace(id="run/one", workflow_id="flow/one", workspace_id=workspace_id)
    assert _workflow_run_url(run) == "/flows/flow%2Fone/runs/run%2Fone"


def test_chat_approval_payload_preserves_edited_review() -> None:
    assert parse_hitl_action(
        '{"hitl_id":"approval-1","action":"approve",'
        '"payload":{"review":{"post_text":"Edited copy"}}}'
    ) == (
        "approval-1",
        "approve",
        {"review": {"post_text": "Edited copy"}},
    )


def test_chat_revision_payload_preserves_revision_request() -> None:
    assert parse_hitl_action(
        '{"hitl_id":"approval-2","action":"revise",'
        '"payload":{"review":{"revision_request":"Shorten the opening."}}}'
    ) == (
        "approval-2",
        "revise",
        {"review": {"revision_request": "Shorten the opening."}},
    )


def test_chat_hitl_payload_preserves_safe_workflow_specific_choice() -> None:
    assert parse_hitl_action(
        '{"hitl_id":"approval-3","action":"leave_unpublished"}'
    ) == ("approval-3", "leave_unpublished", None)
    assert parse_hitl_action(
        '{"hitl_id":"approval-3","action":"not a safe action"}'
    ) is None


@pytest.mark.asyncio
async def test_personal_workflow_update_notification_carries_entity_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=None,
        title="Personal Workflow Chat",
        scope="channel",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="system",
        content="Workflow is running.",
        author_kind="system",
        message_kind="workflow_activity",
    )
    class FakeResult:
        def scalar_one_or_none(self):
            return conversation

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = Session()

        async def execute(self, _statement):
            return FakeResult()

    db_session = FakeAsyncSession()

    published: list[tuple[str, str]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            published.append((channel, payload))

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", fake_get_redis)

    await _notify_update(db_session, message)
    db_session.sync_session.commit()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(published) == 1
    channel, raw_payload = published[0]
    assert channel == "manor:ws_broadcast"
    payload = json.loads(raw_payload)
    assert payload["target"] == "user"
    assert payload["user_id"] == user_id
    assert payload["entity_id"] == entity_id
    assert payload["event"] == "conversation_message"
    db_session.sync_session.close()


@pytest.mark.asyncio
async def test_workspace_run_row_update_announces_where_the_run_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open Workspace Chat already holds the run's row; each step's event
    carries the step statuses so the row moves without reading the chat again."""
    conversation = Conversation(
        id=generate_ulid(), entity_id=generate_ulid(), user_id=None,
        workspace_id=generate_ulid(), title="Workspace Chat", scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(), conversation_id=conversation.id, role="system",
        content="Article: Write is running.", author_kind="system", message_kind="workflow_activity",
        meta={
            "workflow_run_id": "run-1",
            "workflow_status": "running",
            "workflow_current_step_id": "write",
            "workflow_steps": [
                {"id": "research", "name": "Research", "type": "agent", "status": "completed"},
                {"id": "write", "name": "Write", "type": "agent", "status": "running", "subscription_id": "sub-1"},
            ],
        },
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return conversation

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = Session()

        async def execute(self, _statement):
            return FakeResult()

    db_session = FakeAsyncSession()
    published: list[str] = []

    class FakeRedis:
        async def publish(self, _channel: str, payload: str) -> None:
            published.append(payload)

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", fake_get_redis)

    await _notify_update(db_session, message)
    db_session.sync_session.commit()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    payload = json.loads(published[0])
    assert payload["event"] == "workspace_chat_message"
    assert payload["data"]["workflow_progress"] == {
        "workflow_run_id": "run-1",
        "workflow_status": "running",
        "workflow_current_step_id": "write",
        "workflow_steps": [{"id": "research", "status": "completed"}, {"id": "write", "status": "running"}],
    }
    db_session.sync_session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_only", "review", "projected_read_only"),
    [
        (False, "{{topic_brief}}", False),
        (True, "{{topic_brief}}", True),
        # A review assembled from several variables cannot be written back.
        (False, {"brief": "{{topic_brief}}"}, True),
    ],
)
async def test_agent_tool_runner_projects_wait_card_to_personal_chat(
    db_session,
    read_only,
    review,
    projected_read_only,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    activity_id = generate_ulid()
    binding_id = generate_ulid()
    workflow = await create_workflow(
        db_session,
        entity_id=entity_id,
        created_by=user_id,
        name="Agent tool approval projection",
        variables={"topic_brief": {}},
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "name": "Start",
                "config": {},
                "next": ["approve_topic"],
            },
            {
                "id": "approve_topic",
                "type": "wait",
                "name": "Approve Topic Brief",
                "config": {
                    "wait_type": "approval",
                    "message": "Review the Topic Brief.",
                    "options": ["approve", "cancel"],
                    "response_variable": "topic_decision",
                    "review_title": "Review Topic Brief",
                    "review": review,
                    "review_read_only": read_only,
                },
                "next": [],
            },
        ],
    )
    run = await start_workflow(
        db_session,
        entity_id,
        workflow.id,
        variables={"topic_brief": {"title": "A grounded topic"}},
        started_by=user_id,
        binding_id=binding_id,
        trigger_source="mcp",
    )
    db_session.add_all([
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=None,
            title="Personal Workflow Chat",
            scope="channel",
        ),
        Message(
            id=activity_id,
            conversation_id=conversation_id,
            role="system",
            content="Workflow is running.",
            author_kind="system",
            message_kind="workflow_activity",
            meta={
                "workflow_run_id": run.id,
                "workflow_binding_id": binding_id,
                "workflow_title": workflow.name,
                "workflow_status": "queued",
                "workflow_steps": [],
            },
        ),
    ])
    run.trigger_data = {
        **dict(run.trigger_data or {}),
        "_workflow_chat_origin": {
            "enabled": True,
            "conversation_id": conversation_id,
            "activity_message_id": activity_id,
            "projection": {"progress": True, "step_outputs": "explicit"},
            "wait_bridge": True,
        },
    }
    await db_session.commit()

    await WorkflowRunner().run(run.id)
    await db_session.refresh(run)
    assert run.status == "paused", (run.status, run.error, run.step_results)

    approval = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
        )
    )).scalar_one()
    assert approval.message_kind == "hitl_request"
    assert approval.pending_action["workflow_run_id"] == run.id
    assert approval.pending_action["step_id"] == "approve_topic"
    assert approval.pending_action["review"] == (
        {"title": "A grounded topic"} if isinstance(review, str)
        else {"brief": {"title": "A grounded topic"}}
    )
    assert approval.pending_action.get("review_read_only", False) is projected_read_only
    assert approval.meta["hitl_requests"][0]["review_read_only"] is projected_read_only
    assert approval.meta["hitl_requests"][0]["workflow"]["id"] == workflow.id


@pytest.mark.parametrize(
    ("choice", "approved"),
    [("approve", True), ("leave_unpublished", False)],
)
# A review assembled from several variables is read-only; returning it as shown still decides.
@pytest.mark.parametrize("review", ["{{publication_review}}", {"video": "{{publication_review}}"}])
@pytest.mark.asyncio
async def test_variable_backed_video_review_resumes_non_cancel_decisions(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    approved: bool,
    review,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    activity_id = generate_ulid()
    binding_id = generate_ulid()
    publication_review = {
        "review_artifacts": [
            {
                "document_id": "document-1",
                "fs_path": "runs/root/final.mp4",
                "filename": "final.mp4",
            }
        ],
        "title": "A verified Stickman video",
        "description": "Publication description",
        "tags": ["music"],
        "video_change_request": "",
        "visibility": "public",
        "audience": "not_made_for_kids",
        "selected_topic": "A concrete topic",
        "revision_notes": "",
    }
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="test-only",
        role="owner",
        status="active",
    ))
    await db_session.flush()
    workflow = await create_workflow(
        db_session,
        entity_id=entity_id,
        created_by=user_id,
        name="Variable-backed video approval",
        variables={"publication_review": {}},
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "name": "Start",
                "config": {},
                "next": ["approve_video"],
            },
            {
                "id": "approve_video",
                "type": "wait",
                "name": "Approve video",
                "config": {
                    "wait_type": "approval",
                    "message": "Review the exact video.",
                    "options": [
                        "approve",
                        "request_revision",
                        "leave_unpublished",
                        "cancel",
                    ],
                    "approval_values": ["approve"],
                    "response_variable": "video_decision",
                    "review": review,
                    **(
                        {
                            "review_editable_fields": ["title", "description", "tags"],
                            "review_revision_field": "video_change_request",
                        }
                        if isinstance(review, str)
                        else {}
                    ),
                },
                "next": ["done"],
            },
            {"id": "done", "type": "end", "name": "Done"},
        ],
    )
    run = await start_workflow(
        db_session,
        entity_id,
        workflow.id,
        variables={"publication_review": publication_review},
        started_by=user_id,
        binding_id=binding_id,
        trigger_source="mcp",
    )
    db_session.add_all([
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=None,
            title="Video Workflow Chat",
            scope="channel",
        ),
        Message(
            id=activity_id,
            conversation_id=conversation_id,
            role="system",
            content="Workflow is running.",
            author_kind="system",
            message_kind="workflow_activity",
            meta={
                "workflow_run_id": run.id,
                "workflow_binding_id": binding_id,
                "workflow_title": workflow.name,
                "workflow_status": "queued",
                "workflow_steps": [],
            },
        ),
    ])
    run.trigger_data = {
        **dict(run.trigger_data or {}),
        "_workflow_chat_origin": {
            "enabled": True,
            "conversation_id": conversation_id,
            "activity_message_id": activity_id,
            "projection": {"progress": True, "step_outputs": "explicit"},
            "wait_bridge": True,
        },
    }
    await db_session.commit()

    await WorkflowRunner().run(run.id)
    await db_session.refresh(run)
    assert run.status == "paused", (run.status, run.error, run.step_results)
    approval = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
        )
    )).scalar_one()

    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id) or True),
    )
    submitted_review = approval.pending_action["review"]
    if isinstance(review, str):
        submitted_review = {
            **submitted_review,
            "title": "Edited final post",
            "description": "",
            "tags": [],
        }
        assert approval.pending_action["review_editable_fields"] == ["title", "description", "tags"]
        assert approval.meta["hitl_requests"][0]["review_revision_field"] == "video_change_request"
    action_message = json.dumps({
        "hitl_id": approval.id,
        "action": choice,
        "payload": {"review": submitted_review},
    })
    assert await chat_hitl_action_is_pending(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        message=action_message,
    ) is True
    assert await chat_hitl_action_is_pending(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        message=json.dumps({
            "hitl_id": approval.id,
            "action": "undeclared_choice",
        }),
    ) is False
    response, _, _, _ = await resolve_chat_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=action_message,
    )
    assert response and "continuing" in response
    await db_session.commit()
    await db_session.refresh(run)

    assert queued == [run.id]
    assert run.status == "running"
    assert run.variables["video_decision"]["choice"] == choice
    assert run.step_results["approve_video"]["approved"] is approved
    if isinstance(review, str):
        assert run.variables["publication_review"] == submitted_review


@pytest.mark.asyncio
async def test_a_run_started_outside_chat_is_still_in_the_app_queue(db_session) -> None:
    """Regression (jr acceptance 2026-11-04): a Flow started through the API paused
    at its approval with no chat card, so a page without its own list had no way in."""
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_app_sections import _queue_items

    entity_id, user_id, workspace_id = generate_ulid(), generate_ulid(), generate_ulid()
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Music", status="active")
    db_session.add(workspace)
    await db_session.flush()
    workflow = await create_workflow(
        db_session, entity_id=entity_id, created_by=user_id, name="Plan", variables={},
        steps=[
            {"id": "start", "type": "trigger", "name": "Start", "config": {}, "next": ["review"]},
            {"id": "review", "type": "wait", "name": "Review", "next": [],
             "config": {"wait_type": "approval", "message": "Approve the plan?", "options": ["approve", "revise"]}},
        ],
    )
    run = await start_workflow(
        db_session, entity_id, workflow.id, started_by=user_id, trigger_source="manual", workspace_id=workspace_id,
    )
    await db_session.commit()
    await WorkflowRunner().run(run.id)
    await db_session.refresh(run)
    assert run.status == "paused", (run.status, run.error)

    viewer = SimpleNamespace(id=user_id, entity_id=entity_id, preferences={})
    items = await _queue_items(db_session, workspace, user=viewer)
    assert [(item["kind"], item["summary"]) for item in items] == [("workflow_approval", "Approve the plan?")]
    assert items[0]["workflow_run"] == {"workflow_id": workflow.id, "run_id": run.id}

    # A chat-started run decides on its card, which the queue lists on its own.
    run.trigger_data = {**run.trigger_data, "_workflow_chat_origin": {
        "enabled": True, "conversation_id": generate_ulid(), "activity_message_id": generate_ulid(),
    }}
    await db_session.commit()
    assert await _queue_items(db_session, workspace, user=viewer) == []


@pytest.mark.asyncio
async def test_subworkflow_pause_does_not_project_chat_approval(
    db_session,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    activity_id = generate_ulid()
    workflow_id = generate_ulid()
    binding_id = generate_ulid()
    step = {
        "id": "create_video",
        "type": "subworkflow",
        "name": "Create video",
        "config": {"workflow_id": generate_ulid()},
        "next": [],
    }
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow_id,
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        binding_id=binding_id,
        trigger_source="mcp",
        status="paused",
        current_step_id=step["id"],
        variables={},
        step_results={
            step["id"]: {
                "status": "paused",
                "subrun_id": generate_ulid(),
                "output": "Waiting for child workflow",
            }
        },
        trigger_data={
            "_workflow_chat_origin": {
                "enabled": True,
                "conversation_id": conversation_id,
                "activity_message_id": activity_id,
                "projection": {"progress": True},
                "wait_bridge": True,
            },
        },
        definition_snapshot={"name": "parent", "nodes": [step]},
        execution_trace=[],
        started_by=user_id,
    )
    db_session.add_all([
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=run.workspace_id,
            title="Workflow Chat",
            scope="workspace",
        ),
        Message(
            id=activity_id,
            conversation_id=conversation_id,
            role="system",
            content="Workflow is running.",
            author_kind="system",
            message_kind="workflow_activity",
            meta={
                "workflow_run_id": run.id,
                "workflow_binding_id": binding_id,
                "workflow_title": "parent",
                "workflow_status": "running",
                "workflow_steps": [],
            },
        ),
        WorkflowDefinition(
            id=workflow_id,
            entity_id=entity_id,
            workspace_id=run.workspace_id,
            created_by=user_id,
            name="parent",
            trigger_type="manual",
            steps=[step],
            variables={},
            tags=[],
        ),
        run,
    ])
    await db_session.commit()

    await project_workflow_step(
        db_session,
        run=run,
        step=step,
        status="paused",
        result=run.step_results[step["id"]],
    )
    await db_session.commit()

    approvals = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
        )
    )).scalars().all()
    assert approvals == []


@pytest.mark.asyncio
async def test_nested_subworkflow_wait_projects_child_approval_into_root_chat_group(
    db_session,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    workspace_id = generate_ulid()
    conversation_id = generate_ulid()
    activity_id = generate_ulid()
    root_run_id = generate_ulid()
    child_workflow_id = generate_ulid()
    child_step = {
        "id": "approve_video",
        "type": "wait",
        "name": "Approve video",
        "config": {
            "wait_type": "approval",
            "message": "Review the generated video.",
            "options": ["approve", "cancel"],
            "response_variable": "video_decision",
            "review": {"title": "Generated video"},
        },
        "next": [],
    }
    child_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=child_workflow_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        binding_id=None,
        trigger_source="manual",
        status="paused",
        current_step_id=child_step["id"],
        variables={},
        step_results={
            child_step["id"]: {
                "status": "paused",
                "wait_type": "approval",
                "output": "Review the generated video.",
                "review": {"title": "Generated video"},
            }
        },
        trigger_data={
            "parent_run_id": root_run_id,
            "parent_step_id": "publish_video",
            "_workflow_chat_origin": {
                "enabled": True,
                "conversation_id": conversation_id,
                "activity_message_id": activity_id,
                "projection": {"progress": True},
                "wait_bridge": True,
                "host_run_id": root_run_id,
                "nested": True,
            },
        },
        definition_snapshot={"name": "Publication child", "nodes": [child_step]},
        execution_trace=[],
        started_by=user_id,
    )
    root_activity = Message(
        id=activity_id,
        conversation_id=conversation_id,
        role="system",
        content="Root workflow is running.",
        author_kind="system",
        message_kind="workflow_activity",
        meta={
            "workflow_run_id": root_run_id,
            "workflow_binding_id": generate_ulid(),
            "workflow_title": "Root workflow",
            "workflow_status": "paused",
            "workflow_steps": [{"id": "publish_video", "status": "paused"}],
        },
    )
    db_session.add_all([
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            title="Nested Workflow Chat",
            scope="workspace",
        ),
        root_activity,
        WorkflowDefinition(
            id=child_workflow_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            created_by=user_id,
            name="Publication child",
            trigger_type="manual",
            steps=[child_step],
            variables={},
            tags=[],
        ),
        child_run,
    ])
    await db_session.commit()

    await project_workflow_step(
        db_session,
        run=child_run,
        step=child_step,
        status="paused",
        result=child_run.step_results[child_step["id"]],
    )
    await db_session.commit()
    await db_session.refresh(root_activity)

    approval = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
        )
    )).scalar_one()
    assert approval.pending_action["workflow_run_id"] == child_run.id
    assert approval.pending_action["workflow_chat_host_run_id"] == root_run_id
    assert approval.meta["workflow_run_id"] == child_run.id
    assert root_activity.meta["workflow_run_id"] == root_run_id
    assert root_activity.meta["workflow_steps"] == [
        {"id": "publish_video", "status": "paused"}
    ]


@pytest.mark.asyncio
async def test_nested_workflow_chat_cancel_schedules_terminal_propagation(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    root_run_id = generate_ulid()
    child_workflow_id = generate_ulid()
    approval_id = generate_ulid()
    step = {
        "id": "approve_video",
        "type": "wait",
        "config": {
            "wait_type": "approval",
            "options": ["approve", "cancel"],
            "response_variable": "video_decision",
        },
        "next": [],
    }
    child_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=child_workflow_id,
        entity_id=entity_id,
        trigger_source="manual",
        status="paused",
        current_step_id=step["id"],
        variables={},
        step_results={
            step["id"]: {"status": "paused", "wait_type": "approval"}
        },
        trigger_data={
            "parent_run_id": root_run_id,
            "parent_step_id": "publish_video",
            "_workflow_chat_origin": {
                "enabled": True,
                "conversation_id": conversation_id,
                "activity_message_id": generate_ulid(),
                "wait_bridge": True,
                "host_run_id": root_run_id,
                "nested": True,
            },
        },
        definition_snapshot={"name": "Publication child", "nodes": [step]},
        execution_trace=[],
        started_by=user_id,
    )
    db_session.add_all([
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.com",
            password_hash="test-only",
            role="owner",
            status="active",
        ),
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Nested Workflow Chat",
            scope="channel",
        ),
        WorkflowDefinition(
            id=child_workflow_id,
            entity_id=entity_id,
            created_by=user_id,
            name="Publication child",
            trigger_type="manual",
            steps=[step],
            variables={},
            tags=[],
        ),
        child_run,
        Message(
            id=approval_id,
            conversation_id=conversation_id,
            role="system",
            content="Approve publication",
            author_kind="system",
            message_kind="hitl_request",
            pending_action={
                "kind": "workflow_approval",
                "workflow_run_id": child_run.id,
                "workflow_chat_host_run_id": root_run_id,
                "step_id": step["id"],
                "response_variable": "video_decision",
                "options": ["approve", "cancel"],
            },
        ),
    ])
    await db_session.commit()
    queued: list[str] = []
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id, delay_seconds=0: queued.append(run_id) or True),
    )

    response = await resolve_chat_workflow_wait(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=approval_id,
        action="cancel",
    )
    await db_session.commit()
    await db_session.refresh(child_run)

    assert response == "The Workflow was cancelled from its originating Chat."
    assert child_run.status == "cancelled"
    assert child_run.terminal_effects_next_attempt_at is not None
    assert child_run.trigger_data["_workflow_chat_cancel_ancestors"] == {
        "host_run_id": root_run_id,
        "cancelled_by": user_id,
    }
    assert queued == [child_run.id]


@pytest.mark.asyncio
async def test_chat_approval_cannot_resolve_subworkflow_barrier(
    db_session,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    workflow_id = generate_ulid()
    run_id = generate_ulid()
    approval_id = generate_ulid()
    step = {
        "id": "create_video",
        "type": "subworkflow",
        "name": "Create video",
        "config": {"workflow_id": generate_ulid()},
        "next": [],
    }
    workflow = WorkflowDefinition(
        id=workflow_id,
        entity_id=entity_id,
        created_by=user_id,
        name="parent",
        trigger_type="manual",
        steps=[step],
        variables={},
        tags=[],
    )
    run = WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        entity_id=entity_id,
        trigger_source="mcp",
        status="paused",
        current_step_id=step["id"],
        variables={},
        step_results={
            step["id"]: {
                "status": "paused",
                "subrun_id": generate_ulid(),
            }
        },
        trigger_data={
            "_workflow_chat_origin": {
                "enabled": True,
                "conversation_id": conversation_id,
                "activity_message_id": generate_ulid(),
                "projection": {"progress": True},
                "wait_bridge": True,
            },
        },
        definition_snapshot={"name": "parent", "nodes": [step]},
        execution_snapshot=build_execution_snapshot(workflow),
        execution_trace=[],
        started_by=user_id,
    )
    approval = Message(
        id=approval_id,
        conversation_id=conversation_id,
        role="system",
        content="Approve child completion",
        author_kind="system",
        message_kind="hitl_request",
        pending_action={
            "kind": "workflow_approval",
            "workflow_run_id": run_id,
            "step_id": step["id"],
            "response_variable": "create_video_response",
            "options": ["approve", "cancel"],
        },
    )
    db_session.add_all([
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.com",
            password_hash="test-only",
            role="owner",
            status="active",
        ),
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Workflow Chat",
            scope="channel",
        ),
        workflow,
        run,
        approval,
    ])
    await db_session.commit()

    response = await resolve_chat_workflow_wait(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=approval_id,
        action="approve",
    )

    assert response == "This Workflow step is not a human wait."
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert run.status == "paused"
    assert run.step_results[step["id"]]["status"] == "paused"
    assert approval.resolved_at is None


@pytest.mark.asyncio
async def test_personal_chat_approval_uses_execution_snapshot_after_live_config_change(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    run_id = generate_ulid()
    workflow_id = generate_ulid()
    binding_id = generate_ulid()
    activity_id = generate_ulid()
    step = {
        "id": "approve_publish",
        "type": "wait",
        "name": "Approve publication",
        "config": {
            "wait_type": "approval",
            "message": "Review the exact post.",
            "options": ["approve", "cancel"],
            "approval_values": ["approve"],
            "response_variable": "publish_decision",
            "review": "{{publish_packet}}",
        },
        "next": ["done"],
    }
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=None,
        title="Personal Workflow Chat",
        scope="channel",
    )
    user = User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="test-only",
        role="owner",
        status="active",
    )
    activity = Message(
        id=activity_id,
        conversation_id=conversation_id,
        role="system",
        content="Workflow is running.",
        author_kind="system",
        message_kind="workflow_activity",
        meta={
            "workflow_run_id": run_id,
            "workflow_binding_id": binding_id,
            "workflow_title": "Publish post",
            "workflow_status": "running",
            "workflow_steps": [],
        },
    )
    run = WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        entity_id=entity_id,
        binding_id=binding_id,
        trigger_source="mcp",
        status="paused",
        current_step_id=step["id"],
        variables={"publish_packet": {"post_text": "A concrete post to review."}},
        step_results={step["id"]: {"status": "paused", "wait_type": "approval"}},
        trigger_data={
            "_workflow_chat_origin": {
                "enabled": True,
                "conversation_id": conversation_id,
                "activity_message_id": activity_id,
                "projection": {"progress": True},
                "wait_bridge": True,
            },
        },
        definition_snapshot={
            "name": "content-publish-workflow",
            "nodes": [step, {"id": "done", "type": "end", "name": "Done"}],
        },
        execution_trace=[],
        started_by=user_id,
    )
    workflow = WorkflowDefinition(
        id=workflow_id,
        entity_id=entity_id,
        created_by=user_id,
        name="content-publish-workflow",
        trigger_type="manual",
        steps=[step, {"id": "done", "type": "end", "name": "Done"}],
        variables={},
        tags=[],
    )
    run.execution_snapshot = build_execution_snapshot(workflow)
    db_session.add_all([user, conversation, activity, workflow, run])
    await db_session.commit()

    await project_workflow_step(
        db_session,
        run=run,
        step=step,
        status="paused",
        result={
            "status": "paused",
            "wait_type": "approval",
            "output": "Review the exact post.",
            "review_title": "Review the exact LinkedIn post",
            "review": {"post_text": "A concrete post to review."},
        },
    )
    await db_session.commit()

    approval = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
        )
    )).scalar_one()
    assert approval.pending_action["workflow_run_id"] == run_id
    assert approval.meta["hitl_requests"][0]["id"] == approval.id
    assert approval.meta["hitl_requests"][0]["options"] == [
        "approve",
        "cancel",
    ]
    request = approval.meta["hitl_requests"][0]
    assert request["review"] == {"post_text": "A concrete post to review."}
    assert request["review_title"] == "Review the exact LinkedIn post"
    assert request["workflow"] == {
        "id": workflow_id,
        "name": "content-publish-workflow",
        "run_id": run_id,
        "url": f"/flows/{workflow_id}/runs/{run_id}",
    }
    assert request["node"] == {
        "id": "approve_publish",
        "name": "Approve publication",
        "type": "wait",
    }

    changed_steps = deepcopy(workflow.steps)
    changed_steps[0]["config"]["review"] = "{{changed_packet}}"
    workflow.steps = changed_steps
    await db_session.commit()

    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda queued_run_id, *args, **kwargs: queued.append(queued_run_id) or True),
    )
    action_message = (
        '{"hitl_id":"' + approval.id + '","action":"approve",'
        '"payload":{"review":{"post_text":"The edited post that will publish."}}}'
    )
    assert await chat_hitl_action_is_pending(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        message=action_message,
    ) is True
    response, saved_text, save_user, runtime_metadata = await resolve_chat_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=action_message,
    )
    assert response and "continuing" in response
    assert saved_text == "Approved the requested action."
    assert save_user is True
    assert runtime_metadata == {"approval_kind": "workflow"}
    await db_session.commit()
    await db_session.refresh(run)
    await db_session.refresh(approval)

    assert queued == [run_id]
    assert run.status == "running"
    assert run.variables["publish_decision"]["choice"] == "approve"
    assert run.variables["publish_decision"]["review_edited"] is True
    assert run.variables["publish_packet"] == {
        "post_text": "The edited post that will publish.",
    }
    assert run.step_results[step["id"]]["review"] == {
        "post_text": "The edited post that will publish.",
    }
    assert run.step_results[step["id"]]["approved"] is True
    assert approval.resolved_at is not None
    assert approval.resolution["payload"]["review"] == {
        "post_text": "The edited post that will publish.",
    }
    assert approval.meta["hitl_requests"][0]["resolved"] is True
    redundant_receipts = (await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.message_kind == "system",
            Message.content == "✓ Approved",
        )
    )).scalars().all()
    assert redundant_receipts == []
