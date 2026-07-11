"""E2E tests: conversation management and document download."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Conversation, Message
from packages.core.models.worker import CredentialSublease, WorkLease, WorkerActivityLog
from packages.core.services.conversation_lifecycle import delete_conversation


async def _auth(client: AsyncClient, username: str = "convuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_public_webchat_stream_update_preserves_session_metadata(db_session: AsyncSession):
    from packages.core.services.channel_conversations import list_public_webchat_messages
    from packages.core.services.conversation_messages import (
        create_assistant_stream_placeholder,
        save_or_update_assistant_stream_message,
    )

    entity_id = generate_ulid()
    conversation_id = generate_ulid()
    session_id = "public-session-1"
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            channel="webchat",
            title="webchat: stream visitor",
            meta={
                "channel_config_id": generate_ulid(),
                "sender_id": session_id,
                "chat_id": session_id,
                "session_id": session_id,
            },
        )
    )
    await db_session.commit()

    placeholder = await create_assistant_stream_placeholder(
        db_session,
        conversation_id,
        entity_id=entity_id,
        workspace_id=None,
        agent_id=None,
        meta={
            "channel_type": "webchat",
            "sender_id": session_id,
            "chat_id": session_id,
            "session_id": session_id,
        },
    )
    await db_session.commit()

    saved_id = await save_or_update_assistant_stream_message(
        conversation_id=conversation_id,
        entity_id=entity_id,
        workspace_id=None,
        agent_id=None,
        message_id=placeholder.id,
        content="Final streamed public reply.",
        meta={"runtime": {"surface": "public_customer_chat"}},
    )

    assert saved_id == placeholder.id
    db_session.expire_all()
    messages = await list_public_webchat_messages(
        db_session,
        conversation_id,
        session_id=session_id,
    )
    assert [m["content"] for m in messages] == ["Final streamed public reply."]


@pytest.mark.asyncio
async def test_rename_conversation(client: AsyncClient):
    headers = await _auth(client)

    # Create a conversation via the chat endpoint
    resp = await client.post(
        "/api/v1/chat/message",
        headers=headers,
        json={
            "message": "Hello",
        },
    )
    conv_id = resp.json()["conversation_id"]

    # List conversations to confirm it exists
    convs = await client.get("/api/v1/chat/conversations", headers=headers)
    assert any(c["id"] == conv_id for c in convs.json())

    # Rename it
    rename_resp = await client.put(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers,
        json={"title": "My Important Chat"},
    )
    assert rename_resp.status_code == 200
    assert rename_resp.json()["title"] == "My Important Chat"

    # Verify via list
    convs2 = await client.get("/api/v1/chat/conversations", headers=headers)
    conv = next(c for c in convs2.json() if c["id"] == conv_id)
    assert conv["title"] == "My Important Chat"


@pytest.mark.asyncio
async def test_delete_conversation(client: AsyncClient):
    headers = await _auth(client)

    # Create a conversation
    resp = await client.post(
        "/api/v1/chat/message",
        headers=headers,
        json={
            "message": "Delete me",
        },
    )
    conv_id = resp.json()["conversation_id"]

    # Delete it
    del_resp = await client.delete(f"/api/v1/chat/conversations/{conv_id}", headers=headers)
    assert del_resp.status_code == 204

    # Verify it's gone
    convs = await client.get("/api/v1/chat/conversations", headers=headers)
    assert all(c["id"] != conv_id for c in convs.json())

    # Messages should also be gone
    msgs = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers)
    assert msgs.status_code == 404


@pytest.mark.asyncio
async def test_delete_conversation_purges_cli_worker_execution_rows(db_session: AsyncSession):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    upload_plan_id = generate_ulid()
    upload_step_id = generate_ulid()
    upload_lease_id = generate_ulid()
    report_plan_id = generate_ulid()
    report_step_id = generate_ulid()
    report_lease_id = generate_ulid()
    message_id = generate_ulid()
    share_id = generate_ulid()
    credential_sublease_id = generate_ulid()
    kept_plan_id = generate_ulid()
    kept_step_id = generate_ulid()

    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                title="Local coding chat",
                channel="web",
                meta={},
            ),
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="local coding card",
            ),
            ConversationShare(
                id=share_id,
                conversation_id=conversation_id,
                entity_id=entity_id,
                shared_by=user_id,
                share_token=f"test-share-{conversation_id}",
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="completed",
                plan_dag={"source": "cli_worker"},
                dispatcher_state={},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="local_coding",
                kind="code",
                provider="codex_cli",
                action_key="code.run",
                params={"conversation_id": conversation_id, "tool": "codex_cli"},
                step_status="done",
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                worker_id=generate_ulid(),
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="completed",
            ),
            ExecutionPlan(
                id=upload_plan_id,
                entity_id=entity_id,
                status="completed",
                plan_dag={"source": "local_worker"},
                dispatcher_state={},
            ),
            ExecutionStep(
                id=upload_step_id,
                plan_id=upload_plan_id,
                entity_id=entity_id,
                step_key="local_upload_prepare_assets",
                kind="action",
                provider="custom.browser_upload",
                action_key="prepare_upload",
                params={"conversation_id": conversation_id, "artifact_dir": "Uploads/demo/assets"},
                step_status="done",
            ),
            WorkLease(
                id=upload_lease_id,
                step_id=upload_step_id,
                plan_id=upload_plan_id,
                entity_id=entity_id,
                worker_id=generate_ulid(),
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="completed",
            ),
            ExecutionPlan(
                id=report_plan_id,
                entity_id=entity_id,
                status="completed",
                plan_dag={"source": "local_worker"},
                dispatcher_state={},
            ),
            ExecutionStep(
                id=report_step_id,
                plan_id=report_plan_id,
                entity_id=entity_id,
                step_key="local_report_export",
                kind="action",
                provider="custom.report_export",
                action_key="export_report",
                params={"conversation_id": conversation_id, "artifact_dir": "Uploads/demo/reports"},
                step_status="done",
            ),
            WorkLease(
                id=report_lease_id,
                step_id=report_step_id,
                plan_id=report_plan_id,
                entity_id=entity_id,
                worker_id=generate_ulid(),
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="completed",
            ),
            WorkerActivityLog(
                worker_id=generate_ulid(),
                event="completed",
                lease_id=lease_id,
                payload_summary={"step_id": step_id},
            ),
            CredentialSublease(
                id=credential_sublease_id,
                work_lease_id=lease_id,
                integration_id=generate_ulid(),
                vault_lease_id="vault-test",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ),
            ExecutionPlan(
                id=kept_plan_id,
                entity_id=entity_id,
                task_id=generate_ulid(),
                status="completed",
                plan_dag={"source": "cli_worker"},
                dispatcher_state={},
            ),
            ExecutionStep(
                id=kept_step_id,
                plan_id=kept_plan_id,
                entity_id=entity_id,
                step_key="task_bound_code",
                kind="code",
                provider="codex_cli",
                action_key="code.run",
                params={"conversation_id": conversation_id, "tool": "codex_cli"},
                step_status="done",
            ),
        ]
    )
    await db_session.commit()

    assert await delete_conversation(db_session, conversation_id, entity_id) is True
    await db_session.commit()
    db_session.expire_all()

    assert await db_session.get(Conversation, conversation_id) is None
    assert await db_session.get(Message, message_id) is None
    assert await db_session.get(ConversationShare, share_id) is None
    assert await db_session.get(ExecutionPlan, plan_id) is None
    assert await db_session.get(ExecutionStep, step_id) is None
    assert await db_session.get(WorkLease, lease_id) is None
    assert await db_session.get(ExecutionPlan, upload_plan_id) is None
    assert await db_session.get(ExecutionStep, upload_step_id) is None
    assert await db_session.get(WorkLease, upload_lease_id) is None
    assert await db_session.get(ExecutionPlan, report_plan_id) is None
    assert await db_session.get(ExecutionStep, report_step_id) is None
    assert await db_session.get(WorkLease, report_lease_id) is None
    assert await db_session.get(CredentialSublease, credential_sublease_id) is None
    activity_logs = (
        (await db_session.execute(select(WorkerActivityLog).where(WorkerActivityLog.lease_id == lease_id)))
        .scalars()
        .all()
    )
    assert activity_logs == []
    assert await db_session.get(ExecutionPlan, kept_plan_id) is not None
    assert await db_session.get(ExecutionStep, kept_step_id) is not None


@pytest.mark.asyncio
async def test_download_document(client: AsyncClient):
    headers = await _auth(client)

    # Enable FS for this test
    from packages.core.config import get_settings

    settings = get_settings()
    original_enabled = settings.MANOR_FS_ENABLED
    original_root = settings.MANOR_FS_ROOT

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        settings.MANOR_FS_ENABLED = True
        settings.MANOR_FS_ROOT = tmpdir

        try:
            content = b"Hello, this is test file content for download."
            upload_resp = await client.post(
                "/api/v1/documents/upload",
                headers=headers,
                files={"file": ("download_test.txt", content, "text/plain")},
            )
            assert upload_resp.status_code == 201
            doc_id = upload_resp.json()["id"]

            # Download the file
            dl_resp = await client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
            assert dl_resp.status_code == 200
            assert dl_resp.content == content
            assert "download_test.txt" in dl_resp.headers.get("content-disposition", "")
        finally:
            settings.MANOR_FS_ENABLED = original_enabled
            settings.MANOR_FS_ROOT = original_root
