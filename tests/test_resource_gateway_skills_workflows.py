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

import json

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.skill import Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.chat_manual_skills import (
    ManualSkillResolutionError,
    prepare_chat_manual_skill_turn,
)
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

    async with db_module.async_session() as db:
        with pytest.raises(ManualSkillResolutionError) as exc_info:
            await prepare_chat_manual_skill_turn(
                db,
                entity_id=owner["entity_id"],
                agent_id=None,
                user_id=outsider["id"],
                user_role="member",
                message="Use the selected Skill",
                manual_skill_refs=json.dumps([{"kind": "id", "value": skill_id}]),
            )
    assert exc_info.value.missing_skill_ids == [skill_id]

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


@pytest.mark.asyncio
async def test_member_cannot_bind_private_workflow_into_their_workspace(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_private_binding_owner")
    owner = await _me(client, owner_headers)
    source_workspace_id = await _make_workspace(
        owner["entity_id"], "Private source", owner["id"]
    )
    workflow_id = await _make_workflow(
        owner["entity_id"],
        "Private deployment source",
        created_by=owner["id"],
        workspace_id=source_workspace_id,
        visibility="workspace",
    )
    member = await _create_entity_user(
        owner["entity_id"], "gwwf_private_binding_member", "member"
    )
    target_workspace_id = await _make_workspace(
        owner["entity_id"], "Member target", member["id"]
    )

    response = await client.post(
        "/api/v1/workflows/bindings",
        headers=member["headers"],
        json={"workflow_id": workflow_id, "workspace_id": target_workspace_id},
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_entity_binding_mutation_requires_workflow_edit_access(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_entity_binding_owner")
    owner = await _me(client, owner_headers)
    workflow_id = await _make_workflow(
        owner["entity_id"],
        "Shared read-only deployment",
        created_by=owner["id"],
    )
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={"workflow_id": workflow_id},
    )).json()
    member = await _create_entity_user(
        owner["entity_id"], "gwwf_entity_binding_member", "member"
    )

    created = await client.post(
        "/api/v1/workflows/bindings",
        headers=member["headers"],
        json={"workflow_id": workflow_id},
    )
    updated = await client.put(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=member["headers"],
        json={"name": "Unauthorized rename"},
    )
    deleted = await client.delete(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=member["headers"],
    )

    assert created.status_code == 403, created.text
    assert updated.status_code == 403, updated.text
    assert deleted.status_code == 403, deleted.text


@pytest.mark.asyncio
async def test_private_entity_binding_cannot_be_run_by_non_member(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_private_run_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(
        owner["entity_id"], "Private run source", owner["id"]
    )
    workflow_id = await _make_workflow(
        owner["entity_id"],
        "Private entity binding",
        created_by=owner["id"],
        workspace_id=workspace_id,
        visibility="workspace",
    )
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={"workflow_id": workflow_id},
    )).json()
    outsider = await _create_entity_user(
        owner["entity_id"], "gwwf_private_run_outsider", "member"
    )

    response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=outsider["headers"],
        json={"execute": False},
    )
    listed = await client.get(
        "/api/v1/workflows/bindings",
        headers=outsider["headers"],
    )

    assert response.status_code == 404, response.text
    assert binding["id"] not in {item["id"] for item in listed.json()}


@pytest.mark.asyncio
async def test_entity_trigger_fanout_requires_entity_admin(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_entity_trigger_owner")
    owner = await _me(client, owner_headers)
    workflow_id = await _make_workflow(
        owner["entity_id"], "Entity event source", created_by=owner["id"]
    )
    await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow_id,
            "trigger_type": "event",
            "trigger_config": {"event": "private.event"},
        },
    )
    member = await _create_entity_user(
        owner["entity_id"], "gwwf_entity_trigger_member", "member"
    )

    response = await client.post(
        "/api/v1/workflows/trigger",
        headers=member["headers"],
        json={"trigger_type": "event", "event_name": "private.event"},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_binding_list_redacts_secrets_for_workspace_viewer(client: AsyncClient):
    owner_headers = await _auth(client, "gwwf_binding_secret_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(
        owner["entity_id"], "Secret projection", owner["id"]
    )
    viewer = await _create_entity_user(
        owner["entity_id"], "gwwf_binding_secret_viewer", "viewer"
    )
    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=workspace_id,
            staff_id=None,
            user_id=viewer["id"],
            role="viewer",
            status="active",
        ))
        await db.commit()
    workflow_id = await _make_workflow(
        owner["entity_id"], "Webhook source", created_by=owner["id"]
    )
    created = (await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
            "trigger_type": "webhook",
            "trigger_config": {"label": "orders"},
            "variables": {"api_token": "variable-secret"},
            "config": {"credential_id": "credential-secret"},
        },
    )).json()
    assert created["trigger_config"]["webhook_token"]

    response = await client.get(
        "/api/v1/workflows/bindings",
        headers=viewer["headers"],
        params={"workspace_id": workspace_id},
    )

    assert response.status_code == 200, response.text
    binding = response.json()[0]
    assert binding["trigger_config"] == {"label": "orders"}
    assert binding["variables"] == {}
    assert binding["config"] == {}
