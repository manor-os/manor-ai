from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from tests.approval_settings_test_utils import initialize_test_approval_owners

from packages.core.ai.runtime.approval_service import (
    runtime_auto_confirm_provider_approval,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace


async def _initialize_approval_scope(db, *, entity_id, workspace_id, user_id):
    workspace = Workspace(id=workspace_id, entity_id=entity_id,
                          name="Proposal publication scope", status="active")
    db.add_all([
        workspace,
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
             password_hash="fixture", role="owner", status="active"),
        UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
    ])
    await db.flush()
    await initialize_test_approval_owners(db, workspace, user_id=user_id)


def _provider_block(*, approval_id: str, target_label: str) -> str:
    url = "https://studio.youtube.com/channel/demo/videos/upload"
    return json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": approval_id,
            "confirmation_mode": "always_action_time",
            "policy_category": "representational_communication",
            "target_label": target_label,
            "target_role": "button",
            "url": url,
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": {
                    "url": url,
                    "label": target_label,
                    "role": "button",
                    "ref": f"{approval_id}-button",
                },
            },
        }
    )


def _provider_upload_block(*, approval_id: str) -> str:
    url = "https://studio.youtube.com/channel/demo/videos/upload"
    return json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": approval_id,
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "target_label": "Filedata",
            "target_role": "button",
            "url": url,
            "retry_action": {
                "name": "mcp__chrome__upload",
                "arguments": {
                    "url": url,
                    "files": ["/prepared/daily-stickman-video.mp4"],
                    "label": "Filedata",
                    "role": "button",
                    "ref": f"{approval_id}-file",
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_proposal_authorization_covers_upload_entry_then_one_public_publish(
    db_session,
    monkeypatch,
):
    from packages.core import database

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    predecessor_id = generate_ulid()
    publish_task_id = generate_ulid()
    predecessor = Task(
        id=predecessor_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Render verified MP4",
        status="completed",
        actual_output={"files": [{"name": "daily-stickman-video.mp4"}]},
    )
    authorization = {
        "version": 1,
        "authorization_id": f"proposal-item-lifecycle:{publish_task_id}",
        "workspace_id": predecessor.workspace_id,
        "review_id": "review-lifecycle",
        "proposal_id": "proposal-lifecycle",
        "proposal_item_id": "proposal-item-lifecycle",
        "task_id": publish_task_id,
        "predecessor_task_id": predecessor.id,
        "provider": "youtube",
        "action": "publish_video",
        "destination": "studio.youtube.com",
        "visibility": "public",
        "intended_channel": "paired_chrome_signed_in_channel",
        "max_executions": 1,
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "approved_by": "operator-lifecycle",
        "consumed_at": None,
    }
    publish_task = Task(
        id=publish_task_id,
        entity_id=predecessor.entity_id,
        workspace_id=predecessor.workspace_id,
        title="Publish verified MP4",
        status="in_progress",
        details={"proposal_external_authorization": authorization},
    )
    db_session.add_all([predecessor, publish_task])
    await _initialize_approval_scope(db_session, entity_id=entity_id,
        workspace_id=workspace_id, user_id="operator-lifecycle")
    await db_session.commit()

    calls: list[tuple[str, dict]] = []

    async def execute(name: str, args: dict) -> str:
        calls.append((name, args))
        if name == "mcp__chrome__confirm_action":
            return json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "approvalId": args["approvalId"],
                    "approvalToken": f"token-{args['approvalId']}",
                }
            )
        return json.dumps(
            {
                "ok": True,
                "post_action_page_state": {"state_verified": True},
                "status": (
                    "published" if args.get("label") == "Publish" else "upload_workflow_opened"
                ),
            }
        )

    runtime_metadata = {
        "proposal_external_authorization": dict(authorization),
    }
    upload_entry = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__click_element",
        arguments={"label": "Upload videos"},
        result=_provider_block(
            approval_id="chrome-upload-entry",
            target_label="Upload videos",
        ),
        execute=execute,
        entity_id=predecessor.entity_id,
        user_id="operator-lifecycle",
        workspace_id=predecessor.workspace_id,
        task_id=publish_task.id,
        runtime_metadata=runtime_metadata,
    )

    assert json.loads(upload_entry)["status"] == "upload_workflow_opened"
    await db_session.refresh(publish_task)
    started = publish_task.details["proposal_external_authorization"]
    assert started["started_at"]
    assert started["execution_count"] == 1
    assert started["consumed_at"] is None

    uploaded = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__upload",
        arguments={
            "files": ["/prepared/daily-stickman-video.mp4"],
            "label": "Filedata",
        },
        result=_provider_upload_block(approval_id="chrome-file-upload"),
        execute=execute,
        entity_id=predecessor.entity_id,
        user_id="operator-lifecycle",
        workspace_id=predecessor.workspace_id,
        task_id=publish_task.id,
        runtime_metadata=runtime_metadata,
    )

    assert json.loads(uploaded)["status"] == "upload_workflow_opened"
    await db_session.refresh(publish_task)
    transferred = publish_task.details["proposal_external_authorization"]
    assert transferred["execution_count"] == 1
    assert transferred["consumed_at"] is None
    assert transferred["upload_transfer_provider_approval_id"] == "chrome-file-upload"

    published = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__click_element",
        arguments={"label": "Publish"},
        result=_provider_block(
            approval_id="chrome-final-publish",
            target_label="Publish",
        ),
        execute=execute,
        entity_id=predecessor.entity_id,
        user_id="operator-lifecycle",
        workspace_id=predecessor.workspace_id,
        task_id=publish_task.id,
        runtime_metadata=runtime_metadata,
    )

    assert json.loads(published)["status"] == "published"
    await db_session.refresh(publish_task)
    consumed = publish_task.details["proposal_external_authorization"]
    assert consumed["consumed_at"]
    assert consumed["execution_count"] == 1
    assert [name for name, _args in calls] == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
        "mcp__chrome__confirm_action",
        "mcp__chrome__upload",
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    repeated_publish = _provider_block(approval_id="chrome-second-publish", target_label="Publish")
    assert await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__click_element", arguments={"label": "Publish"},
        result=repeated_publish, execute=execute, entity_id=entity_id,
        user_id="operator-lifecycle", workspace_id=workspace_id,
        task_id=publish_task.id, runtime_metadata=runtime_metadata,
    ) == repeated_publish
    assert len(calls) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["missing_token", "retry_not_executed"])
