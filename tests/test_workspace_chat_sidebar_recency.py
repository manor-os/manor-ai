from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.workspace_chat.service import ensure_main_conversation


@pytest.mark.oss_regression
@pytest.mark.asyncio
async def test_workspace_sidebar_recency_uses_only_the_viewers_real_chat_posts(
    client: AsyncClient, db_session: AsyncSession,
):
    db = db_session
    registered = await client.post("/api/v1/auth/register", json={
        "username": "sidebarrecency", "email": "sidebarrecency@test.com",
        "password": "pass123", "entity_name": "Sidebar Test",
    })
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    user = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspaces = []
    for name in ("Older but recently discussed", "Another workspace", "No chat yet"):
        created = await client.post("/api/v1/workspaces", headers=headers, json={"name": name})
        assert created.status_code == 201, created.text
        workspaces.append(created.json())

    for index, workspace in enumerate(workspaces[:2]):
        main = await ensure_main_conversation(db, entity_id=user["entity_id"], workspace_id=workspace["id"])
        db.add(Message(
            id=generate_ulid(), conversation_id=main.id, role="user", content="A real chat",
            author_kind="user", meta={"author_user_id": user["id"]},
            created_at=datetime(2026, 10, 2 - index, tzinfo=timezone.utc),
        ))
        for role, author in (("assistant", user["id"]), ("user", "another-viewer")):
            db.add(Message(
                id=generate_ulid(), conversation_id=main.id, role=role, content="Later activity",
                meta={"author_user_id": author}, created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            ))
    hidden = Conversation(
        id=generate_ulid(), entity_id=user["entity_id"], workspace_id=workspaces[1]["id"],
        user_id=user["id"], scope="channel", channel="email", title="Outside Workspace Chat",
    )
    db.add(hidden)
    await db.flush()
    db.add(Message(
        id=generate_ulid(), conversation_id=hidden.id, role="user", content="Channel activity",
        meta={"author_user_id": user["id"]}, created_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    ))
    await db.commit()

    listed = await client.get("/api/v1/workspaces", headers=headers)
    assert listed.status_code == 200, listed.text
    by_id = {workspace["id"]: workspace for workspace in listed.json()}
    for index, workspace in enumerate(workspaces[:2]):
        assert datetime.fromisoformat(by_id[workspace["id"]]["last_chat_at"]) == datetime(2026, 10, 2 - index, tzinfo=timezone.utc)
    assert by_id[workspaces[2]["id"]]["last_chat_at"] is None
