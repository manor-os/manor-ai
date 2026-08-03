"""Regression: workspace write authorization.

Two things are covered:

1. Cross-workspace WRITE hole — `POST /tasks`, `PUT /tasks/{id}`,
   `DELETE /tasks/{id}` only scoped by entity_id, so a member could create
   tasks into — and modify/delete tasks inside — a members_only workspace they
   cannot even see.
2. Workspace role semantics — a `viewer` membership is read-only;
   `contributor` / `editor` / `owner` may write. Previously every workspace
   role behaved identically.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.task_service import create_task
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _make_workspace(entity_id: str, name: str, owner_user_id: str) -> str:
    async with db_module.async_session() as db:
        ws = Workspace(
            entity_id=entity_id, name=name,
            settings={"access_mode": "members_only"},
        )
        db.add(ws)
        await db.flush()
        ws_id = ws.id
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=owner_user_id,
            role="owner", status="active",
        ))
        await db.commit()
    return ws_id


async def _add_member(ws_id: str, user_id: str, role: str) -> None:
    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=user_id,
            role=role, status="active",
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_non_member_cannot_write_into_workspace(client: AsyncClient):
    owner_headers = await _auth(client, "wswrite_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))

    ws_id = await _make_workspace(entity_id, "Private Ops", owner_id)
    async with db_module.async_session() as db:
        t = await create_task(db, entity_id, title="ws task", workspace_id=ws_id)
        await db.commit()
        task_id = t.id

    outsider = await _create_entity_user(entity_id, "wswrite_outsider", role="member")

    # Cannot CREATE a task into a workspace they can't access.
    r = await client.post(
        "/api/v1/tasks", headers=outsider["headers"],
        json={"title": "injected", "workspace_id": ws_id},
    )
    assert r.status_code == 403, f"create into foreign workspace allowed: {r.status_code}"

    # Cannot UPDATE a task inside it.
    r = await client.put(
        f"/api/v1/tasks/{task_id}", headers=outsider["headers"],
        json={"title": "hijacked"},
    )
    assert r.status_code == 403, f"update of foreign-workspace task allowed: {r.status_code}"

    # Cannot DELETE it.
    r = await client.delete(f"/api/v1/tasks/{task_id}", headers=outsider["headers"])
    assert r.status_code == 403, f"delete of foreign-workspace task allowed: {r.status_code}"

    # The task is untouched.
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=owner_headers)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "ws task"

    # Entity-level tasks (no workspace) are still creatable by any member.
    r = await client.post(
        "/api/v1/tasks", headers=outsider["headers"], json={"title": "entity task"},
    )
    assert r.status_code == 201, f"entity-level create wrongly blocked: {r.text}"


@pytest.mark.asyncio
async def test_viewer_is_read_only_but_contributor_can_write(client: AsyncClient):
    owner_headers = await _auth(client, "wsrole_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Role Semantics", owner_id)

    viewer = await _create_entity_user(entity_id, "wsrole_viewer", role="member")
    contributor = await _create_entity_user(entity_id, "wsrole_contrib", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")
    await _add_member(ws_id, contributor["id"], "contributor")

    # Both can READ the workspace's tasks (they are members).
    for who in (viewer, contributor):
        r = await client.get(
            f"/api/v1/tasks?workspace_id={ws_id}", headers=who["headers"]
        )
        assert r.status_code == 200, f"member denied read: {r.text}"

    # viewer: read-only — cannot create.
    r = await client.post(
        "/api/v1/tasks", headers=viewer["headers"],
        json={"title": "viewer attempt", "workspace_id": ws_id},
    )
    assert r.status_code == 403, f"viewer was allowed to write: {r.status_code}"

    # contributor: may create.
    r = await client.post(
        "/api/v1/tasks", headers=contributor["headers"],
        json={"title": "contributor task", "workspace_id": ws_id},
    )
    assert r.status_code == 201, f"contributor wrongly blocked: {r.text}"
    created_id = r.json()["id"]

    # viewer cannot modify what contributor created.
    r = await client.put(
        f"/api/v1/tasks/{created_id}", headers=viewer["headers"],
        json={"title": "viewer edit"},
    )
    assert r.status_code == 403, f"viewer was allowed to update: {r.status_code}"

    # contributor can.
    r = await client.put(
        f"/api/v1/tasks/{created_id}", headers=contributor["headers"],
        json={"title": "contributor edit"},
    )
    assert r.status_code == 200, f"contributor wrongly blocked on update: {r.text}"
