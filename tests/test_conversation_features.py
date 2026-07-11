"""E2E tests: conversation export and sharing."""

import pytest
from httpx import AsyncClient


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


async def _create_conversation(client: AsyncClient, headers: dict) -> str:
    """Helper — create a conversation with two messages via the chat endpoint."""
    resp = await client.post(
        "/api/v1/chat/message",
        headers=headers,
        data={
            "message": "Hello, how are you?",
        },
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


# ── Export tests ──


@pytest.mark.asyncio
async def test_export_markdown(client: AsyncClient):
    headers = await _auth(client)
    conv_id = await _create_conversation(client, headers)

    resp = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/export?format=markdown",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "# Conversation:" in body
    assert "Exported on" in body
    assert "**User**" in body
    assert "Hello, how are you?" in body
    assert "---" in body


@pytest.mark.asyncio
async def test_export_json(client: AsyncClient):
    headers = await _auth(client)
    conv_id = await _create_conversation(client, headers)

    resp = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/export?format=json",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "conversation" in data
    assert data["conversation"]["id"] == conv_id
    assert "messages" in data
    assert data["message_count"] >= 1
    assert "exported_at" in data
    # Check message structure
    msg = data["messages"][0]
    assert "id" in msg
    assert "role" in msg
    assert "content" in msg


# ── Share tests ──


@pytest.mark.asyncio
async def test_share_conversation(client: AsyncClient):
    headers = await _auth(client)
    conv_id = await _create_conversation(client, headers)

    # Create share
    share_resp = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/share",
        headers=headers,
        json={},
    )
    assert share_resp.status_code == 200
    share = share_resp.json()
    assert share["conversation_id"] == conv_id
    assert share["share_token"]
    assert share["is_active"] is True

    # Access shared conversation WITHOUT auth
    view_resp = await client.get(f"/api/v1/chat/shared/{share['share_token']}")
    assert view_resp.status_code == 200
    data = view_resp.json()
    assert data["conversation"]["id"] == conv_id
    assert len(data["messages"]) >= 1

    # List shares
    list_resp = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/shares",
        headers=headers,
    )
    assert list_resp.status_code == 200
    shares = list_resp.json()
    assert any(s["id"] == share["id"] for s in shares)


@pytest.mark.asyncio
async def test_share_expired(client: AsyncClient):
    headers = await _auth(client)
    conv_id = await _create_conversation(client, headers)

    # Create share that expires in 0 hours (immediately expired)
    # We use expires_hours=0 which sets expires_at = now, so it's already expired
    # Actually, timedelta(hours=0) means expires_at == now, which is <= now, so expired.
    share_resp = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/share",
        headers=headers,
        json={"expires_hours": 0},
    )
    assert share_resp.status_code == 200
    token = share_resp.json()["share_token"]

    # Manually expire the share by updating expires_at to the past
    # Since expires_hours=0 means expires_at=now, the check `< now()` might race.
    # Instead, directly update via DB. Use a second approach: just try accessing —
    # with timedelta(hours=0) the expires_at == creation time, and the check is <,
    # so it may or may not be expired depending on timing. Let's force it via SQL.
    from packages.core.models.conversation_share import ConversationShare
    from sqlalchemy import update
    from datetime import datetime, timezone, timedelta
    import packages.core.database as db_module

    async with db_module.async_session() as session:
        await session.execute(
            update(ConversationShare)
            .where(ConversationShare.share_token == token)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await session.commit()

    # Attempt to access — should fail
    view_resp = await client.get(f"/api/v1/chat/shared/{token}")
    assert view_resp.status_code == 404


@pytest.mark.asyncio
async def test_revoke_share(client: AsyncClient):
    headers = await _auth(client)
    conv_id = await _create_conversation(client, headers)

    # Create share
    share_resp = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/share",
        headers=headers,
        json={},
    )
    assert share_resp.status_code == 200
    share = share_resp.json()
    token = share["share_token"]
    share_id = share["id"]

    # Verify it's accessible
    view_resp = await client.get(f"/api/v1/chat/shared/{token}")
    assert view_resp.status_code == 200

    # Revoke it
    del_resp = await client.delete(
        f"/api/v1/chat/conversations/{conv_id}/share/{share_id}",
        headers=headers,
    )
    assert del_resp.status_code == 204

    # Verify no longer accessible
    view_resp2 = await client.get(f"/api/v1/chat/shared/{token}")
    assert view_resp2.status_code == 404