async def test_upload_entry_is_not_recorded_until_chrome_executes_action(
    db_session,
    monkeypatch,
    failure_mode,
):
    from packages.core import database

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    predecessor = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Render verified MP4",
        status="completed",
        actual_output={"files": [{"name": "daily-stickman-video.mp4"}]},
    )
    publish_task_id = generate_ulid()
    user_id = generate_ulid()
    authorization = {
        "version": 1,
        "authorization_id": f"proposal-item-failure:{publish_task_id}",
        "workspace_id": workspace_id,
        "review_id": "review-failure",
        "proposal_id": "proposal-failure",
        "proposal_item_id": "proposal-item-failure",
        "task_id": publish_task_id,
        "predecessor_task_id": predecessor.id,
        "provider": "youtube",
        "action": "publish_video",
        "destination": "studio.youtube.com",
        "visibility": "public",
        "intended_channel": "paired_chrome_signed_in_channel",
        "max_executions": 1,
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "approved_by": user_id,
        "consumed_at": None,
    }
    publish_task = Task(
        id=publish_task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Publish verified MP4",
        status="in_progress",
        details={"proposal_external_authorization": authorization},
    )
    db_session.add_all([predecessor, publish_task])
    await _initialize_approval_scope(db_session, entity_id=entity_id,
        workspace_id=workspace_id, user_id=user_id)
    await db_session.commit()

    calls: list[str] = []

    async def execute(name: str, args: dict) -> str:
        calls.append(name)
        if name == "mcp__chrome__confirm_action":
            confirmation = {
                "ok": True,
                "status": "approved",
                "approvalId": args["approvalId"],
            }
            if failure_mode != "missing_token":
                confirmation["approvalToken"] = f"token-{args['approvalId']}"
            return json.dumps(confirmation)
        return _provider_block(
            approval_id="chrome-upload-entry-retry",
            target_label="Upload videos",
        )

    original = _provider_block(
        approval_id="chrome-upload-entry",
        target_label="Upload videos",
    )
    result = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__click_element",
        arguments={"label": "Upload videos"},
        result=original,
        execute=execute,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        task_id=publish_task_id,
        runtime_metadata={
            "proposal_external_authorization": dict(authorization),
        },
    )

    assert json.loads(result)["status"] == "approval_required"
    assert calls == (["mcp__chrome__confirm_action"] if failure_mode == "missing_token" else
                     ["mcp__chrome__confirm_action", "mcp__chrome__click_element"])
    await db_session.refresh(publish_task)
    stored = publish_task.details["proposal_external_authorization"]
    assert stored.get("started_at") is None
    assert int(stored.get("execution_count") or 0) == 0
    assert stored.get("upload_entry_provider_approval_id") is None
