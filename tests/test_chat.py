"""E2E tests: chat SSE streaming, conversations, messages."""

import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import apps.api.routers.chat as chat_router
from apps.api.routers.chat import _resolve_chat_workspace_scope, _visible_chat_messages
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, VectorStatus
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.services.auth_service import create_access_token, hash_password
from packages.core.services.chat_feedback import (
    ChatFeedbackIntegrityErrorKind,
    ChatFeedbackRating,
    ChatFeedbackTargetDeletedError,
    ChatFeedbackTargetKind,
    build_chat_feedback_content_preview,
    classify_chat_feedback_integrity_error,
    persist_chat_message_feedback,
)
from packages.core.services.conversation_lifecycle import delete_conversation
from packages.core.services.hitl_requests import user_visible_hitl_action_text

pytestmark = pytest.mark.oss_regression


def test_chat_feedback_preview_matches_multi_final_copy_semantics() -> None:
    blocks = [
        {"type": "text", "phase": "final", "text": "First paragraph"},
        {"type": "text", "phase": "final", "text": "Second paragraph"},
    ]

    assert build_chat_feedback_content_preview(
        requested_preview="Client preview",
        message_content="",
        assistant_blocks=blocks,
    ) == "First paragraph\n\nSecond paragraph"
    assert build_chat_feedback_content_preview(
        requested_preview=None,
        message_content="Context\n\nFirst paragraphSecond paragraph",
        assistant_blocks=blocks,
    ) == "Context\n\nFirst paragraphSecond paragraph"


def test_chat_feedback_preview_excludes_hidden_assistant_protocol() -> None:
    hidden = (
        '<manor-live-edit>{"operation":"replace",'
        '"content":"SECRET_INTERNAL_PATCH"}</manor-live-edit>\n'
        "<manor-final-response>Final answer</manor-final-response>"
    )

    assert build_chat_feedback_content_preview(
        requested_preview="Client preview",
        message_content=hidden,
        assistant_blocks=[
            {"type": "text", "phase": "final", "text": hidden},
        ],
    ) == "Final answer"

    nested_marker = (
        '<manor-live-edit>{"operation":"replace",'
        '"content":"<manor-final-response>SECRET_INTERNAL_PATCH"}'
        "</manor-live-edit>\nVisible answer"
    )
    assert build_chat_feedback_content_preview(
        requested_preview=None,
        message_content=nested_marker,
        assistant_blocks=[
            {"type": "text", "phase": "final", "text": nested_marker},
        ],
    ) == "Visible answer"


def test_chat_feedback_integrity_errors_only_classify_lifecycle_foreign_keys() -> None:
    class DatabaseFailure(Exception):
        def __init__(self, *, constraint_name: str, sqlstate: str) -> None:
            super().__init__(constraint_name)
            self.constraint_name = constraint_name
            self.sqlstate = sqlstate

    deleted_target = IntegrityError(
        "insert",
        {},
        DatabaseFailure(
            constraint_name="fk_chat_feedback_message",
            sqlstate="23503",
        ),
    )
    unrelated_constraint = IntegrityError(
        "insert",
        {},
        DatabaseFailure(
            constraint_name="uq_chat_feedback_message_user",
            sqlstate="23505",
        ),
    )

    assert classify_chat_feedback_integrity_error(
        deleted_target
    ) == ChatFeedbackIntegrityErrorKind.TARGET_DELETED
    assert classify_chat_feedback_integrity_error(
        unrelated_constraint
    ) == ChatFeedbackIntegrityErrorKind.UNEXPECTED


