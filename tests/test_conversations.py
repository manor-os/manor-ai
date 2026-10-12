"""E2E tests: conversation management and document download."""

from datetime import datetime, timedelta, timezone
from inspect import getsource

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.conversation import AiEditTargetKind, ConversationSurfaceKind
from packages.core.models.base import generate_ulid
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Agent, Workspace
from packages.core.models.worker import CredentialSublease, WorkLease, WorkerActivityLog
from packages.core.services.conversation_lifecycle import (
    cleanup_expired_ai_edit_conversations,
    delete_conversation,
    get_or_create_conversation,
)
from packages.core.services.conversation_records import list_conversations
from packages.core.services.conversation_surfaces import (
    ConversationSurfaceMetadataFactory,
)


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


def test_conversation_surface_factory_owns_reserved_metadata() -> None:
    target = ConversationSurfaceMetadataFactory.ai_edit_target(
        AiEditTargetKind.DOCUMENT,
        "document-1",
    )
    ai_edit = ConversationSurfaceMetadataFactory.build(
        ConversationSurfaceKind.AI_EDIT,
        extra={"surface": ConversationSurfaceKind.DASHBOARD_MODULE.value},
        ai_edit_target=target,
    )
    ordinary = ConversationSurfaceMetadataFactory.build(
        ConversationSurfaceKind.ORDINARY_CHAT,
        current={"surface": ConversationSurfaceKind.AI_EDIT.value},
        extra={"surface": ConversationSurfaceKind.DASHBOARD_MODULE.value},
    )

    assert ai_edit["surface"] == ConversationSurfaceKind.AI_EDIT.value
    assert ai_edit["ai_edit_target"] == {
        "kind": AiEditTargetKind.DOCUMENT.value,
        "id": "document-1",
    }
    assert "surface" not in ordinary
    assert ConversationSurfaceMetadataFactory.is_deletable_session(ordinary)
    assert ConversationSurfaceMetadataFactory.is_deletable_session(ai_edit)
    assert not ConversationSurfaceMetadataFactory.is_deletable_session(
        ConversationSurfaceMetadataFactory.build(
            ConversationSurfaceKind.DASHBOARD_MODULE,
        )
    )


