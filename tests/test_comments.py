"""E2E tests: threaded comments — create, reply, edit, delete, reactions."""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.comment import Comment
from packages.core.services.comment_service import (
    _comment_resource_type_clause,
    delete_resource_comments,
    get_comment_count,
    list_comments,
)


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


def test_document_comment_filter_uses_the_normalized_resource_index() -> None:
    clause = _comment_resource_type_clause("Documents")
    sql = str(clause.compile(compile_kwargs={"literal_binds": True}))

    assert "lower(trim(comments.resource_type))" in sql.lower()
    assert "'document'" in sql
    assert "'documents'" in sql


@pytest.mark.asyncio
async def test_legacy_document_alias_remains_visible_and_deletable(db_session):
    comment = Comment(
        entity_id="legacy-comment-entity",
        resource_type=" Documents ",
        resource_id="legacy-comment-document",
        user_id="legacy-comment-user",
        content="Written by an old service during a rolling deploy",
        mentions=[],
        anchor={},
        reactions={},
        is_edited=False,
        status="active",
    )
    db_session.add(comment)
    await db_session.commit()

    comments = await list_comments(
        db_session,
        comment.entity_id,
        "document",
        comment.resource_id,
    )
    assert [item["id"] for item in comments] == [comment.id]
    assert await get_comment_count(
        db_session,
        comment.entity_id,
        "Documents",
        comment.resource_id,
    ) == 1

    assert await delete_resource_comments(
        db_session,
        comment.entity_id,
        "document",
        [comment.resource_id],
    ) == 1
    assert await get_comment_count(
        db_session,
        comment.entity_id,
        "document",
        comment.resource_id,
    ) == 0


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
async def test_delete_parent_comment_keeps_replies_in_a_redacted_tombstone(client: AsyncClient):
    headers = await _auth(client, "commentuser_delete_thread")

    parent_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Parent content must stay hidden",
            "anchor": {"type": "viewer_selection", "quote": "Selected text"},
        },
    )
    assert parent_resp.status_code == 201
    parent_id = parent_resp.json()["id"]

    reply_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Reply remains attached",
            "parent_id": parent_id,
        },
    )
    assert reply_resp.status_code == 201

    delete_resp = await client.delete(
        f"/api/v1/comments/{parent_id}",
        headers=headers,
    )
    assert delete_resp.status_code == 204

    list_resp = await client.get(
        "/api/v1/comments",
        headers=headers,
        params=TASK_RESOURCE,
    )
    assert list_resp.status_code == 200
    comments = list_resp.json()
    assert len(comments) == 1
    assert comments[0]["id"] == parent_id
    assert comments[0]["status"] == "deleted"
    assert comments[0]["content"] == ""
    assert comments[0]["mentions"] == []
    assert comments[0]["reactions"] == {}
    assert comments[0]["anchor"]["quote"] == "Selected text"
    assert [reply["content"] for reply in comments[0]["replies"]] == [
        "Reply remains attached"
    ]


@pytest.mark.asyncio
async def test_reply_to_deleted_parent_is_rejected(client: AsyncClient):
    headers = await _auth(client, "commentuser_deleted_parent_reply")
    parent_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={**TASK_RESOURCE, "content": "Delete before replying"},
    )
    assert parent_resp.status_code == 201
    parent_id = parent_resp.json()["id"]

    delete_resp = await client.delete(
        f"/api/v1/comments/{parent_id}",
        headers=headers,
    )
    assert delete_resp.status_code == 204

    reply_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Must not attach after deletion",
            "parent_id": parent_id,
        },
    )
    assert reply_resp.status_code == 404
    assert reply_resp.json()["detail"] == "Parent comment not found"


@pytest.mark.asyncio
async def test_parent_delete_serializes_against_reply_creation(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core import database as db_module

    headers = await _auth(client, "commentuser_delete_reply_race")
    parent_resp = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={**TASK_RESOURCE, "content": "Delete wins the race"},
    )
    assert parent_resp.status_code == 201
    parent_id = parent_resp.json()["id"]

    loop = asyncio.get_running_loop()
    reply_query_pid: asyncio.Future[int] = loop.create_future()
    original_execute = AsyncSession.execute

    async def track_reply_parent_query(session, statement, *args, **kwargs):
        sql = str(statement).lower()
        if "from comments" in sql and "comments.status !=" in sql:
            pid = (
                await original_execute(session, text("SELECT pg_backend_pid()"))
            ).scalar_one()
            if not reply_query_pid.done():
                reply_query_pid.set_result(int(pid))
        return await original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", track_reply_parent_query)

    reply_task = None
    try:
        async with db_module.async_session() as delete_db:
            parent = (
                await delete_db.execute(
                    select(Comment)
                    .where(Comment.id == parent_id)
                    .with_for_update()
                )
            ).scalar_one()
            parent.status = "deleted"
            await delete_db.flush()

            reply_task = asyncio.create_task(
                client.post(
                    "/api/v1/comments",
                    headers=headers,
                    json={
                        **TASK_RESOURCE,
                        "content": "Must wait for the deletion transaction",
                        "parent_id": parent_id,
                    },
                )
            )
            reply_pid = await asyncio.wait_for(reply_query_pid, timeout=2)

            async with db_module.async_session() as observer_db:
                wait_event_type = None
                deadline = loop.time() + 2
                while loop.time() < deadline:
                    wait_event_type = await observer_db.scalar(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": reply_pid},
                    )
                    if wait_event_type == "Lock":
                        break
                    await asyncio.sleep(0.01)
                assert wait_event_type == "Lock"

            await delete_db.commit()
        assert reply_task is not None
        reply_resp = await asyncio.wait_for(reply_task, timeout=2)
    finally:
        if reply_task is not None and not reply_task.done():
            reply_task.cancel()
            await asyncio.gather(reply_task, return_exceptions=True)
    assert reply_resp.status_code == 404
    assert reply_resp.json()["detail"] == "Parent comment not found"


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


@pytest.mark.asyncio
async def test_comment_inputs_are_validated(client: AsyncClient):
    headers = await _auth(client, "commentuser_validation")

    blank = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={**TASK_RESOURCE, "content": "   "},
    )
    assert blank.status_code == 422

    invalid_anchor = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Invalid range",
            "anchor": {"type": "text_range", "start": 4},
        },
    )
    assert invalid_anchor.status_code == 422

    invalid_occurrence = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            **TASK_RESOURCE,
            "content": "Invalid occurrence",
            "anchor": {"quote": "Repeated", "quote_occurrence": -1},
        },
    )
    assert invalid_occurrence.status_code == 422

    created = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={**TASK_RESOURCE, "content": "  Valid comment  "},
    )
    assert created.status_code == 201
    assert created.json()["content"] == "Valid comment"
    comment_id = created.json()["id"]

    blank_edit = await client.put(
        f"/api/v1/comments/{comment_id}",
        headers=headers,
        json={"content": "\n\t"},
    )
    assert blank_edit.status_code == 422

    invalid_reaction = await client.post(
        f"/api/v1/comments/{comment_id}/reactions",
        headers=headers,
        json={"reaction": "not valid!"},
    )
    assert invalid_reaction.status_code == 422

    unsupported_reaction = await client.post(
        f"/api/v1/comments/{comment_id}/reactions",
        headers=headers,
        json={"reaction": "heart"},
    )
    assert unsupported_reaction.status_code == 422

    oversized_resource_id = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            "resource_type": "task",
            "resource_id": "x" * 27,
            "content": "Must not reach the database",
        },
    )
    assert oversized_resource_id.status_code == 422