async def _auth(client: AsyncClient, username: str = "chatuser") -> dict:
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
async def test_chat_feedback_assigns_server_revisions_to_concurrent_mutations(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_concurrent")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Rate this response",
            author_kind="agent",
            message_kind="text",
        )
    )
    await db_session.commit()

    endpoint = (
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    )
    first, second = await asyncio.gather(
        client.post(
            endpoint,
            headers=headers,
            json={"rating": "up"},
        ),
        client.post(
            endpoint,
            headers=headers,
            json={"rating": "down"},
        ),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    responses = [first.json(), second.json()]
    assert {item["mutation_status"] for item in responses} == {"accepted"}
    assert sorted(item["mutation_sequence"] for item in responses) == [1, 2]
    ordered = sorted(responses, key=lambda item: item["mutation_sequence"])
    assert datetime.fromisoformat(ordered[0]["updated_at"]) <= datetime.fromisoformat(
        ordered[1]["updated_at"]
    )
    newest = ordered[-1]

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.rating == newest["rating"]
    assert stored.mutation_sequence == 2


@pytest.mark.asyncio
async def test_chat_feedback_advances_the_server_revision(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_server_revision")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Rate this response",
            author_kind="agent",
            message_kind="text",
        )
    )
    await db_session.commit()

    endpoint = (
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    )
    first = await client.post(
        endpoint,
        headers=headers,
        json={"rating": "down"},
    )
    second = await client.post(endpoint, headers=headers, json={"rating": "up"})

    assert first.status_code == 200, first.text
    assert first.json()["mutation_status"] == "accepted"
    assert first.json()["mutation_sequence"] == 1
    assert second.status_code == 200, second.text
    assert second.json()["message_id"] == message_id
    assert second.json()["rating"] == "up"
    assert second.json()["mutation_sequence"] == 2
    assert second.json()["mutation_status"] == "accepted"

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.rating == "up"
    assert stored.mutation_sequence == 2


@pytest.mark.asyncio
async def test_chat_feedback_schema_rejects_a_legacy_worker_row(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_legacy_worker")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Legacy worker response",
            author_kind="agent",
            message_kind="text",
        )
    )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("""
                INSERT INTO chat_message_feedback (
                    id, entity_id, user_id, conversation_id, message_id, rating
                ) VALUES (
                    :id, :entity_id, :user_id, :conversation_id, :message_id,
                    :rating
                )
            """),
            {
                "id": generate_ulid(),
                "entity_id": me["entity_id"],
                "user_id": me["id"],
                "conversation_id": conversation_id,
                "message_id": message_id,
                "rating": "down",
            },
        )
    await db_session.rollback()

    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["mutation_sequence"] == 1

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.rating == "up"
    assert stored.target_kind == "response"
    assert stored.target_id == message_id


@pytest.mark.asyncio
async def test_chat_feedback_list_restores_only_the_current_users_ratings(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_hydration")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    other_headers = await _auth(client, "chat_feedback_hydration_other")
    other = (await client.get("/api/v1/auth/me", headers=other_headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Rate this response",
            author_kind="agent",
            message_kind="text",
        )
    )
    await db_session.commit()

    endpoint = (
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    )
    saved = await client.post(endpoint, headers=headers, json={"rating": "down"})
    assert saved.status_code == 200, saved.text
    db_session.add(
        ChatMessageFeedback(
            entity_id=me["entity_id"],
            user_id=other["id"],
            conversation_id=conversation_id,
            message_id=message_id,
            target_kind="response",
            target_id=message_id,
            rating="up",
            mutation_sequence=7,
        )
    )
    await db_session.commit()

    restored = await client.get(
        f"/api/v1/chat/conversations/{conversation_id}/feedback",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json() == [
        {
            "message_id": message_id,
            "rating": "down",
            "mutation_sequence": 1,
            "target_kind": "response",
            "target_id": message_id,
            "task_id": None,
            "plan_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_chat_feedback_uses_final_assistant_block_as_the_content_preview(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_structured_preview")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Working",
            author_kind="agent",
            message_kind="text",
            meta={
                "assistant_blocks": [
                    {"type": "text", "phase": "analysis", "text": "Working"},
                    {"type": "text", "phase": "final", "text": "Final answer"},
                ]
            },
        )
    )
    await db_session.commit()

    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up", "content_preview": "Client-forged preview"},
    )
    assert saved.status_code == 200, saved.text

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.content_preview == "Final answer"


@pytest.mark.asyncio
async def test_chat_feedback_derives_request_preview_from_the_same_conversation(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_request_preview")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    other_conversation_id = generate_ulid()
    request_id = generate_ulid()
    message_id = generate_ulid()
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Conversation(
                id=other_conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=request_id,
                conversation_id=conversation_id,
                role="user",
                content="Authoritative request",
                author_kind="user",
                message_kind="text",
                created_at=now,
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conversation_id,
                role="assistant",
                content="Legacy assistant with a backfilled user author kind",
                author_kind="user",
                message_kind="text",
                created_at=now + timedelta(milliseconds=500),
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conversation_id,
                role="user",
                content="[File permission read]",
                author_kind="user",
                message_kind="text",
                created_at=now + timedelta(seconds=1),
            ),
            Message(
                id=generate_ulid(),
                conversation_id=other_conversation_id,
                role="user",
                content="Request from another conversation",
                author_kind="user",
                message_kind="text",
                created_at=now + timedelta(seconds=1, milliseconds=500),
            ),
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Answer",
                author_kind="agent",
                message_kind="text",
                created_at=now + timedelta(seconds=2),
            ),
        ]
    )
    await db_session.commit()

    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={
            "rating": "up",
            "request_preview": "Client-forged request",
        },
    )
    assert saved.status_code == 200, saved.text

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.request_preview == "Authoritative request"