def test_conversation_delete_uses_runtime_cancellation_lock_order() -> None:
    source = getsource(delete_conversation)
    root_run_query = source[source.index("root_run_ids ="):source.index("if root_run_ids:")]

    assert "select(RuntimeRun.id)" in root_run_query
    assert "with_for_update" not in root_run_query


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
            "origin_user_message_id": "request-1",
        },
    )
    await db_session.commit()
    placeholder_id = placeholder.id

    saved_id = await save_or_update_assistant_stream_message(
        conversation_id=conversation_id,
        entity_id=entity_id,
        workspace_id=None,
        agent_id=None,
        message_id=placeholder_id,
        content="Final streamed public reply.",
        meta={"runtime": {"surface": "public_customer_chat"}},
    )

    assert saved_id == placeholder_id
    db_session.expire_all()
    persisted = await db_session.get(Message, placeholder_id)
    assert persisted is not None
    assert persisted.meta["origin_user_message_id"] == "request-1"
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
async def test_agent_draft_reuses_personal_web_conversation_and_renames_it(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "agent_draft_owner")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    agent = (await client.post(
        "/api/v1/agents", headers=headers, json={"name": "Draft Agent"},
    )).json()
    db_session.add(Conversation(
        entity_id=me["entity_id"], user_id=me["id"], agent_id=agent["id"],
        channel="webchat", title="External channel history",
    ))
    await db_session.commit()

    first = await client.post(
        "/api/v1/chat/conversations/agent-draft",
        headers=headers,
        json={"agent_id": agent["id"], "title": "First title"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["title"] == "First title"
    assert first.json()["user_id"] == me["id"]
    assert first.json()["agent_id"] == agent["id"]
    assert first.json()["workspace_id"] is None
    assert first.json()["channel"] == "web"
    assert first.json()["message_count"] == 0

    again = await client.post(
        "/api/v1/chat/conversations/agent-draft",
        headers=headers,
        json={"agent_id": agent["id"], "title": "Renamed draft"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["title"] == "Renamed draft"
    listing = (await client.get("/api/v1/chat/conversations", headers=headers)).json()
    assert len([row for row in listing if row["agent_id"] == agent["id"] and row["channel"] == "web"]) == 1
    assert next(row for row in listing if row["id"] == first.json()["id"])["title"] == "Renamed draft"
    assert (await db_session.execute(select(Message).where(
        Message.conversation_id == first.json()["id"],
    ))).scalars().first() is None

    workspace_attempt = await client.post(
        "/api/v1/chat/conversations/agent-draft",
        headers=headers,
        json={"agent_id": agent["id"], "workspace_id": generate_ulid()},
    )
    assert workspace_attempt.status_code == 422


@pytest.mark.asyncio
async def test_agent_draft_rejects_agents_not_visible_in_personal_scope(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.user import User, UserMembership
    from packages.core.services.auth_service import create_access_token, hash_password

    owner_headers = await _auth(client, "agent_draft_admin")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    entity_id = owner["entity_id"]
    member = User(
        id=generate_ulid(), entity_id=entity_id, email="agentdraftmember@test.com",
        password_hash=hash_password("pass123"), role="member", status="active",
    )
    hidden_workspace = Workspace(
        entity_id=entity_id, name="Hidden Agent Home",
        settings={"access_mode": "members_only"},
    )
    db_session.add_all([member, UserMembership(
        user_id=member.id, entity_id=entity_id, role="member",
        status="active", is_primary=True,
    ), hidden_workspace])
    await db_session.flush()
    visible = Agent(entity_id=entity_id, name="Visible", visibility="entity")
    template = Agent(entity_id=None, name="Template", is_template=True, is_public=True)
    hidden = [
        Agent(entity_id=entity_id, name="Private", visibility="private", owner_user_id=owner["id"]),
        Agent(entity_id=entity_id, name="Workspace", visibility="workspace", workspace_id=hidden_workspace.id),
        Agent(entity_id=generate_ulid(), name="Other entity", is_public=True),
        Agent(entity_id=entity_id, name="Inactive", status="inactive"),
        Agent(entity_id=entity_id, name="Deleted", deleted_at=datetime.now(timezone.utc)),
        Agent(entity_id=None, name="Unpublished template", is_template=True),
    ]
    db_session.add_all([visible, template, *hidden])
    await db_session.commit()
    member_headers = {
        "Authorization": f"Bearer {create_access_token(member.id, entity_id, 'member')}"
    }

    for agent in (visible, template):
        response = await client.post(
            "/api/v1/chat/conversations/agent-draft",
            headers=member_headers, json={"agent_id": agent.id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["workspace_id"] is None

    for agent in hidden:
        response = await client.post(
            "/api/v1/chat/conversations/agent-draft",
            headers=member_headers, json={"agent_id": agent.id},
        )
        assert response.status_code == 404, (agent.name, response.text)

    rows = (await db_session.execute(select(Conversation).where(
        Conversation.user_id == member.id,
    ))).scalars().all()
    assert {row.agent_id for row in rows} == {visible.id, template.id}


@pytest.mark.asyncio
async def test_ai_edit_conversation_is_hidden_and_cannot_become_ordinary_chat(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    target = ConversationSurfaceMetadataFactory.ai_edit_target(
        AiEditTargetKind.DOCUMENT,
        "document-1",
    )
    conversation = await get_or_create_conversation(
        db_session,
        entity_id,
        user_id,
        title="Temporary AI Edit",
        conversation_surface=ConversationSurfaceKind.AI_EDIT,
        ai_edit_target=target,
    )
    await db_session.commit()

    assert conversation.meta["surface"] == ConversationSurfaceKind.AI_EDIT.value
    assert conversation.id not in {
        row.id for row in await list_conversations(db_session, entity_id, user_id)
    }
    with pytest.raises(PermissionError, match="Conversation not found"):
        await get_or_create_conversation(
            db_session,
            entity_id,
            user_id,
            conversation_id=conversation.id,
            conversation_surface=ConversationSurfaceKind.ORDINARY_CHAT,
        )

    resumed = await get_or_create_conversation(
        db_session,
        entity_id,
        user_id,
        conversation_id=conversation.id,
        conversation_surface=ConversationSurfaceKind.AI_EDIT,
        ai_edit_target=target,
    )
    assert resumed.id == conversation.id

    with pytest.raises(PermissionError, match="Conversation not found"):
        await get_or_create_conversation(
            db_session,
            entity_id,
            user_id,
            conversation_id=conversation.id,
            conversation_surface=ConversationSurfaceKind.AI_EDIT,
            ai_edit_target=ConversationSurfaceMetadataFactory.ai_edit_target(
                AiEditTargetKind.DOCUMENT,
                "document-2",
            ),
        )


@pytest.mark.asyncio
async def test_chat_stream_rejects_conversation_surface_mismatch_before_runtime(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "ai_edit_surface_scope")
    user = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ordinary_id = generate_ulid()
    ai_edit_id = generate_ulid()
    dashboard_id = generate_ulid()
    db_session.add_all([
        Conversation(
            id=ordinary_id,
            entity_id=user["entity_id"],
            user_id=user["id"],
            title="Ordinary chat",
        ),
        Conversation(
            id=ai_edit_id,
            entity_id=user["entity_id"],
            user_id=user["id"],
            title="AI Edit",
            meta=ConversationSurfaceMetadataFactory.build(
                ConversationSurfaceKind.AI_EDIT,
                ai_edit_target=ConversationSurfaceMetadataFactory.ai_edit_target(
                    AiEditTargetKind.DOCUMENT,
                    "document-1",
                ),
            ),
        ),
        Conversation(
            id=dashboard_id,
            entity_id=user["entity_id"],
            user_id=user["id"],
            title="Dashboard module",
            meta=ConversationSurfaceMetadataFactory.build(
                ConversationSurfaceKind.DASHBOARD_MODULE,
            ),
        ),
    ])
    await db_session.commit()

    ai_edit_on_chat = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={"message": "continue", "conversation_id": ai_edit_id},
    )
    assert ai_edit_on_chat.status_code == 404

    chat_on_ai_edit = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "continue",
            "conversation_id": ordinary_id,
            "conversation_surface": ConversationSurfaceKind.AI_EDIT.value,
            "editor_context": '{"target_kind":"document","target_id":"document-1"}',
        },
    )
    assert chat_on_ai_edit.status_code == 404

    voice_on_ai_edit = await client.post(
        "/api/v1/chat/voice-session",
        headers=headers,
        json={"conversation_id": ai_edit_id},
    )
    assert voice_on_ai_edit.status_code == 404

    voice_save_on_ai_edit = await client.post(
        "/api/v1/chat/voice-save",
        headers=headers,
        json={
            "conversation_id": ai_edit_id,
            "turns": [{"role": "user", "content": "ordinary voice turn"}],
        },
    )
    assert voice_save_on_ai_edit.status_code == 404

    invalid_target = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "continue",
            "conversation_surface": ConversationSurfaceKind.AI_EDIT.value,
            "editor_context": "{}",
        },
    )
    assert invalid_target.status_code == 422

    for method, path, payload in (
        ("put", f"/api/v1/chat/conversations/{ai_edit_id}", {"title": "rename"}),
        ("get", f"/api/v1/chat/conversations/{ai_edit_id}/export", None),
        ("post", f"/api/v1/chat/conversations/{ai_edit_id}/share", {}),
    ):
        response = await client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 404

    dashboard_delete = await client.delete(
        f"/api/v1/chat/conversations/{dashboard_id}",
        headers=headers,
    )
    assert dashboard_delete.status_code == 404
    assert await db_session.get(Conversation, dashboard_id) is not None

    ai_edit_delete = await client.delete(
        f"/api/v1/chat/conversations/{ai_edit_id}",
        headers=headers,
    )
    assert ai_edit_delete.status_code == 204
    db_session.expire_all()
    assert await db_session.get(Conversation, ai_edit_id) is None


@pytest.mark.asyncio
async def test_expired_ai_edit_cleanup_uses_conversation_lifecycle(
    db_session: AsyncSession,
):
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    user_id = generate_ulid()
    expired_id = generate_ulid()
    active_id = generate_ulid()
    dashboard_id = generate_ulid()
    expired_at = now - timedelta(hours=25)
    expired_run_id = generate_ulid()
    db_session.add_all([
        Conversation(
            id=expired_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Expired edit",
            meta={"surface": ConversationSurfaceKind.AI_EDIT.value},
            created_at=expired_at,
            updated_at=expired_at,
        ),
        Conversation(
            id=active_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Active edit",
            meta={"surface": ConversationSurfaceKind.AI_EDIT.value},
            created_at=now,
            updated_at=now,
        ),
        Conversation(
            id=dashboard_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Old dashboard module",
            meta={"surface": ConversationSurfaceKind.DASHBOARD_MODULE.value},
            created_at=expired_at,
            updated_at=expired_at,
        ),
        Message(
            id=generate_ulid(),
            conversation_id=expired_id,
            role="user",
            content="temporary context",
        ),
        RuntimeRun(
            id=expired_run_id,
            root_run_id=expired_run_id,
            conversation_id=expired_id,
            entity_id=entity_id,
            user_id=user_id,
            status=RuntimeRunStatus.QUEUED.value,
            execution_payload={"editor_context": {"current_document_content": "secret draft"}},
            checkpoint={"message": "secret checkpoint"},
            result={"content": "secret result"},
            error={"detail": "secret error"},
        ),
    ])
    await db_session.commit()

    cancelled_runs: list[RuntimeRun] = []
    assert await cleanup_expired_ai_edit_conversations(
        db_session,
        now=now,
        cancelled_runtime_runs=cancelled_runs,
    ) == 1
    await db_session.commit()

    assert await db_session.get(Conversation, expired_id) is None
    assert await db_session.get(Conversation, active_id) is not None
    assert await db_session.get(Conversation, dashboard_id) is not None
    cancelled_run = await db_session.get(RuntimeRun, expired_run_id)
    assert cancelled_run is not None
    assert cancelled_run.status == RuntimeRunStatus.CANCELLED.value
    assert cancelled_run.status_reason == "conversation_deleted"
    assert cancelled_run.execution_payload == {
        "redacted": True,
        "reason": "ai_edit_conversation_deleted",
    }
    assert cancelled_run.checkpoint is None
    assert cancelled_run.result is None
    assert cancelled_run.error is None
    assert cancelled_run.assistant_message_id is None
    assert [run.id for run in cancelled_runs] == [expired_run_id]


@pytest.mark.asyncio
async def test_conversation_messages_page_loads_older_messages(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "conv_page")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conv_id = generate_ulid()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            title="Paged chat",
            channel="web",
            scope="channel",
        )
    )
    for index in range(6):
        db_session.add(
            Message(
                id=generate_ulid(),
                conversation_id=conv_id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"message-{index}",
                created_at=base + timedelta(minutes=index),
            )
        )
    await db_session.commit()

    first = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/messages/page?limit=2",
        headers=headers,
    )
    assert first.status_code == 200
    first_body = first.json()
    assert [row["content"] for row in first_body["items"]] == [
        "message-4",
        "message-5",
    ]
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/messages/page",
        headers=headers,
        params={"limit": 2, "before": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert [row["content"] for row in second_body["items"]] == [
        "message-2",
        "message-3",
    ]
    assert second_body["has_more"] is True
    assert second_body["next_cursor"]


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
