"""Regression tests for workspace authorization holes.

1. Cross-workspace data leak: standalone list routers (tasks/goals/plans/
   dashboard) filtered only by entity_id + an attacker-supplied workspace_id,
   with no workspace-read check, so a non-member could read a members_only
   workspace's data via `?workspace_id=W`.
2. Expired workspace-owner grants still authorized management because the
   manage path (`_workspace_role_of`) ignored `expires_at` while the read path
   honored it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.task_service import create_task
from tests.test_document_permissions import _auth, _create_entity_user


async def _entity_id(client: AsyncClient, headers: dict) -> str:
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return me.json()["entity_id"]


async def _make_members_only_workspace(entity_id: str, name: str, owner_user_id: str) -> str:
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


@pytest.mark.asyncio
async def test_non_member_cannot_read_workspace_tasks_via_side_door(client: AsyncClient):
    owner_headers = await _auth(client, "wsauthz_owner")
    entity_id = await _entity_id(client, owner_headers)
    owner_me = await client.get("/api/v1/auth/me", headers=owner_headers)
    _me = owner_me.json()
    owner_id = _me.get("user_id") or _me.get("id")

    ws_id = await _make_members_only_workspace(entity_id, "Secret Ops", owner_id)
    async with db_module.async_session() as db:
        await create_task(
            db, entity_id, title="Confidential board task", workspace_id=ws_id,
        )
        await db.commit()

    # A plain member of the entity who is NOT in the workspace.
    outsider = await _create_entity_user(entity_id, "wsauthz_outsider", role="member")

    # Side-door: GET /tasks?workspace_id=W must NOT leak the workspace's tasks.
    r = await client.get(
        f"/api/v1/tasks?workspace_id={ws_id}", headers=outsider["headers"]
    )
    assert r.status_code == 404, f"tasks side-door leaked: {r.status_code} {r.text}"

    r = await client.get(
        f"/api/v1/dashboard/stats?workspace_id={ws_id}", headers=outsider["headers"]
    )
    assert r.status_code == 404, f"dashboard side-door leaked: {r.status_code}"

    r = await client.get(
        f"/api/v1/goals?workspace_id={ws_id}", headers=outsider["headers"]
    )
    assert r.status_code == 404, f"goals side-door leaked: {r.status_code}"

    # The workspace member (also the entity owner) CAN read it — the gate does
    # not over-block a legitimate reader.
    r = await client.get(
        f"/api/v1/tasks?workspace_id={ws_id}", headers=owner_headers
    )
    assert r.status_code == 200, r.text
    titles = {t["title"] for t in r.json()["items"]}
    assert "Confidential board task" in titles


@pytest.mark.asyncio
async def test_expired_workspace_owner_cannot_manage(client: AsyncClient):
    admin_headers = await _auth(client, "wsauthz_admin2")
    entity_id = await _entity_id(client, admin_headers)

    # A non-admin member who will hold a TIME-BOXED workspace-owner grant.
    grantee = await _create_entity_user(entity_id, "wsauthz_grantee", role="member")

    async with db_module.async_session() as db:
        ws = Workspace(entity_id=entity_id, name="Time-boxed",
                       settings={"access_mode": "members_only"})
        db.add(ws)
        await db.flush()
        ws_id = ws.id
        # Expired owner grant.
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=grantee["id"],
            role="owner", status="active",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        await db.commit()

    # Manage action (rename) must be denied now that the grant has expired.
    r = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=grantee["headers"],
        json={"name": "Renamed by expired owner"},
    )
    assert r.status_code in (403, 404), (
        f"expired workspace-owner still managed: {r.status_code} {r.text}"
    )

    # Give them a NON-expired owner grant → manage works.
    async with db_module.async_session() as db:
        row = (await db.execute(
            __import__("sqlalchemy").select(WorkspaceStaff).where(
                WorkspaceStaff.workspace_id == ws_id,
                WorkspaceStaff.user_id == grantee["id"],
            )
        )).scalar_one()
        row.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        await db.commit()

    r = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=grantee["headers"],
        json={"name": "Renamed by valid owner"},
    )
    assert r.status_code == 200, f"valid workspace-owner wrongly blocked: {r.text}"


@pytest.mark.asyncio
async def test_entity_wide_task_list_excludes_unreadable_workspaces(client: AsyncClient):
    """GET /tasks with no workspace_id must not surface members_only tasks the
    caller can't read — only entity-level (workspace-less) tasks + readable
    workspaces' tasks."""
    owner_headers = await _auth(client, "wsscope_owner")
    entity_id = await _entity_id(client, owner_headers)
    _me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    owner_id = _me.get("user_id") or _me.get("id")

    ws_id = await _make_members_only_workspace(entity_id, "Private WS", owner_id)
    async with db_module.async_session() as db:
        await create_task(db, entity_id, title="ws-only secret task", workspace_id=ws_id)
        await create_task(db, entity_id, title="entity-level task", workspace_id=None)
        await db.commit()

    outsider = await _create_entity_user(entity_id, "wsscope_outsider", role="member")

    # No workspace_id filter: the outsider sees the entity-level task but NOT
    # the members_only workspace's task.
    r = await client.get("/api/v1/tasks", headers=outsider["headers"])
    assert r.status_code == 200, r.text
    titles = {t["title"] for t in r.json()["items"]}
    assert "entity-level task" in titles
    assert "ws-only secret task" not in titles, f"members_only task leaked: {titles}"

    # The board view is scoped the same way.
    r = await client.get("/api/v1/tasks/board", headers=outsider["headers"])
    assert r.status_code == 200, r.text
    board_titles = {
        t["title"] for tasks in r.json().values()
        if isinstance(tasks, list) for t in tasks
    }
    assert "ws-only secret task" not in board_titles, board_titles