@pytest.mark.asyncio
async def test_chat_feedback_prefers_the_causal_request_over_a_newer_user_message(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_causal_request")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    origin_request_id = generate_ulid()
    answer_id = generate_ulid()
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=origin_request_id,
                conversation_id=conversation_id,
                role="user",
                content="Request that caused this answer",
                author_kind="user",
                message_kind="text",
                created_at=now,
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conversation_id,
                role="user",
                content="Newer unrelated request",
                author_kind="user",
                message_kind="text",
                created_at=now + timedelta(seconds=1),
            ),
            Message(
                id=answer_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Answer to the first request",
                author_kind="agent",
                message_kind="text",
                meta={"origin_user_message_id": origin_request_id},
                created_at=now + timedelta(seconds=2),
            ),
        ]
    )
    await db_session.commit()

    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{answer_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )
    assert saved.status_code == 200, saved.text

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == answer_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    assert stored.request_preview == "Request that caused this answer"


@pytest.mark.asyncio
async def test_generic_chat_feedback_rejects_non_response_targets(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_target_policy")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    task_completion_id = generate_ulid()
    system_message_id = generate_ulid()
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=task_completion_id,
                conversation_id=conversation_id,
                role="assistant",
                content="✅ **Task complete — Prepare report**",
                author_kind="agent",
                message_kind="agent_update",
                refs=[{"type": "task", "id": generate_ulid()}],
                meta={"feedback_target_kind": "task_completion"},
            ),
            Message(
                id=system_message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Internal lifecycle update",
                author_kind="system",
                message_kind="system",
            ),
        ]
    )
    await db_session.commit()

    for message_id in (task_completion_id, system_message_id):
        rejected = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
            headers=headers,
            json={"rating": "up"},
        )
        assert rejected.status_code == 422, rejected.text

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id.in_(
                    [task_completion_id, system_message_id]
                )
            )
        )
    ).scalars().all()
    assert stored == []


@pytest.mark.asyncio
async def test_deleting_a_conversation_deletes_its_chat_feedback(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_delete_conversation")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Private preview",
            author_kind="agent",
            message_kind="text",
        )
    )
    await db_session.commit()
    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )
    assert saved.status_code == 200, saved.text

    deleted = await client.delete(
        f"/api/v1/chat/conversations/{conversation_id}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    await db_session.rollback()
    feedback = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.conversation_id == conversation_id
            )
        )
    ).scalar_one_or_none()
    assert feedback is None


@pytest.mark.asyncio
async def test_chat_feedback_maps_a_deleted_target_race_to_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    headers = await _auth(client, "chat_feedback_deleted_race")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Delete before the persistence lock",
                author_kind="agent",
                message_kind="text",
            ),
        ]
    )
    await db_session.commit()

    async def deleted_target(*_args, **_kwargs):
        raise ChatFeedbackTargetDeletedError(message_id)

    monkeypatch.setattr(
        chat_router,
        "persist_chat_message_feedback",
        deleted_target,
    )
    response = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Message not found"


