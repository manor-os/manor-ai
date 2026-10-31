"""Committed Workspace configuration writes tell that Workspace's viewers to refetch.

Without this a second viewer (a teammate, the customer, or a provider in the
customer-trial frame) kept the old configuration until a cache expired or the
page reloaded.
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from auth_helpers import register_user_and_get_token
from packages.core.models.workspace import Workspace
from packages.core.services import workspace_change_events as changes


async def _owner(client: AsyncClient, username: str) -> tuple[str, str]:
    resp = await register_user_and_get_token(client, json={
        "username": username, "email": f"{username}@test.com",
        "password": "TestPassword123!", "entity_name": f"{username} Corp",
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["entity_id"]


@pytest.fixture
def published(monkeypatch):
    calls: list[dict[str, str]] = []

    async def record(found):
        calls.append(dict(found))

    monkeypatch.setattr(changes, "publish", record)
    return calls


async def _settle():
    # Publishing is scheduled on the loop after the commit returns.
    for _ in range(3):
        await asyncio.sleep(0)


def _session():
    import packages.core.database as dbmod

    return dbmod.async_session()


@pytest.mark.asyncio
async def test_config_commits_publish_and_runtime_or_rolled_back_writes_do_not(client, published):
    token, entity_id = await _owner(client, "wschange_owner")
    resp = await client.post(
        "/api/v1/workspaces", json={"name": "Change events"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    workspace_id = resp.json()["id"]
    await _settle()
    # Creating it through the API is itself a change its (new) viewers see.
    assert {workspace_id: entity_id} in published

    published.clear()
    async with _session() as db:
        ws = await db.get(Workspace, workspace_id)
        ws.monthly_spent_usd = 1.5  # runtime spend, not configuration
        await db.commit()
        ws.name = "Renamed"
        await db.rollback()
    await _settle()
    assert published == []

    async with _session() as db:
        ws = await db.get(Workspace, workspace_id)
        ws.name = "Renamed"
        ws.description = "Both in one commit"
        await db.commit()
    await _settle()
    assert published == [{workspace_id: entity_id}]

    published.clear()
    async with _session() as db:
        await db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.entity_id == entity_id)
            .values(operating_context="bulk")
        )
        await db.commit()
    await _settle()
    assert published == [{workspace_id: entity_id}]


def test_bulk_writes_need_a_workspace_and_entity_in_their_where_clause():
    scoped = update(Workspace).where(Workspace.id == "w1", Workspace.entity_id == "e1").values(name="x")
    assert changes._statement_changes(scoped) == {"w1": "e1"}
    unscoped = update(Workspace).where(Workspace.entity_id == "e1").values(name="x")
    assert changes._statement_changes(unscoped) == {}
    runtime = update(Workspace).where(Workspace.id == "w1", Workspace.entity_id == "e1").values(last_heartbeat_at=None)
    assert changes._statement_changes(runtime) == {}
