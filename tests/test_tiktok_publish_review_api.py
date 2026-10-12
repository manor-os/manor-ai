"""Refreshing a TikTok review never grants, retargets, or revives a post."""

from __future__ import annotations

import copy
import hashlib

import pytest
from fastapi import HTTPException

from apps.api.routers.chat import refresh_tiktok_publish_review
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity, User, UserMembership


async def _pending(db):
    entity_id, user_id, conversation_id = (generate_ulid() for _ in range(3))
    user = User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="test",
        role="owner",
        status="active",
    )
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        status="active",
    )
    args = {"video_url": "uploads/chat/example.mp4", "integration_account_id": "account-a"}
    request = HitlRequest(
        id=generate_ulid(),
        entity_id=entity_id,
        origin_kind="tool_call",
        origin_conversation_id=conversation_id,
        action_key="social_post.publish",
        capability_id="social.publish",
        status="pending",
        dedup_key=generate_ulid(),
        context={
            "tool": "mcp__tiktok__post_video",
            "requested_by": user_id,
            "args_hash": "original",
            "continuation": {"version": 1, "tool": "mcp__tiktok__post_video", "arguments": args},
        },
    )
    db.add_all(
        [
            Entity(id=entity_id, name="TikTok review API"),
            user,
            conversation,
            request,
            UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active", is_primary=True),
        ]
    )
    await db.commit()
    return user, conversation, request


def _fresh():
    return {
        "review": {
            "kind": "tiktok_publish",
            "version": 1,
            "review_id": generate_ulid(),
            "media": {"url": "placeholder"},
        },
        "tiktok_publish_evidence": {"source_sha256": "private-source-digest"},
    }


@pytest.mark.asyncio
async def test_refresh_preserves_exact_call_and_returns_only_public_review(db_session, monkeypatch):
    from packages.core.services import tiktok_publish_review as reviews

    user, conversation, request = await _pending(db_session)
    original = copy.deepcopy(request.context)
    fresh = _fresh()
    calls = []

    async def prepare(**kwargs):
        calls.append(kwargs)
        return fresh

    monkeypatch.setattr(reviews, "prepare_tiktok_publish_review", prepare)
    result = await refresh_tiktok_publish_review(request.id, user=user, db=db_session)
    assert result == fresh["review"]
    assert "tiktok_publish_evidence" not in result
    assert calls[0]["conversation_id"] == conversation.id
    assert calls[0]["user_id"] == user.id
    assert calls[0]["arguments"] == original["continuation"]["arguments"]
    await db_session.refresh(request)
    assert request.status == "pending"
    assert request.context["continuation"] == original["continuation"]
    assert request.context["args_hash"] == original["args_hash"]
    assert request.context["tiktok_review_refreshed"] is True
    assert request.context["tiktok_publish_evidence"] == fresh["tiktok_publish_evidence"]
    assert request.decided_by_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["foreign_actor", "foreign_entity", "non_tiktok", "consumed", "missing_conversation"]
)
async def test_invalid_review_is_rejected_before_creator_or_media_io(db_session, monkeypatch, change):
    from packages.core.services import tiktok_publish_review as reviews

    user, conversation, request = await _pending(db_session)
    if change == "foreign_actor":
        request.context = {**request.context, "requested_by": generate_ulid()}
    elif change == "foreign_entity":
        request.entity_id = generate_ulid()
    elif change == "non_tiktok":
        request.context = {**request.context, "tool": "mcp__youtube__upload_video"}
    elif change == "consumed":
        request.status = "consumed"
    else:
        await db_session.delete(conversation)
    await db_session.commit()

    async def no_io(**kwargs):
        pytest.fail("Rejected review reached creator/media I/O")

    monkeypatch.setattr(reviews, "prepare_tiktok_publish_review", no_io)
    with pytest.raises(HTTPException) as caught:
        await refresh_tiktok_publish_review(request.id, user=user, db=db_session)
    assert caught.value.status_code in {403, 404, 409}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["consumed", "changed_args", "revoked_user"])
async def test_provider_wait_cannot_revive_changed_or_unauthorized_review(db_session, monkeypatch, change):
    from packages.core.services import tiktok_publish_review as reviews

    user, _conversation, request = await _pending(db_session)
    request_id = request.id

    async def prepare(**kwargs):
        if change == "consumed":
            request.status = "consumed"
        elif change == "changed_args":
            request.context = {**request.context, "args_hash": "changed"}
        else:
            user.status = "inactive"
        await db_session.commit()
        return _fresh()

    monkeypatch.setattr(reviews, "prepare_tiktok_publish_review", prepare)
    with pytest.raises(HTTPException) as caught:
        await refresh_tiktok_publish_review(request_id, user=user, db=db_session)
    assert caught.value.status_code in {403, 409}
    await db_session.refresh(request)
    assert "tiktok_review_refreshed" not in request.context


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "no_auth", "stale_review", "changed_bytes", "missing_attachment", "consumed"])
async def test_preview_authenticates_and_pins_the_original_attachment(
    client,
    db_session,
    tmp_path,
    monkeypatch,
    change,
):
    from packages.core.config import get_settings
    from packages.core.services.auth_service import create_access_token

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    user, conversation, request = await _pending(db_session)
    content = b"The exact reviewed synthetic video bytes"
    digest = hashlib.sha256(content).hexdigest()
    path = f"uploads/chat/{digest[:16]}.mp4"
    source = tmp_path / user.entity_id / path
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    message = Message(
        conversation_id=conversation.id,
        role="user",
        author_kind="user",
        attachments=[{"kind": "chat_upload", "path": path}],
    )
    db_session.add(message)
    review_id = generate_ulid()
    request.context = {
        **request.context,
        "tiktok_review_refreshed": True,
        "review": {"review_id": review_id},
        "tiktok_publish_evidence": {
            "review_id": review_id,
            "entity_id": user.entity_id,
            "user_id": user.id,
            "conversation_id": conversation.id,
            "workspace_id": None,
            "rel_path": path,
            "source_sha256": digest,
        },
    }
    if change == "changed_bytes":
        source.write_bytes(b"Unreviewed replacement bytes")
    elif change == "missing_attachment":
        message.attachments = []
    elif change == "consumed":
        request.status = "consumed"
    await db_session.commit()
    query_id = generate_ulid() if change == "stale_review" else review_id
    headers = (
        {}
        if change == "no_auth"
        else {
            "Authorization": f"Bearer {create_access_token(user.id, user.entity_id, user.role)}",
        }
    )
    response = await client.get(
        f"/api/v1/chat/approvals/{request.id}/tiktok-preview?review_id={query_id}",
        headers=headers,
    )
    if change is None:
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["cache-control"] == "private, no-store"
    else:
        assert response.status_code in {401, 403, 404, 409}
        assert content not in response.content
        assert b"Unreviewed replacement bytes" not in response.content