@pytest.mark.asyncio
async def test_feedback_update_and_conversation_delete_use_message_first_lock_order(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_delete_lock_order")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Serialize feedback with deletion",
                author_kind="agent",
                message_kind="text",
            ),
        ]
    )
    await db_session.commit()
    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )
    assert saved.status_code == 200, saved.text

    await db_session.rollback()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with (
        session_factory() as feedback_db,
        session_factory() as delete_db,
    ):
        await feedback_db.execute(
            select(Message.id).where(Message.id == message_id).with_for_update()
        )

        async def delete_and_commit() -> bool:
            deleted = await delete_conversation(
                delete_db,
                conversation_id,
                me["entity_id"],
            )
            await delete_db.commit()
            return deleted

        delete_task = asyncio.create_task(delete_and_commit())
        try:
            await asyncio.sleep(0.1)
            assert not delete_task.done()

            updated = await asyncio.wait_for(
                persist_chat_message_feedback(
                    feedback_db,
                    entity_id=me["entity_id"],
                    user_id=me["id"],
                    conversation_id=conversation_id,
                    message_id=message_id,
                    rating=ChatFeedbackRating.DOWN,
                    content_preview="Serialize feedback with deletion",
                    request_preview=None,
                    target_kind=ChatFeedbackTargetKind.RESPONSE,
                    target_id=message_id,
                ),
                timeout=5,
            )
            assert updated.mutation_sequence == 2
            assert await asyncio.wait_for(delete_task, timeout=5) is True
        finally:
            if not delete_task.done():
                delete_task.cancel()
                await asyncio.gather(delete_task, return_exceptions=True)

    await db_session.rollback()
    assert await db_session.get(Conversation, conversation_id) is None
    feedback = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.conversation_id == conversation_id
            )
        )
    ).scalar_one_or_none()
    assert feedback is None


@pytest.mark.asyncio
async def test_deleting_a_message_cascades_its_chat_feedback(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "chat_feedback_delete_message")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=me["entity_id"],
                user_id=me["id"],
                channel="web",
                scope="channel",
            ),
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Private preview",
                author_kind="agent",
                message_kind="text",
            ),
        ]
    )
    await db_session.commit()
    saved = await client.post(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )
    assert saved.status_code == 200, saved.text

    await db_session.execute(sa_delete(Message).where(Message.id == message_id))
    await db_session.commit()

    feedback = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.message_id == message_id
            )
        )
    ).scalar_one_or_none()
    assert feedback is None


@pytest.mark.asyncio
async def test_chat_feedback_rejects_values_outside_the_rating_enum(
    client: AsyncClient,
):
    headers = await _auth(client, "chat_feedback_rating_enum")
    response = await client.post(
        "/api/v1/chat/conversations/does-not-matter/messages/does-not-matter/feedback",
        headers=headers,
        json={"rating": "maybe"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "rating"]


@pytest.mark.asyncio
async def test_chat_stream_sse(client: AsyncClient):
    """Send a message → receive SSE stream with text_delta events."""
    headers = await _auth(client)

    # POST /chat/stream returns SSE
    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Hello AI",
        },
        timeout=10.0,
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # Parse SSE events
    events = []
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
            events.append({"event": event_type, "data": data})

    # Should have: stream_start, text_delta(s), stream_end
    event_types = [e["event"] for e in events]
    assert "stream_start" in event_types
    assert "text_delta" in event_types
    assert "stream_end" in event_types

    # stream_end should contain conversation_id
    end_event = [e for e in events if e["event"] == "stream_end"][0]
    assert "conversation_id" in end_event["data"]


