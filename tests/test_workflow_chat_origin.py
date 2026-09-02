from __future__ import annotations

import asyncio
from copy import deepcopy
import json

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
    project_workflow_step,
)
from packages.core.services.workflow_run_trace import build_execution_snapshot
from packages.core.services.workflow_service import create_workflow, start_workflow


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
async def test_agent_tool_runner_projects_wait_card_to_personal_chat(
    db_session,
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
                    "review": "{{topic_brief}}",
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
    assert approval.pending_action["review"] == {"title": "A grounded topic"}
    assert approval.meta["hitl_requests"][0]["workflow"]["id"] == workflow.id


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
        "url": f"/flows?workflow={workflow_id}",
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
