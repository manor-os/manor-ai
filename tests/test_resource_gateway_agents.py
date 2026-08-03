"""Regression: agents go through the unified resource-access gateway.

Before this, ``agents`` carried only ``entity_id``, so every endpoint could do
no better than entity-level isolation — and the three tool-binding endpoints
(``GET/POST/DELETE /agents/{id}/tools``) passed only ``agent_id`` to the
service layer, checking nothing at all.

Covered here:

1. Cross-entity IDOR on tool bindings — a user in entity B could read and
   rewrite the tool/MCP bindings of an agent in entity A.
2. Workspace scoping — an agent bound to a ``members_only`` workspace is
   invisible to entity members who are not in that workspace.
3. Write narrowing — ``viewer`` is read-only, and a plain member cannot edit
   an entity-wide agent they do not own.
4. No read regression — an entity-visible agent stays readable by every
   member, exactly as before the migration.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.workspace import Agent, Workspace, WorkspaceStaff
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _make_agent(
    entity_id: str,
    name: str,
    *,
    owner_user_id: str | None = None,
    workspace_id: str | None = None,
    visibility: str = "entity",
) -> str:
    async with db_module.async_session() as db:
        agent = Agent(
            entity_id=entity_id,
            name=name,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            visibility=visibility,
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id
        await db.commit()
    return agent_id


async def _make_workspace(entity_id: str, name: str, owner_user_id: str) -> str:
    async with db_module.async_session() as db:
        ws = Workspace(
            entity_id=entity_id,
            name=name,
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
async def test_cross_entity_cannot_touch_agent_tool_bindings(client: AsyncClient):
    """The IDOR: entity B must not read or rewrite entity A's tool bindings."""
    victim_headers = await _auth(client, "gwagent_victim")
    victim = await _me(client, victim_headers)
    agent_id = await _make_agent(victim["entity_id"], "Victim Agent")

    attacker_headers = await _auth(client, "gwagent_attacker")
    attacker = await _me(client, attacker_headers)
    assert attacker["entity_id"] != victim["entity_id"]

    r = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=attacker_headers)
    assert r.status_code == 404, r.text

    r = await client.post(
        f"/api/v1/agents/{agent_id}/tools",
        headers=attacker_headers,
        json={"tool_ids": []},
    )
    assert r.status_code == 404, r.text

    r = await client.request(
        "DELETE",
        f"/api/v1/agents/{agent_id}/tools",
        headers=attacker_headers,
        json={"tool_ids": []},
    )
    assert r.status_code == 404, r.text

    # The owner is unaffected.
    r = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=victim_headers)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_workspace_scoped_agent_hidden_from_non_members(client: AsyncClient):
    owner_headers = await _auth(client, "gwagent_wsowner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Private WS", owner["id"])
    agent_id = await _make_agent(
        owner["entity_id"], "WS Agent",
        owner_user_id=owner["id"], workspace_id=ws_id, visibility="workspace",
    )

    outsider = await _create_entity_user(owner["entity_id"], "gwagent_outsider", "member")

    r = await client.get(f"/api/v1/agents/{agent_id}", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    r = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    listed = await client.get("/api/v1/agents", headers=outsider["headers"])
    assert listed.status_code == 200
    assert agent_id not in [a["id"] for a in listed.json()]

    # A workspace member sees it.
    member = await _create_entity_user(owner["entity_id"], "gwagent_wsmember", "member")
    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=member["id"],
            role="editor", status="active",
        ))
        await db.commit()
    r = await client.get(f"/api/v1/agents/{agent_id}", headers=member["headers"])
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_viewer_cannot_write_agent(client: AsyncClient):
    owner_headers = await _auth(client, "gwagent_vowner")
    owner = await _me(client, owner_headers)
    agent_id = await _make_agent(
        owner["entity_id"], "Shared Agent", owner_user_id=owner["id"],
    )

    viewer = await _create_entity_user(owner["entity_id"], "gwagent_viewer", "viewer")

    # Entity-visible: the viewer may read it.
    r = await client.get(f"/api/v1/agents/{agent_id}", headers=viewer["headers"])
    assert r.status_code == 200, r.text

    # But not modify it.
    r = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=viewer["headers"],
        json={"name": "Hijacked"},
    )
    assert r.status_code == 403, r.text

    r = await client.delete(f"/api/v1/agents/{agent_id}", headers=viewer["headers"])
    assert r.status_code == 403, r.text

    r = await client.post(
        f"/api/v1/agents/{agent_id}/tools",
        headers=viewer["headers"],
        json={"tool_ids": []},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_member_cannot_edit_agent_owned_by_someone_else(client: AsyncClient):
    owner_headers = await _auth(client, "gwagent_owner2")
    owner = await _me(client, owner_headers)
    agent_id = await _make_agent(
        owner["entity_id"], "Owned Agent", owner_user_id=owner["id"],
    )

    other = await _create_entity_user(owner["entity_id"], "gwagent_member2", "member")
    r = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=other["headers"],
        json={"name": "Taken over"},
    )
    assert r.status_code == 403, r.text

    # The owner still can.
    r = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=owner_headers,
        json={"name": "Renamed by owner"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_entity_admin_keeps_firm_wide_override(client: AsyncClient):
    owner_headers = await _auth(client, "gwagent_admin_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Locked WS", owner["id"])
    agent_id = await _make_agent(
        owner["entity_id"], "Locked Agent",
        owner_user_id=owner["id"], workspace_id=ws_id, visibility="workspace",
    )

    admin = await _create_entity_user(owner["entity_id"], "gwagent_admin", "admin")
    r = await client.get(f"/api/v1/agents/{agent_id}", headers=admin["headers"])
    assert r.status_code == 200, r.text
    r = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=admin["headers"],
        json={"name": "Admin edit"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_entity_visible_agent_stays_readable_by_members(client: AsyncClient):
    """No read regression for rows that predate the ownership columns."""
    owner_headers = await _auth(client, "gwagent_legacy_owner")
    owner = await _me(client, owner_headers)
    # owner_user_id=None mirrors a pre-migration row.
    agent_id = await _make_agent(owner["entity_id"], "Legacy Agent")

    member = await _create_entity_user(owner["entity_id"], "gwagent_legacy_member", "member")
    r = await client.get(f"/api/v1/agents/{agent_id}", headers=member["headers"])
    assert r.status_code == 200, r.text

    listed = await client.get("/api/v1/agents", headers=member["headers"])
    assert listed.status_code == 200
    assert agent_id in [a["id"] for a in listed.json()]

    # ...but an ownerless entity-wide agent is writable only by an admin.
    r = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=member["headers"],
        json={"name": "Nope"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_create_agent_records_owner(client: AsyncClient):
    headers = await _auth(client, "gwagent_creator")
    me = await _me(client, headers)

    r = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "My Agent"},
    )
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    async with db_module.async_session() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.owner_user_id == me["id"]
        assert agent.visibility == "entity"
        assert agent.workspace_id is None


@pytest.mark.asyncio
async def test_cannot_create_agent_in_unwritable_workspace(client: AsyncClient):
    owner_headers = await _auth(client, "gwagent_ws_creator")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Closed WS", owner["id"])

    outsider = await _create_entity_user(owner["entity_id"], "gwagent_ws_outsider", "member")
    r = await client.post(
        "/api/v1/agents",
        headers=outsider["headers"],
        json={"name": "Sneaky", "workspace_id": ws_id, "visibility": "workspace"},
    )
    assert r.status_code == 403, r.text