@pytest.mark.asyncio
async def test_chat_stream_projects_workspace_recommendation_after_unrelated_active_draft(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import chat as chat_router
    from packages.core.ai.runtime.auto_route_classifier import (
        AutoRouteDecision,
        AutoRouteMode,
    )
    from packages.core.ai.runtime.general_chat_intent import (
        GeneralChatIntentDecision,
    )
    from packages.core.ai.runtime.workspace_creation_authorization import (
        WorkspaceCreationAuthorizationDecision,
    )
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import chat_intent_routing
    from packages.core.services.sse_events import format_sse

    headers = await _auth(client, "chat_recommendation_projection")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    conversation_id = generate_ulid()
    draft_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            channel="web",
            scope="channel",
        )
    )
    db_session.add(
        WorkspaceDraft(
            id=draft_id,
            entity_id=me["entity_id"],
            user_id=me["id"],
            status="active",
            fields={},
            messages=[],
            missing=[],
        )
    )
    db_session.add(
        Message(
            id=generate_ulid(),
            conversation_id=conversation_id,
            role="assistant",
            content="Property marketing Workspace draft",
            tool_calls=[{"artifact_kind": "workspace_draft", "draft_id": draft_id}],
        )
    )
    await db_session.commit()

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.DIRECT_CHAT, 0.98)

    async def fake_creation_authorization(**_kwargs):
        return WorkspaceCreationAuthorizationDecision.not_authorized(0.99)

    async def fake_candidates(*_args, **_kwargs):
        return []

    async def fake_general_intent(*, candidates, recent_context_text, **_kwargs):
        assert "Property marketing Workspace draft" in recent_context_text
        assert "independent request that may need a separate Workspace" in recent_context_text
        return GeneralChatIntentDecision.from_payload(
            {
                "kind": "workspace_recommendation",
                "confidence": 0.95,
                "reason": "Recruiting needs its own recurring operating system.",
                "action": "create_new",
                "workspace_id": None,
            },
            candidates=candidates,
        )

    monkeypatch.setattr(chat_intent_routing, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(
        chat_intent_routing,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )
    monkeypatch.setattr(
        chat_intent_routing,
        "list_workspace_intent_candidates",
        fake_candidates,
    )
    monkeypatch.setattr(
        chat_intent_routing,
        "classify_general_chat_intent",
        fake_general_intent,
    )

    captured: dict[str, object] = {}

    async def fake_stream(*_args, runtime_metadata=None, **_kwargs):
        captured["runtime_metadata"] = runtime_metadata
        yield format_sse("stream_start", {"conversation_id": conversation_id})
        yield format_sse("text_delta", {"content": "Recruiting response"})
        yield format_sse(
            "stream_end",
            {"conversation_id": conversation_id, "persisted": False},
        )

    monkeypatch.setattr(chat_router, "runtime_stream_chat_turn", fake_stream)

    response = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "我想长期管理招聘流程",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 200, response.text
    metadata = captured["runtime_metadata"]
    assert isinstance(metadata, dict)
    assert metadata["workspace_recommendation"]["action"] == "create_new"
    assert metadata["workspace_recommendation"]["request"] == "我想长期管理招聘流程"


@pytest.mark.asyncio
async def test_chat_creates_conversation(client: AsyncClient):
    """Chat stream creates a conversation and saves messages."""
    headers = await _auth(client)

    # Send a message (creates conversation)
    stream_resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "First message",
        },
    )
    stream_message_id = None
    event_type = ""
    for line in stream_resp.text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
            if event_type == "stream_end":
                stream_message_id = data.get("message_id")

    # List conversations
    resp = await client.get("/api/v1/chat/conversations", headers=headers)
    assert resp.status_code == 200
    convs = resp.json()
    assert len(convs) >= 1

    # Get messages for the conversation
    conv_id = convs[0]["id"]
    msg_resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers)
    assert msg_resp.status_code == 200
    msgs = msg_resp.json()
    # Should have at least the user message
    assert len(msgs) >= 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "First message"
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["id"] == stream_message_id
    assert "still working" not in (assistant_msgs[-1]["content"] or "")


@pytest.mark.asyncio
async def test_chat_stream_persists_knowledge_attachment_refs(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import file_context

    async def _empty_extract(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(file_context, "extract_text", _empty_extract)
    headers = await _auth(client, "chat_attachment_refs")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    doc_id = "doc_chat_attachment_ref"
    db_session.add(
        Document(
            id=doc_id,
            entity_id=me["entity_id"],
            name="completion-reviews/daily-review-template.md",
            fs_path="completion-reviews/daily-review-template.md",
            file_type="md",
            mime_type="text/markdown",
            source="upload",
            vector_status=VectorStatus.READY,
        )
    )
    await db_session.commit()

    stream_resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "使用Chrome，将这个文件上传到youtube草稿箱",
            "document_ids": doc_id,
        },
    )

    assert stream_resp.status_code == 200
    conversation_id = None
    event_type = ""
    for line in stream_resp.text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
            if event_type == "stream_end":
                conversation_id = data.get("conversation_id")
    assert conversation_id

    msg_resp = await client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        headers=headers,
    )
    assert msg_resp.status_code == 200
    user_msg = next(m for m in msg_resp.json() if m["role"] == "user")
    assert user_msg["attachments"] == [
        {
            "kind": "knowledge_document",
            "name": "completion-reviews/daily-review-template.md",
            "mime": "text/markdown",
            "path": "completion-reviews/daily-review-template.md",
            "url": f"/api/v1/fs/{me['entity_id']}/completion-reviews/daily-review-template.md",
            "document_id": doc_id,
            "text": True,
        }
    ]


