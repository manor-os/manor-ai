"""E2E tests: threaded comments — create, reply, edit, delete, reactions."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "commentuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


TASK_RESOURCE = {"resource_type": "task", "resource_id": "01JTASK000000000000000000"}


@pytest.mark.asyncio
async def test_create_comment(client: AsyncClient):
    headers = await _auth(client)

    resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "This task looks great!",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "This task looks great!"
    assert data["resource_type"] == "task"
    assert data["resource_id"] == TASK_RESOURCE["resource_id"]
    assert data["parent_id"] is None
    assert data["is_edited"] is False
    assert data["status"] == "active"

    # Verify it shows up in list
    list_resp = await client.get(
        "/api/v1/comments",
        headers=headers,
        params=TASK_RESOURCE,
    )
    assert list_resp.status_code == 200
    comments = list_resp.json()
    assert len(comments) == 1
    assert comments[0]["content"] == "This task looks great!"


@pytest.mark.asyncio
async def test_reply_to_comment(client: AsyncClient):
    headers = await _auth(client, "commentuser_reply")

    # Create parent comment
    parent_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Parent comment",
        },
    )
    assert parent_resp.status_code == 201
    parent_id = parent_resp.json()["id"]

    # Reply to parent
    reply_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "This is a reply",
            "parent_id": parent_id,
        },
    )
    assert reply_resp.status_code == 201
    assert reply_resp.json()["parent_id"] == parent_id

    # List — verify threading
    list_resp = await client.get(
        "/api/v1/comments",
        headers=headers,
        params=TASK_RESOURCE,
    )
    assert list_resp.status_code == 200
    comments = list_resp.json()
    assert len(comments) == 1  # only one top-level
    assert comments[0]["id"] == parent_id
    assert len(comments[0]["replies"]) == 1
    assert comments[0]["replies"][0]["content"] == "This is a reply"


@pytest.mark.asyncio
async def test_edit_comment(client: AsyncClient):
    headers = await _auth(client, "commentuser_edit")

    # Create
    create_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Original content",
        },
    )
    comment_id = create_resp.json()["id"]

    # Edit
    edit_resp = await client.put(
        f"/api/v1/comments/{comment_id}",
        headers=headers,
        json={"content": "Edited content"},
    )
    assert edit_resp.status_code == 200
    data = edit_resp.json()
    assert data["content"] == "Edited content"
    assert data["is_edited"] is True


@pytest.mark.asyncio
async def test_delete_comment(client: AsyncClient):
    headers = await _auth(client, "commentuser_del")

    # Create
    create_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "To be deleted",
        },
    )
    comment_id = create_resp.json()["id"]

    # Delete
    del_resp = await client.delete(
        f"/api/v1/comments/{comment_id}",
        headers=headers,
    )
    assert del_resp.status_code == 204

    # Verify it no longer appears in list (soft-deleted)
    list_resp = await client.get(
        "/api/v1/comments",
        headers=headers,
        params=TASK_RESOURCE,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 0


@pytest.mark.asyncio
async def test_reactions(client: AsyncClient):
    headers = await _auth(client, "commentuser_react")

    # Create
    create_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "React to me!",
        },
    )
    comment_id = create_resp.json()["id"]

    # Add reaction
    react_resp = await client.post(
        f"/api/v1/comments/{comment_id}/reactions",
        headers=headers,
        json={"reaction": "thumbsup"},
    )
    assert react_resp.status_code == 200
    reactions = react_resp.json()["reactions"]
    assert "thumbsup" in reactions
    assert len(reactions["thumbsup"]) == 1

    # Toggle off (same user, same reaction)
    react_resp2 = await client.post(
        f"/api/v1/comments/{comment_id}/reactions",
        headers=headers,
        json={"reaction": "thumbsup"},
    )
    assert react_resp2.status_code == 200
    reactions2 = react_resp2.json()["reactions"]
    assert "thumbsup" not in reactions2
