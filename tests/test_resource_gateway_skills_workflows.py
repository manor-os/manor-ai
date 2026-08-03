"""Regression: skills and workflows go through the unified gateway.

``skills.py`` previously imported no permission module at all — a ``viewer``
could create, edit, delete and invoke any skill in the organization. Workflow
definitions were entity-scoped only, so a members_only workspace's automation
was visible to the whole company.

The platform catalog is the delicate part: most rows are platform skills
(``entity_id IS NULL``), which every entity must keep seeing. They stay
readable by all and writable by none — previously that held only as a side
effect of ``NULL != entity_id``, now it is an explicit rule.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.skill import Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Workspace, WorkspaceStaff
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


async def _make_skill(
    entity_id: str | None,
    name: str,
    *,
    owner_user_id: str | None = None,
    workspace_id: str | None = None,
    visibility: str = "entity",
) -> str:
    async with db_module.async_session() as db:
        skill = Skill(
            entity_id=entity_id,
            name=name,
            system_prompt="do the thing",
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            visibility=visibility,
        )
        db.add(skill)
        await db.flush()
        skill_id = skill.id
        await db.commit()
    return skill_id


async def _make_workflow(
    entity_id: str,
    name: str,
    *,
    created_by: str | None = None,
    workspace_id: str | None = None,
    visibility: str = "entity",
) -> str:
    async with db_module.async_session() as db:
        wf = WorkflowDefinition(
            entity_id=entity_id,
            name=name,
            steps=[],
            created_by=created_by,
            workspace_id=workspace_id,
            visibility=visibility,
        )
        db.add(wf)
        await db.flush()
        wf_id = wf.id
        await db.commit()
    return wf_id


# ── Platform catalog must not regress ──────────────────────────────────────

@pytest.mark.asyncio
async def test_platform_skill_readable_by_every_entity(client: AsyncClient):
    platform_skill_id = await _make_skill(None, "Platform Builtin")

    for username in ("gwsk_tenant_a", "gwsk_tenant_b"):
        headers = await _auth(client, username)
        r = await client.get(f"/api/v1/skills/{platform_skill_id}", headers=headers)
        assert r.status_code == 200, f"{username}: {r.text}"


@pytest.mark.asyncio
async def test_platform_skill_is_read_only(client: AsyncClient):
    platform_skill_id = await _make_skill(None, "Platform Readonly")
    headers = await _auth(client, "gwsk_platform_writer")

    r = await client.put(
        f"/api/v1/skills/{platform_skill_id}",
        headers=headers,
        json={"name": "Hijacked"},
    )
    assert r.status_code == 403, r.text

    r = await client.delete(f"/api/v1/skills/{platform_skill_id}", headers=headers)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_platform_skill_stays_in_list(client: AsyncClient):
    platform_skill_id = await _make_skill(None, "Platform Listed")
    headers = await _auth(client, "gwsk_lister")

    r = await client.get("/api/v1/skills?include_platform=true", headers=headers)
    assert r.status_code == 200, r.text
    assert platform_skill_id in [s["id"] for s in r.json()]


# ── Skills ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_entity_skill_is_invisible(client: AsyncClient):
    owner_headers = await _auth(client, "gwsk_owner")
    owner = await _me(client, owner_headers)
    skill_id = await _make_skill(owner["entity_id"], "Private Recipe")

    attacker_headers = await _auth(client, "gwsk_attacker")
    r = await client.get(f"/api/v1/skills/{skill_id}", headers=attacker_headers)
    assert r.status_code == 404, r.text

    r = await client.put(
        f"/api/v1/skills/{skill_id}",
        headers=attacker_headers,
        json={"name": "Stolen"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_viewer_cannot_write_skill_but_can_read(client: AsyncClient):
    owner_headers = await _auth(client, "gwsk_vowner")
    owner = await _me(client, owner_headers)
    skill_id = await _make_skill(
        owner["entity_id"], "Team Skill", owner_user_id=owner["id"],
    )

    viewer = await _create_entity_user(owner["entity_id"], "gwsk_viewer", "viewer")

    r = await client.get(f"/api/v1/skills/{skill_id}", headers=viewer["headers"])
    assert r.status_code == 200, r.text

    r = await client.put(
        f"/api/v1/skills/{skill_id}",
        headers=viewer["headers"],
        json={"name": "Viewer edit"},
    )
    assert r.status_code == 403, r.text

    r = await client.delete(f"/api/v1/skills/{skill_id}", headers=viewer["headers"])
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_workspace_scoped_skill_hidden_from_non_members(client: AsyncClient):
    owner_headers = await _auth(client, "gwsk_wsowner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Skill WS", owner["id"])
    skill_id = await _make_skill(
        owner["entity_id"], "WS Skill",
        owner_user_id=owner["id"], workspace_id=ws_id, visibility="workspace",
    )

    outsider = await _create_entity_user(owner["entity_id"], "gwsk_outsider", "member")
    r = await client.get(f"/api/v1/skills/{skill_id}", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    listed = await client.get("/api/v1/skills", headers=outsider["headers"])
    assert listed.status_code == 200
    assert skill_id not in [s["id"] for s in listed.json()]

    # The owner still sees it.
    r = await client.get(f"/api/v1/skills/{skill_id}", headers=owner_headers)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_create_skill_records_owner(client: AsyncClient):
    headers = await _auth(client, "gwsk_creator")
    me = await _me(client, headers)

    r = await client.post(
        "/api/v1/skills",
        headers=headers,
        json={"name": "Mine", "system_prompt": "hello"},
    )
    assert r.status_code == 201, r.text

    async with db_module.async_session() as db:
        skill = await db.get(Skill, r.json()["id"])
        assert skill.owner_user_id == me["id"]
        assert skill.visibility == "entity"


# ── Workflows ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workspace_scoped_workflow_hidden_from_non_members(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "WF WS", owner["id"])
    wf_id = await _make_workflow(
        owner["entity_id"], "Secret Automation",
        created_by=owner["id"], workspace_id=ws_id, visibility="workspace",
    )

    outsider = await _create_entity_user(owner["entity_id"], "gwwf_outsider", "member")

    r = await client.get(f"/api/v1/workflows/{wf_id}", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    r = await client.get(f"/api/v1/workflows/{wf_id}/metadata", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    listed = await client.get("/api/v1/workflows", headers=outsider["headers"])
    assert listed.status_code == 200
    assert wf_id not in [w["id"] for w in listed.json()]

    # Creator still sees it.
    r = await client.get(f"/api/v1/workflows/{wf_id}", headers=owner_headers)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_workflow_creator_is_owner_for_writes(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_creator")
    owner = await _me(client, owner_headers)
    wf_id = await _make_workflow(
        owner["entity_id"], "Owned Flow", created_by=owner["id"],
    )

    other = await _create_entity_user(owner["entity_id"], "gwwf_other", "member")
    r = await client.put(
        f"/api/v1/workflows/{wf_id}",
        headers=other["headers"],
        json={"name": "Taken"},
    )
    assert r.status_code == 403, r.text

    r = await client.put(
        f"/api/v1/workflows/{wf_id}",
        headers=owner_headers,
        json={"name": "Renamed"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_entity_workflow_still_readable_by_members(client: AsyncClient):
    """No read regression for rows that predate the ownership columns."""
    owner_headers = await _auth(client, "gwwf_legacy")
    owner = await _me(client, owner_headers)
    wf_id = await _make_workflow(owner["entity_id"], "Legacy Flow")

    member = await _create_entity_user(owner["entity_id"], "gwwf_member", "member")
    r = await client.get(f"/api/v1/workflows/{wf_id}", headers=member["headers"])
    assert r.status_code == 200, r.text
