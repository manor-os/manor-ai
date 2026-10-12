"""Catalog versions cover the complete freshly authorized list projection."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from apps.api.catalog import catalog_response
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, Workspace, WorkspaceStaff
from packages.core.services.agent_service import create_tool_definition
from tests.test_document_permissions import _auth, _create_entity_user

pytestmark = pytest.mark.oss_regression


async def _catalog(client, resource, headers, version=None, **filters):
    params = {**filters, **({"known_version": version} if version else {})}
    response = await client.get(f"/api/v1/{resource}/catalog", headers=headers, params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["version"]) == 64
    return result


def test_catalog_version_is_canonical_and_scoped():
    class Item(BaseModel):
        config: dict

    user = User(id="user-a", entity_id="entity-a")
    arguments = {"catalog": "agents", "user": user, "filters": {}, "known_version": None}
    first = catalog_response([Item(config={"a": 1, "b": 2})], **arguments)
    reordered = catalog_response([Item(config={"b": 2, "a": 1})], **arguments)
    assert reordered.version == first.version
    unchanged = catalog_response(
        [Item(config={"a": 1, "b": 2})],
        **{**arguments, "known_version": first.version},
    )
    assert unchanged.items is None
    for change in (
        {"user": User(id="user-b", entity_id="entity-a")},
        {"user": User(id="user-a", entity_id="entity-b")},
        {"catalog": "skills"},
        {"filters": {"category": "writing"}},
    ):
        assert catalog_response([Item(config={"a": 1, "b": 2})], **{**arguments, **change}).version != first.version


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["agents", "skills"])
async def test_catalog_tracks_create_rename_delete_and_matches_list(client: AsyncClient, resource):
    headers = await _auth(client, f"catalog_crud_{resource}")
    assert (await client.get(f"/api/v1/{resource}/catalog")).status_code == 401
    empty = await _catalog(client, resource, headers)
    assert empty["items"] == []
    assert (await _catalog(client, resource, headers, empty["version"]))["items"] is None

    created = await client.post(
        f"/api/v1/{resource}",
        headers=headers,
        json={"name": "Original", "system_prompt": "Help with the task."},
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    added = await _catalog(client, resource, headers, empty["version"])
    assert added["version"] != empty["version"]
    assert added["items"] == (await client.get(f"/api/v1/{resource}", headers=headers)).json()

    changed = await client.put(f"/api/v1/{resource}/{item_id}", headers=headers, json={"name": "Renamed"})
    assert changed.status_code == 200, changed.text
    renamed = await _catalog(client, resource, headers, added["version"])
    assert renamed["version"] != added["version"]
    assert renamed["items"][0]["name"] == "Renamed"
    assert (await _catalog(client, resource, headers, renamed["version"]))["items"] is None

    deleted = await client.delete(f"/api/v1/{resource}/{item_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    removed = await _catalog(client, resource, headers, renamed["version"])
    assert removed["items"] == []
    assert removed["version"] == empty["version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource,model", [("agents", Agent), ("skills", Skill)])
async def test_catalog_rechecks_workspace_access_and_actor_scope(client, db_session, resource, model):
    headers = await _auth(client, f"catalog_access_{resource}")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member = await _create_entity_user(owner["entity_id"], f"catalog_member_{resource}")
    workspace = Workspace(entity_id=owner["entity_id"], name="Restricted", settings={"access_mode": "members_only"})
    db_session.add(workspace)
    await db_session.flush()
    membership = WorkspaceStaff(workspace_id=workspace.id, user_id=member["id"], role="viewer", status="active")
    visible = model(
        entity_id=owner["entity_id"],
        name="Visible",
        system_prompt="Assist",
        workspace_id=workspace.id,
        visibility="workspace",
        owner_user_id=owner["id"],
    )
    private = model(
        entity_id=owner["entity_id"],
        name="Private",
        system_prompt="Private",
        visibility="private",
        owner_user_id=owner["id"],
    )
    db_session.add_all([membership, visible, private])
    await db_session.commit()

    first = await _catalog(client, resource, member["headers"])
    assert [item["id"] for item in first["items"]] == [visible.id]
    assert first["items"] == (await client.get(f"/api/v1/{resource}", headers=member["headers"])).json()
    private.name = "Still hidden"
    await db_session.commit()
    assert (await _catalog(client, resource, member["headers"], first["version"]))["items"] is None

    membership.status = "removed"
    await db_session.commit()
    revoked = await _catalog(client, resource, member["headers"], first["version"])
    assert revoked["items"] == []
    assert revoked["version"] != first["version"]

    other = await _create_entity_user(owner["entity_id"], f"catalog_other_{resource}")
    other_empty = await _catalog(client, resource, other["headers"], revoked["version"])
    assert other_empty["items"] == []
    assert other_empty["version"] != revoked["version"]


@pytest.mark.asyncio
async def test_agent_catalog_tracks_binding_counts_and_workspace_names(client, db_session):
    headers = await _auth(client, "catalog_agent_bindings")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = Workspace(entity_id=owner["entity_id"], name="Original workspace")
    db_session.add(workspace)
    await db_session.flush()
    agent = Agent(entity_id=owner["entity_id"], name="Agent", workspace_id=workspace.id)
    skill = Skill(entity_id=owner["entity_id"], name="Bound skill", system_prompt="Assist")
    db_session.add_all([agent, skill])
    await db_session.commit()
    first = await _catalog(client, "agents", headers)
    assert first["items"][0]["workspace_name"] == workspace.name
    tool = await create_tool_definition(db_session, name="catalog_test_tool")
    skill_binding = AgentSkillBinding(agent_id=agent.id, skill_id=skill.id)
    tool_binding = AgentToolBinding(agent_id=agent.id, tool_id=tool.id)
    db_session.add_all([skill_binding, tool_binding])
    await db_session.commit()
    bound = await _catalog(client, "agents", headers, first["version"])
    assert bound["items"][0]["tool_count"] == 1
    assert bound["items"][0]["skill_count"] == 1

    workspace.name = "Renamed workspace"
    await db_session.commit()
    renamed = await _catalog(client, "agents", headers, bound["version"])
    assert renamed["items"][0]["workspace_name"] == workspace.name
    skill_binding.status = "inactive"
    await db_session.delete(tool_binding)
    await db_session.commit()
    unbound = await _catalog(client, "agents", headers, renamed["version"])
    assert unbound["items"][0]["tool_count"] == 0
    assert unbound["items"][0]["skill_count"] == 0
    assert (await _catalog(client, "agents", headers, unbound["version"]))["items"] is None


@pytest.mark.asyncio
async def test_skill_catalog_tracks_filters_platform_and_plugin_preferences(client, db_session):
    headers = await _auth(client, "catalog_skill_filters")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    skill = Skill(entity_id=owner["entity_id"], name="Writing", category="writing", system_prompt="Write")
    platform = Skill(entity_id=None, slug="catalog-test-platform", name="Platform", system_prompt="Assist")
    db_session.add_all([skill, platform])
    await db_session.commit()

    local = await _catalog(client, "skills", headers)
    assert [item["id"] for item in local["items"]] == [skill.id]
    filtered = await _catalog(client, "skills", headers, local["version"], category="writing")
    assert filtered["items"] == local["items"]
    assert filtered["version"] != local["version"]
    missing = await _catalog(client, "skills", headers, filtered["version"], category="missing")
    assert missing["items"] == []
    included = await _catalog(client, "skills", headers, local["version"], include_platform=True)
    assert platform.id in {item["id"] for item in included["items"]}
    assert not any(item["slug"] == "remotion-create" for item in included["items"])

    toggled = await client.put("/api/v1/integrations/plugins/remotion", headers=headers, json={"enabled": True})
    assert toggled.status_code == 200, toggled.text
    enabled = await _catalog(client, "skills", headers, included["version"], include_platform=True)
    assert any(item["slug"] == "remotion-create" for item in enabled["items"])
    assert (
        enabled["items"]
        == (
            await client.get(
                "/api/v1/skills",
                headers=headers,
                params={"include_platform": True},
            )
        ).json()
    )
    assert (await _catalog(client, "skills", headers, enabled["version"], include_platform=True))["items"] is None

    platform.name = "Updated platform skill"
    await db_session.commit()
    updated = await _catalog(client, "skills", headers, enabled["version"], include_platform=True)
    assert next(item for item in updated["items"] if item["id"] == platform.id)["name"] == platform.name
    toggled = await client.put("/api/v1/integrations/plugins/remotion", headers=headers, json={"enabled": False})
    assert toggled.status_code == 200, toggled.text
    disabled = await _catalog(client, "skills", headers, updated["version"], include_platform=True)
    assert not any(item["slug"] == "remotion-create" for item in disabled["items"])


@pytest.mark.asyncio
async def test_skill_catalog_tracks_stable_binding_projection(client, db_session):
    headers = await _auth(client, "catalog_skill_bindings")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    agent = Agent(entity_id=owner["entity_id"], name="Original agent")
    skill = Skill(entity_id=owner["entity_id"], name="Skill", system_prompt="Assist")
    workspaces = [Workspace(entity_id=owner["entity_id"], name=f"Workspace {index}") for index in range(7)]
    db_session.add_all([agent, skill, *workspaces])
    await db_session.flush()
    subscriptions = [
        AgentSubscription(entity_id=owner["entity_id"], agent_id=agent.id, workspace_id=ws.id) for ws in workspaces
    ]
    db_session.add_all(subscriptions)
    await db_session.commit()
    unbound = await _catalog(client, "skills", headers)
    assert unbound["items"][0]["bindings"] == []
    db_session.add(AgentSkillBinding(agent_id=agent.id, skill_id=skill.id))
    await db_session.commit()
    bound = await _catalog(client, "skills", headers, unbound["version"])
    contexts = bound["items"][0]["bindings"]
    selected = [item["workspace_id"] for item in contexts if item.get("source") == "agent_subscription"]
    assert selected == [sub.workspace_id for sub in sorted(subscriptions, key=lambda sub: sub.id)[:5]]
    assert (await _catalog(client, "skills", headers, bound["version"]))["items"] is None

    agent.name = "Renamed agent"
    await db_session.commit()
    renamed = await _catalog(client, "skills", headers, bound["version"])
    assert all(item["agent_name"] == agent.name for item in renamed["items"][0]["bindings"])
    workspace = next(ws for ws in workspaces if ws.id == selected[0])
    workspace.name = "Renamed workspace"
    await db_session.commit()
    updated = await _catalog(client, "skills", headers, renamed["version"])
    assert any(item.get("workspace_name") == workspace.name for item in updated["items"][0]["bindings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("resource,model", [("agents", Agent), ("skills", Skill)])
async def test_catalog_list_order_has_stable_ties(client, db_session, resource, model):
    headers = await _auth(client, f"catalog_order_{resource}")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        model(entity_id=owner["entity_id"], name=f"Item {index}", system_prompt="Assist", created_at=same_time)
        for index in range(3)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    first = await _catalog(client, resource, headers)
    assert [item["id"] for item in first["items"]] == sorted(row.id for row in rows)
    assert (await _catalog(client, resource, headers, first["version"]))["items"] is None