def test_global_chat_message_response_preserves_workflow_action_fields() -> None:
    from datetime import UTC, datetime

    from apps.api.routers.chat import _to_chat_message_response

    now = datetime.now(UTC)
    message = SimpleNamespace(
        id="message-workflow",
        conversation_id="conversation-global",
        role="system",
        content="Review Flow inputs",
        tool_calls=None,
        token_usage=None,
        attachments=None,
        message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": "run-1"}],
        meta={
            "workflow_run_id": "run-1",
            "provider_reasoning_content": "private chain of thought",
            "internal_trace": {"secret": "not public"},
        },
        pending_action={
            "kind": "workflow_starter_input",
            "workflow_run_id": "run-1",
        },
        resolved_at=now,
        resolution={"choice": "run"},
        created_at=now,
    )

    response = _to_chat_message_response(message)

    assert response.meta == {"workflow_run_id": "run-1"}
    assert response.pending_action == message.pending_action
    assert response.resolved_at == now.isoformat()
    assert response.resolution == {"choice": "run"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "items_key"),
    [("messages", None), ("messages/page", "items")],
)
async def test_global_chat_marks_deleted_workspace_workflow_inaccessible(
    client: AsyncClient,
    db_session: AsyncSession,
    path: str,
    items_key: str | None,
) -> None:
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "global_deleted_workflow")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Deleted workflow source"},
        )
    ).json()
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        user_id=me["id"],
        workspace_id=None,
        title="Global workflow history",
        channel="web",
        scope="private",
    )
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        status="failed",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_trace=[],
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="system",
        content="Workflow needs attention.",
        author_kind="system",
        message_kind="workflow_activity",
        refs=[{"type": "workflow_run", "id": run.id}],
        meta={"workflow_run_id": run.id, "workflow_status": "failed"},
    )
    db_session.add_all([conversation, run, message])
    await db_session.commit()

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204

    response = await client.get(
        f"/api/v1/chat/conversations/{conversation.id}/{path}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    body = payload if items_key is None else payload[items_key]
    assert len(body) == 1
    assert body[0]["id"] == message.id
    assert body[0]["meta"] == {
        "workflow_run_id": run.id,
        "workflow_status": "failed",
        "workflow_run_accessible": False,
    }


@pytest.mark.asyncio
async def test_workspace_chat_user_message_records_author(client: AsyncClient):
    """Regression: a user message sent via /chat/stream into a workspace must
    persist its author_user_id. Without it the message reads back with no
    author, and the workspace chat UI renders every member's message as the
    viewer's own ("you")."""
    headers = await _auth(client, "ws_author_member")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Author Attribution WS"},
        )
    ).json()
    ws_id = workspace["id"]

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Hello from a workspace member",
            "workspace_context": "true",
            "workspace_id": ws_id,
        },
        timeout=15.0,
    )
    assert resp.status_code == 200
    # Drain the SSE stream so the user message is committed.
    _ = resp.text

    msgs = (
        await client.get(
            f"/api/v1/workspaces/{ws_id}/chat/messages",
            headers=headers,
        )
    ).json()
    user_msgs = [m for m in msgs if m["author_kind"] == "user"]
    assert user_msgs, "expected the user message to be persisted in workspace chat"
    assert all(m["author_user_id"] == me["id"] for m in user_msgs)


@pytest.mark.asyncio
async def test_workspace_chat_resolution_records_approver(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Resolving a workspace action must record AND surface who approved it,
    both in the resolve response and when re-listing messages."""
    from packages.core.models.task import Conversation, Message

    headers = await _auth(client, "ws_approver")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Approver Attribution WS"},
        )
    ).json()
    ws_id = ws["id"]

    conv_id = generate_ulid()
    msg_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws_id,
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=msg_id,
            conversation_id=conv_id,
            role="assistant",
            content="Approve this action?",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={"kind": "approval"},
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{msg_id}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_by_user_id"] == me["id"]
    assert body["resolved_by_user_name"]

    msgs = (
        await client.get(
            f"/api/v1/workspaces/{ws_id}/chat/messages",
            headers=headers,
        )
    ).json()
    resolved = next(m for m in msgs if m["id"] == msg_id)
    assert resolved["resolved_by_user_id"] == me["id"]
    assert resolved["resolved_by_user_name"]


def test_hitl_action_text_is_user_visible():
    assert user_visible_hitl_action_text("approve") == "Approved the requested action."
    assert user_visible_hitl_action_text("always_approve") == "Approved the requested action."
    assert user_visible_hitl_action_text("reject") == "Rejected the requested action."


def test_visible_chat_messages_hides_stale_stream_placeholder():
    rows = [
        SimpleNamespace(role="user", content="hello", meta={}),
        SimpleNamespace(
            role="assistant",
            content="Codex is still working on that...",
            meta={"stream_status": "running"},
        ),
        SimpleNamespace(role="assistant", content="done", meta={}),
    ]

    visible = _visible_chat_messages(rows)

    assert [m.content for m in visible] == ["hello", "done"]


def test_visible_chat_messages_keeps_pending_placeholder_from_another_turn():
    rows = [
        SimpleNamespace(role="user", content="first", meta={}),
        SimpleNamespace(
            role="assistant",
            content="Still working",
            meta={
                "stream_status": "running",
                "origin_user_message_id": "user-1",
            },
        ),
        SimpleNamespace(role="user", content="second", meta={}),
        SimpleNamespace(
            role="assistant",
            content="Second answer",
            meta={"origin_user_message_id": "user-2"},
        ),
    ]

    visible = _visible_chat_messages(rows)

    assert [message.content for message in visible] == [
        "first",
        "Still working",
        "second",
        "Second answer",
    ]


def test_visible_chat_messages_hides_placeholder_superseded_for_same_turn():
    rows = [
        SimpleNamespace(role="user", content="hello", meta={}),
        SimpleNamespace(
            role="assistant",
            content="Still working",
            meta={
                "stream_status": "running",
                "origin_user_message_id": "user-1",
            },
        ),
        SimpleNamespace(
            role="assistant",
            content="Done",
            meta={"origin_user_message_id": "user-1"},
        ),
    ]

    visible = _visible_chat_messages(rows)

    assert [message.content for message in visible] == ["hello", "Done"]


@pytest.mark.asyncio
async def test_chat_reuse_conversation(client: AsyncClient):
    """Send multiple messages to same conversation."""
    headers = await _auth(client)

    # First message — creates conversation
    resp1 = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Message 1",
        },
    )
    # Extract conversation_id from stream_start event
    conv_id = None
    for line in resp1.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break
    assert conv_id

    # Second message — reuse conversation
    await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Message 2",
            "conversation_id": conv_id,
        },
    )

    # Should have 2 user messages in one conversation
    msg_resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers)
    msgs = msg_resp.json()
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 2


@pytest.mark.asyncio
async def test_chat_no_auth(client: AsyncClient):
    resp = await client.post("/api/v1/chat/stream", data={"message": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_conversation_isolation(client: AsyncClient):
    """User A can't see User B's conversations."""
    headers_a = await _auth(client, "chat_a")
    headers_b = await _auth(client, "chat_b")

    # A chats
    resp = await client.post("/api/v1/chat/stream", headers=headers_a, data={"message": "A's message"})
    conv_id = None
    for line in resp.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break

    # B can't see A's messages
    resp2 = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers_b)
    assert resp2.status_code == 404

    # B's conversation list is empty
    resp3 = await client.get("/api/v1/chat/conversations", headers=headers_b)
    assert len(resp3.json()) == 0


@pytest.mark.asyncio
async def test_personal_conversation_is_private_within_same_entity(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """A teammate in the same organization cannot read or append to a personal chat."""
    headers_a = await _auth(client, "same_entity_a")
    me_resp = await client.get("/api/v1/auth/me", headers=headers_a)
    me = me_resp.json()

    user_b = User(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        email="same_entity_b@test.com",
        display_name="same_entity_b",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(user_b)
    await db_session.commit()
    headers_b = {"Authorization": f"Bearer {create_access_token(user_b.id, user_b.entity_id, user_b.role)}"}

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers_a,
        data={"message": "A private same-entity message"},
    )
    conv_id = None
    for line in resp.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break
    assert conv_id

    read_resp = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        headers=headers_b,
    )
    assert read_resp.status_code == 404

    append_resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers_b,
        data={"message": "B should not append", "conversation_id": conv_id},
    )
    assert append_resp.status_code == 404

    list_resp = await client.get("/api/v1/chat/conversations", headers=headers_b)
    assert all(conv["id"] != conv_id for conv in list_resp.json())


@pytest.mark.asyncio
async def test_workspace_chat_rejects_mismatched_conversation_scope(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.task import Conversation
    from packages.core.services.conversation_lifecycle import get_or_create_conversation

    headers = await _auth(client, "workspace_scope_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws_a = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Scope A"},
        )
    ).json()
    ws_b = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Scope B"},
        )
    ).json()

    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws_a["id"],
            title="Workspace A main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            conversation_id=conv_id,
            workspace_id=ws_b["id"],
        )

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            conversation_id=conv_id,
            workspace_id=ws_a["id"],
            thread_ref_kind="task",
            thread_ref_id=generate_ulid(),
        )


@pytest.mark.asyncio
async def test_chat_workspace_scope_requires_workspace_chat_source(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.task import Conversation

    headers = await _auth(client, "workspace_source_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Workspace Source Guard"},
        )
    ).json()
    user = SimpleNamespace(
        id=me["id"],
        entity_id=me["entity_id"],
        role=me.get("role"),
    )

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=None,
        workspace_id=ws["id"],
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=False,
    ) == (ws["id"], None, None)

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=None,
        workspace_id=ws["id"],
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=True,
    ) == (ws["id"], None, None)

    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws["id"],
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=conv_id,
        workspace_id=None,
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=False,
    ) == (ws["id"], None, None)


@pytest.mark.asyncio
async def test_members_only_workspace_chat_requires_workspace_read_access(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from datetime import UTC, datetime

    from apps.api.routers import workspace_chat
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import WorkspaceStaff

    monkeypatch.setattr(
        workspace_chat,
        "_schedule_workspace_chat_processing",
        lambda **_kwargs: None,
    )

    owner_headers = await _auth(client, "workspace_chat_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=owner_headers,
            json={"name": "Private Workspace Chat"},
        )
    ).json()
    workspace_id = workspace["id"]
    assert workspace["settings"]["access_mode"] == "members_only"

    outsider = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="workspace_chat_outsider@test.com",
        display_name="workspace_chat_outsider",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    conv_id = generate_ulid()
    channel_conv_id = generate_ulid()
    action_message_id = generate_ulid()
    db_session.add(outsider)
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Conversation(
            id=channel_conv_id,
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            title="Private customer channel",
            channel="webchat",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=generate_ulid(),
            conversation_id=conv_id,
            role="user",
            content="workspace-only note",
            author_kind="user",
            message_kind="text",
        )
    )
    db_session.add(
        Message(
            id=action_message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review this private action",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={"kind": "approve_proposals", "review_id": "review_private"},
        )
    )
    await db_session.commit()
    outsider_headers = {
        "Authorization": f"Bearer {create_access_token(outsider.id, outsider.entity_id, outsider.role)}"
    }

    assert (
        await client.get(
            f"/api/v1/workspaces/{workspace_id}/chat/messages",
            headers=outsider_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages",
            headers=outsider_headers,
            json={"body": "should not enter"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{action_message_id}/resolve",
            headers=outsider_headers,
            json={"choice": "approve"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{action_message_id}/feedback",
            headers=outsider_headers,
            json={"rating": "up"},
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages",
            headers=outsider_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/stream",
            headers=outsider_headers,
            data={
                "message": "open private workspace",
                "workspace_context": "true",
                "workspace_id": workspace_id,
            },
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/stream",
            headers=outsider_headers,
            data={"message": "append private workspace", "conversation_id": conv_id},
        )
    ).status_code == 404
    hidden_history = await client.get("/api/v1/chat/conversations", headers=outsider_headers)
    assert all(row["id"] != channel_conv_id for row in hidden_history.json())

    db_session.add(
        WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=outsider.id,
            role="viewer",
            added_by=owner["id"],
            added_at=datetime.now(UTC),
            status="active",
        )
    )
    await db_session.commit()

    allowed_list = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=outsider_headers,
    )
    assert allowed_list.status_code == 200
    allowed_post = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=outsider_headers,
        json={"body": "viewer can participate after membership is granted"},
    )
    assert allowed_post.status_code == 201
    visible_history = await client.get("/api/v1/chat/conversations", headers=outsider_headers)
    assert any(row["id"] == channel_conv_id for row in visible_history.json())


@pytest.mark.asyncio
async def test_workspace_chat_rejects_deleted_workspace_runtime(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.services.conversation_lifecycle import get_or_create_conversation

    headers = await _auth(client, "workspace_deleted_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Deleted Runtime"},
        )
    ).json()
    delete = await client.delete(f"/api/v1/workspaces/{ws['id']}", headers=headers)
    assert delete.status_code == 204

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            workspace_id=ws["id"],
        )
